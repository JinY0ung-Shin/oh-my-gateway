"""Codex ``BackendClient`` built on the direct app-server transport (C0).

Issue #173, PR A: implement ``BackendClient``/``SessionHandle`` on top of
:class:`~src.backends.appserver.transport.AppServerTransport` and map native
Codex thread/turn/item notifications into the canonical ``/v1/responses`` chunk
contract, so the same ChatDRAGON reducer renders Claude and Codex. This module
never reaches into the frozen ``src/backends/codex`` package.

Topology (PR A): **one app-server process per session handle** -- the C0
"1 process : 1 live durable session" placement. Multi-user process sharing,
pooling and sharding are deliberately out of scope until #165's production
evidence (see ``transport.py`` header); this adapter takes the conservative
1:1 placement, which is also the strongest isolation posture. Deeper per-user
filesystem/``CODEX_HOME`` isolation is PR D.

Deliberately NOT in PR A (kept fail-closed here, real bridges land later):
* human interactions (approvals / ``requestUserInput``) -- PR B. A pending
  interaction is failed closed so a turn never hangs.
* subagent thread/activity mapping -- PR C.
* per-user workspace/runtime isolation as a security gate -- PR D.

This module also does not register itself into ``discover_backends``; wiring
``BACKENDS=codex`` onto this adapter is the small independent cutover (PR E).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from src.backends.appserver.auth import AppServerAuthProvider
from src.backends.appserver.constants import (
    app_server_argv,
    approval_policy,
    configured_public_models,
    request_timeout_s,
    sandbox_mode,
)
from src.backends.appserver.events import TurnMapper
from src.backends.appserver.interactions import (
    answer_result_from_output,
    interaction_arguments,
)
from src.backends.appserver.transport import (
    AppServerTransport,
    Notification,
    OrphanedResponse,
    PendingInteraction,
    RpcError,
    RuntimeLost,
    StaleAnswer,
    SubscriberOverflow,
    TerminalEvent,
    Subscription,
)
from src.backends.base import BackendDescriptor, ResolvedModel, SessionHandle
from src.backends.common import (
    TokenEstimateMixin,
    combine_system_prompt,
    error_chunk,
)
from src.message_adapter import MessageAdapter

# Interrupt must return well within the route's 2s cancel budget.
_INTERRUPT_TIMEOUT_S = 2.0

logger = logging.getLogger(__name__)

BACKEND_NAME = "codex"

# Canonical tool name the ChatDRAGON UI renders as the human-input card. The
# route pauses into ``requires_action`` on a ``pending_tool_call`` with this name.
ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"

# Model-param request keys -> Codex turn/start keys. Unset keys stay absent so
# the app-server's own defaults apply.
_MODEL_PARAM_KEYS = {
    "temperature": "temperature",
    "top_p": "topP",
    "max_output_tokens": "maxOutputTokens",
}


def _resolve_approval_policy(permission_mode: Optional[str]) -> str:
    """Map a gateway permission mode onto a Codex approval policy.

    PR A does not bridge approvals, so the safe default is the configured
    policy (``never`` out of the box): no approval server-requests fire, so no
    turn is interrupted by the fail-closed interaction handling below. A caller
    that explicitly asks to be prompted (``permission_mode == "default"``) gets
    ``on-request``; those prompts are then failed closed until PR B.
    """
    if permission_mode == "default":
        return "on-request"
    return approval_policy()


def _translate_model_params(model_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not model_params:
        return {}
    translated: Dict[str, Any] = {}
    for src_key, dst_key in _MODEL_PARAM_KEYS.items():
        value = model_params.get(src_key)
        if value is not None:
            translated[dst_key] = value
    return translated


def _to_turn_input(prompt: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Shape a gateway prompt into Codex ``turn/start`` input items.

    A plain string becomes a single text item; a list of native Codex turn-input
    items (multimodal, from the route's ``codex`` branch) is forwarded verbatim.
    """
    if isinstance(prompt, list):
        return prompt
    return [{"type": "text", "text": prompt}]


def _gateway_interrupt_chunk(message: str) -> Dict[str, Any]:
    """The terminal chunk the route maps to ``response.incomplete``."""
    return {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "error_message": message,
        "gateway_interrupted": True,
    }


class AppServerSessionClient(SessionHandle):
    """Per-session handle owning exactly one ``codex app-server`` process."""

    def __init__(
        self,
        *,
        transport: AppServerTransport,
        thread_id: str,
        generation: int,
        model: Optional[str],
        cwd: Optional[str],
        permission_mode: Optional[str],
        allowed_tools: Optional[List[str]],
        disallowed_tools: Optional[List[str]],
        model_params: Optional[Dict[str, Any]],
        mcp_servers: Optional[Dict[str, Any]],
    ) -> None:
        self.transport = transport
        self.thread_id = thread_id
        self.generation = generation
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.model_params = model_params
        self.mcp_servers = mcp_servers
        # The route sets this per turn; kept so ``interrupt_client`` can address
        # the live turn. ``None`` between turns.
        self.current_turn_id: Optional[str] = None
        # Toggled by the route's ``_configure_client_streaming``.
        self.stream_events = False
        # Per-turn transport view + mapper. The subscription PERSISTS across an
        # AskUserQuestion pause (the turn stays live in the app-server, parked on
        # the server request), so ``resume_approval_with_client`` keeps consuming
        # the SAME stream instead of re-subscribing and losing lossless items.
        self._subscription: Optional[Subscription] = None
        self._mapper: Optional[TurnMapper] = None
        # Native identity of the interaction currently parked at requires_action
        # (issue §3): the canonical call id maps to exactly one live native
        # request. ``None`` when no interaction is pending.
        self.pending_interaction_id: Any = None
        self.pending_interaction_method: Optional[str] = None
        self.pending_interaction_params: Optional[Dict[str, Any]] = None

    def turn_params(self) -> Dict[str, Any]:
        """``turn/start`` params (minus ``threadId``/``input``) for this handle."""
        params: Dict[str, Any] = {
            "approvalPolicy": _resolve_approval_policy(self.permission_mode)
        }
        if self.model:
            params["model"] = self.model
        if self.cwd:
            params["cwd"] = self.cwd
        params.update(_translate_model_params(self.model_params))
        return params

    async def disconnect(self) -> None:
        await self.transport.close()


class AppServerCodexClient(TokenEstimateMixin):
    """``BackendClient`` for Codex over the direct app-server transport."""

    def __init__(self) -> None:
        self._auth_provider = AppServerAuthProvider()

    # -- descriptor surface ------------------------------------------------

    @property
    def name(self) -> str:
        return BACKEND_NAME

    def supported_models(self) -> List[str]:
        return configured_public_models()

    def get_auth_provider(self) -> AppServerAuthProvider:
        return self._auth_provider

    async def verify(self) -> bool:
        try:
            return bool(self._auth_provider.validate().get("valid"))
        except Exception:  # noqa: BLE001 - verify must never raise
            logger.warning("appserver codex verify() failed", exc_info=True)
            return False

    # -- client lifecycle --------------------------------------------------

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
        **_ignored: Any,
    ) -> AppServerSessionClient:
        """Spawn a dedicated app-server, handshake, and start/resume the thread.

        Per #165, a ``codex_thread_id`` is durable only after its first turn has
        completed, so ``thread/start`` here records the thread id on the *handle*
        (tied to this live process) but never on ``session.codex_thread_id`` --
        that marker is written only when a turn completes (see
        :meth:`run_completion_with_client`). An existing durable id therefore
        resumes; otherwise a fresh thread is started.
        """
        generation = getattr(session, "turn_counter", 0) + 1
        env = self._build_subprocess_env(extra_env)
        transport = AppServerTransport(
            app_server_argv(),
            generation=generation,
            cwd=cwd,
            env=env or None,
        )
        try:
            await transport.start()
            thread_params = self._thread_params(
                model=model,
                cwd=cwd,
                system_prompt=combine_system_prompt(_custom_base, system_prompt),
                permission_mode=permission_mode,
                mcp_servers=mcp_servers,
            )
            durable_thread_id = getattr(session, "codex_thread_id", None)
            if durable_thread_id:
                await transport.request(
                    "thread/resume",
                    {"threadId": durable_thread_id, **thread_params},
                    timeout=request_timeout_s(),
                )
                thread_id = str(durable_thread_id)
            else:
                result = await transport.request(
                    "thread/start",
                    {**thread_params, "serviceName": "oh-my-gateway"},
                    timeout=request_timeout_s(),
                )
                thread_id = self._thread_id_from_result(result)
        except BaseException:
            await transport.close()
            raise

        return AppServerSessionClient(
            transport=transport,
            thread_id=thread_id,
            generation=generation,
            model=model,
            cwd=cwd,
            permission_mode=permission_mode,
            allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
            disallowed_tools=(
                list(disallowed_tools) if disallowed_tools is not None else None
            ),
            model_params=dict(model_params) if model_params else None,
            mcp_servers=dict(mcp_servers) if mcp_servers else None,
        )

    async def run_completion_with_client(
        self,
        client: AppServerSessionClient,
        prompt: Union[str, List[Dict[str, Any]]],
        session: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Run one turn and yield canonical chunks. Never raises -- errors are
        surfaced as in-band ``error_chunk``/``_gateway_interrupt_chunk``.
        """
        # Subscribe BEFORE issuing turn/start so the transport's "no
        # registration window" guarantee covers every turn notification, and so
        # an OrphanedResponse for our turn/start could still be reconciled. The
        # subscription is stored on the handle and PERSISTS across an
        # AskUserQuestion pause; it is torn down only when the turn terminalizes.
        subscription = client.transport.subscribe()
        client._subscription = subscription
        try:
            turn_params = {
                "threadId": client.thread_id,
                "input": _to_turn_input(prompt),
                **client.turn_params(),
            }
            try:
                result = await client.transport.request(
                    "turn/start", turn_params, timeout=request_timeout_s()
                )
            except RuntimeLost as exc:
                self._end_turn(client)
                yield error_chunk(f"Codex runtime lost: {exc.reason}")
                return
            except RpcError as exc:
                self._end_turn(client)
                yield error_chunk(f"Codex turn/start failed: {exc.rpc_message}")
                return
            except Exception as exc:  # noqa: BLE001 - stay in-band, never raise
                self._end_turn(client)
                yield error_chunk(f"Codex turn/start error: {exc}")
                return

            turn_id = self._turn_id_from_result(result)
            client.current_turn_id = turn_id
            client._mapper = TurnMapper(thread_id=client.thread_id, turn_id=turn_id)

            async for chunk in self._drive_turn(client, session):
                yield chunk
        except BaseException:
            # The route calls ``aclose()`` on this generator right after a park
            # to end the requires_action stream; that GeneratorExit must NOT tear
            # down the still-live turn (its subscription is needed by the
            # resume). Only a genuine error while NOT parked terminalizes.
            if client.pending_interaction_id is None:
                self._end_turn(client)
            raise

    async def resume_approval_with_client(
        self,
        client: AppServerSessionClient,
        call_id: str,
        output: str,
        session: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Answer a parked interaction and continue the SAME turn (issue §3).

        The canonical ``call_id`` must resolve to exactly one live native
        request; a mismatch, an upstream-resolved/invalidated interaction, a
        stale/duplicate answer, or a dead runtime is a deterministic error that
        is never forwarded to a replacement runtime.
        """
        interaction_id = client.pending_interaction_id
        method = client.pending_interaction_method or ""
        params = client.pending_interaction_params or {}
        self._clear_pending_interaction(client)

        if interaction_id is None:
            yield error_chunk("Codex interaction continuation has no pending request")
            self._end_turn(client)
            return
        if str(interaction_id) != str(call_id):
            yield error_chunk(
                f"Codex interaction id mismatch: pending {interaction_id!r}, "
                f"received {call_id!r}"
            )
            self._end_turn(client)
            return
        if client._subscription is None:
            # The turn's live stream is gone (runtime lost during the pause);
            # never forward the answer to a replacement runtime.
            yield error_chunk("Codex interaction is no longer live; please retry")
            return

        result = answer_result_from_output(method, output, params)
        try:
            await client.transport.answer(
                interaction_id, result, generation=client.generation
            )
        except StaleAnswer:
            # Retired upstream (serverRequest/resolved), wrong generation, or a
            # late/double answer -- the transport already fenced it.
            yield error_chunk("Codex interaction is no longer actionable")
            self._end_turn(client)
            return
        except RuntimeLost as exc:
            yield error_chunk(f"Codex runtime lost: {exc.reason}")
            self._end_turn(client)
            return

        try:
            async for chunk in self._drive_turn(client, session):
                yield chunk
        except BaseException:
            # As in run_completion: a post-park aclose must not terminalize a
            # turn that parked again on a chained interaction.
            if client.pending_interaction_id is None:
                self._end_turn(client)
            raise

    async def _drive_turn(
        self, client: AppServerSessionClient, session: Any
    ) -> AsyncIterator[Dict[str, Any]]:
        """Pump the handle's live subscription until the turn terminalizes or
        parks on a human interaction. On a true terminal the subscription is
        torn down; on a park it stays alive for ``resume_approval_with_client``.
        """
        subscription = client._subscription
        mapper = client._mapper
        turn_id = client.current_turn_id
        if subscription is None or mapper is None:
            return

        while True:
            cancelling = getattr(session, "active_response_state", None) == "cancelling"
            try:
                item = await asyncio.wait_for(
                    subscription.get(), timeout=request_timeout_s()
                )
            except (TimeoutError, asyncio.TimeoutError):
                for chunk in mapper.drain_open_subagents(
                    "killed" if cancelling else "failed"
                ):
                    yield chunk
                self._end_turn(client)
                if cancelling:
                    yield _gateway_interrupt_chunk("Codex turn interrupted")
                else:
                    yield error_chunk("Codex turn stalled (no output)")
                return

            if isinstance(item, Notification):
                # A terminal turn/completed while cancelling becomes an
                # incomplete (user_cancelled) instead of a normal completion.
                if cancelling and self._is_turn_completed(item, turn_id):
                    for chunk in mapper.drain_open_subagents("killed"):
                        yield chunk
                    self._end_turn(client)
                    yield _gateway_interrupt_chunk("Codex turn interrupted")
                    return
                for chunk in mapper.map_notification(item.method, item.params):
                    yield chunk
                if mapper.finished:
                    # Record the thread as durable only on a clean completion
                    # (#165); a failed turn must not seed a resume marker.
                    if mapper.succeeded:
                        setattr(session, "codex_thread_id", client.thread_id)
                    self._end_turn(client)
                    return
                continue

            if isinstance(item, PendingInteraction):
                # Bridge the native server request into the existing
                # AskUserQuestion / approval UX and PARK: set pending_tool_call,
                # emit the codex_approval tool_use chunk the route recognizes,
                # then stop yielding. The subscription is intentionally kept
                # alive so the resume continues this same turn.
                yield self._park_interaction(client, session, item)
                return

            if isinstance(item, OrphanedResponse):
                # Our turn/start is awaited synchronously above, so it does not
                # orphan; a stray orphan is logged and ignored.
                logger.debug("appserver codex orphaned response: %s", item.method)
                continue

            if isinstance(item, TerminalEvent):
                # Parent runtime loss terminalizes every open child row.
                for chunk in mapper.drain_open_subagents(
                    "killed" if cancelling else "failed"
                ):
                    yield chunk
                self._end_turn(client)
                if cancelling:
                    yield _gateway_interrupt_chunk("Codex turn interrupted")
                else:
                    yield error_chunk(f"Codex runtime terminated: {item.reason}")
                return

            if isinstance(item, SubscriberOverflow):
                for chunk in mapper.drain_open_subagents("failed"):
                    yield chunk
                self._end_turn(client)
                yield error_chunk("Codex event stream overflowed")
                return

    def _park_interaction(
        self,
        client: AppServerSessionClient,
        session: Any,
        interaction: PendingInteraction,
    ) -> Dict[str, Any]:
        """Record the parked interaction and build the codex_approval chunk.

        The chunk carries a ``tool_use`` block named ``codex_approval`` whose
        metadata id equals ``pending_tool_call.call_id``; the route's
        ``_is_codex_pending_approval_chunk`` detects that and pauses the stream
        into a ``requires_action`` response.
        """
        request_id = str(interaction.id)
        params = interaction.params if isinstance(interaction.params, dict) else {}
        arguments = interaction_arguments(interaction.method, params)

        client.pending_interaction_id = interaction.id
        client.pending_interaction_method = interaction.method
        client.pending_interaction_params = params
        session.pending_tool_call = {
            "call_id": request_id,
            "name": ASK_USER_QUESTION_TOOL_NAME,
            "arguments": arguments,
            "backend": BACKEND_NAME,
            "codex_resume": "approval",
        }
        tool_block = {
            "type": "tool_use",
            "id": request_id,
            "name": "codex_approval",
            "input": arguments,
            "metadata": {
                "codex_approval_request_id": request_id,
                "codex_approval_method": interaction.method,
                "codex_thread_id": str(client.thread_id or ""),
                "codex_turn_id": str(client.current_turn_id or ""),
            },
        }
        return {"type": "assistant", "content": [tool_block]}

    def _clear_pending_interaction(self, client: AppServerSessionClient) -> None:
        client.pending_interaction_id = None
        client.pending_interaction_method = None
        client.pending_interaction_params = None

    def _end_turn(self, client: AppServerSessionClient) -> None:
        """Tear down the per-turn transport view (only at a true terminal)."""
        client.current_turn_id = None
        self._clear_pending_interaction(client)
        subscription = client._subscription
        client._subscription = None
        client._mapper = None
        if subscription is not None:
            with contextlib.suppress(Exception):
                client.transport.unsubscribe(subscription)

    # -- continuation / cancellation --------------------------------------

    async def interrupt_client(self, client: AppServerSessionClient) -> None:
        """Interrupt the live turn (backing ``POST /v1/responses/{id}/cancel``)."""
        turn_id = client.current_turn_id
        if not turn_id:
            return
        try:
            await client.transport.interrupt(
                client.thread_id, turn_id, timeout=_INTERRUPT_TIMEOUT_S
            )
        except (RuntimeLost, RpcError, StaleAnswer, TimeoutError, asyncio.TimeoutError):
            pass

    # -- non-streaming / background ---------------------------------------

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

    # -- helpers -----------------------------------------------------------

    def _build_subprocess_env(
        self, extra_env: Optional[Dict[str, str]]
    ) -> Dict[str, str]:
        env = dict(self._auth_provider.build_env())
        if extra_env:
            from src.constants import METADATA_ENV_ALLOWLIST

            for key, value in extra_env.items():
                if key in METADATA_ENV_ALLOWLIST and isinstance(value, str):
                    env[key] = value
        return env

    def _thread_params(
        self,
        *,
        model: Optional[str],
        cwd: Optional[str],
        system_prompt: Optional[str],
        permission_mode: Optional[str],
        mcp_servers: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "approvalPolicy": _resolve_approval_policy(permission_mode),
            "sandbox": sandbox_mode(),
        }
        if model:
            params["model"] = model
        if cwd:
            params["cwd"] = cwd
        if system_prompt:
            params["developerInstructions"] = system_prompt
        if mcp_servers:
            params["mcpServers"] = dict(mcp_servers)
        return params

    def _thread_id_from_result(self, result: Any) -> str:
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or not thread.get("id"):
            raise RuntimeError("thread/start response missing thread.id")
        return str(thread["id"])

    def _turn_id_from_result(self, result: Any) -> Optional[str]:
        turn = result.get("turn") if isinstance(result, dict) else None
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        return None

    def _is_turn_completed(self, item: Notification, turn_id: Optional[str]) -> bool:
        if item.method != "turn/completed":
            return False
        params = item.params if isinstance(item.params, dict) else {}
        notification_turn_id = params.get("turnId")
        turn = params.get("turn")
        if isinstance(turn, dict):
            notification_turn_id = turn.get("id") or notification_turn_id
        return turn_id is None or notification_turn_id in (None, turn_id)


# ---------------------------------------------------------------------------
# Descriptor / registration (not wired into discover_backends until PR E)
# ---------------------------------------------------------------------------


def _resolve_model(model: str) -> Optional[ResolvedModel]:
    if model in configured_public_models():
        return ResolvedModel(
            public_model=model,
            backend=BACKEND_NAME,
            provider_model=model[len("codex/") :],
        )
    if model.startswith("codex/"):
        provider = model[len("codex/") :]
        if provider:
            return ResolvedModel(
                public_model=model, backend=BACKEND_NAME, provider_model=provider
            )
    return None


DESCRIPTOR = BackendDescriptor(
    name=BACKEND_NAME,
    owned_by="openai",
    models=configured_public_models(),
    resolve_fn=_resolve_model,
    capabilities={"image_input": True},
)


def register(registry_cls: Any) -> None:
    """Register the descriptor and (if the binary is available) the client.

    Mirrors the other backends' ``register`` shape. Client construction failure
    is swallowed so ``/v1/models`` and auth status still work. NOTE: this is not
    called by ``discover_backends`` yet -- the ``BACKENDS=codex`` cutover onto
    this adapter is PR E.
    """
    registry_cls.register_descriptor(DESCRIPTOR)
    try:
        registry_cls.register(BACKEND_NAME, AppServerCodexClient())
    except Exception:  # noqa: BLE001 - keep model/auth listing working
        logger.warning("appserver codex client construction failed", exc_info=True)
