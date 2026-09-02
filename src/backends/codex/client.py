"""Codex backend client built on the official ``openai-codex`` SDK.

The previous implementation kept a hand-rolled ``codex app-server`` JSON-RPC
protocol client in-tree because no official Python SDK existed. OpenAI now
publishes ``openai-codex`` (Codex-pinned versioning, bundled CLI binary via
``openai-codex-cli-bin``), which speaks the same app-server protocol with
generated v2 types. This module keeps the gateway-facing contract —
``BackendClient`` methods, emitted chunk shapes, and the interactive approval
continuation — while delegating transport, process lifecycle, and typed
protocol handling to the SDK.

Architecture notes:

- **One SDK client (= one ``codex app-server`` process) per gateway session.**
  ``session.client`` is persistent across continuation requests (see
  ``_ensure_response_session_client``), so the process lives for the session
  and ``disconnect()`` closes it. This removes the old shared-RPC design and
  its head-of-line blocking lock.
- **Turn streaming** uses the SDK's per-turn notification routing. A pump task
  forwards typed notifications as camelCase wire dicts (``model_dump(
  by_alias=True)``), so the notification→chunk mapping layer below is the same
  dict-based code the old client used.
- **Approvals** arrive through the SDK's synchronous ``approval_handler``
  callback (invoked on the SDK reader thread). Tool-policy decisions are
  answered inline; anything interactive is bridged onto the active turn's
  queue and the handler blocks on a ``threading.Event`` until
  ``resume_approval_with_client`` supplies the decision (or the approval
  timeout lapses, which cancels).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncGenerator, Dict, Iterable, Iterator, List, Optional, Tuple

from openai_codex import AsyncCodex, CodexConfig
from openai_codex.models import UnknownNotification

from src.backends.base import SessionHandle
from src.backends.codex.auth import CodexAuthProvider
from src.backends.codex.constants import (
    CODEX_MODELS,
    approval_policy,
    approval_timeout_ms,
    codex_bin_override,
    configured_config_overrides,
    disallowed_tools_from_env,
    read_idle_timeout_ms,
    sandbox_mode,
)
from src.backends.common import (
    TokenEstimateMixin,
    combine_system_prompt,
    completion_chunks,
    error_chunk,
)
from src.backends.mcp_headers import inject_mcp_headers
from src.constants import DEFAULT_TIMEOUT_MS
from src.mcp_config import resolve_mcp_servers
from src.message_adapter import MessageAdapter

logger = logging.getLogger(__name__)

CODEX_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/mcpToolCall/requestApproval",
    "item/dynamicToolCall/requestApproval",
}

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"

# Gateway tool names (Claude-style) mapped to the Codex item type they map onto.
# ``allowed_tools``/``disallowed_tools`` may use either the Claude alias or the
# Codex-native name; both resolve to the same enforcement bucket.
CODEX_TOOL_NAME_ALIASES: Dict[str, str] = {
    "Bash": "commandExecution",
    "BashOutput": "commandExecution",
    "KillShell": "commandExecution",
    "Edit": "fileChange",
    "Write": "fileChange",
    "NotebookEdit": "fileChange",
}

_APPROVAL_METHOD_TO_TOOL: Dict[str, str] = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/mcpToolCall/requestApproval": "mcpToolCall",
    "item/dynamicToolCall/requestApproval": "dynamicToolCall",
}

# Gateway permission_mode (Claude vocabulary) -> Codex approvalPolicy.
# ``bypassPermissions`` translates to ``never`` (skip approvals); the remaining
# modes default to ``on-request`` so risky operations still pause for review.
CODEX_PERMISSION_MODE_TO_APPROVAL: Dict[str, str] = {
    "bypassPermissions": "never",
    "default": "on-request",
    "acceptEdits": "on-request",
    "plan": "on-request",
}

# OpenAI Responses-style control fields the current Codex turn API understands.
# The app-server dropped raw sampling knobs (temperature/top_p/max tokens) from
# ``turn/start`` in favor of reasoning controls; unknown sampling fields are
# logged and skipped so a request carrying them still runs.
CODEX_MODEL_PARAM_KEY_MAP: Dict[str, str] = {
    "effort": "effort",
    "reasoning_effort": "effort",
    "summary": "summary",
    "reasoning_summary": "summary",
}


def _translate_model_params(model_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Translate OpenAI-style model params into the Codex turn/start vocabulary.

    - ``None`` or empty dict yields ``{}`` (no payload pollution when the
      request didn't ask for overrides).
    - ``None`` values are skipped (Pydantic optional fields default to None).
    - Keys the current turn API has no equivalent for (temperature, top_p,
      max_output_tokens, ...) are dropped with a debug log instead of being
      forwarded, because the app-server rejects unknown turn fields.
    """
    if not model_params:
        return {}
    out: Dict[str, Any] = {}
    for key, value in model_params.items():
        if value is None:
            continue
        mapped = CODEX_MODEL_PARAM_KEY_MAP.get(key)
        if mapped is None:
            logger.debug(
                "Codex turn API has no equivalent for model param %r; dropping", key
            )
            continue
        out[mapped] = value
    return out


_UNKNOWN_PERMISSION_MODE_FALLBACK = "on-request"


def _resolve_approval_policy(
    permission_mode: Optional[str],
    *,
    has_tool_policy: bool = False,
) -> str:
    """Map gateway permission_mode onto Codex approvalPolicy.

    When ``permission_mode`` is ``None``, fall back to the operator's
    ``CODEX_APPROVAL_POLICY`` env (defaults to ``never``). When it is set to
    an *unknown* value, fall back to a safe ``on-request`` rather than the env,
    so a typo or invalid mode can't silently bypass approval-time enforcement.

    When ``has_tool_policy`` is set (the caller has allowed_tools/disallowed_tools
    or a global DISALLOWED_TOOLS env), a resolved ``never`` is upgraded to
    ``on-request`` so Codex actually emits approval requests; otherwise the
    gateway's auto-deny handler never runs and the tool policy is silently
    bypassed.
    """
    if permission_mode is None:
        resolved = approval_policy()
    else:
        mapped = CODEX_PERMISSION_MODE_TO_APPROVAL.get(permission_mode)
        if mapped is None:
            logger.warning(
                "Codex received unknown permission_mode %r; falling back to %s",
                permission_mode,
                _UNKNOWN_PERMISSION_MODE_FALLBACK,
            )
            resolved = _UNKNOWN_PERMISSION_MODE_FALLBACK
        else:
            resolved = mapped

    if has_tool_policy and resolved == "never":
        return "on-request"
    return resolved


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server transport or protocol fails."""


def _consume_task_result(task: "asyncio.Task") -> None:
    """Swallow a detached task's outcome so it never logs as un-retrieved."""
    with contextlib.suppress(BaseException):
        task.exception()


@dataclass
class _PendingApproval:
    """One approval request bridged from the SDK reader thread.

    ``request_id`` is gateway-generated (the SDK hides the JSON-RPC id); it
    only needs to be consistent between the surfaced tool chunk and the
    continuation's ``call_id``. The reader thread blocks on
    ``decision_event`` until ``resume_approval_with_client`` stores a
    ``decision`` and sets the event, or ``expired`` flips after the approval
    timeout and the handler answers with a safe cancel on its own.
    """

    request_id: str
    method: str
    params: Dict[str, Any]
    decision_event: threading.Event = field(default_factory=threading.Event)
    decision: Optional[Dict[str, Any]] = None
    expired: bool = False


@dataclass
class _ActiveTurn:
    """Streaming state for the turn currently running on a session client.

    Lives across an approval suspension: the pump task keeps feeding
    ``queue`` while the HTTP response that surfaced the approval is long
    gone, and ``items``/``usage_box`` accumulate for the whole turn so the
    final completion chunks see pre-approval output too.
    """

    queue: "asyncio.Queue[Tuple[str, Any]]"
    # Filled in once turn/start returns; the queue exists earlier so an
    # approval request racing the turn/start response still has a consumer.
    turn_id: str = ""
    pump_task: Optional[asyncio.Task] = None
    items: list[dict[str, Any]] = field(default_factory=list)
    usage_box: dict[str, Optional[dict[str, int]]] = field(
        default_factory=lambda: {"usage": None}
    )


@dataclass
class CodexSessionClient(SessionHandle):
    """Handle for one gateway session mapped to one Codex thread + process."""

    codex: AsyncCodex
    thread_id: str
    model: Optional[str]
    cwd: Optional[str]
    loop: asyncio.AbstractEventLoop
    allowed_tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None
    permission_mode: Optional[str] = None
    model_params: Optional[Dict[str, Any]] = None
    mcp_servers: Optional[Dict[str, Any]] = None
    effort: Optional[str] = None
    active_turn: Optional[_ActiveTurn] = None
    pending_approval: Optional[_PendingApproval] = None

    @property
    def options(self) -> SimpleNamespace:
        """Continuation-validation shim mirroring the claude client's options.

        ``_validate_continuation_reasoning`` inspects ``client.options.effort``
        (and ``options.thinking`` for the ``none`` case) regardless of backend.
        """
        thinking = {"type": "disabled"} if self.effort == "none" else None
        return SimpleNamespace(effort=self.effort, thinking=thinking)

    async def interrupt_active_turn(self) -> bool:
        """Best-effort interrupt of the currently running turn."""
        turn = self.active_turn
        if turn is None:
            return False
        try:
            await self.codex._client.turn_interrupt(self.thread_id, turn.turn_id)
            return True
        except Exception:
            logger.debug("Codex turn interrupt failed", exc_info=True)
            return False

    async def disconnect(self) -> None:
        # Unblock a reader thread stuck waiting for an approval decision so
        # process shutdown can't hang on it.
        pending = self.pending_approval
        self.pending_approval = None
        if pending is not None and not pending.decision_event.is_set():
            pending.decision = {"decision": "cancel"}
            pending.decision_event.set()
        turn = self.active_turn
        self.active_turn = None
        # Close the process BEFORE the pump can wind down: its blocked
        # notification read sits on a non-cancellable executor thread that
        # only wakes when an event arrives or the transport fails over
        # (process exit -> fail_all). Never cancel the pump task while that
        # read is pending — cancellation would run its finally-unregister
        # first, and fail_all cannot wake a queue that is no longer
        # registered (the executor thread would block forever and stall
        # interpreter shutdown).
        await self.codex.close()
        if turn is not None and turn.pump_task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(turn.pump_task), timeout=5)


class CodexClient(TokenEstimateMixin):
    """BackendClient implementation backed by the official openai-codex SDK."""

    _combine_system_prompt = staticmethod(combine_system_prompt)

    def __init__(
        self,
        timeout: Optional[int] = None,
        read_idle_timeout: Optional[int] = None,
    ) -> None:
        # ``timeout`` is the overall turn budget (wall-clock cap for a whole
        # turn's notification drain). ``read_idle_timeout`` caps inter-event
        # silence on one turn's stream; each session owns its own app-server
        # process, so a wedged turn only ever stalls itself.
        self.timeout = (timeout if timeout is not None else DEFAULT_TIMEOUT_MS) / 1000
        self.read_idle_timeout = (
            read_idle_timeout
            if read_idle_timeout is not None
            else read_idle_timeout_ms()
        ) / 1000

    @property
    def name(self) -> str:
        return "codex"

    def supported_models(self) -> List[str]:
        return list(CODEX_MODELS)

    def get_auth_provider(self) -> CodexAuthProvider:
        return CodexAuthProvider()

    def update_request_policy(
        self,
        client: CodexSessionClient,
        *,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        model_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Replace the per-request tool policy stored on an existing session client.

        Codex reuses one persistent ``CodexSessionClient`` for the lifetime of a
        gateway session, but the gateway's tool-policy fields (``allowed_tools``
        / ``disallowed_tools``) are per-request. Continuation requests must be
        able to refresh those fields so each turn honors its own body.

        An explicit empty list ``[]`` for tool fields is preserved as a
        block-all policy; only ``None`` clears them.

        ``permission_mode`` is session-level (acceptEdits / bypassPermissions /
        ...). ``None`` here means "no override sent by this request"; the
        existing value is preserved. Pass an explicit string to change it.
        """
        client.allowed_tools = (
            list(allowed_tools) if allowed_tools is not None else None
        )
        client.disallowed_tools = (
            list(disallowed_tools) if disallowed_tools is not None else None
        )
        # ``model_params`` is per-request like tool lists; ``None`` resets so the
        # next turn doesn't inherit a previous request's sampling overrides.
        client.model_params = dict(model_params) if model_params else None
        if permission_mode is not None:
            client.permission_mode = permission_mode

    def runtime_metadata(self) -> Dict[str, Any]:
        try:
            from openai_codex import __version__ as sdk_version
        except Exception:  # pragma: no cover - version metadata is best-effort
            sdk_version = "unknown"
        return {
            "mode": "sdk",
            "sdk_version": sdk_version,
            "models": self.supported_models(),
            "approval_policy": approval_policy(),
            "sandbox": sandbox_mode(),
        }

    def close(self) -> None:
        """No shared process to close; sessions own their SDK clients."""

    shutdown = close

    def _codex_config(self, env: Optional[Dict[str, str]] = None) -> CodexConfig:
        return CodexConfig(
            codex_bin=codex_bin_override(),
            config_overrides=tuple(configured_config_overrides()),
            env=dict(env) if env else None,
            client_name="oh_my_gateway",
            client_title="Oh My Gateway",
        )

    def _new_codex(
        self,
        env: Optional[Dict[str, str]] = None,
        approval_handler=None,
    ) -> AsyncCodex:
        codex = AsyncCodex(self._codex_config(env))
        if approval_handler is not None:
            # The SDK only exposes ``approval_handler`` on the low-level sync
            # client constructor; the high-level AsyncCodex builds that client
            # itself, so install the handler through the (pinned-SDK) seam
            # before the process starts. tests/test_codex_backend.py asserts
            # this attribute path so an SDK bump that moves it fails loudly.
            codex._client._sync._approval_handler = approval_handler
        return codex

    @staticmethod
    async def _start_codex(codex: AsyncCodex) -> None:
        """Start + initialize the SDK client so raw ``_client`` calls work.

        Only the high-level ``AsyncCodex`` methods lazy-initialize; this
        backend drives the raw typed client (for wire-dict params the
        high-level API doesn't expose), so initialization is explicit.
        """
        await codex._ensure_initialized()

    async def verify(self) -> bool:
        codex = self._new_codex()
        try:
            await self._start_codex(codex)
            payload = await codex._client.model_list()
            return isinstance(payload.data, list)
        except Exception as exc:
            logger.error("Codex backend verification failed: %s", exc)
            return False
        finally:
            with contextlib.suppress(Exception):
                await codex.close()

    async def create_client(
        self,
        *,
        session: Any,
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
        _custom_base: Any = None,
        forward_headers: Optional[Dict[str, str]] = None,
        effort: Optional[str] = None,
    ) -> CodexSessionClient:
        _ = (task_budget,)
        env = self._metadata_env(extra_env)
        # Resolve ``{{env:NAME}}`` templates in per-server env/headers, then inject
        # the gateway-resolved MCP context header (identity + caller-owned
        # credentials) into http/SSE server configs before forwarding them to the
        # app-server, mirroring the claude backend.
        mcp_servers = resolve_mcp_servers(mcp_servers) or mcp_servers
        mcp_servers = inject_mcp_headers(mcp_servers, forward_headers)

        client = CodexSessionClient(
            codex=None,  # type: ignore[arg-type] - set right below
            thread_id="",
            model=model,
            cwd=cwd,
            loop=asyncio.get_running_loop(),
            allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
            disallowed_tools=(
                list(disallowed_tools) if disallowed_tools is not None else None
            ),
            permission_mode=permission_mode,
            model_params=dict(model_params) if model_params else None,
            mcp_servers=dict(mcp_servers) if mcp_servers else None,
            effort=effort,
        )
        codex = self._new_codex(
            env,
            approval_handler=lambda method, params: self._handle_approval_request(
                client, method, params
            ),
        )
        client.codex = codex

        params = self._thread_params(
            model=model,
            cwd=cwd,
            system_prompt=combine_system_prompt(_custom_base, system_prompt),
            permission_mode=permission_mode,
            has_tool_policy=self._has_tool_policy(allowed_tools, disallowed_tools),
            mcp_servers=mcp_servers,
        )
        try:
            await self._start_codex(codex)
            thread_id = getattr(session, "codex_thread_id", None)
            if thread_id:
                await codex._client.thread_resume(thread_id, params)
            else:
                result = await codex._client.thread_start(
                    {**params, "serviceName": "oh-my-gateway"}
                )
                thread_id = str(result.thread.id)
                setattr(session, "codex_thread_id", thread_id)
        except Exception:
            with contextlib.suppress(Exception):
                await codex.close()
            raise

        client.thread_id = thread_id
        return client

    def _metadata_env(self, extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
        if not extra_env:
            return {}
        from src.constants import METADATA_ENV_ALLOWLIST

        return {k: v for k, v in extra_env.items() if k in METADATA_ENV_ALLOWLIST}

    @staticmethod
    def _codex_mcp_server_entry(config: Dict[str, Any]) -> Dict[str, Any]:
        """Convert one Claude-style MCP server config to Codex config schema.

        stdio servers keep command/args/env; http/SSE servers map ``url`` plus
        ``headers`` -> ``http_headers`` (the Codex config key for static
        streamable-HTTP headers).
        """
        url = config.get("url")
        if isinstance(url, str) and url:
            entry: Dict[str, Any] = {"url": url}
            headers = config.get("headers")
            if isinstance(headers, dict) and headers:
                entry["http_headers"] = dict(headers)
            return entry
        entry = {}
        if config.get("command"):
            entry["command"] = config["command"]
        if isinstance(config.get("args"), list):
            entry["args"] = list(config["args"])
        if isinstance(config.get("env"), dict) and config["env"]:
            entry["env"] = dict(config["env"])
        return entry

    @classmethod
    def _codex_mcp_config(cls, mcp_servers: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, config in mcp_servers.items():
            if not isinstance(config, dict):
                continue
            entry = cls._codex_mcp_server_entry(config)
            if entry:
                out[name] = entry
        return out

    def _thread_params(
        self,
        *,
        model: Optional[str],
        cwd: Optional[str],
        system_prompt: Optional[str],
        permission_mode: Optional[str] = None,
        has_tool_policy: bool = False,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "approvalPolicy": _resolve_approval_policy(
                permission_mode, has_tool_policy=has_tool_policy
            ),
            "sandbox": sandbox_mode(),
        }
        if model:
            params["model"] = model
        if cwd:
            params["cwd"] = cwd
        if system_prompt:
            params["developerInstructions"] = system_prompt
        if mcp_servers:
            # MCP servers ride the thread-scoped config override tree in the
            # current app-server API (the old top-level ``mcpServers`` request
            # field is gone). An empty dict behaves like "no servers" and the
            # key is left out so the app-server keeps its defaults.
            #
            # Filtering note: unlike the Claude backend (which narrows
            # ``mcp_servers`` against the per-request ``allowed_tools``
            # patterns via ``mcp__<server>__*``), Codex forwards the full
            # server set and enforces per-tool policy at approval time via
            # ``item/mcpToolCall/requestApproval``. The net effect is the same
            # (disallowed MCP tools never execute) but the enforcement point
            # differs.
            converted = self._codex_mcp_config(mcp_servers)
            if converted:
                params["config"] = {"mcp_servers": converted}
        return params

    def _turn_params(self, client: CodexSessionClient) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "approvalPolicy": _resolve_approval_policy(
                client.permission_mode,
                has_tool_policy=self._client_has_tool_policy(client),
            ),
        }
        if client.model:
            params["model"] = client.model
        if client.cwd:
            params["cwd"] = client.cwd
        if client.effort:
            params["effort"] = client.effort
        # Reasoning-control overrides supplied by the request body. Unset
        # request fields stay absent from the payload so the app-server's
        # defaults still apply; a per-request effort wins over the session one.
        translated = _translate_model_params(client.model_params)
        if translated:
            params.update(translated)
        return params

    @staticmethod
    def _has_tool_policy(
        allowed_tools: Optional[List[str]],
        disallowed_tools: Optional[List[str]],
    ) -> bool:
        # ``allowed_tools is not None`` matters because ``[]`` is a real
        # block-all policy that must still flip approvalPolicy off ``never``.
        if allowed_tools is not None or disallowed_tools:
            return True
        return bool(disallowed_tools_from_env())

    @classmethod
    def _client_has_tool_policy(cls, client: CodexSessionClient) -> bool:
        return cls._has_tool_policy(client.allowed_tools, client.disallowed_tools)

    @staticmethod
    def _coerce_turn_input_items(prompt: Any) -> list[Dict[str, Any]]:
        """Normalize the run_completion ``prompt`` argument into Codex input items.

        - ``str``: wrapped into a single ``{"type": "text", "text": ...}`` item
          so existing string callers stay unchanged.
        - ``list``: must contain only dicts; each is forwarded verbatim so
          callers can express multimodal payloads (text + image/file). Order
          and metadata are preserved.
        - anything else: ``ValueError`` (the route catches this and surfaces a
          clean error chunk).
        """
        if isinstance(prompt, str):
            return [{"type": "text", "text": prompt}]
        if isinstance(prompt, list):
            if not prompt:
                raise ValueError("Codex turn input list must contain at least one item")
            for index, item in enumerate(prompt):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Codex turn input item at index {index} must be a dict, "
                        f"got {type(item).__name__}"
                    )
            return [dict(item) for item in prompt]
        raise ValueError(
            f"Codex turn input must be a string or list of dicts, got {type(prompt).__name__}"
        )

    # ------------------------------------------------------------------
    # Approval handling (SDK reader thread -> event loop bridge)
    # ------------------------------------------------------------------

    def _handle_approval_request(
        self,
        client: CodexSessionClient,
        method: str,
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Answer an app-server approval request.

        Runs on the SDK reader thread, so it must not touch the event loop
        except through ``call_soon_threadsafe``. Policy decisions (auto-deny /
        auto-accept) return immediately; interactive approvals are queued to
        the active turn's consumer and this thread blocks until the
        continuation supplies a decision or the timeout lapses.
        """
        params = params if isinstance(params, dict) else {}
        notification = {"id": None, "method": method, "params": params}

        if method in CODEX_APPROVAL_METHODS:
            # Disallow / allowlist policies always take precedence over
            # ``acceptEdits`` auto-accept so an explicit block can't be
            # silently turned into an accept.
            if self._should_auto_deny_approval(client, notification):
                logger.info("Codex auto-denied approval: method=%s", method)
                return self._deny_result(method)
            if self._should_auto_accept_approval(client, notification):
                logger.info(
                    "Codex auto-accepted approval (acceptEdits): method=%s", method
                )
                return {"decision": "accept"}
        else:
            logger.warning("Unknown Codex server request method: %r", method)
            return {}

        turn = client.active_turn
        if turn is None:
            # No consumer to surface the approval to (e.g. a request raced the
            # turn teardown). Fail closed rather than approving blind.
            logger.warning(
                "Codex approval %s arrived with no active turn consumer; denying",
                method,
            )
            return self._deny_result(method)

        pending = _PendingApproval(
            request_id=str(uuid.uuid4()),
            method=method,
            params=dict(params),
        )
        client.pending_approval = pending
        client.loop.call_soon_threadsafe(turn.queue.put_nowait, ("approval", pending))

        if not pending.decision_event.wait(timeout=approval_timeout_ms() / 1000):
            pending.expired = True
            if client.pending_approval is pending:
                client.pending_approval = None
            logger.warning(
                "Codex approval %s timed out waiting for a continuation; cancelling",
                method,
            )
            if method == "item/permissions/requestApproval":
                return {"permissions": {}, "scope": "turn"}
            return {"decision": "cancel"}
        return pending.decision or self._deny_result(method)

    @staticmethod
    def _deny_result(method: str) -> Dict[str, Any]:
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        return {"decision": "decline"}

    def _should_auto_deny_approval(
        self,
        client: CodexSessionClient,
        notification: Dict[str, Any],
    ) -> bool:
        tool_identities = self._approval_tool_identities(notification)
        if not tool_identities:
            return False
        request_disallowed = list(client.disallowed_tools or [])
        env_disallowed = disallowed_tools_from_env()
        disallowed = self._normalize_tool_names(request_disallowed + env_disallowed)
        if self._tool_policy_matches(disallowed, tool_identities):
            return True
        # ``allowed_tools is not None`` distinguishes an explicit policy (even
        # an empty block-all list) from "no allow-list set".
        if client.allowed_tools is not None:
            allowed = self._normalize_tool_names(client.allowed_tools)
            if not self._tool_policy_matches(allowed, tool_identities):
                return True
        return False

    @staticmethod
    def _approval_tool_identities(notification: Dict[str, Any]) -> set[str]:
        method = str(notification.get("method") or "")
        codex_tool = _APPROVAL_METHOD_TO_TOOL.get(method)
        if codex_tool is None:
            return set()

        identities = {codex_tool}
        params = notification.get("params")
        if not isinstance(params, dict):
            return identities

        if codex_tool == "mcpToolCall":
            server_label = params.get("serverLabel") or params.get("serverName")
            tool_name = params.get("toolName")
            if isinstance(server_label, str) and server_label:
                server_names = {server_label, "_".join(server_label.split("-"))}
                if isinstance(tool_name, str) and tool_name:
                    for server_name in server_names:
                        identities.add(f"mcp__{server_name}__{tool_name}")
                else:
                    for server_name in server_names:
                        identities.add(f"mcp__{server_name}__*")

        return identities

    @staticmethod
    def _normalize_tool_names(names: Optional[List[str]]) -> set[str]:
        if not names:
            return set()
        return {CODEX_TOOL_NAME_ALIASES.get(name, name) for name in names}

    @staticmethod
    def _tool_policy_matches(policy_names: set[str], tool_identities: set[str]) -> bool:
        for policy_name in policy_names:
            for identity in tool_identities:
                if policy_name == identity:
                    return True
                if policy_name.startswith("mcp__") and fnmatch.fnmatchcase(
                    identity, policy_name
                ):
                    return True
        return False

    def _should_auto_accept_approval(
        self,
        client: CodexSessionClient,
        notification: Dict[str, Any],
    ) -> bool:
        # ``acceptEdits`` mirrors Claude's permission_mode: only file edits are
        # auto-accepted; commands and other approvals still need explicit user
        # consent.
        if client.permission_mode != "acceptEdits":
            return False
        return notification.get("method") == "item/fileChange/requestApproval"

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_completion_with_client(
        self,
        client: CodexSessionClient,
        prompt: Any,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        try:
            input_items = self._coerce_turn_input_items(prompt)
            turn_params = self._turn_params(client)
            raw = client.codex._client
            # The consumer queue must exist BEFORE turn/start: the app-server
            # may emit an approval request on the reader thread before the
            # event loop resumes after the turn/start response, and the
            # approval handler needs somewhere to surface it.
            turn_state = _ActiveTurn(queue=asyncio.Queue())
            client.active_turn = turn_state
            result = await raw.turn_start(
                client.thread_id, input_items, params=turn_params
            )
            turn_state.turn_id = str(result.turn.id)
            # Register before the pump task runs so early notifications are
            # buffered instead of dropped (the router replays pre-registration
            # events, except a turn/completed that lands entirely before
            # registration — the same exposure the SDK's own TurnHandle has).
            raw.register_turn_notifications(turn_state.turn_id)
            turn_state.pump_task = asyncio.create_task(
                self._pump_turn(raw, turn_state.turn_id, turn_state.queue)
            )
        except Exception as exc:
            client.active_turn = None
            logger.error("Codex turn start failed: %s", exc, exc_info=True)
            yield error_chunk(self._public_error_message(exc))
            return

        async for chunk in self._drain_turn(client, session):
            yield chunk

    async def resume_approval_with_client(
        self,
        client: CodexSessionClient,
        call_id: str,
        output: str,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        pending = client.pending_approval
        turn = client.active_turn
        if pending is None or turn is None:
            logger.error(
                "Codex approval continuation has no pending approval for call_id %r",
                call_id,
            )
            yield error_chunk(
                "Codex approval transport was lost before the response "
                "arrived; please retry the original request"
            )
            return
        if str(pending.request_id) != str(call_id):
            message = (
                "Codex approval request id mismatch: pending "
                f"{pending.request_id!r}, received {call_id!r}"
            )
            logger.error(message)
            yield error_chunk(message)
            return

        result = self._approval_result_from_output(
            pending.method, output, pending.params
        )
        client.pending_approval = None
        pending.decision = result
        pending.decision_event.set()

        async for chunk in self._drain_turn(client, session):
            yield chunk

    async def _pump_turn(
        self,
        raw: Any,
        turn_id: str,
        queue: "asyncio.Queue[Tuple[str, Any]]",
    ) -> None:
        """Forward SDK notifications for one turn onto its consumer queue.

        Runs until the turn completes or the transport fails. The consumer
        generator may come and go (approval suspensions end the HTTP stream),
        so this task, not the generator, owns the SDK-side stream lifecycle.
        """
        try:
            while True:
                notification = await raw.next_turn_notification(turn_id)
                wire = self._wire_notification(notification)
                await queue.put(("notification", wire))
                if wire.get("method") == "turn/completed":
                    break
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - surface transport failures
            await queue.put(("error", exc))
        finally:
            with contextlib.suppress(Exception):
                raw.unregister_turn_notifications(turn_id)
            with contextlib.suppress(Exception):
                await queue.put(("end", None))

    @staticmethod
    def _wire_notification(notification: Any) -> Dict[str, Any]:
        """Convert an SDK ``Notification`` into its camelCase wire dict.

        The chunk-mapping layer below predates the SDK and operates on wire
        dicts; ``model_dump(by_alias=True)`` reproduces them exactly, and
        unknown notification payloads pass their raw params through.
        """
        payload = notification.payload
        if isinstance(payload, UnknownNotification):
            params: Dict[str, Any] = dict(payload.params)
        else:
            params = payload.model_dump(by_alias=True, mode="json")
        return {"method": notification.method, "params": params}

    async def _drain_turn(
        self,
        client: CodexSessionClient,
        session: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Yield gateway chunks for the active turn until it ends or suspends."""
        turn = client.active_turn
        if turn is None:
            yield error_chunk("Codex turn state was lost")
            return
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        turn.queue.get(), timeout=self.read_idle_timeout
                    )
                except asyncio.TimeoutError:
                    await client.interrupt_active_turn()
                    raise CodexAppServerError(
                        "Timed out waiting for Codex app-server message "
                        f"after {self.read_idle_timeout:.3g}s"
                    )
                if kind == "approval":
                    chunk = self._approval_request_chunk(payload)
                    tool_chunk = self._store_pending_approval(session, client, chunk)
                    yield tool_chunk
                    return
                if kind == "error":
                    raise payload
                if kind == "end":
                    client.active_turn = None
                    return
                # kind == "notification"
                for chunk in self._chunks_from_notification(
                    thread_id=client.thread_id,
                    turn_id=turn.turn_id,
                    notification=payload,
                    items=turn.items,
                    usage_box=turn.usage_box,
                ):
                    yield chunk
                if time.monotonic() > deadline:
                    await client.interrupt_active_turn()
                    raise CodexAppServerError(
                        f"Codex turn exceeded the overall turn budget of {self.timeout:.3g}s"
                    )
        except Exception as exc:
            await self._teardown_turn(client)
            logger.error("Codex turn failed: %s", exc, exc_info=True)
            yield error_chunk(self._public_error_message(exc))

    async def _teardown_turn(self, client: CodexSessionClient) -> None:
        turn = client.active_turn
        client.active_turn = None
        pending = client.pending_approval
        client.pending_approval = None
        if pending is not None and not pending.decision_event.is_set():
            pending.decision = {"decision": "cancel"}
            pending.decision_event.set()
        if (
            turn is not None
            and turn.pump_task is not None
            and not turn.pump_task.done()
        ):
            # Leave the pump running rather than cancelling it: cancellation
            # would unregister the turn queue while the blocked notification
            # read still references it, leaving an executor thread nothing
            # can ever wake. The error paths that reach this teardown
            # interrupt the turn, so the pump exits on its turn/completed
            # (or on disconnect's process close via fail_all); until then it
            # drains into an orphaned queue.
            turn.pump_task.add_done_callback(_consume_task_result)

    def _public_error_message(self, exc: BaseException) -> str:
        message = str(exc)
        # SDK transport errors append the process stderr tail; keep it out of
        # client-facing chunks (it lands in server logs via exc_info instead).
        message = message.split("stderr_tail=", 1)[0].rstrip()
        return message or "Codex app-server error"

    # ------------------------------------------------------------------
    # Notification -> chunk mapping (wire-dict based, shared with tests)
    # ------------------------------------------------------------------

    def _chunks_from_notifications(
        self,
        *,
        thread_id: Optional[str] = None,
        turn_id: str,
        notifications: Iterable[Dict[str, Any]],
    ) -> Iterator[Dict[str, Any]]:
        items: list[dict[str, Any]] = []
        usage_box: dict[str, Optional[dict[str, int]]] = {"usage": None}
        for notification in notifications:
            yield from self._chunks_from_notification(
                thread_id=thread_id,
                turn_id=turn_id,
                notification=notification,
                items=items,
                usage_box=usage_box,
            )

    def _chunks_from_notification(
        self,
        *,
        thread_id: Optional[str],
        turn_id: str,
        notification: Dict[str, Any],
        items: list[dict[str, Any]],
        usage_box: dict[str, Optional[dict[str, int]]],
    ) -> Iterator[Dict[str, Any]]:
        method = notification.get("method")
        params = notification.get("params") if isinstance(notification, dict) else None
        if not isinstance(params, dict):
            return

        notification_turn_id = params.get("turnId")
        turn = params.get("turn")
        if isinstance(turn, dict):
            notification_turn_id = turn.get("id") or notification_turn_id

        if self._is_thread_idle_notification(thread_id, notification):
            yield from self._completion_chunks(items, usage_box)
            return

        if notification_turn_id != turn_id:
            return

        if method == "item/started":
            item = params.get("item")
            tool_use = self._tool_use_from_item(item)
            if tool_use:
                yield {"type": "assistant", "content": [tool_use]}
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str) and delta:
                yield {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": delta},
                    },
                }
            return

        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict):
                tool_result = self._tool_result_from_item(item)
                if tool_result:
                    yield {"type": "user", "content": [tool_result]}
                    return
                items.append(item)
            return

        if method == "thread/tokenUsage/updated":
            usage_box["usage"] = self._extract_usage(params.get("tokenUsage"))
            return

        if method != "turn/completed":
            return

        if isinstance(turn, dict) and turn.get("status") == "failed":
            yield error_chunk(self._turn_error_message(turn))
            return

        yield from self._completion_chunks(items, usage_box)

    def _approval_request_chunk(self, pending: _PendingApproval) -> Dict[str, Any]:
        arguments = self._approval_arguments(pending.method, pending.params)
        tool_block = {
            "type": "tool_use",
            "id": str(pending.request_id),
            "name": "codex_approval",
            "input": arguments,
            "metadata": {
                "codex_approval_request_id": str(pending.request_id),
                "codex_approval_method": pending.method,
                "codex_thread_id": str(pending.params.get("threadId") or ""),
                "codex_turn_id": str(pending.params.get("turnId") or ""),
            },
        }
        return {
            "type": "codex_approval_request",
            "request_id": pending.request_id,
            "method": pending.method,
            "params": pending.params,
            "tool_chunk": {"type": "assistant", "content": [tool_block]},
        }

    def _approval_arguments(
        self, method: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        kind = self._approval_kind(method)
        reason = params.get("reason")
        arguments: Dict[str, Any] = {
            "kind": kind,
            "question": self._approval_question(kind, params),
        }
        if isinstance(params.get("command"), str):
            arguments["command"] = params["command"]
        if isinstance(params.get("cwd"), str):
            arguments["cwd"] = params["cwd"]
        if isinstance(reason, str) and reason:
            arguments["reason"] = reason
        if "permissions" in params:
            arguments["permissions"] = params.get("permissions") or {}
        if "grantRoot" in params and params.get("grantRoot"):
            arguments["grantRoot"] = params["grantRoot"]
        for key in (
            "itemId",
            "approvalId",
            "additionalPermissions",
            "commandActions",
            "networkApprovalContext",
            "proposedExecpolicyAmendment",
            "proposedNetworkPolicyAmendments",
        ):
            if key in params and params.get(key) is not None:
                arguments[key] = params[key]
        arguments["options"] = self._approval_options(kind, params)
        return arguments

    def _approval_kind(self, method: str) -> str:
        if method == "item/commandExecution/requestApproval":
            return "command"
        if method == "item/fileChange/requestApproval":
            return "file_change"
        if method == "item/permissions/requestApproval":
            return "permissions"
        return "approval"

    def _approval_question(self, kind: str, params: Dict[str, Any]) -> str:
        if kind == "command":
            command = params.get("command")
            if isinstance(command, str) and command:
                return f"Codex requests approval to run command: {command}"
            return "Codex requests approval to run a command."
        if kind == "file_change":
            return "Codex requests approval to apply file changes."
        if kind == "permissions":
            return "Codex requests additional permissions."
        return "Codex requests approval."

    def _approval_options(
        self, kind: str, params: Dict[str, Any]
    ) -> list[dict[str, Any]]:
        if kind == "permissions":
            decisions: list[Any] = ["accept", "acceptForSession", "decline"]
        else:
            raw = params.get("availableDecisions")
            decisions = raw if isinstance(raw, list) else []
            if not decisions:
                decisions = ["accept", "acceptForSession", "decline", "cancel"]
        descriptions = {
            "accept": "Approve this request once.",
            "acceptForSession": "Approve matching requests for this session.",
            "acceptWithExecpolicyAmendment": (
                "Approve and apply the proposed execpolicy amendment."
            ),
            "applyNetworkPolicyAmendment": "Apply the proposed network policy rule.",
            "decline": "Deny and let Codex continue.",
            "cancel": "Deny and interrupt the turn.",
        }
        options = []
        for decision in decisions:
            label = self._approval_decision_label(decision)
            if not label:
                continue
            option: Dict[str, Any] = {
                "label": label,
                "description": descriptions.get(label, f"Choose {label}."),
            }
            if isinstance(decision, dict):
                option["decision"] = decision
            options.append(option)
        return options

    def _approval_decision_label(self, decision: Any) -> str:
        if isinstance(decision, str):
            return decision
        if not isinstance(decision, dict) or not decision:
            return ""
        if "acceptWithExecpolicyAmendment" in decision:
            return "acceptWithExecpolicyAmendment"
        if "applyNetworkPolicyAmendment" in decision:
            amendment = decision.get("applyNetworkPolicyAmendment")
            if isinstance(amendment, dict):
                policy = amendment.get("network_policy_amendment")
                if isinstance(policy, dict):
                    action = policy.get("action")
                    host = policy.get("host")
                    if action and host:
                        return f"applyNetworkPolicyAmendment:{action}:{host}"
            return "applyNetworkPolicyAmendment"
        return next(iter(decision.keys()), "")

    def _store_pending_approval(
        self,
        session: Any,
        client: CodexSessionClient,
        chunk: Dict[str, Any],
    ) -> Dict[str, Any]:
        tool_chunk = chunk["tool_chunk"]
        tool_block = tool_chunk["content"][0]
        metadata = tool_block["metadata"]
        session.pending_tool_call = {
            "call_id": metadata["codex_approval_request_id"],
            "name": ASK_USER_QUESTION_TOOL_NAME,
            "arguments": tool_block["input"],
            "backend": "codex",
            "codex_resume": "approval",
        }
        return tool_chunk

    def _tool_use_from_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in {
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "dynamicToolCall",
        }:
            return None
        if not isinstance(item_id, str) or not item_id:
            return None
        tool_input = {
            k: v for k, v in item.items() if k not in {"id", "type", "aggregatedOutput"}
        }
        return {
            "type": "tool_use",
            "id": item_id,
            "name": str(item_type),
            "input": tool_input,
        }

    def _tool_result_from_item(self, item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        item_id = item.get("id")
        if item_type not in {
            "commandExecution",
            "fileChange",
            "mcpToolCall",
            "dynamicToolCall",
        }:
            return None
        if not isinstance(item_id, str) or not item_id:
            return None
        status = str(item.get("status") or "")
        is_error = status in {"failed", "declined"}
        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                is_error = True
            content = item.get("aggregatedOutput")
            if not isinstance(content, str) or not content:
                content = json.dumps(
                    {
                        "status": status,
                        "exitCode": exit_code,
                        "command": item.get("command"),
                    },
                    ensure_ascii=False,
                )
        else:
            content = json.dumps(
                {k: v for k, v in item.items() if k not in {"id", "type"}},
                ensure_ascii=False,
            )
        return {
            "type": "tool_result",
            "tool_use_id": item_id,
            "content": content,
            "is_error": is_error,
        }

    def _approval_result_from_output(
        self,
        method: str,
        output: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        parsed: Any = None
        if isinstance(output, str):
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(output)
        if isinstance(parsed, dict):
            if method == "item/permissions/requestApproval" and "permissions" in parsed:
                return parsed
            if "decision" in parsed:
                return {"decision": parsed["decision"]}
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                return {"decision": parsed}
            logger.warning("Unrecognized Codex approval output: %r", parsed)

        decision = self._normalize_approval_decision(
            parsed if parsed is not None else output
        )
        if method == "item/permissions/requestApproval":
            if decision in {"accept", "acceptForSession"}:
                result: Dict[str, Any] = {
                    "permissions": params.get("permissions") or {}
                }
                result["scope"] = (
                    "session" if decision == "acceptForSession" else "turn"
                )
                return result
            return {"permissions": {}, "scope": "turn"}
        selected_decision = self._approval_decision_from_available_options(
            output, params
        )
        if selected_decision is not None:
            return {"decision": selected_decision}
        return {"decision": decision}

    def _approval_decision_from_available_options(
        self,
        output: str,
        params: Dict[str, Any],
    ) -> Optional[Any]:
        raw = str(output or "").strip()
        decisions = params.get("availableDecisions")
        if not isinstance(decisions, list):
            return None
        for decision in decisions:
            label = self._approval_decision_label(decision)
            if raw == label:
                return decision
        return None

    def _normalize_approval_decision(self, value: Any) -> str:
        if isinstance(value, list) and value:
            value = value[0]
        raw = str(value or "").strip()
        aliases = {
            "": "decline",
            "yes": "accept",
            "y": "accept",
            "allow": "accept",
            "approve": "accept",
            "approved": "accept",
            "once": "accept",
            "no": "decline",
            "n": "decline",
            "deny": "decline",
            "denied": "decline",
            "reject": "decline",
            "rejected": "decline",
            "always": "acceptForSession",
            "session": "acceptForSession",
            "stop": "cancel",
        }
        if raw in {
            "accept",
            "acceptForSession",
            "decline",
            "cancel",
        }:
            return raw
        return aliases.get(raw, "decline")

    def _is_thread_idle_notification(
        self,
        thread_id: Optional[str],
        notification: Dict[str, Any],
    ) -> bool:
        if not thread_id or notification.get("method") != "thread/status/changed":
            return False
        params = notification.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            return False
        status = params.get("status")
        return isinstance(status, dict) and status.get("type") == "idle"

    def _completion_chunks(
        self,
        items: list[dict[str, Any]],
        usage_box: dict[str, Optional[dict[str, int]]],
    ) -> Iterator[Dict[str, Any]]:
        final_text = self._final_response_from_items(items) or ""
        usage = usage_box.get("usage")
        yield from completion_chunks(final_text, usage)

    def _extract_usage(self, token_usage: Any) -> Optional[dict[str, int]]:
        if not isinstance(token_usage, dict):
            return None
        last = token_usage.get("last")
        if not isinstance(last, dict):
            return None
        input_tokens = int(last.get("inputTokens") or 0) + int(
            last.get("cachedInputTokens") or 0
        )
        # Reasoning tokens are emitted by the model alongside visible output;
        # OpenAI-compatible usage reporting rolls them into output_tokens so
        # totals match (input + output == totalTokens).
        output_tokens = int(last.get("outputTokens") or 0) + int(
            last.get("reasoningOutputTokens") or 0
        )
        return {"input_tokens": input_tokens, "output_tokens": output_tokens}

    def _turn_error_message(self, turn: dict[str, Any]) -> str:
        error = turn.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return "Codex turn failed"

    def _final_response_from_items(self, items: list[dict[str, Any]]) -> Optional[str]:
        last_unknown_phase: Optional[str] = None
        for item in reversed(items):
            if item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            if item.get("phase") == "final_answer":
                return text
            if item.get("phase") is None and last_unknown_phase is None:
                last_unknown_phase = text
        return last_unknown_phase

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        for message in reversed(messages):
            if message.get("subtype") == "success" and isinstance(
                message.get("result"), str
            ):
                result = message["result"]
                if result.strip():
                    return result
        parts = []
        for message in messages:
            if message.get("type") == "assistant" and isinstance(
                message.get("content"), list
            ):
                text = MessageAdapter.format_blocks(message["content"])
                if text:
                    parts.append(text)
        return "\n".join(parts) if parts else None
