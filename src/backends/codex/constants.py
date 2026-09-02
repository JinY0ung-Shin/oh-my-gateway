"""Codex backend constants and environment parsing."""

from __future__ import annotations

import os
from typing import Optional

from src.backends.common import parse_csv
from src.env_utils import parse_int_env

# Per-event idle-gap timeout for one turn's notification stream, in
# milliseconds. This bounds how long the stream consumer waits for the *next*
# event before interrupting the turn, and is SEPARATE from the overall turn
# budget (``DEFAULT_TIMEOUT_MS`` / ``CodexClient.timeout``). Each gateway
# session owns its own ``codex app-server`` process, so a wedged turn only
# ever stalls itself. Override via CODEX_READ_IDLE_TIMEOUT_MS.
DEFAULT_READ_IDLE_TIMEOUT_MS = parse_int_env("CODEX_READ_IDLE_TIMEOUT_MS", 60_000)


def read_idle_timeout_ms() -> int:
    """Per-event idle timeout in ms, read from the env on each call."""
    return parse_int_env("CODEX_READ_IDLE_TIMEOUT_MS", 60_000)


def approval_timeout_ms() -> int:
    """How long an interactive approval may stay pending, in ms.

    The SDK answers approval requests through a synchronous callback on its
    reader thread; the gateway blocks that callback until the caller's
    continuation request supplies a decision. This bounds the wait so an
    abandoned approval eventually cancels instead of pinning the reader
    thread forever. Override via CODEX_APPROVAL_TIMEOUT_MS.
    """
    return parse_int_env("CODEX_APPROVAL_TIMEOUT_MS", 600_000)


def configured_provider_models() -> list[str]:
    """Return Codex model IDs configured for model listing."""
    return parse_csv(os.getenv("CODEX_MODELS", "gpt-5.5"))


def configured_public_models() -> list[str]:
    """Return public wrapper model IDs for configured Codex models."""
    return [f"codex/{model}" for model in configured_provider_models()]


def configured_config_overrides() -> list[str]:
    """Return Codex CLI ``--config`` overrides from CODEX_CONFIG_OVERRIDES."""
    return parse_csv(os.getenv("CODEX_CONFIG_OVERRIDES", ""))


def codex_bin_override() -> Optional[str]:
    """Operator override for the codex binary path.

    ``None`` (the default) lets the SDK resolve its bundled CLI binary from
    the ``openai-codex-cli-bin`` runtime package, so deployments no longer
    need a separately installed ``codex`` on PATH.
    """
    value = (os.getenv("CODEX_BIN") or "").strip()
    return value or None


def approval_policy() -> str:
    return os.getenv("CODEX_APPROVAL_POLICY", "never").strip() or "never"


def sandbox_mode() -> str:
    raw = (
        os.getenv("CODEX_SANDBOX", "danger-full-access").strip() or "danger-full-access"
    )
    legacy_aliases = {
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
        # SDK-preset spelling (``Sandbox.full_access``) for the wire mode.
        "full-access": "danger-full-access",
    }
    return legacy_aliases.get(raw, raw)


def disallowed_tools_from_env() -> list[str]:
    """Read DISALLOWED_TOOLS env (shared with Claude backend) for hard-block tool names."""
    return parse_csv(os.getenv("DISALLOWED_TOOLS", ""))


CODEX_PROVIDER_MODELS = configured_provider_models()
CODEX_MODELS = configured_public_models()
