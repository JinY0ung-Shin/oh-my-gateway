"""Shared dependencies and helpers for route handlers."""

import logging
from typing import Any

from fastapi import HTTPException

from src.backends import (
    BackendClient,
    BackendRegistry,
    resolve_model,
    ResolvedModel,
)
from src.auth import validate_backend_auth

logger = logging.getLogger(__name__)


def resolve_and_get_backend(
    model: str,
) -> tuple[ResolvedModel, "BackendClient"]:
    """Resolve model -> backend and validate backend availability.

    Raises HTTPException(400) if the model is not recognised by any backend.
    Raises HTTPException(400) if the backend is not registered.
    """
    resolved = resolve_model(model)

    if resolved is None:
        supported = sorted(BackendRegistry.all_model_ids())
        raise HTTPException(
            status_code=400,
            detail=(f"Model '{model}' is not supported. Supported models: {supported}"),
        )

    if not BackendRegistry.is_registered(resolved.backend):
        raise HTTPException(
            status_code=400,
            detail=f"Backend '{resolved.backend}' for model '{model}' is not available.",
        )

    return resolved, BackendRegistry.get(resolved.backend)


def validate_backend_auth_or_raise(backend_name: str) -> None:
    """Validate backend authentication, raise HTTPException on failure."""
    auth_valid, auth_info = validate_backend_auth(backend_name)
    if not auth_valid:
        errors = auth_info.get("errors", [])
        error_suffix = f" ({'; '.join(errors)})" if errors else ""
        raise HTTPException(
            status_code=503,
            detail=(
                f"{backend_name} backend authentication failed{error_suffix}. "
                "Check /v1/auth/status for detailed information."
            ),
        )


def request_has_images(request: Any) -> bool:
    """Check if the Responses API request contains image content parts."""
    input_data = getattr(request, "input", None)
    if isinstance(input_data, list):
        for item in input_data:
            content = getattr(item, "content", None) or (
                item.get("content") if isinstance(item, dict) else None
            )
            if isinstance(content, list):
                for part in content:
                    ptype = (
                        part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
                    )
                    if ptype == "input_image":
                        return True
    return False


def validate_image_request(request: Any, backend: BackendClient) -> None:
    """Validate image requests: backend must support images.

    Raises HTTPException(400) on failure.
    """
    if not request_has_images(request):
        return

    # Check backend supports images
    if not hasattr(backend, "image_handler"):
        raise HTTPException(
            status_code=400,
            detail=f"Image input is not supported for the {backend.name} backend.",
        )


def truncate_image_data(obj: Any) -> Any:
    """Deep-copy and truncate base64 image data for safe logging."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in ("data", "url") and isinstance(v, str) and len(v) > 200:
                if "base64" in v[:50] or v.startswith("data:image"):
                    result[k] = v[:50] + "...[truncated]"
                    continue
            result[k] = truncate_image_data(v)
        return result
    if isinstance(obj, list):
        return [truncate_image_data(item) for item in obj]
    return obj
