"""Runtime MCP-server CRUD + validation + connection-test for the admin panel.

Env servers (from MCP_CONFIG) are the immutable base and are NOT editable via
this service; only manifest-layer servers are mutable. Every successful mutation
persists to :mod:`src.mcp_manifest` and hot-reloads into NEW sessions. All
functions synchronous except :func:`test_connection` (async); routes wrap the
sync functions in ``fastapi.concurrency.run_in_threadpool``.

Mirrors the typed-error + validate-before-write shape of
:mod:`src.plugin_admin_service` (:class:`PluginAdminError`, ``_NAME_RE``,
``_validate_name``). Far simpler than plugins: pure JSON, no git/subprocess for
CRUD.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from src import mcp_manifest
from src.mcp_config import (
    get_mcp_tool_patterns,
    load_mcp_config,
    mcp_safe_name,
    validate_server,
)

logger = logging.getLogger(__name__)


class McpAdminError(ValueError):
    """Invalid MCP admin input (bad name/type/collision/not-editable)."""


# Server names: letters, digits and a small punctuation set. No whitespace, no
# shell metacharacters. Unlike ``plugin_admin_service._NAME_RE`` this omits
# ``/``: update/delete/test address a server as a single ``/{name}`` path
# segment, so a name containing ``/`` (even percent-encoded) 404s in FastAPI.
_NAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

# Sentinel the admin read layer substitutes for secret values (see
# ``admin_service._redact_mcp_config``). On update it means "keep the stored
# value" so the redacted edit view never overwrites a real secret with the mask.
_REDACTED = "***REDACTED***"


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise McpAdminError("server name is required")
    if not _NAME_RE.match(name) or name.startswith("-"):
        raise McpAdminError(
            f"invalid server name {name!r}: only letters, digits and ._@- are allowed"
        )
    return name


def _env_names() -> set:
    """Names originating from the MCP_CONFIG base (immutable)."""
    return set(load_mcp_config().keys())


def _collision(name: str, existing: set) -> Optional[str]:
    """A different name that normalizes to the same ``mcp__<safe>__`` namespace."""
    safe = mcp_safe_name(name)
    for other in existing:
        if other != name and mcp_safe_name(other) == safe:
            return other
    return None


def _merge_redacted(new: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """Restore secrets masked by the admin read layer on update.

    The list/detail API returns configs with secret values replaced by
    ``_REDACTED`` (``admin_service._redact_mcp_config``), and the edit form
    round-trips that masked view. So on update any ``_REDACTED`` value means
    "keep the stored value" rather than persisting the mask over a real secret.
    Mirrors the redaction shape: top-level keys plus nested ``env``/``headers``.
    A newly typed value (anything but the sentinel) always wins; dropping a key
    still drops it.
    """
    if not isinstance(existing, dict):
        return new
    merged: Dict[str, Any] = {}
    for k, v in new.items():
        ev = existing.get(k)
        if v == _REDACTED and k in existing:
            merged[k] = ev
        elif k in ("env", "headers") and isinstance(v, dict) and isinstance(ev, dict):
            merged[k] = {
                kk: (ev[kk] if vv == _REDACTED and kk in ev else vv)
                for kk, vv in v.items()
            }
        else:
            merged[k] = v
    return merged


def validate_config(name: str, config: Any) -> Dict[str, Any]:
    """Pure preview for the /validate endpoint. NEVER raises, NEVER persists."""
    from src.mcp_config import mcp_secret_maps_meta

    errors: List[str] = []
    n = (name or "").strip()
    if n and (not _NAME_RE.match(n) or n.startswith("-")):
        errors.append("invalid name: only letters, digits and ._@- are allowed")
    if not isinstance(config, dict):
        errors.append("config must be a JSON object")
    else:
        ok, reason = validate_server(n or "x", config)
        if not ok:
            errors.append(reason)
    safe = mcp_safe_name(n) if n else ""
    meta = (
        mcp_secret_maps_meta(config)
        if isinstance(config, dict)
        else {
            "env_key_count": 0,
            "header_key_count": 0,
            "env_keys": [],
            "header_keys": [],
            "env_refs": [],
        }
    )
    return {
        "valid": not errors,
        "errors": errors,
        "normalized_name": safe,
        "tool_pattern": f"mcp__{safe}__*" if safe else "",
        "server_type": (
            config.get("type", "stdio") if isinstance(config, dict) else None
        ),
        **meta,
    }


def create_server(name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return _write(name, config, updating=False)


def update_server(name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return _write(name, config, updating=True)


def _write(name: str, config: Any, *, updating: bool) -> Dict[str, Any]:
    name = _validate_name(name)
    if not isinstance(config, dict):
        raise McpAdminError("config must be a JSON object")
    env_names = _env_names()
    manifest = mcp_manifest.list_servers()
    manifest_names = set(manifest.keys())
    if name in env_names and name not in manifest_names:
        # env base is immutable (route maps this to 409).
        raise McpAdminError(
            f"'{name}' is defined by MCP_CONFIG (env) and is not editable"
        )
    if not updating and name in manifest_names:
        raise McpAdminError(f"server '{name}' already exists")
    if updating and name not in manifest_names:
        raise McpAdminError(f"server '{name}' not found")
    if updating:
        # The edit form round-trips the redacted view; keep stored secrets.
        config = _merge_redacted(config, manifest.get(name) or {})
    ok, reason = validate_server(name, config)
    if not ok:
        raise McpAdminError(reason)
    hit = _collision(name, env_names | manifest_names)
    if hit:
        raise McpAdminError(
            f"name collides with '{hit}': both map to tool namespace "
            f"'mcp__{mcp_safe_name(name)}__*'"
        )
    mcp_manifest.upsert_server(name, config)  # reloads
    return {
        "status": "saved" if updating else "created",
        "server": name,
        "patterns": get_mcp_tool_patterns({name: config}),
    }


def delete_server(name: str) -> Dict[str, Any]:
    name = _validate_name(name)
    if name in _env_names() and name not in set(mcp_manifest.list_servers().keys()):
        raise McpAdminError(
            f"'{name}' is defined by MCP_CONFIG (env) and cannot be deleted"
        )
    existed = mcp_manifest.delete_server(name)  # reloads
    if not existed:
        raise McpAdminError(f"server '{name}' not found")
    return {"status": "deleted", "server": name}


async def test_connection(name: str) -> Dict[str, Any]:
    """Look up the server and probe it. Never raises.

    Checks the effective config (env base + manifest overlay) first, then falls
    back to plugin-provided servers (loaded by the SDK via ``setting_sources``),
    so a read-only plugin row in the MCP tab is testable too.
    """
    from src import mcp_connection_test
    from src.mcp_config import get_mcp_servers

    config = get_mcp_servers().get(name)
    source = "mcp_config"
    if config is not None:
        # A GATEWAY_MCP_SERVER_ENV overlay for this name merges into the config at
        # session create; probe with it or the credentials would be missing here.
        try:
            from src import mcp_plugin_overlay

            env_overlay = mcp_plugin_overlay.get_env_overlay(name)
            if env_overlay:
                config = mcp_plugin_overlay.merge_overlay_into_config(
                    config, env_overlay
                )
        except Exception:
            logger.debug("MCP env overlay merge failed for %s", name, exc_info=True)
    if config is None:
        from src import plugin_service

        config = plugin_service.get_plugin_mcp_server_config(name)
        source = "plugin"
        # Probe the session-equivalent config: admin credential overlay merged
        # and ${CLAUDE_PLUGIN_ROOT} expanded, matching what a new Claude
        # session materializes.
        if config is not None:
            try:
                from src import mcp_plugin_overlay

                materialized = mcp_plugin_overlay.materialize_plugin_server(name)
                if materialized is not None:
                    config = materialized
            except Exception:
                logger.debug(
                    "Plugin MCP overlay materialization failed for %s",
                    name,
                    exc_info=True,
                )
    if config is None:
        return {"ok": False, "detail": f"server '{name}' not found"}
    result = await mcp_connection_test.test_mcp_server(name, config)
    result["agent"] = _agent_availability(name, config, source, result)
    return result


def _agent_availability(
    name: str, config: Dict[str, Any], source: str, probe: Dict[str, Any]
) -> Dict[str, Any]:
    """Explain whether a successful probe can reach new agent sessions.

    This is intentionally not a protocol-level MCP tool invocation. The
    connection test remains a safe reachability check; this diagnostic adds the
    gateway/backend exposure conditions that must also be true for a new agent
    session to see the server.
    """
    try:
        if source == "plugin":
            from src.admin_service import compute_plugin_mcp_reach

            reach = compute_plugin_mcp_reach(name)
        else:
            from src.admin_service import compute_mcp_server_reach

            reach = compute_mcp_server_reach(name, config)
    except Exception:
        reach = []

    backends = [
        str(r.get("backend"))
        for r in reach
        if isinstance(r, dict) and r.get("reaches") and r.get("backend")
    ]
    reachable = bool(probe.get("ok"))
    exposed = bool(backends)
    usable = reachable and exposed

    if not reachable:
        status = "unreachable"
        message = "connection test failed; agent use is not available"
    elif not exposed:
        status = "not_exposed"
        message = "reachable, but no enabled backend exposes it to new sessions"
    else:
        status = "exposed"
        message = "reachable and exposed to new agent sessions: " + ", ".join(backends)
        if source == "plugin":
            message += " (Claude plugin setting_sources only)"
        else:
            message += " (existing sessions keep their pinned MCP set)"
        detail = str(probe.get("detail") or "")
        if "not spawned" in detail:
            message += "; stdio command was not spawned by this safe test"

    return {
        "usable": usable,
        "reachable": reachable,
        "exposed": exposed,
        "backends": backends,
        "source": source,
        "status": status,
        "message": message,
    }
