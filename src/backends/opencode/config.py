"""OpenCode config generation helpers."""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, Optional

OpenCodeConfig = Dict[str, Any]
WrapperMcpServers = Dict[str, Dict[str, Any]]

_REMOTE_MCP_TYPES = {"sse", "http", "streamable-http"}
# Keep this transport set aligned with src.mcp_config.ALLOWED_TYPES.


def _deep_merge_missing(target: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Merge defaults into target without overriding explicit target values."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        if isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge_missing(target[key], value)
    return target


def _copy_optional_fields(
    source: Dict[str, Any],
    target: Dict[str, Any],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field in source:
            target[field] = copy.deepcopy(source[field])


def _command_list(server: Dict[str, Any]) -> list[str]:
    command = server["command"]
    args = server.get("args") or []
    if isinstance(command, list):
        return [str(item) for item in [*command, *args]]
    return [str(command), *[str(item) for item in args]]


def _convert_mcp_server(server: Dict[str, Any]) -> Dict[str, Any]:
    server_type = server.get("type", "stdio")
    if server_type == "stdio":
        converted: Dict[str, Any] = {
            "type": "local",
            "command": _command_list(server),
        }
        environment = server.get("environment", server.get("env"))
        if environment is not None:
            converted["environment"] = copy.deepcopy(environment)
        _copy_optional_fields(server, converted, ("enabled", "timeout"))
        return converted

    if server_type in _REMOTE_MCP_TYPES:
        converted = {
            "type": "remote",
            "url": server["url"],
        }
        _copy_optional_fields(server, converted, ("headers", "enabled", "oauth", "timeout"))
        return converted

    raise ValueError(f"Unsupported MCP server type for OpenCode config: {server_type}")


def build_opencode_config(
    *,
    base_config: OpenCodeConfig,
    mcp_servers: WrapperMcpServers,
    default_model: Optional[str],
    question_permission: str,
) -> OpenCodeConfig:
    """Build managed OpenCode config from base content and wrapper MCP servers."""
    config = copy.deepcopy(base_config)
    defaults: Dict[str, Any] = {
        "permission": {"question": question_permission},
        "share": "disabled",
    }
    if default_model:
        defaults["model"] = default_model
    _deep_merge_missing(config, defaults)

    if mcp_servers:
        mcp_config = config.setdefault("mcp", {})
        if isinstance(mcp_config, dict):
            for name, server in mcp_servers.items():
                mcp_config.setdefault(name, _convert_mcp_server(server))

    return config


def parse_opencode_config_content(content: Optional[str]) -> OpenCodeConfig:
    """Parse OPENCODE_CONFIG_CONTENT as a JSON object."""
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OPENCODE_CONFIG_CONTENT is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("OPENCODE_CONFIG_CONTENT must be a JSON object")
    return parsed
