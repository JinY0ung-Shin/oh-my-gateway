"""Environment variable parsing utilities.

Provides consistent boolean and integer parsing across all modules so that
every ``os.getenv(...)`` check uses the same accepted values and fall-back
logic.  This module intentionally has **no** intra-project imports to avoid
circular dependencies — any module can safely import from here.
"""

import os
from pathlib import Path
from typing import Optional

_BOOL_TRUE = {"true", "1", "yes", "on"}


def parse_bool_env(name: str, default: str = "false") -> bool:
    """Parse a boolean environment variable.

    Accepted true values:  ``true``, ``1``, ``yes``, ``on``  (case-insensitive).
    Accepted false values: ``false``, ``0``, ``no``, ``off`` (case-insensitive).

    If the variable is unset the *default* string is evaluated instead.
    """
    raw = os.getenv(name, default)
    return raw.lower() in _BOOL_TRUE


def parse_int_env(name: str, default: int) -> int:
    """Parse an integer environment variable with a safe fallback.

    If the variable is unset or not a valid integer, *default* is returned.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_float_env(name: str, default: float) -> float:
    """Parse a float environment variable with a safe fallback.

    If the variable is unset or not a valid float, *default* is returned.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ``.claude/{skills,agents}`` is the Claude backend's gateway-managed
# compatibility namespace, not free workspace space: the resource bridge
# migrates a legacy native tree into the canonical ``skills``/``agents`` roots
# and then maintains ``.claude/<kind>`` as a marked mirror of them. Seeding into
# it therefore does not do what the operator wrote. Two ways it goes wrong:
#
# - ``WORKSPACE_INITIAL_DIRS=.claude/skills/review`` creates an *unmanaged*
#   native tree. With canonical ``skills/`` absent the bridge migrates that
#   directory to ``skills/``, so the configured path is gone after the first
#   resolve.
# - ``skills/review,.claude/skills/custom`` creates canonical and unmanaged
#   native trees at once, which the bridge refuses to reconcile — the starter
#   layout silently turns off mirror management for that workspace.
#
# The canonical roots express the same intent (``skills/review``), so these are
# rejected rather than quietly rewritten.
WORKSPACE_RESERVED_SEED_DIRS: tuple[tuple[str, ...], ...] = (
    (".claude", "skills"),
    (".claude", "agents"),
)


def _reserved_seed_prefix(parts: tuple[str, ...]) -> Optional[str]:
    """Return the reserved prefix *parts* falls under, or ``None``."""
    for reserved in WORKSPACE_RESERVED_SEED_DIRS:
        if parts[: len(reserved)] == reserved:
            return "/".join(reserved)
    return None


def parse_workspace_initial_dirs(raw: Optional[str]) -> tuple[Path, ...]:
    """Parse ``WORKSPACE_INITIAL_DIRS`` into safe workspace-relative paths.

    Comma-separated; nested paths and dot-directories are allowed. Duplicates
    collapse, order is preserved. Raises :class:`ValueError` for an entry that
    is absolute, carries ``..``, or falls inside a reserved backend namespace
    (:data:`WORKSPACE_RESERVED_SEED_DIRS`).

    Lives here — with the other env parsers and no intra-project imports — so
    the startup config check and the workspace manager validate through the
    same code instead of two mirrors that can drift apart.
    """
    if raw is None or not raw.strip():
        return ()

    result: list[Path] = []
    seen: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        path = Path(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part == ".." for part in path.parts)
        ):
            raise ValueError(
                f"unsafe entry {value!r}: entries must be relative workspace "
                "paths without '..'"
            )
        normalized = Path(*path.parts)
        reserved = _reserved_seed_prefix(normalized.parts)
        if reserved is not None:
            raise ValueError(
                f"entry {value!r} is inside {reserved}/, which the Claude backend "
                "manages as a mirror of the canonical resource roots; seed "
                f"{normalized.parts[-1]!r} under skills/ or agents/ instead"
            )
        key = normalized.as_posix()
        if key not in seen:
            result.append(normalized)
            seen.add(key)
    return tuple(result)


# ---------------------------------------------------------------------------
# USER_API_KEYS
# ---------------------------------------------------------------------------

import json as _json
import re as _re

# A workspace/user id: a single pure source shared by the auth manager
# (src/auth.py) and the startup config check (src/config_check.py) so the two
# can never drift on what counts as a valid USER_API_KEYS mapping.
_USER_ID_PATTERN = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")


def parse_user_api_keys(raw: Optional[str]) -> dict[str, str]:
    """Parse ``USER_API_KEYS`` (user -> bearer token) into a validated dict.

    A blank/unset value yields ``{}``. Invalid configuration raises
    ``ValueError`` (fail-fast at import/startup rather than silently falling
    back to an unscoped identity boundary). A valid-but-empty JSON object
    ``{}`` parses to an empty dict — which provides **no** authenticating token,
    so callers must test the returned dict's truthiness, not merely that the
    env var was set.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        raise ValueError(
            "USER_API_KEYS must be a JSON object mapping user ids to API keys"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("USER_API_KEYS must be a JSON object mapping user ids to API keys")

    result: dict[str, str] = {}
    seen_keys: set[str] = set()
    for user, key in parsed.items():
        if not isinstance(user, str) or not _USER_ID_PATTERN.fullmatch(user):
            raise ValueError(
                "USER_API_KEYS user ids must match "
                "^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$"
            )
        if not isinstance(key, str) or not key:
            raise ValueError(f"USER_API_KEYS entry for {user!r} must be a non-empty string")
        if key in seen_keys:
            raise ValueError("USER_API_KEYS must not map the same API key to multiple users")
        seen_keys.add(key)
        result[user] = key
    return result
