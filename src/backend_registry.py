"""Backward compatibility — use src.backends instead.

Provides lazy re-exports so that existing ``from src.backend_registry import ...``
continues to work without triggering circular imports.
"""


def __getattr__(name):
    _BASE_NAMES = {
        "BackendClient",
        "BackendDescriptor",
        "BackendRegistry",
        "ResolvedModel",
    }
    if name in _BASE_NAMES:
        from src.backends import base as _base

        return getattr(_base, name)

    if name == "resolve_model":
        from src.backends import resolve_model

        return resolve_model

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
