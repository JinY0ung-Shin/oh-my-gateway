"""
Constants and configuration for Claude Code Gateway.

Single source of truth for shared configuration values.
Backend-specific constants live in ``src/backends/<name>/constants.py``.
All configurable values can be overridden via environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Default model (recommended for most use cases)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "sonnet")

# Custom system prompt file path (empty = use claude_code preset)
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "")

# API Configuration
DEFAULT_MAX_TURNS = int(os.getenv("DEFAULT_MAX_TURNS", "10"))
DEFAULT_TIMEOUT_MS = int(os.getenv("MAX_TIMEOUT", "600000"))  # 10 minutes
DEFAULT_PORT = int(os.getenv("PORT", "8000"))
DEFAULT_HOST = os.getenv("CLAUDE_WRAPPER_HOST", "0.0.0.0")  # nosec B104
MAX_REQUEST_SIZE = int(os.getenv("MAX_REQUEST_SIZE", str(10 * 1024 * 1024)))  # 10MB

# Permission Modes
PERMISSION_MODE_BYPASS = "bypassPermissions"

# Session Management
SESSION_CLEANUP_INTERVAL_MINUTES = int(os.getenv("SESSION_CLEANUP_INTERVAL_MINUTES", "5"))
SESSION_MAX_AGE_MINUTES = int(os.getenv("SESSION_MAX_AGE_MINUTES", "60"))

# MCP Server Configuration
# Path to MCP config JSON file or inline JSON string
# Format: {"mcpServers": {"name": {"type": "stdio", "command": "...", "args": [...]}}}
MCP_CONFIG = os.getenv("MCP_CONFIG", "")

# SSE keepalive interval (seconds).  During long SDK operations (tool
# execution, context compaction) no events flow to the client.  Emitting
# an SSE comment (`: keepalive\n\n`) on this interval prevents HTTP
# proxies, load balancers, and client-side timeouts from closing the
# connection.  Set to 0 to disable.
SSE_KEEPALIVE_INTERVAL = int(os.getenv("SSE_KEEPALIVE_INTERVAL", "15"))

# Rate Limiting defaults (requests per minute)
# These are used by rate_limiter.py as the single source of truth
RATE_LIMITS = {
    "chat": int(os.getenv("RATE_LIMIT_CHAT_PER_MINUTE", "10")),
    "debug": int(os.getenv("RATE_LIMIT_DEBUG_PER_MINUTE", "2")),
    "auth": int(os.getenv("RATE_LIMIT_AUTH_PER_MINUTE", "10")),
    "session": int(os.getenv("RATE_LIMIT_SESSION_PER_MINUTE", "15")),
    "health": int(os.getenv("RATE_LIMIT_HEALTH_PER_MINUTE", "30")),
    "responses": int(os.getenv("RATE_LIMIT_RESPONSES_PER_MINUTE", "10")),
    "general": int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
}

# ---------------------------------------------------------------------------
# Backward compatibility — import backend-specific constants directly from
# the constants submodules (NOT the backend __init__.py) to avoid triggering
# the full backend package initialization (which imports auth providers and
# creates a circular dependency chain with src.auth).
# ---------------------------------------------------------------------------
from src.backends.claude.constants import (  # noqa: E402, F401
    CLAUDE_TOOLS,
    DEFAULT_ALLOWED_TOOLS,
    CLAUDE_MODELS,
    THINKING_MODE,
    THINKING_BUDGET_TOKENS,
    TOKEN_STREAMING,
    DISALLOWED_SUBAGENT_TYPES,
)
from src.backends.codex.constants import (  # noqa: E402, F401
    CODEX_MODELS,
    CODEX_DEFAULT_MODEL,
    CODEX_CLI_PATH,
    CODEX_TIMEOUT_MS,
    CODEX_CONFIG_ISOLATION,
)

ALL_MODELS = CLAUDE_MODELS + CODEX_MODELS
