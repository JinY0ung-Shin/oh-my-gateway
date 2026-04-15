"""Backend subpackage — multi-backend discovery, registration, and model resolution.

Re-exports core types and provides ``discover_backends()`` and ``resolve_model()``
as the primary entry points for ``main.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.backends.base import (  # noqa: F401
    BackendClient,
    BackendDescriptor,
    BackendRegistry,
    ResolvedModel,
)

logger = logging.getLogger(__name__)


def discover_backends(registry_cls=None) -> None:
    """Discover and register all known backends."""
    if registry_cls is None:
        registry_cls = BackendRegistry

    from src.backends.claude import register as register_claude

    register_claude(registry_cls=registry_cls)


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
