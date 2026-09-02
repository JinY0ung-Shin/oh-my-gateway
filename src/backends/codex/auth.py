"""Codex backend authentication and availability provider."""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List

from src.auth import BackendAuthProvider
from src.backends.codex.constants import codex_bin_override


def _resolve_binary() -> tuple[str | None, str]:
    """Resolve the codex binary the SDK would launch.

    An explicit ``CODEX_BIN`` wins; otherwise the openai-codex SDK resolves
    its bundled CLI from the ``openai-codex-cli-bin`` runtime package, so a
    PATH lookup alone would wrongly report the backend unavailable.
    """
    override = codex_bin_override()
    if override:
        found = override if os.path.exists(override) else shutil.which(override)
        return found, override
    try:
        from openai_codex.client import CodexConfig, _resolve_codex_bin

        return str(_resolve_codex_bin(CodexConfig())), "(sdk-bundled)"
    except Exception:
        return None, "(sdk-bundled)"


class CodexAuthProvider(BackendAuthProvider):
    """Codex SDK availability checks."""

    @property
    def name(self) -> str:
        return "codex"

    def validate(self) -> Dict[str, Any]:
        binary, requested = _resolve_binary()
        if binary:
            return {
                "valid": True,
                "errors": [],
                "config": {"mode": "sdk", "binary": binary},
            }
        return {
            "valid": False,
            "errors": [
                "codex binary not found: install the openai-codex package "
                "(bundled CLI) or point CODEX_BIN at a codex binary"
            ],
            "config": {"mode": "sdk", "binary": requested},
        }

    def build_env(self) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for key in (
            "CODEX_BIN",
            "CODEX_HOME",
            "CODEX_MODELS",
            "CODEX_APPROVAL_POLICY",
            "CODEX_SANDBOX",
            "CODEX_CONFIG_OVERRIDES",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
        ):
            value = os.getenv(key)
            if value:
                env[key] = value
        return env

    def get_isolation_vars(self) -> List[str]:
        return ["ANTHROPIC_AUTH_TOKEN"]
