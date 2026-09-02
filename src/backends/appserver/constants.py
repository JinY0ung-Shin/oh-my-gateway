"""Configuration for the app-server-backed Codex adapter.

These read the same ``CODEX_*`` environment variables the frozen ``codex``
backend used, so a future ``BACKENDS=codex`` cutover onto this adapter needs no
operator config changes. The values are re-parsed on every call so tests and a
running server can flip them without a restart. This module owns its own env
parsing rather than importing ``src.backends.codex.constants`` -- the frozen
package stays frozen and this adapter never reaches into it.
"""

from __future__ import annotations

import os

from src.backends.common import parse_csv
from src.env_utils import parse_int_env

# Bound on how long the adapter waits for a single ``turn/start`` /
# ``turn/interrupt`` round trip and, separately, for the next item of a live
# turn's stream. Overridable so a slow provider does not have to change code.
DEFAULT_REQUEST_TIMEOUT_S = 60.0


def codex_bin() -> str:
    """Path/name of the ``codex`` binary that hosts the app-server."""
    return os.getenv("CODEX_BIN", "codex")


def app_server_argv() -> list[str]:
    """Argv that launches one ``codex app-server`` speaking JSON-RPC over stdio."""
    return [codex_bin(), "app-server", "--listen", "stdio://"]


def configured_provider_models() -> list[str]:
    """Provider-side Codex model IDs configured for listing."""
    return parse_csv(os.getenv("CODEX_MODELS", "gpt-5.5"))


def configured_public_models() -> list[str]:
    """Public wrapper model IDs (``codex/<model>``) for configured models."""
    return [f"codex/{model}" for model in configured_provider_models()]


def approval_policy() -> str:
    """Default Codex approval policy when the request sets no permission mode."""
    return os.getenv("CODEX_APPROVAL_POLICY", "never").strip() or "never"


def sandbox_mode() -> str:
    """Codex native sandbox mode, normalizing the legacy camelCase spellings."""
    raw = (
        os.getenv("CODEX_SANDBOX", "danger-full-access").strip() or "danger-full-access"
    )
    legacy_aliases = {
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }
    return legacy_aliases.get(raw, raw)


def request_timeout_s() -> float:
    """Per-request/per-item deadline in seconds (from ``CODEX_REQUEST_TIMEOUT_MS``)."""
    return (
        parse_int_env("CODEX_REQUEST_TIMEOUT_MS", int(DEFAULT_REQUEST_TIMEOUT_S * 1000))
        / 1000.0
    )


CODEX_PROVIDER_MODELS = configured_provider_models()
CODEX_MODELS = configured_public_models()
