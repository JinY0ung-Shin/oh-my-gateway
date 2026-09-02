"""Codex backend subpackage."""

from __future__ import annotations

import logging
from typing import Optional

from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel
from src.backends.codex.constants import configured_public_models

logger = logging.getLogger(__name__)


def _codex_resolve(model: str) -> Optional[ResolvedModel]:
    """Resolve codex/<model> into the Codex backend."""
    prefix = "codex/"
    if not model.startswith(prefix):
        return None
    provider_model = model[len(prefix) :]
    if not provider_model:
        return None
    return ResolvedModel(
        public_model=model,
        backend="codex",
        provider_model=provider_model,
    )


def _codex_discover_models():
    """Lazy import so descriptor creation never pulls the SDK-backed module."""
    from src.backends.codex.model_discovery import discover_models

    return discover_models()


CODEX_DESCRIPTOR = BackendDescriptor(
    name="codex",
    owned_by="openai",
    models=configured_public_models(),
    resolve_fn=_codex_resolve,
    # Codex accepts multimodal input natively via turn input items (see
    # validate_image_request in src/routes/deps.py).
    capabilities={"image_input": True},
    # Best-effort live model listing from the app-server (opt-in via
    # CODEX_MODEL_DISCOVERY_ENABLED); /v1/models never depends on it.
    model_discovery_fn=_codex_discover_models,
)


def __getattr__(name):
    if name == "CodexClient":
        from src.backends.codex.client import CodexClient

        return CodexClient
    if name == "CodexAuthProvider":
        from src.backends.codex.auth import CodexAuthProvider

        return CodexAuthProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def register(registry_cls=None) -> None:
    """Register Codex descriptor and live client when available."""
    if registry_cls is None:
        registry_cls = BackendRegistry

    registry_cls.register_descriptor(CODEX_DESCRIPTOR)

    try:
        from src.backends.codex.client import CodexClient

        client = CodexClient()
        registry_cls.register("codex", client)
        logger.info("Registered backend: codex")
    except Exception as exc:
        logger.error("Codex backend client creation failed: %s", exc)
