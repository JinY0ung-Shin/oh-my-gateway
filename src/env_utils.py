"""Environment variable parsing utilities.

Provides consistent boolean and integer parsing across all modules so that
every ``os.getenv(...)`` check uses the same accepted values and fall-back
logic.  This module intentionally has **no** intra-project imports to avoid
circular dependencies — any module can safely import from here.
"""

import os


_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}
_BOOL_ALL = _BOOL_TRUE | _BOOL_FALSE


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
