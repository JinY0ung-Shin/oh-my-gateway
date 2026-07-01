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

import re
from typing import Any, Dict, List, Optional

from src import mcp_manifest
from src.mcp_config import (
    get_mcp_tool_patterns,
    load_mcp_config,
    mcp_safe_name,
    validate_server,
)


class McpAdminError(ValueError):
    """Invalid MCP admin input (bad name/type/collision/not-editable)."""


# Server names: letters, digits and a small punctuation set. No whitespace, no
# shell metacharacters. Copy of ``plugin_admin_service._NAME_RE``.
_NAME_RE = re.compile(r"^[A-Za-z0-9._@/-]+$")


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise McpAdminError("server name is required")
    if not _NAME_RE.match(name) or name.startswith("-"):
        raise McpAdminError(
            f"invalid server name {name!r}: only letters, digits and ._@/- are allowed"
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


def validate_config(name: str, config: Any) -> Dict[str, Any]:
    """Pure preview for the /validate endpoint. NEVER raises, NEVER persists."""
    errors: List[str] = []
    n = (name or "").strip()
    if n and (not _NAME_RE.match(n) or n.startswith("-")):
        errors.append("invalid name: only letters, digits and ._@/- are allowed")
    if not isinstance(config, dict):
        errors.append("config must be a JSON object")
    else:
        ok, reason = validate_server(n or "x", config)
        if not ok:
            errors.append(reason)
    safe = mcp_safe_name(n) if n else ""
    return {
        "valid": not errors,
        "errors": errors,
        "normalized_name": safe,
        "tool_pattern": f"mcp__{safe}__*" if safe else "",
        "server_type": (
            config.get("type", "stdio") if isinstance(config, dict) else None
        ),
    }


def create_server(name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return _write(name, config, updating=False)


def update_server(name: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return _write(name, config, updating=True)


def _write(name: str, config: Any, *, updating: bool) -> Dict[str, Any]:
    name = _validate_name(name)
    if not isinstance(config, dict):
        raise McpAdminError("config must be a JSON object")
    ok, reason = validate_server(name, config)
    if not ok:
        raise McpAdminError(reason)
    env_names = _env_names()
    manifest_names = set(mcp_manifest.list_servers().keys())
    if name in env_names and name not in manifest_names:
        # env base is immutable (route maps this to 409).
        raise McpAdminError(
            f"'{name}' is defined by MCP_CONFIG (env) and is not editable"
        )
    if not updating and name in manifest_names:
        raise McpAdminError(f"server '{name}' already exists")
    if updating and name not in manifest_names:
        raise McpAdminError(f"server '{name}' not found")
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
    """Look up the server in the effective config and probe it. Never raises."""
    from src import mcp_connection_test
    from src.mcp_config import get_mcp_servers

    servers = get_mcp_servers()
    config = servers.get(name)
    if config is None:
        return {"ok": False, "detail": f"server '{name}' not found"}
    return await mcp_connection_test.test_mcp_server(name, config)
