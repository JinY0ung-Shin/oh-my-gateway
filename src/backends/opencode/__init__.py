"""OpenCode backend subpackage."""

from __future__ import annotations

import logging
from typing import Optional

from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel
from src.backends.opencode.constants import OPENCODE_MODELS

logger = logging.getLogger(__name__)


def _opencode_resolve(model: str) -> Optional[ResolvedModel]:
    """Resolve opencode/<provider>/<model> into the OpenCode backend."""
    prefix = "opencode/"
    if not model.startswith(prefix):
        return None
    provider_model = model[len(prefix) :]
    if "/" not in provider_model:
        return None
    return ResolvedModel(
        public_model=model,
        backend="opencode",
        provider_model=provider_model,
    )


OPENCODE_DESCRIPTOR = BackendDescriptor(
    name="opencode",
    owned_by="opencode",
    models=list(OPENCODE_MODELS),
    resolve_fn=_opencode_resolve,
)


def __getattr__(name):
    if name == "OpenCodeClient":
        from src.backends.opencode.client import OpenCodeClient

        return OpenCodeClient
    if name == "OpenCodeAuthProvider":
        from src.backends.opencode.auth import OpenCodeAuthProvider

        return OpenCodeAuthProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def register(registry_cls=None) -> None:
    """Register OpenCode descriptor and live client when available."""
    if registry_cls is None:
        registry_cls = BackendRegistry

    registry_cls.register_descriptor(OPENCODE_DESCRIPTOR)

    try:
        from src.backends.opencode.client import OpenCodeClient

        client = OpenCodeClient()
        registry_cls.register("opencode", client)
        logger.info("Registered backend: opencode")
    except Exception as exc:
        logger.error("OpenCode backend client creation failed: %s", exc)
