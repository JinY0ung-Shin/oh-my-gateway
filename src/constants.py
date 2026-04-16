"""
Constants and configuration for Claude Code Gateway.

Single source of truth for shared configuration values.
Backend-specific constants live in ``src/backends/<name>/constants.py``.
All configurable values can be overridden via environment variables.
"""

import os
from dotenv import load_dotenv

from src.env_utils import parse_bool_env, parse_int_env

load_dotenv()

# Default model (recommended for most use cases)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "sonnet")

# Custom system prompt file path (empty = use claude_code preset)
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "")

# System prompt placeholder values (resolved in {{PLACEHOLDER}} tokens)
PROMPT_LANGUAGE = os.getenv("PROMPT_LANGUAGE", "English")
PROMPT_MEMORY_PATH = os.getenv("PROMPT_MEMORY_PATH", "")

# API Configuration
DEFAULT_MAX_TURNS = int(os.getenv("DEFAULT_MAX_TURNS", "10"))
DEFAULT_TIMEOUT_MS = parse_int_env("MAX_TIMEOUT", 600_000)  # 10 minutes
DEFAULT_PORT = int(os.getenv("PORT", "8000"))
DEFAULT_HOST = os.getenv("CLAUDE_WRAPPER_HOST", "0.0.0.0")  # nosec B104
MAX_REQUEST_SIZE = int(os.getenv("MAX_REQUEST_SIZE", str(10 * 1024 * 1024)))  # 10MB

# Permission Modes
PERMISSION_MODE_BYPASS = "bypassPermissions"

# Session Management
SESSION_CLEANUP_INTERVAL_MINUTES = int(os.getenv("SESSION_CLEANUP_INTERVAL_MINUTES", "5"))
SESSION_MAX_AGE_MINUTES = int(os.getenv("SESSION_MAX_AGE_MINUTES", "60"))

# Per-user workspace isolation
# Base directory for user workspaces. Falls back to CLAUDE_CWD if empty.
USER_WORKSPACES_DIR = os.getenv("USER_WORKSPACES_DIR", "")

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

# ---------------------------------------------------------------------------
# Subagent Streaming Visibility
# ---------------------------------------------------------------------------
SUBAGENT_STREAM_TEXT = parse_bool_env("SUBAGENT_STREAM_TEXT", "false")
SUBAGENT_STREAM_TOOL_BLOCKS = parse_bool_env("SUBAGENT_STREAM_TOOL_BLOCKS", "true")
SUBAGENT_STREAM_PROGRESS = parse_bool_env("SUBAGENT_STREAM_PROGRESS", "true")

# Rate Limiting defaults (requests per minute)
RATE_LIMITS = {
    "debug": int(os.getenv("RATE_LIMIT_DEBUG_PER_MINUTE", "2")),
    "auth": int(os.getenv("RATE_LIMIT_AUTH_PER_MINUTE", "10")),
    "session": int(os.getenv("RATE_LIMIT_SESSION_PER_MINUTE", "15")),
    "health": int(os.getenv("RATE_LIMIT_HEALTH_PER_MINUTE", "30")),
    "responses": int(os.getenv("RATE_LIMIT_RESPONSES_PER_MINUTE", "10")),
    "general": int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")),
}

# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------
from src.backends.claude.constants import (  # noqa: E402, F401
    CLAUDE_TOOLS,
    DEFAULT_ALLOWED_TOOLS,
    CLAUDE_MODELS,
    THINKING_MODE,
    TOKEN_STREAMING,
)

ALL_MODELS = CLAUDE_MODELS

METADATA_ENV_ALLOWLIST: frozenset[str] = frozenset(
    k.strip() for k in os.getenv("METADATA_ENV_ALLOWLIST", "").split(",") if k.strip()
)

ASK_USER_TIMEOUT_SECONDS = int(os.environ.get("ASK_USER_TIMEOUT_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Docker Per-User Sandbox
# ---------------------------------------------------------------------------
DOCKER_SANDBOX_ENABLED = parse_bool_env("DOCKER_SANDBOX_ENABLED", "false")
DOCKER_SANDBOX_ROLE = os.getenv("DOCKER_SANDBOX_ROLE", "orchestrator")
DOCKER_SANDBOX_IMAGE = os.getenv("DOCKER_SANDBOX_IMAGE", "claude-code-gateway:latest")
DOCKER_SANDBOX_NETWORK = os.getenv("DOCKER_SANDBOX_NETWORK", "claude-sandbox-net")
DOCKER_SANDBOX_CPU_LIMIT = os.getenv("DOCKER_SANDBOX_CPU_LIMIT", "1.0")
DOCKER_SANDBOX_MEMORY_LIMIT = os.getenv("DOCKER_SANDBOX_MEMORY_LIMIT", "2g")
DOCKER_SANDBOX_IDLE_TIMEOUT = int(os.getenv("DOCKER_SANDBOX_IDLE_TIMEOUT", "3600"))
DOCKER_SANDBOX_MAX_CONTAINERS = int(os.getenv("DOCKER_SANDBOX_MAX_CONTAINERS", "50"))
# Default: /app/data/sandboxes (uses the already-mounted ./data:/app/data volume)
# Override with DOCKER_SANDBOX_WORKSPACE_BASE for a custom host path.
DOCKER_SANDBOX_WORKSPACE_BASE = os.getenv("DOCKER_SANDBOX_WORKSPACE_BASE", "/app/data/sandboxes")

# Debug / Verbose mode
DEBUG_MODE = parse_bool_env("DEBUG_MODE", "false")
VERBOSE = parse_bool_env("VERBOSE", "false")
