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
from typing import AsyncGenerator, Dict, Any, Optional, List
from pathlib import Path
import logging

from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
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
    CLAUDE_TOOLS,
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_TASK_BUDGET,
    THINKING_BUDGET_TOKENS,
    DISALLOWED_SUBAGENT_TYPES,
    CLAUDE_SANDBOX_ENABLED,
    CLAUDE_SANDBOX_AUTO_ALLOW_BASH,
    CLAUDE_SANDBOX_EXCLUDED_COMMANDS,
    CLAUDE_SANDBOX_ALLOW_UNSANDBOXED,
    CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL,
    CLAUDE_SANDBOX_WEAKER_NESTED,
)
from src.backends.base import ResolvedModel
from src.constants import ASK_USER_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_MS, PERMISSION_MODE_BYPASS
from src.message_adapter import MessageAdapter
from src.image_handler import ImageHandler
from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns

logger = logging.getLogger(__name__)


class ClaudeCodeCLI:
    """Gateway for Claude Agent SDK queries.

    Implements the ``BackendClient`` protocol defined in
    ``src/backends/base.py`` so it can be registered as the ``claude``
    backend.

    Each call to ``run_completion`` / ``verify`` invokes the SDK's
    ``query()`` function which is **single-use**: the underlying anyio
    channel closes after the first response completes.  Never cache or
    reuse the async generator returned by ``query()``; always create a
    fresh call.  For multi-turn conversations use the ``resume``
    parameter instead of attempting to reuse a previous query.
    """

    backend_name: str = "claude"

    def __init__(self, timeout: int = None, cwd: Optional[str] = None):
        if timeout is None:
            timeout = DEFAULT_TIMEOUT_MS
        self.timeout = timeout / 1000  # Convert ms to seconds
        self.temp_dir = None

        # If cwd is provided (from CLAUDE_CWD env var), use it
        # Otherwise create an isolated temp directory
        if cwd:
            self.cwd = Path(cwd)
            if not self.cwd.exists():
                logger.error(f"ERROR: Specified working directory does not exist: {self.cwd}")
                logger.error(
                    "Please create the directory first or unset CLAUDE_CWD to use a temporary directory"
                )
                raise ValueError(f"Working directory does not exist: {self.cwd}")
            else:
                logger.info(f"Using CLAUDE_CWD: {self.cwd}")
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

    @property
    def owned_by(self) -> str:
        return "anthropic"

    def supported_models(self) -> List[str]:
        return list(CLAUDE_MODELS)

    def resolve(self, model: str) -> Optional[ResolvedModel]:
        """Check if this backend handles the given model string.

        Handles:
        - Direct matches in CLAUDE_MODELS (e.g. "opus", "sonnet", "haiku")
        - ``claude/<sub>`` slash syntax (e.g. "claude/opus")

        Returns None if this backend does not handle the model.
        """
        if "/" in model:
            prefix, sub_model = model.split("/", 1)
            if prefix == "claude":
                return ResolvedModel(public_model=model, backend="claude", provider_model=sub_model)
            return None

        if model in CLAUDE_MODELS:
            return ResolvedModel(public_model=model, backend="claude", provider_model=model)

        return None

    def build_options(
        self,
        request: Any,
        resolved: ResolvedModel,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build backend options from request, resolved model, and header overrides.

        Raises BackendConfigError instead of HTTPException.
        """
        from src.parameter_validator import ParameterValidator

        options = request.to_claude_options()

        if overrides:
            options.update(overrides)

        # Use the provider model from resolution (may differ from request.model)
        if resolved.provider_model:
            options["model"] = resolved.provider_model

        if options.get("model"):
            ParameterValidator.validate_model(options["model"])

        # Claude-specific tool configuration
        if not request.enable_tools:
            options["disallowed_tools"] = CLAUDE_TOOLS
            options["max_turns"] = 1
            logger.debug("Tools disabled (default behavior for OpenAI compatibility)")
        elif request.allowed_tools is not None:
            # Caller provided explicit tool list — use it directly
            options["allowed_tools"] = list(request.allowed_tools)
            options["permission_mode"] = PERMISSION_MODE_BYPASS
            logger.debug(f"Tools enabled (caller-specified): {request.allowed_tools}")
        else:
            options["allowed_tools"] = DEFAULT_ALLOWED_TOOLS
            options["permission_mode"] = PERMISSION_MODE_BYPASS
            logger.debug(f"Tools enabled by user request: {DEFAULT_ALLOWED_TOOLS}")

        # Add MCP servers if configured (Claude only).
        # The SDK connects to the servers and resolves tool schemas internally.
        # We add symbolic tool patterns (mcp__<server>__*) to allowed_tools
        # so the SDK knows which MCP tools to enable without serializing full
        # JSON schemas into the API request payload.
        mcp_servers = get_mcp_servers()
        if mcp_servers:
            options["mcp_servers"] = mcp_servers
            if request.enable_tools and request.allowed_tools is None:
                # Only add all MCP patterns when no explicit list was given
                mcp_patterns = get_mcp_tool_patterns(mcp_servers)
                options.setdefault("allowed_tools", []).extend(mcp_patterns)
                logger.debug(f"MCP tools enabled symbolically: {mcp_patterns}")
            logger.debug(f"MCP servers enabled: {list(mcp_servers.keys())}")

        return options

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
        """Apply tool allow/disallow lists to *options*."""
        if allowed_tools:
            options.allowed_tools = allowed_tools
        base_disallowed = list(DISALLOWED_SUBAGENT_TYPES)
        if disallowed_tools:
            base_disallowed.extend(disallowed_tools)
        if base_disallowed:
            options.disallowed_tools = base_disallowed

    def _configure_sandbox(self, options: ClaudeAgentOptions) -> None:
        """Apply bash sandbox configuration to *options*.

        Tri-state logic based on ``CLAUDE_SANDBOX_ENABLED``:

        * ``None`` (env unset) — do **not** set ``options.sandbox`` at all,
          allowing project-level settings (``setting_sources=["project"]``)
          to take effect.
        * ``True`` — force-enable sandbox with env-configured parameters.
        * ``False`` — force-disable sandbox explicitly.
        """
        if CLAUDE_SANDBOX_ENABLED is None:
            return  # Respect project-level settings

        if not CLAUDE_SANDBOX_ENABLED:
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

    def _configure_session(
        self,
        options: ClaudeAgentOptions,
        session_id: Optional[str],
        resume: Optional[str],
    ) -> None:
        """Apply session / resume configuration to *options*.

        ``resume`` takes priority: when set, the SDK picks up the existing
        conversation by its session ID.  ``session_id`` is only used for
        brand-new sessions via the native ``session_id`` option.
        """
        if resume:
            options.resume = resume
        elif session_id:
            options.session_id = session_id

    _UNSET = object()  # sentinel for _custom_base default

    def _build_sdk_options(
        self,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_turns: int = 10,
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
    ) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with common parameters."""
        effective_cwd = cwd or self.cwd
        options = ClaudeAgentOptions(
            max_turns=max_turns, cwd=effective_cwd, setting_sources=["project", "local"]
        )

        self._configure_thinking(options)
        self._configure_sandbox(options)
        self._configure_tools(options, allowed_tools, disallowed_tools)

        if model:
            options.model = model
        # Resolve the custom base prompt.  When _custom_base is the sentinel
        # (_UNSET) the caller did not provide a frozen value so we read the
        # current global state (appropriate for stateless / first-turn calls).
        # When it is explicitly ``None`` the session was created in preset mode;
        # when it is a string the session was created in custom-base mode.
        if _custom_base is self._UNSET:
            from src.system_prompt import get_system_prompt

            custom_base = get_system_prompt()
        else:
            custom_base = _custom_base

        if custom_base:
            full = custom_base + ("\n\n" + system_prompt if system_prompt else "")
            options.system_prompt = full
        elif system_prompt:
            options.system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": system_prompt,
            }
        else:
            options.system_prompt = {"type": "preset", "preset": "claude_code"}
        if permission_mode:
            options.permission_mode = permission_mode
        if output_format:
            options.output_format = output_format
        if mcp_servers:
            options.mcp_servers = mcp_servers
        from src.runtime_config import get_token_streaming

        if get_token_streaming():
            options.include_partial_messages = True

        self._configure_session(options, session_id, resume)

        # Task budget: per-request value overrides the global default.
        effective_budget = task_budget if task_budget is not None else DEFAULT_TASK_BUDGET
        if effective_budget is not None:
            options.task_budget = {"total": effective_budget}

        # Pass allowlisted metadata keys as subprocess env vars.
        # Allowlist is configured via METADATA_ENV_ALLOWLIST in .env.
        if extra_env:
            from src.constants import METADATA_ENV_ALLOWLIST

            env_map = {k: v for k, v in extra_env.items() if k in METADATA_ENV_ALLOWLIST}
            if env_map:
                options.env = env_map

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
            return result
        return message

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

            messages = []
            async for message in query(
                prompt="Hello",
                options=ClaudeAgentOptions(
                    max_turns=1,
                    cwd=self.cwd,
                    system_prompt={"type": "preset", "preset": "claude_code"},
                ),
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

        async def hook(input_data, tool_use_id, context):
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
    ) -> ClaudeSDKClient:
        """Create and connect a :class:`ClaudeSDKClient` for *session*.

        The client is connected with ``prompt=None`` (interactive mode)
        so subsequent turns can be sent via ``client.query()``.
        """
        options = self._build_sdk_options(
            model=model,
            system_prompt=system_prompt,
            max_turns=10,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            session_id=session.session_id,
            resume=None,
            permission_mode=permission_mode,
            mcp_servers=mcp_servers,
            task_budget=task_budget,
            cwd=Path(cwd) if cwd else None,
        )
        options.hooks = {
            "PreToolUse": [
                HookMatcher(
                    matcher="AskUserQuestion",
                    hooks=[self._make_ask_user_hook(session)],
                )
            ]
        }

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
        """
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                yield self._convert_message(message)
        except Exception as exc:
            logger.error("ClaudeSDKClient error: %s", exc, exc_info=True)
            session.client = None
            yield {"type": "error", "is_error": True, "error": str(exc)}

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
            yield {"type": "error", "is_error": True, "error": str(exc)}

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,  # Accepted for caller compatibility; always yields chunks.
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        **_extra,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Run a single-use SDK query and yield converted message dicts.

        For multi-turn conversations:
        - First turn: pass ``session_id`` (native SDK option)
        - Follow-up turns: pass ``resume=<session_id>`` (uses ``--resume``)

        The SDK ``query()`` function is always invoked fresh per call — see
        the class docstring for why reuse is unsafe.
        """
        try:
            with self._sdk_env():
                options = self._build_sdk_options(
                    model=model,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    allowed_tools=allowed_tools,
                    _custom_base=_extra.get("_custom_base", self._UNSET),
                    disallowed_tools=disallowed_tools,
                    permission_mode=permission_mode,
                    output_format=output_format,
                    mcp_servers=mcp_servers,
                    session_id=session_id,
                    resume=resume,
                    extra_env=_extra.get("_metadata"),
                    task_budget=task_budget,
                    cwd=Path(cwd) if cwd else None,
                )

                async for message in query(prompt=prompt, options=options):
                    logger.debug(f"Raw SDK message type: {type(message)}")
                    logger.debug(f"Raw SDK message: {message}")

                    converted = self._convert_message(message)
                    logger.debug(f"Converted message: {converted}")
                    yield converted

        except Exception as e:
            logger.error(f"Claude Agent SDK error: {e}")
            yield {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error_message": str(e),
            }

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

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        """Estimate token usage (~4 characters per token)."""
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(completion) // 4)

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def _cleanup_temp_dir(self):
        """Clean up temporary directory on exit."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.info(f"Cleaned up temporary workspace: {self.temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory {self.temp_dir}: {e}")
