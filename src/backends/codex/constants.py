"""Codex backend constants and environment parsing."""

from __future__ import annotations

import os

from src.backends.common import parse_csv
from src.env_utils import parse_int_env

# Per-message (idle-gap) read timeout for the shared Codex app-server JSON-RPC
# transport, in milliseconds. This bounds how long a single ``_read_message``
# waits for the *next* line of output before failing, and is SEPARATE from the
# overall turn budget (``DEFAULT_TIMEOUT_MS`` / ``CodexClient.timeout``).
#
# Why a short value: the app-server is a single shared process serialized by
# one asyncio lock per ``CodexClient``. A wedged turn (process alive but
# emitting nothing) otherwise holds that lock for the full turn budget and
# head-of-line-blocks every other concurrent Codex request. A normal turn
# resets this window on every incremental notification, so the cap only bites
# on genuine inter-message silence. Override via CODEX_READ_IDLE_TIMEOUT_MS.
DEFAULT_READ_IDLE_TIMEOUT_MS = parse_int_env("CODEX_READ_IDLE_TIMEOUT_MS", 60_000)


def read_idle_timeout_ms() -> int:
    """Per-message idle read timeout in ms, read from the env on each call."""
    return parse_int_env("CODEX_READ_IDLE_TIMEOUT_MS", 60_000)


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
