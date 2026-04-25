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
from threading import Lock
from typing import Any, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Editable key definitions
# ---------------------------------------------------------------------------

# Each key maps to: (display_name, type, description, restart_required)
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
    "thinking_mode": {
        "label": "Thinking Mode",
        "type": "string",
        "description": "Claude thinking mode (disabled / adaptive)",
    },
    "token_streaming": {
        "label": "Token Streaming",
        "type": "bool",
        "description": "Stream individual tokens vs batched chunks",
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
        """Set a runtime override.  Raises ``KeyError`` for unknown keys."""
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
        result = {}
        for key, meta in EDITABLE_KEYS.items():
            value = self.get(key)
            with self._lock:
                is_overridden = key in self._overrides
            result[key] = {
                **meta,
                "value": value,
                "original": self._get_original(key),
                "overridden": is_overridden,
            }
        return result

    # ---- helpers ----

    @staticmethod
    def _get_original(key: str) -> Any:
        """Return the original startup value from constants."""
        from src.constants import (
            DEFAULT_MODEL,
            DEFAULT_MAX_TURNS,
            SESSION_MAX_AGE_MINUTES,
        )
        from src.backends.claude.constants import (
            THINKING_MODE,
            TOKEN_STREAMING,
        )

        _map = {
            "default_model": DEFAULT_MODEL,
            "default_max_turns": DEFAULT_MAX_TURNS,
            "session_max_age_minutes": SESSION_MAX_AGE_MINUTES,
            "thinking_mode": THINKING_MODE,
            "token_streaming": TOKEN_STREAMING,
        }
        return _map.get(key)

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        """Coerce *value* to the expected type for *key*."""
        meta = EDITABLE_KEYS[key]
        expected = meta["type"]
        if expected == "int":
            v = int(value)
            if v < 1:
                raise ValueError(f"{key} must be >= 1, got {v}")
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
                raise ValueError(f"{key} must be a boolean (true/false/yes/no/1/0), got {value!r}")
            if isinstance(value, (int, float)):
                return bool(value)
            raise ValueError(f"{key} must be a boolean, got {type(value).__name__}")
        return str(value)


# ---------------------------------------------------------------------------
# Convenience getters — import these instead of raw constants
# ---------------------------------------------------------------------------

runtime_config = RuntimeConfig()


def get_default_model() -> str:
    return runtime_config.get("default_model")


def get_default_max_turns() -> int:
    return runtime_config.get("default_max_turns")


def get_thinking_mode() -> str:
    return runtime_config.get("thinking_mode")


def get_token_streaming() -> bool:
    return runtime_config.get("token_streaming")
