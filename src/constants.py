"""
Constants and configuration for Claude Code OpenAI Wrapper.

Single source of truth for tool names, models, and other configuration values.
All configurable values can be overridden via environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Claude Agent SDK Tool Names
# These are the built-in tools available in the Claude Agent SDK
# See: https://docs.anthropic.com/en/docs/claude-code/sdk
CLAUDE_TOOLS = [
    "Task",  # Launch agents for complex tasks
    "Bash",  # Execute bash commands
    "Glob",  # File pattern matching
    "Grep",  # Search file contents
    "Read",  # Read files
    "Edit",  # Edit files
    "Write",  # Write files
    "NotebookEdit",  # Edit Jupyter notebooks
    "WebFetch",  # Fetch web content
    "TodoWrite",  # Manage todo lists
    "WebSearch",  # Search the web
    "BashOutput",  # Get bash output
    "KillShell",  # Kill bash shells
    "Skill",  # Execute skills
    "SlashCommand",  # Execute slash commands
]

# Default tools to allow when tools are enabled
# Subset of CLAUDE_TOOLS that are safe and commonly used
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "Write",
    "Edit",
]

# Claude Models
# Models supported by Claude Code SDK
# See: https://docs.anthropic.com/en/docs/about-claude/models/overview
# See: https://docs.anthropic.com/en/docs/claude-code/model-config

CLAUDE_MODELS = [
    "opus",
    "sonnet",
    "haiku",
]

# Codex Models
# Codex sub-models (e.g. "codex/o3") are resolved via slash pattern in resolve_model()
CODEX_MODELS = [
    "codex",
]

# Combined model list for /v1/models and validation
ALL_MODELS = CLAUDE_MODELS + CODEX_MODELS

# Default model (recommended for most use cases)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "sonnet")

# Thinking Mode Configuration
# Options: "adaptive" (recommended for Opus 4.6/Sonnet 4.6), "enabled", "disabled"
THINKING_MODE = os.getenv("THINKING_MODE", "adaptive")
THINKING_BUDGET_TOKENS = int(os.getenv("THINKING_BUDGET_TOKENS", "10000"))

# Token-Level Streaming
# When enabled, uses SDK's include_partial_messages to stream individual tokens
# instead of waiting for complete messages
TOKEN_STREAMING = os.getenv("TOKEN_STREAMING", "true").lower() in ("true", "1", "yes")

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

# Disallowed Subagent Types
# Comma-separated list of subagent types to block via Agent(type) syntax
# Example: "statusline-setup,Plan"
_raw_disallowed = os.getenv("DISALLOWED_SUBAGENT_TYPES", "statusline-setup")
DISALLOWED_SUBAGENT_TYPES = [f"Agent({t.strip()})" for t in _raw_disallowed.split(",") if t.strip()]

# Codex Backend Configuration
# Set CODEX_ENABLED=true to activate the Codex backend for model dispatch
CODEX_ENABLED = os.getenv("CODEX_ENABLED", "false").lower() in ("true", "1", "yes")
CODEX_CLI_PATH = os.getenv("CODEX_CLI_PATH", "codex")
CODEX_APPROVAL_MODE = os.getenv("CODEX_APPROVAL_MODE", "full-auto")
CODEX_TIMEOUT_MS = int(os.getenv("CODEX_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
CODEX_CONFIG_ISOLATION = os.getenv("CODEX_CONFIG_ISOLATION", "true").lower() in ("true", "1", "yes")

# MCP Server Configuration
# Path to MCP config JSON file or inline JSON string
# Format: {"mcpServers": {"name": {"type": "stdio", "command": "...", "args": [...]}}}
MCP_CONFIG = os.getenv("MCP_CONFIG", "")

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
