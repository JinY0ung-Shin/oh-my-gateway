"""Codex backend constants and environment parsing."""

from __future__ import annotations

import os

from src.backends.common import parse_csv


def configured_provider_models() -> list[str]:
    """Return Codex model IDs configured for model listing."""
    return parse_csv(os.getenv("CODEX_MODELS", "gpt-5.5"))


def configured_public_models() -> list[str]:
    """Return public wrapper model IDs for configured Codex models."""
    return [f"codex/{model}" for model in configured_provider_models()]


def configured_config_overrides() -> list[str]:
    """Return Codex CLI ``--config`` overrides from CODEX_CONFIG_OVERRIDES."""
    return parse_csv(os.getenv("CODEX_CONFIG_OVERRIDES", ""))


def codex_bin() -> str:
    return os.getenv("CODEX_BIN", "codex")


def approval_policy() -> str:
    return os.getenv("CODEX_APPROVAL_POLICY", "never").strip() or "never"


def sandbox_mode() -> str:
    raw = os.getenv("CODEX_SANDBOX", "danger-full-access").strip() or "danger-full-access"
    legacy_aliases = {
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }
    return legacy_aliases.get(raw, raw)


def disallowed_tools_from_env() -> list[str]:
    """Read DISALLOWED_TOOLS env (shared with Claude backend) for hard-block tool names."""
    return parse_csv(os.getenv("DISALLOWED_TOOLS", ""))


CODEX_PROVIDER_MODELS = configured_provider_models()
CODEX_MODELS = configured_public_models()
