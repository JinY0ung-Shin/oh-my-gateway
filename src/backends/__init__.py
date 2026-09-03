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

# Frozen 2026-07: still registerable, but unmaintained and excluded from the
# default test suite (tests/conftest.py). Only 'claude' is maintained.
STALE_BACKENDS = ("opencode", "codex")


def _use_frozen_codex() -> bool:
    """Whether ``BACKENDS=codex`` should register the FROZEN codex client.

    The app-server adapter (``src/backends/appserver``, #173) is opt-in and NOT
    the default: the #173 traffic cutover is a hard release gate that requires the
    production two-user filesystem isolation + zero-egress proof, which is not met
    on this branch. So ``BACKENDS=codex`` keeps registering the frozen client
    unless an operator explicitly opts into the adapter with
    ``CODEX_BACKEND=appserver``. The final default flip belongs to a separate,
    small cutover PR made only after that gate passes.
    """
    return os.getenv("CODEX_BACKEND", "frozen").strip().lower() != "appserver"


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
        # 'codex' on the app-server adapter (the default after the #173 cutover)
        # is maintained; only the frozen fallback and opencode are stale.
        is_stale = name == "opencode" or (name == "codex" and _use_frozen_codex())
        if is_stale:
            logger.warning(
                "Backend %r is stale: frozen since 2026-07 and unmaintained; "
                "it may break without notice. Only 'claude' is supported.",
                name,
            )
        if name == "claude":
            from src.backends import claude

            claude.register(registry_cls=registry_cls)
        elif name == "opencode":
            from src.backends import opencode

            opencode.register(registry_cls=registry_cls)
        elif name == "codex":
            if _use_frozen_codex():
                from src.backends import codex

                codex.register(registry_cls=registry_cls)
            else:
                # Opt-in only (CODEX_BACKEND=appserver): the #173 adapter is not
                # the default until the production isolation/zero-egress gate
                # passes and a separate small cutover PR flips it.
                from src.backends.appserver import client as appserver_client

                appserver_client.register(registry_cls=registry_cls)
        else:
            logger.warning("Unknown backend in BACKENDS=%r; skipping", name)


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
