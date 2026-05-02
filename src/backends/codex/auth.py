"""Codex backend authentication and availability provider."""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List

from src.auth import BackendAuthProvider
from src.backends.codex.constants import codex_bin


class CodexAuthProvider(BackendAuthProvider):
    """Codex app-server availability checks."""

    @property
    def name(self) -> str:
        return "codex"

    def validate(self) -> Dict[str, Any]:
        binary_name = codex_bin()
        binary = shutil.which(binary_name)
        if binary:
            return {
                "valid": True,
                "errors": [],
                "config": {"mode": "app-server", "binary": binary},
            }
        return {
            "valid": False,
            "errors": ["codex binary not found on PATH"],
            "config": {"mode": "app-server", "binary": binary_name},
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
