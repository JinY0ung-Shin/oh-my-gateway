"""
Constants and configuration for Claude Code Gateway.

Single source of truth for shared configuration values.
Backend-specific constants live in ``src/backends/<name>/constants.py``.
All configurable values can be overridden via environment variables.
"""

import os
from dotenv import dotenv_values, load_dotenv

from src.env_utils import parse_bool_env, parse_int_env

load_dotenv()

# Selected Anthropic-related keys: .env wins over pre-existing shell env.
# Why: shell-injected values (e.g. corp defaults) silently override .env,
# making local routing/model overrides ineffective without this opt-in.
_DOTENV_OVERRIDE_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)
_dotenv_file_values = dotenv_values()
for _k in _DOTENV_OVERRIDE_KEYS:
    _v = _dotenv_file_values.get(_k)
    if _v is not None:
        os.environ[_k] = _v

# Default model (recommended for most use cases)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "sonnet")

# Custom system prompt file path (empty = use claude_code preset)
SYSTEM_PROMPT_FILE = os.getenv("SYSTEM_PROMPT_FILE", "")

# System prompt placeholder values (resolved in {{PLACEHOLDER}} tokens)
PROMPT_LANGUAGE = os.getenv("PROMPT_LANGUAGE", "English")

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
# Control which subagent outputs are forwarded to the client during streaming.
# These only affect events with parent_tool_use_id (i.e., from subagents).
#
# SUBAGENT_STREAM_TEXT: Forward subagent text deltas (thinking/response).
#   Default false — subagent text is suppressed, only final summary reaches orchestrator.
# SUBAGENT_STREAM_TOOL_BLOCKS: Forward subagent tool_use/tool_result blocks.
#   Default true — clients can render "View Result" for subagent tool calls.
# SUBAGENT_STREAM_PROGRESS: Forward task_started/task_progress/task_notification events.
#   Default true — clients can show subagent execution progress.
SUBAGENT_STREAM_TEXT = parse_bool_env("SUBAGENT_STREAM_TEXT", "false")
SUBAGENT_STREAM_TOOL_BLOCKS = parse_bool_env("SUBAGENT_STREAM_TOOL_BLOCKS", "true")
SUBAGENT_STREAM_PROGRESS = parse_bool_env("SUBAGENT_STREAM_PROGRESS", "true")

# Rate Limiting defaults (requests per minute)
# These are used by rate_limiter.py as the single source of truth
RATE_LIMITS = {
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
    TOKEN_STREAMING,
)

ALL_MODELS = CLAUDE_MODELS

# Metadata → subprocess env var allowlist (comma-separated).
# Only metadata keys listed here are passed as env vars to the Claude subprocess.
# Example: METADATA_ENV_ALLOWLIST=THREAD_ID,A2A_BASE_URL
METADATA_ENV_ALLOWLIST: frozenset[str] = frozenset(
    k.strip() for k in os.getenv("METADATA_ENV_ALLOWLIST", "").split(",") if k.strip()
)

# AskUserQuestion hook timeout (seconds).
# If the client does not respond within this window the hook denies the tool
# call and the SDK resumes.  Set via ASK_USER_TIMEOUT_SECONDS env var.
ASK_USER_TIMEOUT_SECONDS = int(os.environ.get("ASK_USER_TIMEOUT_SECONDS", "300"))

# Comma-separated list of file extensions that the sensitive-file PreToolUse
# hook should auto-allow without prompting the user.  Opt-in via env: by
# default the hook prompts for every sensitive-file edit, matching the
# strictest possible behaviour.  Set in ``.env`` to relax for safe types,
# e.g.::
#
#     SENSITIVE_FILE_AUTO_ALLOW_EXTS=.md
#
# Markdown files can't carry the kind of executable / credential payload
# that motivates the prompt (``settings.json``, ``.env``, ssh keys, …),
# and the most common case — repeated ``.claude/MEMORY.md`` updates the
# user explicitly asked for — would otherwise surface a card on every
# edit.  Extend with e.g. ``.md,.txt,.log`` for more types.
SENSITIVE_FILE_AUTO_ALLOW_EXTS = tuple(
    (ext.strip().lower() if ext.strip().startswith(".") else "." + ext.strip().lower())
    for ext in os.environ.get("SENSITIVE_FILE_AUTO_ALLOW_EXTS", "").split(",")
    if ext.strip()
)

# Debug / Verbose mode — single source of truth
DEBUG_MODE = parse_bool_env("DEBUG_MODE", "false")
VERBOSE = parse_bool_env("VERBOSE", "false")
