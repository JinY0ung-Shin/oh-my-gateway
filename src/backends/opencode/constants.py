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


def _parse_bool(value: str, *, default: bool = False) -> bool:
    """Parse common truthy and falsy environment values."""
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def configured_provider_models() -> list[str]:
    """Return provider/model IDs configured for OpenCode model listing."""
    return _parse_csv(os.getenv("OPENCODE_MODELS", ""))


def configured_public_models() -> list[str]:
    """Return public wrapper model IDs for configured OpenCode models."""
    return [f"opencode/{model}" for model in configured_provider_models()]


def use_wrapper_mcp_config() -> bool:
    """Return whether managed OpenCode should include wrapper MCP config."""
    return _parse_bool(os.getenv("OPENCODE_USE_WRAPPER_MCP_CONFIG", "false"))


OPENCODE_PROVIDER_MODELS = configured_provider_models()
OPENCODE_MODELS = configured_public_models()
