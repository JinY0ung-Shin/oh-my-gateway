"""Admin service — config redaction, backend inspection, and session views.

Keeps route handlers thin by centralising secret masking, backend health
inspection, and read-only session views in one place.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Secrets that must be redacted in config output.
_SECRET_PATTERNS = re.compile(
    r"(ANTHROPIC_AUTH_TOKEN|API_KEY|ADMIN_API_KEY|OPENAI_API_KEY"
    r"|SECRET|PASSWORD|TOKEN|CREDENTIAL)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Session message history (read-only, no TTL refresh)
# ---------------------------------------------------------------------------


def get_session_messages(
    session_id: str,
    truncate: int = 500,
) -> Optional[List[Dict[str, Any]]]:
    """Return message history for a session without refreshing TTL.

    Returns ``None`` when the session does not exist.  Content is truncated
    to *truncate* characters in the response; set to ``0`` for full content.

    Content is truncated to *truncate* characters for display.
    """
    from src.session_manager import session_manager

    session = session_manager.peek_session(session_id)
    if session is None:
        return None

    messages = session.get_all_messages()  # returns a shallow copy
    result: List[Dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        content = msg.content
        display = str(content) if content else ""
        thinking = [str(text) for text in getattr(msg, "thinking", []) if text]

        truncated = False
        if truncate > 0 and len(display) > truncate:
            display = display[:truncate]
            truncated = True
        thinking_truncated = False
        display_thinking: List[str] = []
        for text in thinking:
            if truncate > 0 and len(text) > truncate:
                display_thinking.append(text[:truncate])
                thinking_truncated = True
            else:
                display_thinking.append(text)

        result.append(
            {
                "index": idx,
                "role": msg.role,
                "content": display,
                "truncated": truncated,
                "thinking": display_thinking,
                "thinking_truncated": thinking_truncated,
                "name": msg.name,
            }
        )

    thinking_message_count = sum(1 for msg in messages if getattr(msg, "thinking", []))
    logger.info(
        "Admin session messages returned: session_id=%s total=%d "
        "thinking_messages=%d truncate=%d",
        session_id,
        len(messages),
        thinking_message_count,
        truncate,
    )

    return result


def get_redacted_config() -> Dict[str, Any]:
    """Return runtime configuration with secrets masked."""
    from src.constants import (
        DEFAULT_MODEL,
        DEFAULT_MAX_TURNS,
        DEFAULT_TIMEOUT_MS,
        DEFAULT_PORT,
        DEFAULT_HOST,
        MAX_REQUEST_SIZE,
        SESSION_CLEANUP_INTERVAL_MINUTES,
        SESSION_MAX_AGE_MINUTES,
        RATE_LIMITS,
        THINKING_MODE,
        TOKEN_STREAMING,
    )

    def _redact(key: str, value: Any) -> Any:
        if key == "MCP_CONFIG" or _SECRET_PATTERNS.search(key):
            if value and str(value).strip():
                return "***REDACTED***"
            return "(not set)"
        return value

    # Collect all relevant env-based settings
    env_keys = [
        "DEFAULT_MODEL",
        "DEFAULT_MAX_TURNS",
        "MAX_TIMEOUT",
        "PORT",
        "GATEWAY_HOST",
        "CLAUDE_WRAPPER_HOST",
        "MAX_REQUEST_SIZE",
        "CORS_ORIGINS",
        "USER_WORKSPACES_DIR",
        "DEBUG_MODE",
        "VERBOSE",
        "THINKING_MODE",
        "TOKEN_STREAMING",
        "SESSION_CLEANUP_INTERVAL_MINUTES",
        "SESSION_MAX_AGE_MINUTES",
        "RATE_LIMIT_ENABLED",
        "ADMIN_API_KEY",
        "API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "MCP_CONFIG",
        "CLAUDE_SANDBOX_ENABLED",
        "PERMISSION_MODE",
        "BACKENDS",
        "OPENCODE_USE_WRAPPER_MCP_CONFIG",
        "OPENCODE_BASE_URL",
        "CODEX_APPROVAL_POLICY",
        "DISALLOWED_TOOLS",
        "GATEWAY_MCP_MANIFEST",
        "GATEWAY_MCP_SERVER_ENV",
        "GATEWAY_CLAUDE_SETTINGS_ENV",
        "CLAUDE_SETTING_SOURCES",
    ]

    env_snapshot = {}
    for k in env_keys:
        raw = os.getenv(k)
        env_snapshot[k] = _redact(k, raw) if raw else "(not set)"

    # MCP config: show server names only, not full config
    mcp_servers_info = None
    try:
        from src.mcp_config import get_mcp_servers

        servers = get_mcp_servers()
        if servers:
            mcp_servers_info = list(servers.keys())
    except Exception:
        logger.debug("Failed to read MCP server names for config", exc_info=True)

    return {
        "runtime": {
            "default_model": DEFAULT_MODEL,
            "default_max_turns": DEFAULT_MAX_TURNS,
            "timeout_ms": DEFAULT_TIMEOUT_MS,
            "port": DEFAULT_PORT,
            "host": DEFAULT_HOST,
            "max_request_size": MAX_REQUEST_SIZE,
            "thinking_mode": THINKING_MODE,
            "token_streaming": TOKEN_STREAMING,
        },
        "sessions": {
            "cleanup_interval_minutes": SESSION_CLEANUP_INTERVAL_MINUTES,
            "max_age_minutes": SESSION_MAX_AGE_MINUTES,
        },
        "rate_limits": dict(RATE_LIMITS),
        "mcp_servers": mcp_servers_info,
        "environment": env_snapshot,
        "_note": "Values marked ***REDACTED*** contain secrets. MCP servers "
        "hot-reload for new sessions; most other settings require a server restart.",
    }


# ---------------------------------------------------------------------------
# Backend health & auth inspection
# ---------------------------------------------------------------------------


async def get_backends_health() -> List[Dict[str, Any]]:
    """Return detailed health and auth info for every known backend."""
    from src.backends.base import BackendRegistry
    from src.auth import auth_manager

    descriptors = BackendRegistry.all_descriptors()
    backend_names = sorted(set(descriptors.keys()) | {"claude"})

    results: List[Dict[str, Any]] = []
    for name in backend_names:
        info: Dict[str, Any] = {"name": name, "registered": False}
        client = None

        # Registration status
        if BackendRegistry.is_registered(name):
            info["registered"] = True
            client = BackendRegistry.get(name)
            info["models"] = client.supported_models()

            # Active health check via verify()
            try:
                info["healthy"] = await client.verify()
            except Exception as e:
                info["healthy"] = False
                info["health_error"] = str(e)
        else:
            desc = descriptors.get(name)
            info["models"] = list(desc.models) if desc else []
            info["healthy"] = False
            info["health_error"] = "Backend not registered"

        # Auth details
        try:
            provider = auth_manager.get_provider(name)
            auth_status = provider.validate()
            info["auth"] = {
                "valid": auth_status.get("valid", False),
                "method": auth_status.get("config", {}).get("auth_method", "unknown"),
                "errors": auth_status.get("errors", []),
                "env_vars": list(provider.build_env().keys()),
                "isolation_vars": provider.get_isolation_vars(),
            }
        except Exception as e:
            info["auth"] = {"valid": False, "method": "unknown", "errors": [str(e)]}

        metadata: Dict[str, Any] = {}
        runtime_metadata = getattr(client, "runtime_metadata", None) if client else None
        if callable(runtime_metadata):
            try:
                metadata = runtime_metadata()
            except Exception:
                logger.warning(
                    "Failed to collect backend runtime metadata for %s",
                    name,
                    exc_info=True,
                )
        info["metadata"] = metadata

        results.append(info)

    return results


def get_sandbox_config() -> Dict[str, Any]:
    """Return sandbox and permission mode configuration."""
    return {
        "permission_mode": os.getenv("PERMISSION_MODE", "default"),
        "sandbox_enabled": os.getenv("CLAUDE_SANDBOX_ENABLED", "true"),
        "sandbox_auto_allow_bash": os.getenv("CLAUDE_SANDBOX_AUTO_ALLOW_BASH", "false"),
        "metadata_env_allowlist": sorted(
            k.strip()
            for k in os.getenv("METADATA_ENV_ALLOWLIST", "").split(",")
            if k.strip()
        ),
    }


def get_tools_registry() -> Dict[str, Any]:
    """Return available tools and their configuration."""
    result: Dict[str, Any] = {"backends": {}}

    try:
        from src.backends.claude.constants import CLAUDE_TOOLS, DEFAULT_ALLOWED_TOOLS

        result["backends"]["claude"] = {
            "all_tools": CLAUDE_TOOLS,
            "default_allowed": DEFAULT_ALLOWED_TOOLS,
        }
    except ImportError:
        pass

    # MCP tool patterns
    try:
        from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns

        servers = get_mcp_servers()
        if servers:
            result["mcp_tools"] = get_mcp_tool_patterns(servers)
        else:
            result["mcp_tools"] = []
    except Exception:
        logger.warning("Failed to read MCP tools", exc_info=True)
        result["mcp_tools"] = []

    return result


def get_mcp_servers_detail() -> List[Dict[str, Any]]:
    """Return detailed MCP server configuration (names, types, tool patterns).

    Also surfaces ``source`` (env vs. manifest), ``editable``, the redacted
    ``config``, the tool ``pattern``, and per-backend ``reach`` (display only).
    """
    try:
        from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns, mcp_safe_name
        from src import mcp_manifest

        servers = get_mcp_servers()
        if not servers:
            return []

        try:
            manifest_names = set(mcp_manifest.list_servers().keys())
        except Exception:
            manifest_names = set()

        from src.mcp_config import mcp_secret_maps_meta

        patterns = get_mcp_tool_patterns(servers)
        result = []
        for name, config in servers.items():
            safe_prefix = f"mcp__{mcp_safe_name(name)}__"
            server_patterns = [p for p in patterns if p.startswith(safe_prefix)]
            source = "manifest" if name in manifest_names else "env"
            # A GATEWAY_MCP_SERVER_ENV overlay for a name no plugin declares
            # merges into this config at session create — display it merged.
            has_env_overlay = False
            try:
                from src import mcp_plugin_overlay

                env_overlay = mcp_plugin_overlay.get_env_overlay(name)
                if env_overlay:
                    has_env_overlay = True
                    config = mcp_plugin_overlay.merge_overlay_into_config(
                        config, env_overlay
                    )
            except Exception:
                logger.debug(
                    "Failed to read MCP env overlay for %s", name, exc_info=True
                )
            meta = mcp_secret_maps_meta(config)
            result.append(
                {
                    "name": name,
                    "type": config.get("type", "unknown"),
                    "tools": server_patterns,
                    "config_keys": [k for k in config.keys() if k not in ("type",)],
                    "pattern": f"{safe_prefix}*",
                    "source": source,
                    "editable": source == "manifest",
                    "has_env_overlay": has_env_overlay,
                    "config": _redact_mcp_config(config),
                    "reach": compute_mcp_server_reach(name, config),
                    **meta,
                }
            )
        return result
    except Exception:
        logger.warning("Failed to read MCP servers detail", exc_info=True)
        return []


def _redact_mcp_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Redact a single MCP server config for display.

    Masks secret-looking top-level keys, every ``headers`` value, and any
    secret-looking ``env`` value (server configs carry tokens/credentials).

    Values that are (or embed) ``{{env:NAME}}`` templates are left visible so
    operators can see gateway-env references without storing resolved secrets in
    the admin response.
    """
    from src.mcp_config import contains_env_ref

    out: Dict[str, Any] = {}
    for k, v in config.items():
        if _SECRET_PATTERNS.search(k) and not contains_env_ref(v):
            out[k] = "***REDACTED***"
        elif k in ("env", "headers") and isinstance(v, dict):
            redacted: Dict[str, Any] = {}
            for kk, vv in v.items():
                if contains_env_ref(vv):
                    redacted[kk] = vv
                elif _SECRET_PATTERNS.search(kk) or k == "headers":
                    redacted[kk] = "***REDACTED***"
                else:
                    redacted[kk] = vv
            out[k] = redacted
        else:
            out[k] = v
    return out


def compute_mcp_server_reach(name: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-backend reach for one MCP server. DISPLAY ONLY (no enforcement change).

    Reflects how each enabled backend applies the server: Claude filters at
    create-time, Codex denies at approval-time, and OpenCode bakes it into the
    managed server at startup only (not hot-reloaded — restart required).
    """
    from src.backends import _enabled_backend_names
    from src.backends.base import BackendRegistry
    from src.backends.opencode.constants import use_wrapper_mcp_config
    from src.mcp_config import ALLOWED_TYPES, mcp_safe_name

    enabled = set(_enabled_backend_names())
    safe = mcp_safe_name(name)
    pattern = f"mcp__{safe}__*"
    stype = config.get("type", "stdio")
    out: List[Dict[str, Any]] = []

    def _reg(n: str) -> bool:
        return n in enabled and BackendRegistry.is_registered(n)

    # Claude — create-time allowlist filter
    out.append(
        {
            "backend": "claude",
            "reaches": _reg("claude"),
            "mode": "create-time",
            "pattern": pattern,
            "condition": "all servers when request sends no allowed_tools; "
            f"else only if {pattern} is in allowed_tools",
            "gated_by": ["BACKENDS", "request allowed_tools"],
        }
    )

    # Codex — create-time forward + approval-time deny
    approval = None
    if _reg("codex"):
        try:
            approval = (
                BackendRegistry.get("codex").runtime_metadata().get("approval_policy")
            )
        except Exception:
            pass
    out.append(
        {
            "backend": "codex",
            "reaches": _reg("codex"),
            "mode": "approval-time",
            "pattern": pattern,
            "condition": "forwarded at create-time; per-tool deny only when a tool "
            "policy is active (approvalPolicy off 'never')",
            "gated_by": ["BACKENDS", "CODEX_APPROVAL_POLICY", "DISALLOWED_TOOLS"],
            "approval_policy": approval,
        }
    )

    # OpenCode — startup-only, doubly gated, type-dependent
    oc_mode, wrapper = None, use_wrapper_mcp_config()
    if _reg("opencode"):
        try:
            oc_mode = BackendRegistry.get("opencode").runtime_metadata().get("mode")
        except Exception:
            oc_mode = None
    oc_reaches = (
        _reg("opencode") and oc_mode == "managed" and wrapper and stype in ALLOWED_TYPES
    )
    out.append(
        {
            "backend": "opencode",
            "reaches": bool(oc_reaches),
            "mode": "startup-only",
            "pattern": None,
            "condition": "baked into managed server at startup only when "
            "OPENCODE_USE_WRAPPER_MCP_CONFIG=true; NOT hot-reloaded — restart required",
            "gated_by": [
                "BACKENDS",
                "OPENCODE_USE_WRAPPER_MCP_CONFIG",
                "OPENCODE_BASE_URL",
            ],
            "opencode_mode": oc_mode,
        }
    )
    return out


def compute_plugin_mcp_reach(name: str) -> List[Dict[str, Any]]:
    """Per-backend reach for a plugin-provided MCP server. DISPLAY ONLY.

    Plugin MCP servers are loaded by the Claude SDK via ``setting_sources``
    (reading the plugin's ``.mcp.json``), NOT through the gateway's
    ``mcp_servers`` option. So they reach Claude only; Codex and OpenCode never
    read Claude plugins and therefore never see these servers.
    """
    from src.backends import _enabled_backend_names
    from src.backends.base import BackendRegistry
    from src.mcp_config import mcp_safe_name

    enabled = set(_enabled_backend_names())
    claude_on = "claude" in enabled and BackendRegistry.is_registered("claude")
    pattern = f"mcp__{mcp_safe_name(name)}__*"
    return [
        {
            "backend": "claude",
            "reaches": claude_on,
            "mode": "setting_sources",
            "pattern": pattern,
            "condition": "loaded from the plugin's .mcp.json via setting_sources "
            "when the plugin is enabled; covered by the mcp__* auto-allow",
            "gated_by": ["BACKENDS", "CLAUDE_SETTING_SOURCES", "plugin enabled"],
        },
        {
            "backend": "codex",
            "reaches": False,
            "mode": "n/a",
            "pattern": None,
            "condition": "Codex does not read Claude plugins",
            "gated_by": [],
        },
        {
            "backend": "opencode",
            "reaches": False,
            "mode": "n/a",
            "pattern": None,
            "condition": "OpenCode does not read Claude plugins",
            "gated_by": [],
        },
    ]


def get_plugin_mcp_servers_detail() -> List[Dict[str, Any]]:
    """MCP servers contributed by installed plugins (read-only, ``source='plugin'``).

    These are loaded by the Claude SDK via ``setting_sources`` and never pass
    through the gateway's effective MCP config, so ``get_mcp_servers_detail``
    cannot surface them. Returned as extra rows for the admin MCP tab: not
    editable/deletable (the plugin owns them), config redacted, tagged with the
    owning plugin, and flagged ``shadowed`` when a same-named env/manifest server
    also exists (that one is what non-Claude backends see).
    """
    try:
        from src import plugin_service
        from src.mcp_config import get_mcp_servers, mcp_safe_name, validate_server

        entries = plugin_service.list_plugin_mcp_servers()
        if not entries:
            return []

        try:
            effective = set(get_mcp_servers().keys())
        except Exception:
            effective = set()

        result: List[Dict[str, Any]] = []
        for entry in entries:
            name = entry["server_name"]
            config = entry["config"] if isinstance(entry.get("config"), dict) else {}
            safe_prefix = f"mcp__{mcp_safe_name(name)}__"
            from src.mcp_config import mcp_secret_maps_meta

            ok, reason = validate_server(name, config)
            meta = mcp_secret_maps_meta(config)
            # Credential overlay (env/headers only) for this plugin server:
            # ``overlay`` is the stored admin layer the edit form round-trips,
            # ``has_env_overlay`` flags the read-only GATEWAY_MCP_SERVER_ENV layer.
            overlay_meta: Dict[str, Any] = {
                "has_overlay": False,
                "has_env_overlay": False,
                "overlay_env_key_count": 0,
                "overlay_header_key_count": 0,
                "overlay_env_refs": [],
                "overlay": {},
            }
            try:
                from src import mcp_plugin_overlay

                overlay = mcp_plugin_overlay.get_overlay(name)
                effective_overlay = mcp_plugin_overlay.get_effective_overlay(name)
                if effective_overlay:
                    ometa = mcp_secret_maps_meta(effective_overlay)
                    overlay_meta = {
                        "has_overlay": bool(overlay),
                        "has_env_overlay": bool(
                            mcp_plugin_overlay.get_env_overlay(name)
                        ),
                        "overlay_env_key_count": ometa["env_key_count"],
                        "overlay_header_key_count": ometa["header_key_count"],
                        "overlay_env_refs": ometa["env_refs"],
                        "overlay": _redact_mcp_config(overlay) if overlay else {},
                    }
                    # Effective env/headers for display = base merged with overlay.
                    from src.mcp_plugin_overlay import merge_overlay_into_config

                    effective_cfg = merge_overlay_into_config(config, effective_overlay)
                    meta = mcp_secret_maps_meta(effective_cfg)
            except Exception:
                logger.debug("Failed to read plugin MCP overlay for %s", name, exc_info=True)

            result.append(
                {
                    "name": name,
                    "type": config.get("type", "stdio") if config else "unknown",
                    "tools": [f"{safe_prefix}*"],
                    "config_keys": [k for k in config.keys() if k != "type"],
                    "pattern": f"{safe_prefix}*",
                    "source": "plugin",
                    "editable": False,
                    # Overlay credentials are editable even though the plugin
                    # definition (command/url) is not.
                    "overlay_editable": True,
                    "plugin": entry.get("plugin_id"),
                    "plugin_name": entry.get("plugin_name"),
                    "scope": entry.get("scope"),
                    "config": _redact_mcp_config(config),
                    "reach": compute_plugin_mcp_reach(name),
                    "valid": ok,
                    "invalid_reason": None if ok else reason,
                    "shadowed": name in effective,
                    **meta,
                    **overlay_meta,
                }
            )
        return result
    except Exception:
        logger.warning("Failed to read plugin MCP servers detail", exc_info=True)
        return []


def get_dropped_mcp_servers() -> List[Dict[str, Any]]:
    """Return MCP servers dropped from the effective config, with reasons."""
    try:
        from src.mcp_config import list_dropped_servers

        return list_dropped_servers()
    except Exception:
        logger.warning("Failed to read dropped MCP servers", exc_info=True)
        return []


def get_session_detail(session_id: str) -> Optional[Dict[str, Any]]:
    """Return detailed session metadata (beyond message history)."""
    from src.session_manager import session_manager

    session = session_manager.peek_session(session_id)
    if session is None:
        return None

    return {
        "session_id": session.session_id,
        "backend": session.backend,
        "turn_counter": session.turn_counter,
        "ttl_minutes": session.ttl_minutes,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_accessed": (
            session.last_accessed.isoformat() if session.last_accessed else None
        ),
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "message_count": len(session.messages),
        "has_system_prompt": session.base_system_prompt is not None,
    }


def export_session_json(session_id: str) -> Optional[Dict[str, Any]]:
    """Export full session data as JSON-serializable dict."""
    from src.session_manager import session_manager

    session = session_manager.peek_session(session_id)
    if session is None:
        return None

    messages = []
    for msg in session.get_all_messages():
        content = msg.content
        display = str(content) if content else ""
        thinking = [str(text) for text in getattr(msg, "thinking", []) if text]

        item: Dict[str, Any] = {"role": msg.role, "content": display, "name": msg.name}
        if thinking:
            item["thinking"] = thinking
        messages.append(item)

    return {
        "session_id": session.session_id,
        "backend": session.backend,
        "turn_counter": session.turn_counter,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "messages": messages,
    }
