"""MCP server configuration management.

Loads server-level MCP config from the MCP_CONFIG environment variable.
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.constants import MCP_CONFIG

logger = logging.getLogger(__name__)

McpServersDict = Dict[str, Dict[str, Any]]

ALLOWED_TYPES = {"stdio", "sse", "http", "streamable-http"}

# Required fields per server type (module-level: shared by the loader, the
# manifest overlay, and the diagnostics).
REQUIRED_FIELDS: Dict[str, tuple] = {
    "stdio": ("command",),
    "sse": ("url",),
    "http": ("url",),
    "streamable-http": ("url",),
}


def mcp_safe_name(name: str) -> str:
    """Dash->underscore: the Claude/Codex MCP tool-namespace convention (mcp__<name>__*)."""
    return "_".join(name.split("-"))


def validate_server(name: str, config: Any) -> Tuple[bool, Optional[str]]:
    """One accept/drop rule set shared by loader, manifest overlay, and diagnostics."""
    if not isinstance(config, dict):
        return False, "not a dict"
    server_type = config.get("type", "stdio")
    if server_type not in ALLOWED_TYPES:
        return False, f"unsupported type '{server_type}'"
    missing = [f for f in REQUIRED_FIELDS.get(server_type, ()) if not config.get(f)]
    if missing:
        return False, f"missing required field(s) {missing} for type '{server_type}'"
    return True, None


def validate_mcp_servers(raw: McpServersDict) -> Tuple[McpServersDict, List[dict]]:
    """Split raw servers into (validated, dropped=[{name,type,reason}]). Never raises.

    Emits the same per-server ``logger.warning`` the old inline loop did, so
    ``test_edge_cases_unit.py``'s warning-scrape assertion still holds.
    """
    validated: McpServersDict = {}
    dropped: List[dict] = []
    for name, config in raw.items():
        ok, reason = validate_server(name, config)
        if ok:
            validated[name] = config
        else:
            dropped.append(
                {
                    "name": name,
                    "type": config.get("type") if isinstance(config, dict) else None,
                    "reason": reason,
                }
            )
            logger.warning(f"Skipping invalid MCP server config '{name}': {reason}")
    return validated, dropped


def _read_mcp_config_source() -> Optional[dict]:
    """Parse the ``MCP_CONFIG`` env source (file path or inline JSON).

    Returns the raw parsed dict, or ``None`` when unset / unparseable / not a
    JSON object. Single parser shared by ``load_mcp_config`` and the diagnostics.
    """
    if not MCP_CONFIG:
        return None

    config_str = MCP_CONFIG.strip()

    # Try as file path first
    config_path = Path(config_str)
    if config_path.is_file():
        try:
            with open(config_path) as f:
                raw = json.load(f)
            logger.info(f"Loaded MCP config from file: {config_path}")
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load MCP config file {config_path}: {e}")
            return None
    else:
        try:
            raw = json.loads(config_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse MCP_CONFIG as JSON: {e}")
            return None

    if not isinstance(raw, dict):
        logger.error("MCP_CONFIG must be a JSON object")
        return None

    return raw


def load_mcp_config() -> McpServersDict:
    """Load MCP server config from MCP_CONFIG environment variable.

    Accepts a JSON file path or inline JSON string.
    Format: {"mcpServers": {"name": {...}}} or {"name": {...}}
    """
    raw = _read_mcp_config_source()
    if raw is None:
        return {}

    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        logger.error("MCP_CONFIG servers must be a JSON object")
        return {}

    validated, _ = validate_mcp_servers(servers)
    if validated:
        logger.info(f"Loaded {len(validated)} MCP server(s): {list(validated.keys())}")
    return validated


def get_mcp_tool_patterns(servers: McpServersDict) -> List[str]:
    """Return symbolic MCP tool patterns for allowed_tools.

    The Claude Agent SDK resolves MCP tools using the naming convention
    ``mcp__<server_name>__*``.  By adding these patterns to ``allowed_tools``
    the SDK manages tool schemas internally — the gateway never needs to
    serialize full MCP tool JSON schemas into the API request payload.
    """
    return [f"mcp__{mcp_safe_name(name)}__*" for name in servers]


def _validated_manifest_servers() -> McpServersDict:
    """Admin-managed overlay from the manifest, validated. Never raises."""
    try:
        from src import mcp_manifest  # lazy: mcp_manifest lazily imports us back

        raw = mcp_manifest.list_servers()
    except Exception:
        logger.warning("Failed to read MCP manifest; ignoring overlay", exc_info=True)
        return {}
    validated, _ = validate_mcp_servers(raw)
    return validated


def _compute_effective_config() -> McpServersDict:
    """env base (MCP_CONFIG) OVERLAID by manifest (manifest wins on name).

    Returns a FRESH top-level dict every call so a reload never rebinds to a
    dict object that a prior session's ``options.mcp_servers`` already
    references (claude/client.py stores the passed dict by reference).
    """
    merged: McpServersDict = dict(load_mcp_config())  # env base, re-read each call
    merged.update(_validated_manifest_servers())  # overlay wins on collision
    return merged


_server_mcp_config: McpServersDict = _compute_effective_config()


def reload_mcp_config() -> McpServersDict:
    """Recompute effective config and atomically REBIND the singleton.

    Rebinding (not in-place mutation) is what lets already-created sessions keep
    the MCP set they pinned at create_client time; only new ``get_mcp_servers()``
    callers see the new dict.
    """
    global _server_mcp_config
    new_config = _compute_effective_config()
    _server_mcp_config = new_config
    return new_config


def get_mcp_servers() -> McpServersDict:
    """Get the pre-loaded server-level MCP server config."""
    return _server_mcp_config


def get_validated_mcp_config() -> McpServersDict:
    """Return a copy of the already validated gateway MCP config."""
    return copy.deepcopy(_server_mcp_config)


def _dropped_env_servers() -> List[dict]:
    """Re-parse ``MCP_CONFIG`` raw source and return validation rejects. Never raises."""
    raw = _read_mcp_config_source()
    if raw is None:
        return []
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        return []
    _, dropped = validate_mcp_servers(servers)
    return dropped


def list_dropped_servers() -> List[dict]:
    """Servers the effective config dropped (invalid), with reasons. Never raises.

    Re-parses env source + manifest overlay and re-runs validation, collecting
    rejects. Manifest entries overshadow env entries of the same name, so an
    env server later fixed by the manifest is not reported as dropped.
    """
    dropped: List[dict] = []
    seen = set()
    try:
        from src import mcp_manifest

        manifest_raw = mcp_manifest.list_servers()
    except Exception:
        manifest_raw = {}
    _, man_drop = validate_mcp_servers(manifest_raw)
    for d in man_drop:
        d = {**d, "source": "manifest"}
        dropped.append(d)
        seen.add(d["name"])
    # env drops, but skip names the manifest supplies (overlay wins)
    for d in _dropped_env_servers():
        if d["name"] in manifest_raw or d["name"] in seen:
            continue
        dropped.append({**d, "source": "env"})
    return dropped
