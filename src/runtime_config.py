"""Runtime-editable configuration for admin hot-reload.

Provides a thread-safe singleton that stores configuration overrides.
Getters check overrides first, then fall back to the original constants
loaded at startup.

Only values listed in ``EDITABLE_KEYS`` can be changed at runtime.
Changes take effect on the **next request** — already-running requests
and already-created sessions are not retroactively affected.

Rate limits and timeout are intentionally excluded because slowapi
caches limit strings at decorator time and the backend captures
timeout at init time.  Changing those requires a server restart.
"""

import logging
import os
from threading import Lock
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Multipart file uploads carry a small boundary/header envelope in addition to
# file bytes. The request-boundary middleware and /files/upload route share this
# one reserve so the advertised file ceiling and the accepted request body stay
# in lockstep.
WORKSPACE_UPLOAD_MULTIPART_RESERVE = 8192


def _int_env(name: str) -> int:
    """Read an int env var, treating unset/junk as 0 ("not configured")."""
    try:
        return int(os.environ.get(name, "") or 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Editable key definitions
# ---------------------------------------------------------------------------

# Each key maps to display metadata and type/validation information.
EDITABLE_KEYS: Dict[str, Dict[str, Any]] = {
    "default_model": {
        "label": "Default Model",
        "type": "string",
        "description": "Fallback model when none specified in request",
    },
    "default_max_turns": {
        "label": "Max Turns",
        "type": "int",
        "description": "Maximum agentic turns per request",
    },
    "session_max_age_minutes": {
        "label": "Session TTL (min)",
        "type": "int",
        "description": "TTL for new sessions (existing sessions keep their original TTL)",
    },
    "session_eviction_policy": {
        "label": "Session Eviction",
        "type": "string",
        "options": ["reject", "lru"],
        "description": (
            "At the MAX_LIVE_SESSIONS cap: reject new sessions with 503, or evict "
            "the least-recently-used idle session (erases its in-memory "
            "conversation; resuming via previous_response_id restores only the "
            "turn counter and workspace)"
        ),
    },
    "thinking_mode": {
        "label": "Thinking Mode",
        "type": "string",
        "options": ["disabled", "adaptive", "enabled"],
        "description": (
            "Claude thinking mode. enabled = fixed budget (THINKING_BUDGET_TOKENS). "
            "Requests that send reasoning.effort / effort override this."
        ),
    },
    "token_streaming": {
        "label": "Token Streaming",
        "type": "bool",
        "description": "Stream individual tokens vs batched chunks",
    },
    "sanitizer_enabled": {
        "label": "Sanitizer (/v1/messages)",
        "type": "bool",
        "description": "Anthropic SSE sanitizer; effective only when ANTHROPIC_BASE_URL is set",
    },
    "auto_compact_window": {
        "label": "Auto-compact window (tokens)",
        "type": "int",
        "min": 20000,
        "description": (
            "Context size at which the CLI auto-compacts a session "
            "(CLAUDE_CODE_AUTO_COMPACT_WINDOW). Lower = compacts sooner, so long "
            "agentic turns stay under a small upstream window at the cost of "
            "losing older detail. 0 = leave the CLI default alone. Applies to NEW "
            "sessions; an override wins over the gateway process env."
        ),
    },
    "workspace_upload_max_bytes": {
        "label": "Workspace upload limit (bytes)",
        "type": "int",
        "min": 0,
        "description": (
            "Maximum size of one file accepted by POST /files/upload. This is the "
            "single file-upload ceiling used by both request-boundary enforcement "
            "and the workspace file route. Applies on the next upload request."
        ),
    },
    "agent_teams_enabled": {
        "label": "Agent Teams",
        "type": "bool",
        "description": (
            "Experimental CLI agent teams (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS) "
            "for NEW Claude sessions; an override wins over the gateway process "
            "env, and team tools also need the CLI's account-side feature gate"
        ),
    },
}


# ---------------------------------------------------------------------------
# RuntimeConfig singleton
# ---------------------------------------------------------------------------


class RuntimeConfig:
    """Thread-safe runtime configuration store."""

    def __init__(self) -> None:
        self._overrides: Dict[str, Any] = {}
        self._lock = Lock()

    def get(self, key: str) -> Any:
        """Return the runtime value for *key* (override or original constant)."""
        with self._lock:
            if key in self._overrides:
                return self._overrides[key]
        return self._get_original(key)

    def set(self, key: str, value: Any) -> None:
        """Set a runtime override. Raises ``KeyError`` for unknown keys."""
        if key not in EDITABLE_KEYS:
            raise KeyError(f"Key '{key}' is not editable at runtime")
        coerced = self._coerce(key, value)
        with self._lock:
            self._overrides[key] = coerced
        logger.info(f"Runtime config updated: {key} = {coerced!r}")

    def is_overridden(self, key: str) -> bool:
        """Return ``True`` if *key* has a runtime override."""
        with self._lock:
            return key in self._overrides

    def reset(self, key: str) -> None:
        """Remove a runtime override, reverting to the startup value.

        Raises ``KeyError`` for unknown keys.
        """
        if key not in EDITABLE_KEYS:
            raise KeyError(f"Key '{key}' is not editable at runtime")
        with self._lock:
            self._overrides.pop(key, None)
        logger.info(f"Runtime config reset: {key}")

    def reset_all(self) -> None:
        """Remove all runtime overrides."""
        with self._lock:
            self._overrides.clear()
        logger.info("Runtime config: all overrides cleared")

    def get_all(self) -> Dict[str, Any]:
        """Return all editable keys with their current effective values."""
        with self._lock:
            overrides = dict(self._overrides)
        result = {}
        for key, meta in EDITABLE_KEYS.items():
            original = self._get_original(key)
            is_overridden = key in overrides
            value = overrides[key] if is_overridden else original
            result[key] = {
                **meta,
                "value": value,
                "original": original,
                "overridden": is_overridden,
            }
        return result

    @staticmethod
    def _get_original(key: str) -> Any:
        """Return the original startup value from constants."""
        from src.constants import (
            DEFAULT_MAX_TURNS,
            DEFAULT_MODEL,
            SESSION_EVICTION_POLICY,
            SESSION_MAX_AGE_MINUTES,
            WORKSPACE_UPLOAD_MAX_BYTES,
        )
        from src.backends.claude.constants import (
            THINKING_MODE,
            TOKEN_STREAMING,
        )

        from src.sanitizer.config import _env_enabled as _sanitizer_env_enabled

        _map = {
            "default_model": DEFAULT_MODEL,
            "default_max_turns": DEFAULT_MAX_TURNS,
            "session_max_age_minutes": SESSION_MAX_AGE_MINUTES,
            "session_eviction_policy": SESSION_EVICTION_POLICY,
            "thinking_mode": THINKING_MODE,
            "token_streaming": TOKEN_STREAMING,
            "sanitizer_enabled": _sanitizer_env_enabled(),
            "workspace_upload_max_bytes": WORKSPACE_UPLOAD_MAX_BYTES,
            "agent_teams_enabled": bool(
                os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
            ),
            "auto_compact_window": _int_env("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
        }
        return _map.get(key)

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        """Coerce *value* to the expected type for *key*."""
        meta = EDITABLE_KEYS[key]
        expected = meta["type"]
        if expected == "int":
            v = int(value)
            low = meta.get("min", 1)
            if v < low:
                raise ValueError(f"{key} must be >= {low}, got {v}")
            return v
        if expected == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                low = value.lower()
                if low in ("true", "1", "yes", "on"):
                    return True
                if low in ("false", "0", "no", "off"):
                    return False
                raise ValueError(
                    f"{key} must be a boolean (true/false/yes/no/1/0), got {value!r}"
                )
            if isinstance(value, (int, float)):
                return bool(value)
            raise ValueError(f"{key} must be a boolean, got {type(value).__name__}")
        s = str(value)
        options = meta.get("options")
        if options and s not in options:
            raise ValueError(f"{key} must be one of {options}, got {s!r}")
        return s


runtime_config = RuntimeConfig()


def get_default_model() -> str:
    return runtime_config.get("default_model")


def get_default_max_turns() -> int:
    return runtime_config.get("default_max_turns")


def get_thinking_mode() -> str:
    return runtime_config.get("thinking_mode")


def get_token_streaming() -> bool:
    return runtime_config.get("token_streaming")


def get_workspace_upload_max_bytes() -> int:
    return runtime_config.get("workspace_upload_max_bytes")


def get_workspace_upload_request_max_bytes() -> int:
    """Maximum raw multipart body accepted by POST /files/upload."""
    return max(0, get_workspace_upload_max_bytes()) + WORKSPACE_UPLOAD_MULTIPART_RESERVE
