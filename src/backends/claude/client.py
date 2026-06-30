"""Claude Agent SDK backend client.

Wraps the Claude Agent SDK ``query()`` function into a ``BackendClient``
implementation registered as the ``claude`` backend.
"""

import asyncio
import os
import tempfile
import atexit
import shutil
import contextlib
from typing import AsyncGenerator, Dict, Any, Literal, Optional, List, cast
from pathlib import Path
import logging

from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
from src.constants import DEFAULT_MAX_TURNS
from claude_agent_sdk.types import (
    StreamEvent,
    AssistantMessage,
    ResultMessage,
    UserMessage,
    SystemMessage,
    RateLimitEvent,
    HookMatcher,
)
from claude_agent_sdk.types import (
    SandboxSettings,
    SandboxNetworkConfig,
)
from src.backends.claude.constants import (
    CLAUDE_MODELS,
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_TASK_BUDGET,
    THINKING_BUDGET_TOKENS,
    DISALLOWED_SUBAGENT_TYPES,
    DISALLOWED_TOOLS,
    CLAUDE_SANDBOX_ENABLED,
    CLAUDE_SANDBOX_AUTO_ALLOW_BASH,
    CLAUDE_SANDBOX_EXCLUDED_COMMANDS,
    CLAUDE_SANDBOX_ALLOW_UNSANDBOXED,
    CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL,
    CLAUDE_SANDBOX_WEAKER_NESTED,
)
from src.backends.common import TokenEstimateMixin, error_chunk
from src.constants import ASK_USER_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_MS
from src.message_adapter import MessageAdapter
from src.image_handler import ImageHandler
from src.mcp_config import get_mcp_tool_patterns
from src.response_models import PermissionMode
from src.runtime_config import get_default_max_turns
from src.backends.claude.workspace_sandbox import (
    make_workspace_sandbox_hook,
    sandbox_enabled,
)

logger = logging.getLogger(__name__)

_DEFAULT_SETTING_SOURCES = ["project", "local"]
_VALID_SETTING_SOURCES = {"user", "project", "local"}

def _skilldbg_short(value: object, limit: int = 800) -> str:
    """Render *value* as a single-line, length-capped string for diagnostics."""
    try:
        s = value if isinstance(value, str) else repr(value)
    except Exception:  # pragma: no cover - defensive
        s = "<unrepr>"
    s = s.replace("\n", "\\n")
    return s if len(s) <= limit else f"{s[:limit]}...(+{len(s) - limit} chars)"


# Catch-all granular permission rule that auto-approves every skill invocation.
# The CLI matches Skill rules by stripping a trailing ``:*`` and prefix-matching
# the remainder against the skill name; with an empty prefix this matches all
# skills (plugin-qualified ``plugin:skill`` and bare ``skill`` alike). A bare
# ``Skill`` rule does NOT work — the matcher discards content-less rules. See
# :meth:`ClaudeCodeCLI._set_allowed_tools`.
SKILL_ALLOW_ALL_RULE = "Skill(:*)"


def _get_setting_sources() -> List[Literal["user", "project", "local"]]:
    """Return Claude config sources for SDK calls.

    By default the gateway keeps user-level Claude config out of non-Docker
    runs. Docker Compose sets CLAUDE_SETTING_SOURCES=user,project,local so
    user-scope plugins installed at container startup are visible to Claude.
    """
    SettingSource = Literal["user", "project", "local"]
    raw = os.getenv("CLAUDE_SETTING_SOURCES")
    if raw is None or not raw.strip():
        return cast(List[SettingSource], list(_DEFAULT_SETTING_SOURCES))

    sources = [part.strip() for part in raw.split(",") if part.strip()]
    invalid = [source for source in sources if source not in _VALID_SETTING_SOURCES]
    if invalid or not sources:
        logger.warning(
            "Invalid CLAUDE_SETTING_SOURCES=%r; using default %s",
            raw,
            ",".join(_DEFAULT_SETTING_SOURCES),
        )
        return cast(List[SettingSource], list(_DEFAULT_SETTING_SOURCES))

    deduped: List[SettingSource] = []
    seen = set()
    for source in sources:
        if source not in seen:
            deduped.append(cast(SettingSource, source))
            seen.add(source)
    return deduped


class UnsupportedContinuationPolicy(ValueError):
    """Raised when a continuation request asks for a policy change the SDK can't apply mid-session.

    The Claude SDK has no runtime API for swapping ``allowed_tools`` /
    ``disallowed_tools``; route handlers should surface this as a 400 so the
    caller can either drop the tool change or start a fresh session.
    """


class ClaudeCodeCLI(TokenEstimateMixin):
    """Gateway for Claude Agent SDK queries.

    Implements the ``BackendClient`` protocol defined in
    ``src/backends/base.py`` so it can be registered as the ``claude``
    backend.

    First-turn and follow-up Responses API requests use a persistent
    ``ClaudeSDKClient`` stored on the gateway session.  Reconnect paths use
    the gateway session id to resume the SDK transcript from disk when the
    in-memory client is missing.
    """

    def __init__(self, timeout: Optional[int] = None, cwd: Optional[str] = None):
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_MS
        self.timeout = timeout / 1000  # Convert ms to seconds
        self.temp_dir = None

        # If an explicit cwd is provided, use it. Otherwise create an isolated
        # temp directory. Live requests override cwd per request with the
        # resolved per-user workspace, so this default is only a fallback.
        if cwd:
            self.cwd = Path(cwd)
            if not self.cwd.exists():
                logger.error(f"ERROR: Specified working directory does not exist: {self.cwd}")
                logger.error("Please create the directory first to use it as a working directory")
                raise ValueError(f"Working directory does not exist: {self.cwd}")
            else:
                logger.info(f"Using configured working directory: {self.cwd}")
        else:
            self.temp_dir = tempfile.mkdtemp(prefix="claude_code_workspace_")
            self.cwd = Path(self.temp_dir)
            logger.info(f"Using temporary isolated workspace: {self.cwd}")
            atexit.register(self._cleanup_temp_dir)

        self._image_handler = ImageHandler(self.cwd)

        from src.auth import auth_manager, validate_claude_code_auth

        is_valid, auth_info = validate_claude_code_auth()
        if not is_valid:
            logger.warning(f"Claude Code authentication issues detected: {auth_info['errors']}")
        else:
            logger.info(f"Claude Code authentication method: {auth_info.get('method', 'unknown')}")

        # Auth env vars for SDK – constant per instance, set before each query.
        self.claude_env_vars = auth_manager.get_claude_code_env_vars()

    @property
    def image_handler(self) -> "ImageHandler":
        return self._image_handler

    def cleanup_images(self, max_age_seconds: int = 3600) -> int:
        """Clean up old image files from the workspace."""
        return self._image_handler.cleanup(max_age_seconds)

    # ------------------------------------------------------------------
    # BackendClient protocol — new properties and methods
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "claude"

    def supported_models(self) -> List[str]:
        return list(CLAUDE_MODELS)

    def get_auth_provider(self):
        """Return a ClaudeAuthProvider instance."""
        from src.backends.claude.auth import ClaudeAuthProvider

        return ClaudeAuthProvider()

    # ------------------------------------------------------------------
    # SDK option helpers
    # ------------------------------------------------------------------

    def _configure_thinking(self, options: ClaudeAgentOptions) -> None:
        """Apply thinking-mode configuration to *options*."""
        from src.runtime_config import get_thinking_mode

        mode = get_thinking_mode()
        if mode == "adaptive":
            options.thinking = {"type": "adaptive"}
        elif mode == "enabled":
            options.thinking = {"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS}
        elif mode != "disabled":
            logger.warning(f"Unrecognized THINKING_MODE={mode!r}, thinking not configured")

    def _configure_tools(
        self,
        options: ClaudeAgentOptions,
        allowed_tools: Optional[List[str]],
        disallowed_tools: Optional[List[str]],
    ) -> None:
        """Apply tool allow/disallow lists to *options*.

        Translates the deprecated ``"Skill"`` entry in ``allowed_tools`` into
        the modern ``skills="all"`` option (claude-agent-sdk 0.1.62+).
        """
        if allowed_tools:
            self._set_allowed_tools(options, allowed_tools)
        base_disallowed = list(DISALLOWED_SUBAGENT_TYPES) + list(DISALLOWED_TOOLS)
        if disallowed_tools:
            base_disallowed.extend(disallowed_tools)
        if base_disallowed:
            options.disallowed_tools = list(dict.fromkeys(base_disallowed))

    def _set_allowed_tools(self, options: ClaudeAgentOptions, tools: List[str]) -> None:
        """Set allowed_tools while translating deprecated Skill access.

        ``skills="all"`` enables and lists every discovered skill, and makes the
        SDK add a bare ``Skill`` rule to ``--allowedTools``. That bare rule is
        enough to expose the tool, but **not** to auto-approve a skill *call*:
        the CLI's permission matcher ignores Skill rules that carry no content
        (``ruleContent === undefined``) and only honors granular
        ``Skill(<name>)`` / ``Skill(<name>:*)`` rules. With no granular rule,
        every invocation falls through to an interactive "ask", which the
        headless gateway can only resolve as a denial — surfacing to the client
        as ``Execute skill: <name>``. ``Skill(:*)`` is the catch-all granular
        rule: the CLI strips the trailing ``:*`` and prefix-matches the skill
        name against the empty string, so it approves every skill while still
        leaving the skill's own downstream tool calls (Bash/Read/Write) subject
        to ``allowed_tools``, ``disallowed_tools`` and the workspace sandbox.
        """
        filtered = [t for t in tools if t not in DISALLOWED_TOOLS]
        if "Skill" in filtered:
            filtered = [t for t in filtered if t != "Skill"]
            options.skills = "all"
            if SKILL_ALLOW_ALL_RULE not in filtered:
                filtered.append(SKILL_ALLOW_ALL_RULE)
        options.allowed_tools = filtered

    def _configure_sandbox(self, options: ClaudeAgentOptions) -> None:
        """Apply bash sandbox configuration to *options*.

        Tri-state logic based on ``CLAUDE_SANDBOX_ENABLED``:

        * ``None`` (env unset) — do **not** set ``options.sandbox`` at all,
          allowing project-level settings (``setting_sources=["project"]``)
          to take effect. As a defense-in-depth backstop, if the per-workspace
          sandbox is enabled (``WORKSPACE_SANDBOX_ENABLED``) the OS-level bash
          sandbox is force-enabled here so runtime shell expansions the
          PreToolUse hook cannot resolve statically are still confined.
        * ``True`` — force-enable sandbox with env-configured parameters.
        * ``False`` — force-disable sandbox explicitly (honored even when the
          workspace sandbox is on).
        """
        effective_enabled = CLAUDE_SANDBOX_ENABLED
        if effective_enabled is None and sandbox_enabled():
            effective_enabled = True

        if effective_enabled is None:
            return  # Respect project-level settings

        if not effective_enabled:
            options.sandbox = SandboxSettings(enabled=False)
            return

        network_config = SandboxNetworkConfig(
            allowLocalBinding=CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL,
        )

        options.sandbox = SandboxSettings(
            enabled=True,
            autoAllowBashIfSandboxed=CLAUDE_SANDBOX_AUTO_ALLOW_BASH,
            excludedCommands=list(CLAUDE_SANDBOX_EXCLUDED_COMMANDS),
            allowUnsandboxedCommands=CLAUDE_SANDBOX_ALLOW_UNSANDBOXED,
            network=network_config,
            enableWeakerNestedSandbox=CLAUDE_SANDBOX_WEAKER_NESTED,
        )

    _UNSET = object()  # sentinel for _custom_base default

    def _resolve_custom_base_prompt(
        self,
        custom_base_arg: object,
        effective_cwd: Path,
    ) -> Optional[str]:
        if custom_base_arg is self._UNSET:
            from src.system_prompt import get_system_prompt, resolve_request_placeholders

            custom_base = get_system_prompt()
            if custom_base and effective_cwd:
                custom_base = resolve_request_placeholders(custom_base, str(effective_cwd))
            return custom_base
        if custom_base_arg is None or isinstance(custom_base_arg, str):
            return custom_base_arg
        raise TypeError("_custom_base must be a string, None, or omitted")

    def _configure_system_prompt(
        self,
        options: ClaudeAgentOptions,
        custom_base: Optional[str],
        system_prompt: Optional[str],
    ) -> None:
        if custom_base:
            options.system_prompt = custom_base + ("\n\n" + system_prompt if system_prompt else "")
        elif system_prompt:
            options.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt,
            }
        else:
            options.system_prompt = {"type": "preset", "preset": "claude_code"}

    def _configure_mcp_servers(
        self,
        options: ClaudeAgentOptions,
        mcp_servers: Optional[Dict[str, Any]],
        allowed_tools: Optional[List[str]],
    ) -> None:
        if not mcp_servers:
            return

        if allowed_tools is not None:
            allowed_set = set(allowed_tools)
            filtered = {}
            for name, config in mcp_servers.items():
                safe_name = "_".join(name.split("-"))
                pattern = f"mcp__{safe_name}__*"
                if pattern in allowed_set:
                    filtered[name] = config
            if not filtered:
                logger.debug("No MCP servers match allowed_tools, skipping MCP")
                return

            options.mcp_servers = filtered
            if options.allowed_tools is not None:
                for pattern in get_mcp_tool_patterns(filtered):
                    if pattern not in options.allowed_tools:
                        options.allowed_tools.append(pattern)
            logger.debug(f"MCP servers filtered to: {list(filtered.keys())}")
            return

        options.mcp_servers = mcp_servers
        if not options.allowed_tools:
            self._set_allowed_tools(options, list(DEFAULT_ALLOWED_TOOLS))
        # The caller passed no allowed_tools, so the gateway is choosing the
        # default surface here. Allow *all* MCP tools rather than only the
        # MCP_CONFIG servers' patterns: the SDK also loads plugin-bundled MCP
        # servers via setting_sources, and pinning a narrow allowlist would
        # silently lock those out the moment MCP_CONFIG is set. The CLI treats
        # ``mcp__*`` as "every MCP tool" (covering MCP_CONFIG and plugin
        # servers alike), so configuring MCP_CONFIG no longer narrows the MCP
        # surface. Callers who pass an explicit allowed_tools take the branch
        # above and keep full control.
        if "mcp__*" not in options.allowed_tools:
            options.allowed_tools.append("mcp__*")
        logger.debug("MCP tools enabled (mcp__*); servers=%s", list(mcp_servers))

    def _configure_session_identity(
        self,
        options: ClaudeAgentOptions,
        session_id: Optional[str],
        resume: Optional[str],
    ) -> None:
        if resume:
            options.resume = resume
        elif session_id:
            options.session_id = session_id

    def _configure_task_budget(
        self,
        options: ClaudeAgentOptions,
        task_budget: Optional[int],
    ) -> None:
        effective_budget = task_budget if task_budget is not None else DEFAULT_TASK_BUDGET
        if effective_budget is not None:
            options.task_budget = {"total": effective_budget}

    def _configure_metadata_env(
        self,
        options: ClaudeAgentOptions,
        extra_env: Optional[Dict[str, str]],
    ) -> None:
        if not extra_env:
            return
        from src.constants import METADATA_ENV_ALLOWLIST

        env_map = {k: v for k, v in extra_env.items() if k in METADATA_ENV_ALLOWLIST}
        if env_map:
            options.env.update(env_map)

    def _configure_sdk_env(self, options: ClaudeAgentOptions) -> None:
        sdk_env = dict(self.claude_env_vars or {})

        from src.runtime_config import runtime_config
        from src.sanitizer.config import (
            get_gateway_base_url,
            has_upstream_url,
            is_enabled as sanitizer_enabled,
        )

        if sanitizer_enabled():
            gateway_base_url = get_gateway_base_url()
            sdk_env["ANTHROPIC_BASE_URL"] = gateway_base_url
            logger.info(
                "Claude SDK sanitizer routing enabled: sdk_anthropic_base_url=%s",
                gateway_base_url,
            )
        elif runtime_config.get("sanitizer_enabled") is True and not has_upstream_url():
            logger.warning(
                "Claude SDK sanitizer requested but inactive: ANTHROPIC_BASE_URL is not set"
            )

        if sdk_env:
            options.env.update(sdk_env)

    def _build_sdk_options(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        _custom_base: object = _UNSET,
        extra_env: Optional[Dict[str, str]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[Path] = None,
        user: Optional[str] = None,
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with common parameters."""
        effective_cwd = cwd or self.cwd
        options = ClaudeAgentOptions(
            max_turns=max_turns,
            cwd=effective_cwd,
            setting_sources=_get_setting_sources(),
        )

        self._configure_thinking(options)
        self._configure_sandbox(options)
        self._configure_tools(options, allowed_tools, disallowed_tools)

        if model:
            options.model = model

        # Inject user identity into system prompt so Claude Code knows who
        # is driving the conversation.  Only added on the first turn;
        # resume turns skip system_prompt entirely so the context persists.
        if user:
            user_context = f"Current user: {user}"
            system_prompt = f"{system_prompt}\n\n{user_context}" if system_prompt else user_context

        custom_base = self._resolve_custom_base_prompt(_custom_base, effective_cwd)
        self._configure_system_prompt(options, custom_base, system_prompt)
        if permission_mode:
            # The SDK narrows to a 6-value Literal; the value is validated
            # upstream (request schema / runtime), so cast at this boundary.
            options.permission_mode = cast(Any, permission_mode)
        if output_format:
            options.output_format = output_format
        self._configure_mcp_servers(options, mcp_servers, allowed_tools)
        from src.runtime_config import get_token_streaming

        if get_token_streaming():
            options.include_partial_messages = True

        # Surface hook lifecycle events (PreToolUse/PostToolUse/Stop/…) into the
        # message stream so the gateway can forward them as `response.hook_event`
        # liveness signals. Additive: does not change hook *callback* behaviour
        # (e.g. the AskUserQuestion PreToolUse hook still parks as before).
        from src.constants import STREAM_HOOK_EVENTS

        if STREAM_HOOK_EVENTS:
            options.include_hook_events = True

        self._configure_session_identity(options, session_id, resume)
        self._configure_task_budget(options, task_budget)
        self._configure_metadata_env(options, extra_env)
        self._configure_sdk_env(options)

        return options

    # ------------------------------------------------------------------
    # SDK message conversion (SDK types -> plain dicts)
    # ------------------------------------------------------------------

    # Order matters: subclasses before base classes for isinstance checks
    _TYPE_CHECKS = [
        (StreamEvent, "stream_event"),
        (AssistantMessage, "assistant"),
        (ResultMessage, "result"),
        (RateLimitEvent, "rate_limit"),
        (UserMessage, "user"),
        (SystemMessage, "system"),  # Must be last: TaskStarted/Progress/Notification are subclasses
    ]

    def _convert_message(self, message) -> Dict[str, Any]:
        """Convert SDK message object to dict if needed."""
        if isinstance(message, dict):
            return message
        if hasattr(message, "__dict__"):
            result = {
                k: v for k, v in vars(message).items() if not k.startswith("_") and not callable(v)
            }
            if "type" not in result:
                for cls, type_name in self._TYPE_CHECKS:
                    if isinstance(message, cls):
                        result["type"] = type_name
                        break
            # SDK ResultMessage uses ``result``/``errors`` for error details,
            # but downstream consumers expect ``error_message``.
            if result.get("is_error") and "error_message" not in result:
                error_msg = result.get("result") or ""
                if not error_msg and result.get("errors"):
                    error_msg = "; ".join(result["errors"])
                if error_msg:
                    result["error_message"] = error_msg
            self._log_skill_observability(result)
            return result
        return message

    def _log_skill_observability(self, result: Dict[str, Any]) -> None:
        """SKILL_DEBUG: log tool calls and (especially) error tool results.

        Temporary instrumentation for diagnosing plugin-skill failures. Only
        full ``assistant``/``user`` messages are inspected (partial stream
        events are skipped to avoid duplicate noise). Logged at WARNING with the
        ``SKILL_DEBUG`` prefix; never allowed to raise into the stream.
        """
        try:
            if result.get("type") not in ("assistant", "user"):
                return
            content = result.get("content")
            if not isinstance(content, list):
                return
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    name = block.get("name")
                    binput = block.get("input")
                    is_err = block.get("is_error")
                    bcontent = block.get("content")
                    tuid = block.get("tool_use_id")
                else:
                    btype = getattr(block, "type", None)
                    name = getattr(block, "name", None)
                    binput = getattr(block, "input", None)
                    is_err = getattr(block, "is_error", None)
                    bcontent = getattr(block, "content", None)
                    tuid = getattr(block, "tool_use_id", None)

                is_tool_use = btype == "tool_use" or (name is not None and binput is not None)
                is_tool_result = (
                    btype == "tool_result" or (tuid is not None) or is_err is not None
                ) and not is_tool_use

                if is_tool_use:
                    logger.warning(
                        "SKILL_DEBUG tool_use: name=%s input=%s",
                        name,
                        _skilldbg_short(binput),
                    )
                elif is_tool_result:
                    logger.warning(
                        "SKILL_DEBUG tool_result: is_error=%s tool_use_id=%s content=%s",
                        is_err,
                        tuid,
                        _skilldbg_short(bcontent),
                    )
        except Exception:  # pragma: no cover - diagnostics must never break streaming
            pass

    # ------------------------------------------------------------------
    # Environment management
    # ------------------------------------------------------------------

    # Env vars from other backends that must be hidden during Claude SDK calls
    _ISOLATION_VARS = ["OPENAI_API_KEY"]

    @contextlib.contextmanager
    def _sdk_env(self):
        """Temporarily inject auth env vars for an SDK call.

        The SDK reads authentication from ``os.environ``.  Because these
        values are constant per instance the worst-case concurrent-write
        scenario is benign (same values), but we still restore the originals
        to keep tests hermetic.

        Also temporarily removes env vars belonging to other backends
        (e.g. ``OPENAI_API_KEY``) to prevent cross-contamination.
        """
        original = {}
        removed = {}
        try:
            # Inject Claude auth vars
            for key, value in (self.claude_env_vars or {}).items():
                original[key] = os.environ.get(key)
                os.environ[key] = value

            # Remove other backends' credentials (cross-isolation)
            for key in self._ISOLATION_VARS:
                if key in os.environ:
                    removed[key] = os.environ.pop(key)

            yield
        finally:
            # Restore Claude auth vars
            for key, original_value in original.items():
                if original_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original_value

            # Restore removed isolation vars
            for key, value in removed.items():
                os.environ[key] = value

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify(self) -> bool:
        """Verify Claude Agent SDK is working and authenticated."""
        try:
            logger.info("Testing Claude Agent SDK...")

            options = self._build_sdk_options(max_turns=1)
            messages = []
            async for message in query(
                prompt="Hello",
                options=options,
            ):
                messages.append(message)
                msg_type = getattr(message, "type", None) or (
                    message.get("type") if isinstance(message, dict) else None
                )
                if msg_type == "assistant":
                    break

            if messages:
                logger.info("Claude Agent SDK verified successfully")
                return True
            else:
                logger.warning("Claude Agent SDK test returned no messages")
                return False

        except Exception as e:
            logger.error(f"Claude Agent SDK verification failed: {e}")
            logger.warning("Please ensure Claude Code is installed and authenticated:")
            logger.warning("  1. Install: npm install -g @anthropic-ai/claude-code")
            logger.warning("  2. Set ANTHROPIC_AUTH_TOKEN environment variable")
            logger.warning("  3. Test: claude --print 'Hello'")
            return False

    # Backward-compatible alias — existing code calls verify_cli().
    verify_cli = verify

    # ------------------------------------------------------------------
    # ClaudeSDKClient lifecycle (persistent, bidirectional sessions)
    # ------------------------------------------------------------------

    def _make_ask_user_hook(self, session):
        """Create a PreToolUse hook that intercepts AskUserQuestion.

        When AskUserQuestion is detected, parks the session and waits for
        client input. Returns deny + user's response as the reason, which
        the CLI converts to a tool_result that Claude reads as the answer.
        """

        async def hook(input_data, tool_use_id, _context):
            tool_name = input_data.get("tool_name", "") if isinstance(input_data, dict) else ""
            if tool_name != "AskUserQuestion":
                return {}  # Allow other tools to proceed

            tool_input = input_data.get("tool_input", {}) if isinstance(input_data, dict) else {}
            actual_tool_use_id = (
                input_data.get("tool_use_id", tool_use_id)
                if isinstance(input_data, dict)
                else tool_use_id
            )

            session.pending_tool_call = {
                "call_id": actual_tool_use_id,
                "name": "AskUserQuestion",
                "arguments": tool_input,
            }
            session.input_event = asyncio.Event()

            # Signal the streaming loop to break so the route can
            # emit function_call + requires_action before the hook
            # blocks waiting for user input.
            if session.stream_break_event is not None:
                session.stream_break_event.set()

            try:
                await asyncio.wait_for(
                    session.input_event.wait(),
                    timeout=ASK_USER_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "AskUserQuestion hook timed out after %ds for session %s",
                    ASK_USER_TIMEOUT_SECONDS,
                    session.session_id,
                )
                session.input_response = None
                session.input_event = None
                session.pending_tool_call = None
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "User did not respond within the timeout period."
                        ),
                    }
                }

            # Capture response before clearing state
            user_response = session.input_response or ""
            session.input_response = None
            session.input_event = None

            # Deny with user's response as reason — CLI converts this to
            # a tool_result that Claude reads as the user's answer.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"User responded: {user_response}",
                }
            }

        return hook

    async def update_request_policy(
        self,
        client: ClaudeSDKClient,
        *,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[PermissionMode] = None,
        model_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Refresh per-request policy on an existing Claude SDK client.

        Mirrors the Codex backend's contract (added in Step 1 of the Codex
        parity work) so the gateway's continuation paths apply per-request
        policy changes for Claude sessions, too — but fails closed when the
        request asks for changes Claude cannot honor mid-session.

        ``permission_mode`` is delegated to the SDK's
        ``client.set_permission_mode``. If the SDK rejects the update, the
        exception propagates so the route can surface the failure rather than
        running the turn with the previous (potentially weaker) mode.

        ``allowed_tools`` / ``disallowed_tools`` raise
        :class:`UnsupportedContinuationPolicy` because the SDK has no runtime
        API to swap them: silently dropping a ``disallowed_tools`` hard-block
        on a continuation would be a security gap. Callers should surface
        this as a 400 to the client and (if needed) start a fresh session
        with the new tool lists.

        ``model_params`` is accepted for contract parity with the Codex
        backend but ignored — the SDK bakes model params at create time and
        does not expose a mid-session update.
        """
        _ = model_params
        if allowed_tools is not None or disallowed_tools is not None:
            raise UnsupportedContinuationPolicy(
                "Claude does not support changing allowed_tools / disallowed_tools "
                "on a continuation turn; start a new session to apply the new "
                "tool policy."
            )
        if permission_mode is not None:
            # Let SDK exceptions propagate so the route can fail closed —
            # silently running the next turn under the old mode could be a
            # downgrade (e.g. from acceptEdits back to default).
            await client.set_permission_mode(permission_mode)

    async def create_client(
        self,
        session,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        model_params: Optional[Dict[str, Any]] = None,
        output_format: Optional[Dict[str, Any]] = None,
        _custom_base: object = _UNSET,
    ) -> ClaudeSDKClient:
        """Create and connect a :class:`ClaudeSDKClient` for *session*.

        The client is connected with ``prompt=None`` (interactive mode)
        so subsequent turns can be sent via ``client.query()``.

        ``output_format`` is the SDK structured-output config
        (``{"type": "json_schema", "schema": {...}}``); it is baked into the
        session at create time and cannot be changed on later turns.

        ``_custom_base`` follows the same contract as ``run_completion``:
        when provided, the caller is responsible for having already resolved
        ``{{WORKING_DIRECTORY}}`` (and any other request-time placeholders).
        """
        # Reuse the gateway's session_id so logs, OpenAI response IDs,
        # and the on-disk SDK transcript all agree.  Disk presence chooses
        # between starting a new SDK session and resuming an existing one
        # (the latter applies after rehydrate or after an in-memory client
        # crash).
        from src.session_manager import _session_jsonl_exists

        has_history = _session_jsonl_exists(session)
        options = self._build_sdk_options(
            model=model,
            system_prompt=system_prompt,
            max_turns=get_default_max_turns(),
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            session_id=None if has_history else session.session_id,
            resume=session.session_id if has_history else None,
            permission_mode=permission_mode,
            output_format=output_format,
            mcp_servers=mcp_servers,
            task_budget=task_budget,
            cwd=Path(cwd) if cwd else None,
            extra_env=extra_env,
            _custom_base=_custom_base,
        )
        pre_tool_use = [
            HookMatcher(
                matcher="AskUserQuestion",
                hooks=[self._make_ask_user_hook(session)],
            )
        ]
        if cwd and sandbox_enabled():
            pre_tool_use.append(
                HookMatcher(
                    matcher="",
                    hooks=[make_workspace_sandbox_hook(Path(cwd))],
                )
            )
        options.hooks = {"PreToolUse": pre_tool_use}

        # SKILL_DEBUG: temporary diagnostics for plugin-skill execution. Logged
        # at WARNING with a unique prefix so it surfaces under any log level and
        # is trivial to grep/remove. Confirms which code is deployed (look for
        # ``Skill(:*)`` in allowed_tools) and the exact policy handed to the CLI.
        logger.warning(
            "SKILL_DEBUG create_client: backend_cls=%s allowed_tools=%s skills=%s "
            "setting_sources=%s permission_mode=%s sandbox_hook=%s cwd=%s",
            type(self).__name__,
            options.allowed_tools,
            getattr(options, "skills", None),
            options.setting_sources,
            getattr(options, "permission_mode", None),
            bool(cwd and sandbox_enabled()),
            cwd,
        )

        with self._sdk_env():
            client = ClaudeSDKClient(options=options)
            await client.connect(prompt=None)

        return client

    async def run_completion_with_client(
        self,
        client: ClaudeSDKClient,
        prompt: str,
        session,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Run a completion turn on an existing *client*.

        Sends *prompt* via ``client.query()`` then yields converted
        message dicts from ``client.receive_response()``.  On error the
        session's client reference is cleared so the caller can detect
        the broken connection and create a fresh client.

        When a PreToolUse hook fires for AskUserQuestion, it sets
        ``session.stream_break_event`` to signal this loop to stop
        yielding so the route can emit function_call + requires_action.
        """
        # Provide an event the hook can signal to break streaming
        break_event = asyncio.Event()
        session.stream_break_event = break_event
        try:
            await client.query(prompt)
            response_iter = client.receive_response().__aiter__()
            while True:
                # Race: next message vs hook-fired break signal
                get_next = asyncio.ensure_future(response_iter.__anext__())
                wait_break = asyncio.ensure_future(break_event.wait())
                done, pending = await asyncio.wait(
                    [get_next, wait_break],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass

                if wait_break in done:
                    # Hook fired — yield any message that arrived concurrently,
                    # then break so the route can emit function_call.
                    if get_next in done:
                        try:
                            yield self._convert_message(get_next.result())
                        except StopAsyncIteration:
                            pass
                    break

                # Normal message arrived
                if get_next in done:
                    try:
                        message = get_next.result()
                    except StopAsyncIteration:
                        break  # Stream ended normally (ResultMessage received)
                    yield self._convert_message(message)
        except Exception as exc:
            logger.error("ClaudeSDKClient error: %s", exc, exc_info=True)
            session.client = None
            yield error_chunk(str(exc))
        finally:
            session.stream_break_event = None

    async def receive_response_from_client(
        self,
        client: ClaudeSDKClient,
        session,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield remaining messages from *client* without sending a new query.

        Used after the PreToolUse hook returns (deny + reason) — the SDK
        continues processing from where it left off.  A new ``query()``
        call is unnecessary because the original request is still active.
        """
        try:
            async for message in client.receive_response():
                yield self._convert_message(message)
        except Exception as exc:
            logger.error("ClaudeSDKClient receive error: %s", exc, exc_info=True)
            session.client = None
            yield error_chunk(str(exc))

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Extract the assistant message from Claude Agent SDK messages.

        Implements ``BackendClient.parse_message()``.

        Renders all content blocks (text, tool_use, tool_result, thinking)
        into a single text string. Prioritizes ResultMessage.result to avoid
        duplication with AssistantMessage content (SDK sends both with the
        same text).
        """
        # First pass: check if a ResultMessage with result exists
        result_text = None
        for message in messages:
            if message.get("subtype") == "success" and "result" in message:
                result = message["result"]
                if result and result.strip():
                    result_text = result

        if result_text is not None:
            return result_text

        # Fallback: extract from AssistantMessage content blocks
        all_parts = []
        for message in messages:
            # AssistantMessage (new SDK format): has content list
            if "content" in message and isinstance(message["content"], list):
                formatted = MessageAdapter.format_blocks(message["content"])
                if formatted:
                    all_parts.append(formatted)

            # AssistantMessage (old format)
            elif message.get("type") == "assistant" and "message" in message:
                sdk_message = message["message"]
                if isinstance(sdk_message, dict) and "content" in sdk_message:
                    content = sdk_message["content"]
                    if isinstance(content, list):
                        formatted = MessageAdapter.format_blocks(content)
                        if formatted:
                            all_parts.append(formatted)
                    elif isinstance(content, str) and content.strip():
                        all_parts.append(content)

        return "\n".join(all_parts) if all_parts else None

    # Backward-compatible alias — existing code calls parse_claude_message().
    parse_claude_message = parse_message

    def _cleanup_temp_dir(self):
        """Best-effort removal of the temporary workspace at process exit.

        Registered as an ``atexit`` handler (see ``__init__``). It runs when
        logging streams may already be torn down — under pytest the capture
        handlers are closed before atexit fires, and ``sys.is_finalizing()`` is
        still ``False`` there, so the timing can't be guarded reliably. It
        therefore performs **no logging**: a stray log call would surface as a
        noisy ``ValueError: I/O operation on closed file`` traceback. The
        workspace is already logged at construction time. The handler
        unregisters itself afterwards so repeated client churn never
        accumulates stale callbacks and a redundant second call is a no-op.
        """
        if self.temp_dir and os.path.exists(self.temp_dir):
            with contextlib.suppress(Exception):
                shutil.rmtree(self.temp_dir)
        with contextlib.suppress(Exception):
            atexit.unregister(self._cleanup_temp_dir)
