"""Backend subpackage — multi-backend discovery, registration, and model resolution.

Re-exports core types and provides ``discover_backends()`` and ``resolve_model()``
as the primary entry points for ``main.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from src.backends.base import (  # noqa: F401
    BackendClient,
    BackendDescriptor,
    BackendRegistry,
    ResolvedModel,
)

logger = logging.getLogger(__name__)


def _enabled_backend_names() -> list[str]:
    """Return backend names enabled by BACKENDS, preserving order."""
    raw = os.getenv("BACKENDS", "claude")
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip().lower()
        if name and name not in names:
            names.append(name)
    return names or ["claude"]


def discover_backends(registry_cls=None) -> None:
    """Discover and register all known backends."""
    if registry_cls is None:
        registry_cls = BackendRegistry

    for name in _enabled_backend_names():
        if name == "claude":
            from src.backends import claude as backend_pkg
        elif name == "opencode":
            from src.backends import opencode as backend_pkg
        elif name == "codex":
            from src.backends import codex as backend_pkg
        else:
            logger.warning("Unknown backend in BACKENDS=%r; skipping", name)
            continue
        backend_pkg.register(registry_cls=registry_cls)


def resolve_model(model: str) -> Optional[ResolvedModel]:
    """Parse a model string into backend + provider model.

    Queries registered descriptors and returns the first match.
    Returns ``None`` if no backend recognises the model.

    Resolution rules:
    - ``sonnet``        -> backend="claude", provider_model="sonnet"
    - ``opus``          -> backend="claude", provider_model="opus"
    - ``claude/opus``   -> backend="claude", provider_model="opus"
    """
    # Try each registered descriptor's resolve function
    for desc in BackendRegistry.all_descriptors().values():
        resolved = desc.resolve_fn(model)
        if resolved is not None:
            return resolved

    return None
