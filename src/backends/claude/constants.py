"""Claude backend constants and configuration.

Single source of truth for Claude-specific tool names, models, and configuration.
All configurable values can be overridden via environment variables.
"""

import logging as _logging
import os

from src.env_utils import parse_bool_env

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
    "Skill",
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

# Thinking Mode Configuration
# Options: "adaptive" (recommended for Opus 4.6/Sonnet 4.6), "enabled", "disabled"
THINKING_MODE = os.getenv("THINKING_MODE", "adaptive")
THINKING_BUDGET_TOKENS = int(os.getenv("THINKING_BUDGET_TOKENS", "10000"))

# Task Budget (tokens)
# When set, the model is made aware of its remaining token budget so it can
# pace tool use and wrap up before the limit.  Unset (None) means no limit.
_task_budget_raw = os.getenv("TASK_BUDGET")
if _task_budget_raw:
    try:
        DEFAULT_TASK_BUDGET: int | None = int(_task_budget_raw)
    except ValueError:
        _sandbox_logger.warning(
            "Invalid TASK_BUDGET=%r (expected integer), treating as unset",
            _task_budget_raw,
        )
        DEFAULT_TASK_BUDGET = None
else:
    DEFAULT_TASK_BUDGET: int | None = None

# Token-Level Streaming
# When enabled, uses SDK's include_partial_messages to stream individual tokens
# instead of waiting for complete messages
TOKEN_STREAMING = parse_bool_env("TOKEN_STREAMING", "true")

# Disallowed Subagent Types
# Comma-separated list of subagent types to block via Agent(type) syntax
# Example: "statusline-setup,Plan"
_raw_disallowed = os.getenv("DISALLOWED_SUBAGENT_TYPES", "statusline-setup")
DISALLOWED_SUBAGENT_TYPES = [f"Agent({t.strip()})" for t in _raw_disallowed.split(",") if t.strip()]

# ---------------------------------------------------------------------------
# Bash Sandbox Configuration
# ---------------------------------------------------------------------------
# OS-level process isolation for Bash tool execution (macOS Seatbelt / Linux bubblewrap).
# Only affects Bash commands; Read/Edit/Write access is controlled by SDK permission rules.
#
# Tri-state: unset = respect project-level settings, true = force enable, false = force disable.
_SANDBOX_VALID_TRUE = {"true", "1", "yes"}
_SANDBOX_VALID_FALSE = {"false", "0", "no"}
_SANDBOX_VALID_ALL = _SANDBOX_VALID_TRUE | _SANDBOX_VALID_FALSE

_sandbox_logger = _logging.getLogger(__name__)

_sandbox_raw = os.getenv("CLAUDE_SANDBOX_ENABLED")
if _sandbox_raw is None:
    CLAUDE_SANDBOX_ENABLED: bool | None = None
elif _sandbox_raw.lower() in _SANDBOX_VALID_ALL:
    CLAUDE_SANDBOX_ENABLED = _sandbox_raw.lower() in _SANDBOX_VALID_TRUE
else:
    _sandbox_logger.warning(
        "Invalid CLAUDE_SANDBOX_ENABLED=%r (expected true/false/1/0/yes/no), treating as unset",
        _sandbox_raw,
    )
    CLAUDE_SANDBOX_ENABLED = None


def _parse_sandbox_bool(name: str, default: str) -> bool:
    """Parse a sandbox boolean env var with strict validation.

    Valid values: true/false/1/0/yes/no/on/off (case-insensitive).
    Invalid values log a warning and fall back to *default*.
    """
    raw = os.getenv(name)
    if raw is None:
        return parse_bool_env(name, default)
    if raw.lower() in _SANDBOX_VALID_ALL:
        return raw.lower() in _SANDBOX_VALID_TRUE
    _sandbox_logger.warning(
        "Invalid %s=%r (expected true/false/1/0/yes/no), using default %r",
        name,
        raw,
        default,
    )
    return default.lower() in _SANDBOX_VALID_TRUE


CLAUDE_SANDBOX_AUTO_ALLOW_BASH: bool = _parse_sandbox_bool("CLAUDE_SANDBOX_AUTO_ALLOW_BASH", "true")

CLAUDE_SANDBOX_EXCLUDED_COMMANDS: list[str] = [
    c.strip() for c in os.getenv("CLAUDE_SANDBOX_EXCLUDED_COMMANDS", "").split(",") if c.strip()
]

CLAUDE_SANDBOX_ALLOW_UNSANDBOXED: bool = _parse_sandbox_bool(
    "CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", "false"
)

CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL: bool = _parse_sandbox_bool(
    "CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", "false"
)

CLAUDE_SANDBOX_WEAKER_NESTED: bool = _parse_sandbox_bool("CLAUDE_SANDBOX_WEAKER_NESTED", "false")
