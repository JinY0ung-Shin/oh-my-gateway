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
        "_note": "Values marked ***REDACTED*** contain secrets. "
        "Most settings require server restart to take effect.",
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
            k.strip() for k in os.getenv("METADATA_ENV_ALLOWLIST", "").split(",") if k.strip()
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
    """Return detailed MCP server configuration (names, types, tool patterns)."""
    try:
        from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns

        servers = get_mcp_servers()
        if not servers:
            return []

        patterns = get_mcp_tool_patterns(servers)
        result = []
        for name, config in servers.items():
            server_patterns = [p for p in patterns if p.startswith(f"mcp__{name}__")]
            result.append(
                {
                    "name": name,
                    "type": config.get("type", "unknown"),
                    "tools": server_patterns,
                    "config_keys": [k for k in config.keys() if k not in ("type",)],
                }
            )
        return result
    except Exception:
        logger.warning("Failed to read MCP servers detail", exc_info=True)
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
        "last_accessed": session.last_accessed.isoformat() if session.last_accessed else None,
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

