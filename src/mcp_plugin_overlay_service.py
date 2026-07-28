"""Admin API logic for plugin MCP env/headers overlays."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src import mcp_plugin_overlay
from src.mcp_config import (
    list_env_refs,
    mcp_secret_maps_meta,
    plugin_mcp_tool_pattern,
    validate_string_map,
)


class McpPluginOverlayError(ValueError):
    """Invalid plugin MCP overlay input."""


class McpPluginOverlayNotFound(McpPluginOverlayError):
    """Overlay target missing: not a plugin server, or no stored overlay."""


# Same character class as mcp_admin_service server names.
_NAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise McpPluginOverlayError("server name is required")
    if not _NAME_RE.match(name) or name.startswith("-"):
        raise McpPluginOverlayError(
            f"invalid server name {name!r}: only letters, digits and ._@- are allowed"
        )
    return name


def _plugin_server_names() -> Dict[str, Dict[str, Any]]:
    """Map server_name -> first matching plugin entry metadata."""
    from src import plugin_service

    out: Dict[str, Dict[str, Any]] = {}
    for entry in plugin_service.list_plugin_mcp_servers():
        name = entry.get("server_name")
        if isinstance(name, str) and name not in out:
            out[name] = entry
    return out


_REDACTED = "***REDACTED***"


def _restore_redacted_map(
    new: Dict[str, Any], existing: Dict[str, Any]
) -> Dict[str, Any]:
    """Keep stored secrets when the admin form round-trips ``***REDACTED***``."""
    out: Dict[str, Any] = {}
    for k, v in new.items():
        if v == _REDACTED and k in existing:
            out[k] = existing[k]
        else:
            out[k] = v
    return out


def upsert_overlay(
    name: str,
    *,
    env: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, Any]] = None,
    plugin_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and persist an overlay for a plugin MCP server."""
    name = _validate_name(name)
    env = env if isinstance(env, dict) else {}
    headers = headers if isinstance(headers, dict) else {}

    existing = mcp_plugin_overlay.get_overlay(name)
    if existing:
        env = _restore_redacted_map(env, existing.get("env") or {})
        headers = _restore_redacted_map(headers, existing.get("headers") or {})

    for field, mapping in (("env", env), ("headers", headers)):
        if not mapping:
            continue
        ok, reason = validate_string_map(mapping, field)
        if not ok:
            raise McpPluginOverlayError(reason or f"invalid {field}")
        # A leftover placeholder means the key has no stored secret to restore
        # (new or renamed key) — storing the literal mask would break the server.
        for k, v in mapping.items():
            if v == _REDACTED:
                raise McpPluginOverlayError(
                    f"{field} key {k!r} still holds the redaction placeholder; "
                    "enter the actual value (a renamed key cannot reuse a stored secret)"
                )

    plugins = _plugin_server_names()
    if name not in plugins:
        raise McpPluginOverlayNotFound(
            f"server '{name}' is not a plugin-provided MCP server "
            "(overlay is only for source=plugin rows)"
        )

    # Reject empty body after string-map filter.
    if not env and not headers:
        raise McpPluginOverlayError(
            "overlay must include at least one env or headers entry"
        )

    entry = plugins[name]
    resolved_plugin_id = plugin_id or entry.get("plugin_id")
    stored = mcp_plugin_overlay.upsert_overlay(
        name,
        env=env,
        headers=headers,
        plugin_id=resolved_plugin_id if isinstance(resolved_plugin_id, str) else None,
    )
    plugin_label = entry.get("plugin_name") or "<plugin>"
    note = (
        "Applies to new Claude sessions: materializes this plugin server into "
        "gateway mcp_servers with merged env/headers, scoped to the MCP server "
        "process. This moves the server's tools from "
        f"{plugin_mcp_tool_pattern(plugin_label, name)} to mcp__{name}__* — "
        "update any name-keyed tool allowlists. Existing sessions keep their "
        "pinned set."
    )
    env_overlay = mcp_plugin_overlay.get_env_overlay(name)
    if env_overlay:
        note += (
            f" This server also has a {mcp_plugin_overlay.ENV_OVERLAY_VAR} layer; "
            "these saved keys win over it, other env-declared keys still apply."
        )
    return {
        "status": "saved",
        "server": name,
        "plugin_id": stored.get("plugin_id") or resolved_plugin_id,
        "overlay": _public_overlay(stored),
        "patterns": [f"mcp__{name}__*"],
        "note": note,
    }


def delete_overlay(name: str) -> Dict[str, Any]:
    name = _validate_name(name)
    existed = mcp_plugin_overlay.delete_overlay(name)
    if not existed:
        # An env-declared layer is not deletable from here: say so instead of
        # reporting "no overlay" while the server visibly still has credentials.
        if mcp_plugin_overlay.get_env_overlay(name):
            raise McpPluginOverlayNotFound(
                f"no stored overlay for server '{name}'; its credentials are "
                f"declared in {mcp_plugin_overlay.ENV_OVERLAY_VAR} and must be "
                "removed from the gateway environment"
            )
        raise McpPluginOverlayNotFound(f"no overlay for server '{name}'")
    return {"status": "deleted", "server": name}


def get_overlay_detail(name: str) -> Dict[str, Any]:
    name = _validate_name(name)
    from src.admin_service import _redact_mcp_config

    stored = mcp_plugin_overlay.get_overlay(name)
    plugins = _plugin_server_names()
    meta = plugins.get(name)
    # ``overlay`` stays the stored (file) layer — it is what the admin form
    # round-trips, and an env-declared value must not be written back to disk.
    # The env layer is reported alongside, read-only.
    env_overlay = mcp_plugin_overlay.get_env_overlay(name)
    effective = mcp_plugin_overlay.get_effective_overlay(name)
    return {
        "server": name,
        "exists": bool(stored),
        "plugin": meta.get("plugin_id") if meta else None,
        "overlay": _public_overlay(stored),
        "config_redacted": _redact_mcp_config(stored) if stored else {},
        **mcp_secret_maps_meta(stored or {}),
        "env_refs": list_env_refs(stored or {}),
        "env_declared": bool(env_overlay),
        "env_declared_var": mcp_plugin_overlay.ENV_OVERLAY_VAR,
        "env_overlay": _public_overlay(env_overlay),
        "env_overlay_env_keys": sorted((env_overlay.get("env") or {}).keys()),
        "env_overlay_header_keys": sorted((env_overlay.get("headers") or {}).keys()),
        "effective": _public_overlay(effective),
        "effective_env_refs": list_env_refs(effective or {}),
    }


def _public_overlay(stored: Dict[str, Any]) -> Dict[str, Any]:
    from src.admin_service import _redact_mcp_config

    if not stored:
        return {}
    return _redact_mcp_config(stored)
