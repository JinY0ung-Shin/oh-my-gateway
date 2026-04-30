"""OpenCode backend authentication provider."""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List
from urllib.parse import urlparse

from src.auth import BackendAuthProvider

_INVALID_BASE_URL_ERROR = "OPENCODE_BASE_URL must be an http(s) URL with a host"


def normalize_opencode_base_url(base_url: str) -> tuple[str | None, str | None]:
    """Return a normalized OpenCode base URL or a validation error."""
    normalized = base_url.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, _INVALID_BASE_URL_ERROR
    return normalized, None


class OpenCodeAuthProvider(BackendAuthProvider):
    """OpenCode backend auth and availability checks."""

    @property
    def name(self) -> str:
        return "opencode"

    def validate(self) -> Dict[str, Any]:
        base_url = os.getenv("OPENCODE_BASE_URL")
        if base_url and base_url.strip():
            normalized_url, error = normalize_opencode_base_url(base_url)
            if error:
                return {
                    "valid": False,
                    "errors": [error],
                    "config": {"mode": "external", "base_url": base_url},
                }
            return {
                "valid": True,
                "errors": [],
                "config": {"mode": "external", "base_url": normalized_url},
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
            "OPENCODE_BASE_URL",
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
