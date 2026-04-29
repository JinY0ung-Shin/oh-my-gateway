"""OpenCode backend authentication provider."""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List

from src.auth import BackendAuthProvider


class OpenCodeAuthProvider(BackendAuthProvider):
    """OpenCode backend auth and availability checks."""

    @property
    def name(self) -> str:
        return "opencode"

    def validate(self) -> Dict[str, Any]:
        if os.getenv("OPENCODE_BASE_URL"):
            return {
                "valid": False,
                "errors": [
                    "OPENCODE_BASE_URL is no longer supported; unset it to use managed OpenCode"
                ],
                "config": {"mode": "managed"},
            }

        binary = shutil.which(os.getenv("OPENCODE_BIN", "opencode"))
        if binary:
            return {
                "valid": True,
                "errors": [],
                "config": {"mode": "managed", "binary": binary},
            }

        return {
            "valid": False,
            "errors": ["opencode binary not found on PATH"],
            "config": {"mode": "managed"},
        }

    def build_env(self) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for key in (
            "OPENCODE_BIN",
            "OPENCODE_HOST",
            "OPENCODE_PORT",
            "OPENCODE_START_TIMEOUT_MS",
            "OPENCODE_AGENT",
            "OPENCODE_SERVER_USERNAME",
            "OPENCODE_SERVER_PASSWORD",
            "OPENCODE_CONFIG_CONTENT",
            "OPENCODE_DEFAULT_MODEL",
            "OPENCODE_QUESTION_PERMISSION",
            "OPENCODE_USE_WRAPPER_MCP_CONFIG",
        ):
            value = os.getenv(key)
            if value:
                env[key] = value
        return env

    def get_isolation_vars(self) -> List[str]:
        return []
