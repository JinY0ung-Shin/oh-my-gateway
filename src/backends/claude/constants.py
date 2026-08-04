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
    "TaskCreate",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "TaskUpdate",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "TaskGet",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "TaskList",  # Task tracking (0.2.82+, opt-in via CLAUDE_CODE_ENABLE_TASKS=1)
    "Bash",  # Execute bash commands
    "Glob",  # File pattern matching
    "Grep",  # Search file contents
    "Read",  # Read files
    "Edit",  # Edit files
    "Write",  # Write files
    "NotebookEdit",  # Edit Jupyter notebooks
    "WebFetch",  # Fetch web content
    "TodoWrite",  # Default task-tracking tool when CLAUDE_CODE_ENABLE_TASKS is unset
    "WebSearch",  # Search the web
    "BashOutput",  # Get bash output
    "KillShell",  # Kill bash shells
    "Skill",  # Execute skills (deprecated 0.1.77 — translated to skills= option)
    "SlashCommand",  # Execute slash commands
]

# Default tools to allow when tools are enabled
# Subset of CLAUDE_TOOLS that are safe and commonly used.
# Includes both TodoWrite (default) and Task* (active only when
# CLAUDE_CODE_ENABLE_TASKS=1 is set on the CLI subprocess env).
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "Write",
    "Edit",
    "Skill",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TodoWrite",
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

# Optional alias exposure via ANTHROPIC_DEFAULT_*_MODEL.
# The Claude CLI maps the bare opus/sonnet/haiku aliases to a concrete model id
# (or a custom upstream alias) through these env vars — see the pass-through in
# ``src/constants.py`` and ``extract_model_id`` in ``src/usage_logger.py``. When
# an override is set we ALSO advertise that name as a public model id so callers
# can request the model by its configured name; resolution maps it back to the
# bare alias so the CLI stays the single source of truth for alias resolution.
_ALIAS_MODEL_ENV = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}


def configured_model_aliases() -> dict[str, str]:
    """Map each configured ``ANTHROPIC_DEFAULT_*_MODEL`` value to its bare alias.

    Reads the env on each call. Returns an empty dict when none are set, so the
    default surface (bare ``opus``/``sonnet``/``haiku``) is unchanged. A value
    equal to a bare alias is skipped to avoid a self-referential entry.
    """
    mapping: dict[str, str] = {}
    for alias, env_name in _ALIAS_MODEL_ENV.items():
        value = (os.getenv(env_name) or "").strip()
        if value and value not in CLAUDE_MODELS:
            mapping[value] = alias
    return mapping


def configured_public_models() -> list[str]:
    """Public Claude model ids: bare aliases plus any configured override names."""
    return list(CLAUDE_MODELS) + list(configured_model_aliases().keys())

# Thinking Mode Configuration
# Options: "adaptive" (recommended for Opus 4.6/Sonnet 4.6), "enabled", "disabled"
THINKING_MODE = os.getenv("THINKING_MODE", "adaptive")
THINKING_BUDGET_TOKENS = int(os.getenv("THINKING_BUDGET_TOKENS", "10000"))

# Logger used by config parsing below (must be defined before first use)
_sandbox_logger = _logging.getLogger(__name__)

# Task Budget (tokens)
# When set, the model is made aware of its remaining token budget so it can
# pace tool use and wrap up before the limit.  Unset (None) means no limit.
_task_budget_raw = os.getenv("TASK_BUDGET")
DEFAULT_TASK_BUDGET: int | None
if _task_budget_raw:
    try:
        DEFAULT_TASK_BUDGET = int(_task_budget_raw)
    except ValueError:
        _sandbox_logger.warning(
            "Invalid TASK_BUDGET=%r (expected integer), treating as unset",
            _task_budget_raw,
        )
        DEFAULT_TASK_BUDGET = None
else:
    DEFAULT_TASK_BUDGET = None

# Token-Level Streaming
# When enabled, uses SDK's include_partial_messages to stream individual tokens
# instead of waiting for complete messages
TOKEN_STREAMING = parse_bool_env("TOKEN_STREAMING", "true")

# Claude CLI binary override
# The SDK spawns its own bundled CLI by default (claude-agent-sdk==0.2.128
# bundles CLI 2.1.220). Set CLAUDE_CLI_PATH to an executable to spawn that
# binary instead — e.g. a newer CLI whose MCP client speaks a protocol
# revision the bundled one predates (2.1.220 negotiates up to 2025-11-25;
# the 2026-07-28 stateless revision needs >= 2.1.221) — without bumping the
# deliberately pinned SDK. Invalid paths are ignored with a warning at
# session creation so a typo cannot take sessions down.
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH") or None

# Disallowed Subagent Types
# Comma-separated list of subagent types to block via Agent(type) syntax
# Example: "statusline-setup,Plan"
_raw_disallowed = os.getenv("DISALLOWED_SUBAGENT_TYPES", "statusline-setup")
DISALLOWED_SUBAGENT_TYPES = [f"Agent({t.strip()})" for t in _raw_disallowed.split(",") if t.strip()]

# Disallowed Tools
# Comma-separated list of Claude SDK tool names to always block. These are
# merged into the SDK ``disallowed_tools`` option so they remain blocked even
# under ``bypassPermissions``, where ``allowed_tools`` is only an auto-approve
# hint and does not strictly restrict tool use. Unset by default (no extra
# blocking); set explicitly to enforce a deny-list, e.g. ``WebFetch,WebSearch``.
_raw_disallowed_tools = os.getenv("DISALLOWED_TOOLS", "")
DISALLOWED_TOOLS = [t.strip() for t in _raw_disallowed_tools.split(",") if t.strip()]

# Hidden Skills
# Comma-separated skill names removed from the model's skill catalog. A
# ``Skill(<name>)`` deny in DISALLOWED_TOOLS blocks execution but leaves the
# skill listed in the system prompt; hiding requires the SDK ``skills``
# allowlist (a context filter: unlisted skills are dropped from the listing
# and rejected by the Skill tool). Keep the deny entries in sync for defense
# in depth, and BLOCKED_SLASH_COMMANDS for the client-typed ``/name`` path.
_raw_hidden_skills = os.getenv("HIDDEN_SKILLS", "")
HIDDEN_SKILLS = frozenset(
    name
    for name in (s.strip().lstrip("/") for s in _raw_hidden_skills.split(","))
    if name
)

# ---------------------------------------------------------------------------
# Bash Sandbox Configuration
# ---------------------------------------------------------------------------
# OS-level process isolation for Bash tool execution (macOS Seatbelt / Linux bubblewrap).
# Only affects Bash commands; Read/Edit/Write access is controlled by SDK permission rules.
#
# Tri-state: unset = respect project-level settings, true = force enable, false = force disable.
_SANDBOX_VALID_TRUE = {"true", "1", "yes", "on"}
_SANDBOX_VALID_FALSE = {"false", "0", "no", "off"}
_SANDBOX_VALID_ALL = _SANDBOX_VALID_TRUE | _SANDBOX_VALID_FALSE

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

# ---------------------------------------------------------------------------
# MCP Connection Behavior (claude-agent-sdk 0.2.82+)
# ---------------------------------------------------------------------------
# By default, MCP servers connect in the background; sessions start
# immediately and slow servers report ``status: "pending"`` in init.
#
# To restore pre-0.2.82 behavior (wait up to 5s before first query), set:
#     MCP_CONNECTION_NONBLOCKING=0
#
# Alternative: mark a specific server with ``alwaysLoad: true`` in the
# mcp_servers config so the SDK waits for that server in turn 1.
#
# We accept the new default; downstream consumers must handle ``pending``
# server state in init messages. See docs/api/breaking-changes.md.
