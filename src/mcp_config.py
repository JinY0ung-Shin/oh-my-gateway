"""MCP server configuration management.

Loads server-level MCP config from the MCP_CONFIG environment variable.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from src.constants import MCP_CONFIG

logger = logging.getLogger(__name__)

McpServersDict = Dict[str, Dict[str, Any]]

ALLOWED_TYPES = {"stdio", "sse", "http", "streamable-http"}


def load_mcp_config() -> McpServersDict:
    """Load MCP server config from MCP_CONFIG environment variable.

    Accepts a JSON file path or inline JSON string.
    Format: {"mcpServers": {"name": {...}}} or {"name": {...}}
    """
    if not MCP_CONFIG:
        return {}

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
            return {}
    else:
        try:
            raw = json.loads(config_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse MCP_CONFIG as JSON: {e}")
            return {}

    if not isinstance(raw, dict):
        logger.error("MCP_CONFIG must be a JSON object")
        return {}

    servers = raw.get("mcpServers", raw)

    # Required fields per server type
    _REQUIRED_FIELDS = {
        "stdio": ("command",),
        "sse": ("url",),
        "http": ("url",),
        "streamable-http": ("url",),
    }

    validated: McpServersDict = {}
    for name, config in servers.items():
        if not isinstance(config, dict):
            logger.warning(f"Skipping invalid MCP server config '{name}': not a dict")
            continue
        server_type = config.get("type", "stdio")
        if server_type not in ALLOWED_TYPES:
            logger.warning(f"Skipping MCP server '{name}': unsupported type '{server_type}'")
            continue
        required = _REQUIRED_FIELDS.get(server_type, ())
        missing = [f for f in required if not config.get(f)]
        if missing:
            logger.warning(
                f"Skipping MCP server '{name}': missing required field(s) {missing} for type '{server_type}'"
            )
            continue
        validated[name] = config

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
    return [f"mcp__{'_'.join(name.split('-'))}__*" for name in servers]


_server_mcp_config: McpServersDict = load_mcp_config()


def get_mcp_servers() -> McpServersDict:
    """Get the pre-loaded server-level MCP server config."""
    return _server_mcp_config
