"""OpenCode backend constants and environment parsing."""

from __future__ import annotations

import os


def _parse_csv(value: str) -> list[str]:
    """Parse comma-separated environment values, preserving order."""
    items: list[str] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if item and item not in items:
            items.append(item)
    return items


def configured_provider_models() -> list[str]:
    """Return provider/model IDs configured for OpenCode model listing."""
    return _parse_csv(os.getenv("OPENCODE_MODELS", ""))


def configured_public_models() -> list[str]:
    """Return public wrapper model IDs for configured OpenCode models."""
    return [f"opencode/{model}" for model in configured_provider_models()]


OPENCODE_PROVIDER_MODELS = configured_provider_models()
OPENCODE_MODELS = configured_public_models()
