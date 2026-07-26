"""Admin API logic for the gateway-managed Claude settings ``env`` block."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from src import claude_settings_env
from src.mcp_config import contains_env_ref

_REDACTED = "***REDACTED***"


class ClaudeSettingsEnvError(ValueError):
    """Invalid Claude settings env input."""


def _redact_value(key: str, value: str) -> str:
    """Mask secret-looking values; keep ``{{env:NAME}}`` references visible.

    Same rule as the MCP config redaction: an operator needs to see which gateway
    env var a value points at without the response carrying the resolved secret.
    """
    from src.admin_service import _SECRET_PATTERNS

    if contains_env_ref(value):
        return value
    if _SECRET_PATTERNS.search(key):
        return _REDACTED
    return value


def _redact_map(env: Mapping[str, str]) -> Dict[str, str]:
    return {k: _redact_value(k, v) for k, v in env.items()}


def _restore_redacted(
    new: Mapping[str, Any], existing: Mapping[str, str]
) -> Dict[str, Any]:
    """Keep stored secrets when the admin form round-trips ``***REDACTED***``.

    Runs before validation: a placeholder for a key that has no stored value is
    left in place so :func:`_validate` can reject it with a clear message.
    """
    out: Dict[str, Any] = {}
    for k, v in new.items():
        key = k.strip() if isinstance(k, str) else k
        if v == _REDACTED and isinstance(key, str) and key in existing:
            out[k] = existing[key]
        else:
            out[k] = v
    return out


def _validate(env: Mapping[str, Any]) -> Dict[str, str]:
    """Validate an incoming env map, raising on anything the store would drop."""
    validated: Dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key.strip():
            raise ClaudeSettingsEnvError("env var names must be non-empty strings")
        name = key.strip()
        if not claude_settings_env.is_valid_env_name(name):
            raise ClaudeSettingsEnvError(
                f"invalid env var name {name!r}: use letters, digits and underscore "
                "(no leading digit)"
            )
        if not isinstance(value, str):
            raise ClaudeSettingsEnvError(
                f"env var {name!r} must have a string value "
                f"(got {type(value).__name__})"
            )
        if name in claude_settings_env.RESERVED_KEYS:
            raise ClaudeSettingsEnvError(
                f"{name} is reserved: Claude settings env overrides the process "
                "environment, so setting it here would break the gateway's own "
                "Claude authentication"
            )
        if value == _REDACTED:
            raise ClaudeSettingsEnvError(
                f"env var {name!r} still holds the redaction placeholder; enter the "
                "actual value (a renamed key cannot reuse a stored secret)"
            )
        validated[name] = value
    return validated


def get_detail() -> Dict[str, Any]:
    """Panel payload: managed keys with their source, plus unmanaged file keys."""
    snap = claude_settings_env.snapshot()
    warnings = []
    if snap["settings_error"]:
        warnings.append(
            f"settings file problem: {snap['settings_error']} — projection is skipped "
            "until it parses"
        )
    if snap["effective"] and not snap["applies_to_sessions"]:
        warnings.append(
            "CLAUDE_SETTING_SOURCES does not include 'user', so gateway sessions do "
            "not read this file (Docker Compose sets user,project,local)"
        )
    return {
        "settings_path": snap["settings_path"],
        "settings_error": snap["settings_error"],
        "store_path": snap["store_path"],
        "env_layer_var": snap["env_layer_var"],
        "env_layer_declared": snap["env_layer_declared"],
        "applies_to_sessions": snap["applies_to_sessions"],
        "reserved_keys": snap["reserved_keys"],
        # ``admin`` is what the edit form round-trips; the deploy layer is
        # read-only here and must never be written into the store.
        "admin": _redact_map(snap["admin"]),
        "env_layer": _redact_map(snap["env_layer"]),
        "effective": _redact_map(snap["effective"]),
        "sources": snap["sources"],
        "projected": snap["projected"],
        "unmanaged_keys": snap["unmanaged_keys"],
        "warnings": warnings,
        "note": (
            "Claude Code applies these to the session process, so the agent's own "
            "Bash can read them. Values scoped to one MCP server belong in "
            "GATEWAY_MCP_SERVER_ENV / the MCP credential overlay instead."
        ),
    }


def replace_env(env: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate, store, and project the admin-managed env map."""
    incoming = env if isinstance(env, dict) else {}
    restored = _restore_redacted(incoming, claude_settings_env.get_admin_env())
    validated = _validate(restored)
    report = claude_settings_env.replace_admin_env(validated)
    if not report.get("ok"):
        raise ClaudeSettingsEnvError(
            report.get("error") or "failed to write the Claude settings file"
        )
    return {
        "status": "saved",
        "report": report,
        "detail": get_detail(),
    }


def clear_env() -> Dict[str, Any]:
    """Drop every admin-managed key; the deploy-time layer stays in place."""
    report = claude_settings_env.clear_admin_env()
    if not report.get("ok"):
        raise ClaudeSettingsEnvError(
            report.get("error") or "failed to write the Claude settings file"
        )
    return {"status": "cleared", "report": report, "detail": get_detail()}


def reproject() -> Dict[str, Any]:
    """Re-read both layers and rewrite the settings file (rotation / drift fix)."""
    claude_settings_env.reload()
    report = claude_settings_env.project()
    if not report.get("ok"):
        raise ClaudeSettingsEnvError(
            report.get("error") or "failed to write the Claude settings file"
        )
    return {"status": "projected", "report": report, "detail": get_detail()}
