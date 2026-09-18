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
