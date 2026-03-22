"""Codex backend constants and configuration.

Single source of truth for Codex-specific models and configuration.
All configurable values can be overridden via environment variables.
"""

import os

from src.env_utils import parse_bool_env, parse_int_env

# NOTE: This module must NOT import from src.constants to avoid circular imports.
# Use parse_int_env (no intra-project deps) so the default stays in sync.
_DEFAULT_TIMEOUT_MS = parse_int_env("MAX_TIMEOUT", 600_000)

# Codex Models
# Codex sub-models (e.g. "codex/o3") are resolved via slash pattern in resolve_model()
CODEX_MODELS = [
    "codex",
]

# Codex Backend Configuration
CODEX_DEFAULT_MODEL = os.getenv("CODEX_DEFAULT_MODEL", "gpt-5.4")
CODEX_CLI_PATH = os.getenv("CODEX_CLI_PATH", "codex")
CODEX_TIMEOUT_MS = int(os.getenv("CODEX_TIMEOUT_MS", str(_DEFAULT_TIMEOUT_MS)))
CODEX_CONFIG_ISOLATION = parse_bool_env("CODEX_CONFIG_ISOLATION", "false")
