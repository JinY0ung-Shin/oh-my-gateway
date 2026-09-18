"""Claude backend constants and configuration.

Single source of truth for Claude-specific tool names, models, and configuration.
All configurable values can be overridden via environment variables.
"""

import logging as _logging
import os

from src.env_utils import parse_bool_env

logger = _logging.getLogger(__name__)

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

# Effort support is a property of the CONCRETE model, not of the tier name.
# ``output_config.effort`` is accepted by the Opus and Sonnet generations below;
# Claude Haiku 4.5 has NO effort parameter and errors on one. So the tier name
# cannot answer "is effort applied here" — only the model the tier currently
# resolves to can, and that changes with every generation.
#
# Keyed by concrete model id, with ``False`` spelled out rather than left absent,
# so the reason a tier does or does not carry the guarantee stays readable. A new
# generation is one edit here plus its row below.
EFFORT_CAPABLE_MODELS = {
    "claude-opus-5": True,
    "claude-sonnet-5": True,
    "claude-haiku-4-5": False,
}

# What each bare tier resolves to on a FIRST-PARTY upstream with no
# ``ANTHROPIC_DEFAULT_*_MODEL`` override. The Claude CLI owns the real
# resolution — this is the gateway's record of it, used for nothing but reading
# a concrete model's effort support off ``EFFORT_CAPABLE_MODELS``. An unlisted
# tier, or one resolving to a model missing from that table, fails closed.
FIRST_PARTY_TIER_MODELS = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def tier_applies_effort(tier: str) -> bool:
    """Does the concrete model this bare tier resolves to apply ``effort``?

    Fails closed: an unknown tier, or one whose resolved model is not in
    ``EFFORT_CAPABLE_MODELS``, is treated as not applying effort.
    """
    resolved = FIRST_PARTY_TIER_MODELS.get(tier)
    if resolved is None:
        return False
    return EFFORT_CAPABLE_MODELS.get(resolved, False)


# The effort ladder the CLI sends on the wire (``output_config.effort``). ``none``
# is not a level — it disables extended thinking and rides ``thinking`` — so it is
# absent here on purpose. Order is weakest → strongest; clients rely on it.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Operator certification for a CUSTOM upstream (``ANTHROPIC_BASE_URL`` set).
#
# The gateway cannot know whether a LiteLLM/sanitizer/vLLM chain applies the
# effort it forwards, so the strong ``reasoning_effort`` capability fails closed
# there. This env is the operator saying "it does" — per public model id, and
# optionally naming the LEVELS that upstream accepts, because a served model's
# chat template decides that subset (a Qwen3.x template takes ``low|medium|xhigh``
# and answers ``high`` with a 400; see Kyutinium/litellm_serving#26)::
#
#     CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS=qwen3.6-27b=low|medium|xhigh,glm-5-fp8,*
#
# Grammar, comma-separated: ``<id>`` (every level), ``<id>=<lvl>|<lvl>…`` (that
# subset), ``*`` / ``*=<lvls>`` (every model of this backend). An exact id wins
# over ``*``. Validity is all-or-nothing: one unknown level or an empty subset
# invalidates the WHOLE setting — a typo must never quietly become a narrower or
# a wider promise (startup refuses it; a running process treats it as unset).
CUSTOM_UPSTREAM_EFFORT_ENV = "CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS"
CUSTOM_UPSTREAM_EFFORT_WILDCARD = "*"


def parse_custom_upstream_effort_certification(
    raw: str | None,
) -> dict[str, tuple[str, ...]]:
    """Parse ``CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS`` into ``{id: levels}``.

    ``None``/blank → ``{}`` (nothing certified). Raises ``ValueError`` on any
    malformed entry so callers can fail closed or refuse to start.
    """
    if raw is None or not raw.strip():
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        model, sep, levels_raw = item.partition("=")
        model = model.strip()
        if not model:
            raise ValueError(f"{CUSTOM_UPSTREAM_EFFORT_ENV}: entry {item!r} has no model id")
        if not sep:
            levels: tuple[str, ...] = EFFORT_LEVELS
        else:
            parts = [p.strip().lower() for p in levels_raw.split("|") if p.strip()]
            if not parts:
                raise ValueError(
                    f"{CUSTOM_UPSTREAM_EFFORT_ENV}: {model!r} declares no levels "
                    f"(omit '=' to certify every level)"
                )
            unknown = sorted({p for p in parts if p not in EFFORT_LEVELS})
            if unknown:
                raise ValueError(
                    f"{CUSTOM_UPSTREAM_EFFORT_ENV}: {model!r} names unknown level(s) "
                    f"{', '.join(unknown)}; known: {', '.join(EFFORT_LEVELS)}"
                )
            levels = tuple(level for level in EFFORT_LEVELS if level in parts)
        if model in out and out[model] != levels:
            raise ValueError(
                f"{CUSTOM_UPSTREAM_EFFORT_ENV}: {model!r} is declared twice with different levels"
            )
        out[model] = levels
    return out


def custom_upstream_effort_certification() -> dict[str, tuple[str, ...]]:
    """The live certification, or ``{}`` when unset **or invalid**.

    Invalid never certifies: a bad declaration is treated as no declaration, so
    every custom-upstream id stays ``reasoning_effort: false``. The startup
    config check is what turns a bad value into a hard failure for a fresh deploy.
    """
    try:
        return parse_custom_upstream_effort_certification(
            os.getenv(CUSTOM_UPSTREAM_EFFORT_ENV)
        )
    except ValueError as exc:
        logger.warning("%s; certifying nothing", exc)
        return {}


def certified_effort_levels(model: str) -> tuple[str, ...] | None:
    """Levels the operator certified for *model* on a custom upstream, else ``None``.

    An exact id wins over the ``*`` wildcard. Only meaningful when
    ``ANTHROPIC_BASE_URL`` is set — the caller checks that.
    """
    cert = custom_upstream_effort_certification()
    if model in cert:
        return cert[model]
    return cert.get(CUSTOM_UPSTREAM_EFFORT_WILDCARD)


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

# Subagent Tool Names
# The CLI renamed the subagent tool: recent builds ship it as ``Agent`` and keep
# ``Task`` only as a legacy alias (the binary carries the rename map
# ``{Task:"Agent", KillShell:"TaskStop", AgentOutputTool:"TaskOutput", …}``).
# Permission *rules* are translated through that map, but PreToolUse hook
# matchers fire on the tool's real name — so a hook registered for "Task" alone
# silently never runs on a build that calls it "Agent", taking the gateway's
# subagent allowlist and foreground forcing down with it. Govern both names.
SUBAGENT_TOOL_NAMES = ("Task", "Agent")

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

# Deferred-Delivery Tools
# CLI harness tools whose payoff fires only AFTER the current turn ends:
# ScheduleWakeup re-invokes the agent later, CronCreate schedules future runs.
# In the standalone CLI that works — the terminal is still attached when the
# wakeup fires. On /v1/responses the HTTP stream closes at turn end, so the
# follow-up runs invisibly inside the CLI subprocess and its output is never
# delivered ("I'll report back when it's done" — and nothing ever arrives).
# Blocked by default so the model keeps the turn open and polls async jobs to
# completion instead. Operators embedding the gateway behind a surface that
# CAN deliver post-turn output may override with BLOCKED_DEFERRED_TOOLS
# (comma-separated; empty string disables the block).
# Subagent delivery mode
# The CLI's Task tool runs subagents in the BACKGROUND by default and tells the
# model it will be "notified when one completes". In a headless HTTP turn that
# notification lands after the response stream has closed, so the model says
# "I'll report back" and the turn ends with no result ever arriving — the same
# undeliverable-payoff shape as BLOCKED_DEFERRED_TOOLS. Forcing
# ``run_in_background: false`` keeps the subagent inside the turn that asked for
# it. Set to false only for a client that can poll a session across turns.
FORCE_FOREGROUND_SUBAGENTS = parse_bool_env("FORCE_FOREGROUND_SUBAGENTS", "true")

_raw_blocked_deferred = os.getenv("BLOCKED_DEFERRED_TOOLS", "ScheduleWakeup,CronCreate")
_blocked_deferred = [t.strip() for t in _raw_blocked_deferred.split(",") if t.strip()]

# Companions of a blocked scheduler. Blocking only the *create* half leaves the
# rest of the family in the catalog, and the model reads that as "the feature is
# here, I just have the wrong name": measured against ChatDRAGON, a ``/loop``
# turn spent its budget reasoning "I don't see CronCreate available even though
# CronDelete and CronList are listed. Let me try to invoke it directly…" before
# giving up. Nothing can be scheduled, so nothing can be listed or deleted
# either; drop the whole family together so the surface states one thing.
# Operators who clear BLOCKED_DEFERRED_TOOLS get all of them back.
_DEFERRED_COMPANIONS: dict[str, tuple[str, ...]] = {
    "CronCreate": ("CronList", "CronDelete"),
}


def _with_companions(names: list[str]) -> list[str]:
    out = list(names)
    for name in names:
        for companion in _DEFERRED_COMPANIONS.get(name, ()):
            if companion not in out:
                out.append(companion)
    return out


BLOCKED_DEFERRED_TOOLS = _with_companions(_blocked_deferred)

# Which concrete tool each advertised deferred capability requires. A client
# surface asks "can this gateway do X", so the answer has to come from the tool
# X actually needs — not from whether the blocked set happens to be empty.
# `BLOCKED_DEFERRED_TOOLS=ScheduleWakeup` leaves cron fully working, so a flag
# derived from set-emptiness would tell a client to disable a scheduler that
# works (review on #202). Add a capability here with the tool it needs, never a
# broader check.
_DEFERRED_CAPABILITY_TOOLS: dict[str, tuple[str, ...]] = {
    # Claude Code `/loop <interval>` schedules recurring work with CronCreate.
    "cron_scheduling_available": ("CronCreate",),
    # `/loop` without an interval self-paces with ScheduleWakeup.
    "wakeup_scheduling_available": ("ScheduleWakeup",),
}


def deferred_capabilities() -> dict[str, bool]:
    """Advertised deferred capabilities, each from the tool it requires.

    `deferred_delivery_available` is the rollup a surface uses to decide whether
    *any* payoff can land after the HTTP turn closes, so it is true when at
    least one scheduling mechanism survives — not when nothing is blocked.
    """
    blocked = set(BLOCKED_DEFERRED_TOOLS)
    caps = {
        name: not (set(required) & blocked)
        for name, required in _DEFERRED_CAPABILITY_TOOLS.items()
    }
    caps["deferred_delivery_available"] = any(caps.values())
    return caps

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
