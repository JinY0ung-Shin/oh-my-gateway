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
import uuid
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
from src.backends.appserver.isolation import (
    ISOLATION_ENV_REMOVE,
    build_isolated_env,
    child_env_allowlist,
)
from src.backends.appserver.policy import (
    CapabilityError,
    resolve_runtime_policy,
    should_auto_accept_approval,
    should_auto_deny_approval,
)
from src.backends.claude.client import UnsupportedContinuationPolicy
from src.backends.appserver.transport import (
    AmbiguousRequest,
    AppServerTransport,
    CommittedRequestCancelled,
    Notification,
    OrphanedResponse,
    PendingInteraction,
    RequestOutcomeUnknown,
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

# Gateway ``model_params`` keys the adapter can PROVE map onto a real current-v2
# ``TurnStartParams`` field. This is deliberately EMPTY: the OpenAI-style sampling
# controls the Responses route forwards (``temperature`` / ``top_p`` /
# ``max_output_tokens``) are NOT fields in the current generated v2
# ``TurnStartParams`` (which exposes ``model`` / ``effort`` / ``serviceTier`` /
# ``sandboxPolicy`` / ``summary`` / ... instead), so none of them is promised to
# be honored by the app-server. Writing them into ``turn/start`` only looked safe
# because the fake server accepts unknown fields. Until a canonical product
# contract maps onto real v2 turn fields (checkpoint-2), the adapter supports NO
# model params and fails closed on any that are requested rather than silently
# dropping them (#174 review, blocker 1).
_SUPPORTED_MODEL_PARAM_KEYS: frozenset = frozenset()


def _validate_model_params(model_params: Optional[Dict[str, Any]]) -> None:
    """Fail closed on model params the adapter cannot map to current v2 turn/start.

    No OpenAI sampling control is a proven current-v2 ``TurnStartParams`` field,
    so any non-empty ``model_params`` is rejected explicitly (never silently
    dropped or written as an unpromised field). ``reasoning.effort`` is already
    rejected for non-claude backends at the route; a future checkpoint that
    exposes Codex-native ``effort`` / ``serviceTier`` must map them deliberately
    from a canonical product contract, not reinterpret OpenAI sampling fields.
    """
    if not model_params:
        return
    unsupported = sorted(
        key for key in model_params if key not in _SUPPORTED_MODEL_PARAM_KEYS
    )
    if unsupported:
        raise CapabilityError(
            "Codex adapter does not support these model params (no current-v2 "
            f"TurnStartParams field maps to them): {unsupported}. Use the claude "
            "backend for sampling controls."
        )


def _to_turn_input(prompt: Union[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Shape a gateway prompt into Codex ``turn/start`` input items.

    A plain string becomes a single text item; a list of native Codex turn-input
    items (multimodal, from the route's ``codex`` branch) is forwarded verbatim.
    """
    if isinstance(prompt, list):
        return prompt
    return [{"type": "text", "text": prompt}]


def _card_is_completable(arguments: Dict[str, Any]) -> bool:
    """Whether the current AskUserQuestion card can actually be submitted.

    The renderer (``AskUserQuestionBlock.svelte``) shows only option buttons and
    its ``complete`` predicate requires at least one selected option per
    question. A question with no options (a free-text / secret / "Other" native
    ``requestUserInput``) would render a card with no answerable control, so such
    an interaction is not completable and must be failed closed (#174 review §1).
    """
    questions = arguments.get("questions")
    if not isinstance(questions, list) or not questions:
        return False
    for question in questions:
        options = question.get("options") if isinstance(question, dict) else None
        if not isinstance(options, list) or not options:
            return False
    return True


def _gateway_interrupt_chunk(message: str) -> Dict[str, Any]:
    """The terminal chunk the route maps to ``response.incomplete``."""
    return {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "error_message": message,
        "gateway_interrupted": True,
    }


def _rpc_error_message(error: Any) -> str:
    """Human-readable message from a JSON-RPC error object (or its repr)."""
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


# Last-resort wall-clock bound for OWNING a committed-but-unresolved
# ``turn/start`` (a generous multiple of the per-request timeout). It is NOT an
# abandon deadline: once ``turn/start`` is known committed there is no safe state
# "live generation + unresolved orphan + no owner", so on expiry the owner
# DELIBERATELY terminalizes the generation and keeps ownership until the accepted
# request is classified (see ``_await_turn_start_outcome``). A merely-slow
# response is recovered well within it, never terminalized (#174 blocker: the
# second local timeout must not abandon accepted work while the runtime is live).
_RECONCILE_HEALTH_MULTIPLIER = 4


def _reconcile_health_bound_s() -> float:
    return request_timeout_s() * _RECONCILE_HEALTH_MULTIPLIER


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
        approval_policy: str,
        sandbox: str,
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
        # Resolved once at create time (issue §7): the turn reuses the same
        # approval/sandbox policy the thread was started under.
        self.approval_policy = approval_policy
        self.sandbox = sandbox
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
        # The interaction currently parked at requires_action (issue §3). The
        # UI only ever sees ``pending_interaction_call_id`` -- an OPAQUE per-
        # occurrence id, NOT the native JSON-RPC request id, which a later
        # interaction could reuse. The adapter resolves that canonical id back
        # to the exact native request + occurrence token here; a stale card for
        # a retired occurrence can never match a new interaction. ``None`` when
        # nothing is pending.
        self.pending_interaction_call_id: Optional[str] = None
        self.pending_interaction_native_id: Any = None
        self.pending_interaction_method: Optional[str] = None
        self.pending_interaction_params: Optional[Dict[str, Any]] = None
        self.pending_interaction_token: Optional[str] = None
        self.pending_interaction_generation: Optional[int] = None
        # A detached, handle-owned task that reconciles a ``turn/start`` orphaned
        # by a post-commit CALLER cancellation (the SSE generator was cancelled
        # by a client disconnect). Its lifetime is independent of that cancelled
        # generator; ``disconnect()`` coordinates with it (#174 blocker: owner
        # liveness). ``None`` when no orphan is being owned.
        self._reconcile_task: Optional["asyncio.Task[Any]"] = None

    def turn_params(self) -> Dict[str, Any]:
        """``turn/start`` params (minus ``threadId``/``input``) for this handle.

        No ``model_params`` are emitted: none of the OpenAI sampling controls map
        to a current-v2 ``TurnStartParams`` field, so any request carrying them is
        already refused at create/continuation (#174 blocker 1) and never reaches
        a live handle.
        """
        params: Dict[str, Any] = {"approvalPolicy": self.approval_policy}
        if self.model:
            params["model"] = self.model
        if self.cwd:
            params["cwd"] = self.cwd
        return params

    async def disconnect(self) -> None:
        # Coordinate with the detached orphan-reconciliation owner instead of
        # destroying its evidence: close() terminalizes the generation (which
        # unblocks the owner with an AmbiguousRequest/TerminalEvent), then we
        # await the owner so it settles and releases its subscription rather than
        # being torn out from under mid-reconciliation.
        task = self._reconcile_task
        try:
            await self.transport.close()
        finally:
            if task is not None and not task.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=_INTERRUPT_TIMEOUT_S
                    )


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
        forward_headers: Optional[Dict[str, str]] = None,
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

        # Resolve the canonical capability policy BEFORE spawning: an explicit
        # allow-list, or a requested deny that no Codex setting can enforce
        # (shell.execute, Read, Skill, Task/Agent, mcp__*, ...), raises
        # CapabilityError here (#174 §B1/§B2), so session creation fails closed
        # (HTTP 503) rather than running with a requested constraint silently
        # ignored.
        runtime_policy = resolve_runtime_policy(
            default_sandbox=sandbox_mode(),
            default_approval=approval_policy(),
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

        # §B5: reject unsupported model-param overrides rather than dropping them.
        _validate_model_params(model_params)

        # §B3: gateway-supplied MCP server configuration is NOT contract-proven
        # on this runtime -- the exact ThreadStartParams.config shape (dotted
        # ``mcp_servers.`` overrides vs a nested object) and per-server
        # enabled/disabled-tools filtering can only be certified against the
        # pinned app-server (checkpoint-2, #173). Rather than send an unverified
        # config that might silently expose unfiltered MCP tools, refuse the
        # session when MCP servers are requested.
        if mcp_servers:
            raise CapabilityError(
                "Codex adapter cannot yet prove gateway-supplied MCP server "
                "configuration is applied and per-tool filtered on the "
                "app-server runtime; refusing the session rather than exposing "
                "unverified MCP tools (checkpoint-2, #173). Remove mcp_servers "
                "or use the claude backend for MCP."
            )

        # Per-user isolation (issue §6): a dedicated process (C0), a per-user
        # workspace (cwd), a per-user CODEX_HOME, and a NON-INHERITING child
        # environment -- the child starts from an explicit allowlist plus the
        # injected per-user overrides, so unrelated gateway secrets never reach
        # a model-controlled child (env_remove stays as defense-in-depth).
        env = build_isolated_env(
            auth_env=self._auth_provider.build_env(),
            extra_env=extra_env,
            user=getattr(session, "user", None),
            session_id=getattr(session, "session_id", None),
            metadata_allowlist=self._metadata_allowlist(),
        )
        env_remove = ISOLATION_ENV_REMOVE | frozenset(
            self._auth_provider.get_isolation_vars()
        )
        transport = AppServerTransport(
            app_server_argv(),
            generation=generation,
            cwd=cwd,
            env=env or None,
            env_remove=env_remove,
            env_allowlist=child_env_allowlist(),
        )
        try:
            await transport.start()
            thread_params = self._thread_params(
                model=model,
                cwd=cwd,
                system_prompt=combine_system_prompt(_custom_base, system_prompt),
                runtime_policy=runtime_policy,
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
            approval_policy=runtime_policy["approvalPolicy"],
            sandbox=runtime_policy["sandbox"],
            allowed_tools=list(allowed_tools) if allowed_tools is not None else None,
            disallowed_tools=(
                list(disallowed_tools) if disallowed_tools is not None else None
            ),
            model_params=dict(model_params) if model_params else None,
            mcp_servers=None,
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
            except RequestOutcomeUnknown as exc:
                # turn/start bytes were accepted and the generation then died
                # before the response: accepted work with an unknown outcome
                # (#172). Retain the thread identity, reconcile against durable
                # state, and NEVER replay -- collapsing this into a generic
                # runtime-lost error (as ``except RuntimeLost`` would) discards
                # exactly the seam #172 built (#174 blocker 2).
                async for chunk in self._reconcile_committed_turn_start_loss(
                    client, exc.method, exc.terminal_reason
                ):
                    yield chunk
                return
            except RuntimeLost as exc:
                # Plain loss is known-not-sent: the bytes never crossed the wire,
                # so there is no accepted work to reconcile and ending here is safe.
                self._end_turn(client)
                yield error_chunk(f"Codex runtime lost before turn start: {exc.reason}")
                return
            except RpcError as exc:
                self._end_turn(client)
                yield error_chunk(f"Codex turn/start failed: {exc.rpc_message}")
                return
            except (TimeoutError, asyncio.TimeoutError):
                # The response deadline expired AFTER the bytes were accepted: the
                # transport has ORPHANED the accepted turn/start and owns its
                # outcome, delivering it on the persisted subscription (a late
                # OrphanedResponse, or an AmbiguousRequest before the
                # TerminalEvent). Do NOT unsubscribe -- drive the subscription to
                # reconcile that owned outcome rather than discarding it (#174
                # blocker 2). A committed turn/start is never re-sent.
                async for chunk in self._reconcile_orphaned_turn_start(client, session):
                    yield chunk
                return
            except CommittedRequestCancelled:
                # turn/start's bytes were committed and the caller was cancelled
                # (a client disconnect cancels the StreamingResponse generator;
                # see responses.py _shielded_stream_teardown). The transport
                # raises this for EVERY committed cancellation -- including the
                # response-beats-cancellation race where the OrphanedResponse is
                # already queued and the unresolved-orphan registry is empty -- so
                # ownership is transferred ATOMICALLY on the type, never inferred
                # from that registry (#174 round-6). Hand the orphan to a
                # DETACHED, handle-owned owner whose lifetime is independent of
                # this cancelled generator (it may immediately consume the queued
                # OrphanedResponse or wait for a pending one), then re-raise.
                self._detach_orphan_owner(client)
                raise
            except asyncio.CancelledError:
                # A plain cancellation is known-not-sent: turn/start's bytes never
                # crossed the wire, so there is no accepted work to reconcile.
                self._end_turn(client)
                raise
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
            if client.pending_interaction_call_id is None:
                self._end_turn(client)
            raise

    async def _reconcile_committed_turn_start_loss(
        self,
        client: AppServerSessionClient,
        method: str,
        terminal_reason: str,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Reconcile a ``turn/start`` whose bytes were accepted before the
        generation died (``RequestOutcomeUnknown``).

        The turn may or may not have been created; the outcome is genuinely
        unknown. Surface a deterministic reconciliation-required terminal that
        keeps the thread identity, never seed a durable id (the turn did not
        cleanly complete), and NEVER replay -- the durable-state reconciliation
        belongs to the supervisor, not a blind re-send here (#172 / #174 blocker
        2).
        """
        thread_id = client.thread_id
        self._end_turn(client)
        yield error_chunk(
            f"Codex {method} was accepted but its outcome is unknown after "
            f"runtime loss ({terminal_reason}); not replayed -- reconcile "
            f"against thread {thread_id}"
        )

    async def _await_turn_start_outcome(
        self,
        client: AppServerSessionClient,
        subscription: Subscription,
        *,
        health_bound_s: Optional[float],
    ) -> tuple:
        """Own a committed-but-unresolved ``turn/start`` subscription to a
        DEFINITIVE outcome, never abandoning a live generation.

        The bytes were accepted, so the transport owns the outcome and delivers
        it here (#172): a late ``OrphanedResponse`` for ``turn/start``, or -- if
        the generation ends first -- an ``AmbiguousRequest`` before the
        ``TerminalEvent``. Returns one of:

          ``("recovered", turn_id)`` | ``("failed", message)``
          | ``("ambiguous", reason)`` | ``("terminal", reason)``
          | ``("overflow", None)``

        ``health_bound_s`` is a LAST-RESORT wall-clock bound, never an abandon
        deadline: on expiry the generation is deliberately terminalized
        (``transport.close()``) and ownership is HELD until the accepted request
        is classified ambiguous -- it never merely unsubscribes from a still-live
        generation (#174 blocker: second-timeout orphan abandonment). ``None``
        waits without any local bound (the caller guarantees termination, e.g.
        ``disconnect()``).

        Pre-outcome notifications/interactions are dropped: current app-server
        returns the ``turn/start`` result before its turn notifications, so the
        outcome is observed first (reviewer's non-blocking ordering note).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + health_bound_s if health_bound_s is not None else None
        terminalized = False
        while True:
            if deadline is not None and not terminalized and loop.time() >= deadline:
                # Deliberately terminalize this generation, then keep ownership
                # until the orphan is classified (close() fans an AmbiguousRequest
                # before the TerminalEvent for the accepted request).
                terminalized = True
                deadline = None
                with contextlib.suppress(Exception):
                    await client.transport.close()
                continue
            try:
                if deadline is None:
                    item = await subscription.get()
                else:
                    item = await asyncio.wait_for(
                        subscription.get(), timeout=max(0.0, deadline - loop.time())
                    )
            except (TimeoutError, asyncio.TimeoutError):
                # Do NOT abandon: loop back so the health-bound branch above
                # terminalizes rather than releasing a live-generation orphan.
                continue

            if isinstance(item, OrphanedResponse) and item.method == "turn/start":
                if item.error is not None:
                    return ("failed", _rpc_error_message(item.error))
                return ("recovered", self._turn_id_from_result(item.result))
            if isinstance(item, AmbiguousRequest):
                return ("ambiguous", item.terminal_reason)
            if isinstance(item, TerminalEvent):
                return ("terminal", item.reason)
            if isinstance(item, SubscriberOverflow):
                return ("overflow", None)
            # A notification / interaction / other-method orphan before the
            # outcome cannot be mapped yet (no turn id); ignore and keep waiting.
            continue

    async def _reconcile_orphaned_turn_start(
        self, client: AppServerSessionClient, session: Any
    ) -> AsyncIterator[Dict[str, Any]]:
        """Reconcile a ``turn/start`` orphaned by a response-deadline expiry,
        with the caller still present (streaming into this generator).

        On a late ``OrphanedResponse`` the accepted turn was really created:
        adopt and stream it to completion (or interrupt/retire it if the caller
        is cancelling). An ambiguous/terminal outcome ends deterministically. A
        committed ``turn/start`` is never re-sent, and ownership is never
        abandoned on a wall-clock deadline while the generation is live.
        """
        subscription = client._subscription
        if subscription is None:
            yield error_chunk("Codex turn/start timed out")
            return

        outcome, detail = await self._await_turn_start_outcome(
            client, subscription, health_bound_s=_reconcile_health_bound_s()
        )
        cancelling = getattr(session, "active_response_state", None) == "cancelling"

        if outcome == "recovered":
            turn_id = detail
            if cancelling:
                await self._interrupt_turn(client, turn_id)
                self._end_turn(client)
                yield _gateway_interrupt_chunk("Codex turn interrupted")
                return
            # Adopt the accepted turn and stream it to completion. The turn/start
            # response precedes its turn notifications on the wire, so the mapper
            # opens on the response and sees the rest.
            client.current_turn_id = turn_id
            client._mapper = TurnMapper(thread_id=client.thread_id, turn_id=turn_id)
            async for chunk in self._drive_turn(client, session):
                yield chunk
            return

        self._end_turn(client)
        if outcome == "failed":
            yield error_chunk(f"Codex turn/start failed: {detail}")
        elif outcome == "ambiguous":
            yield error_chunk(
                f"Codex turn/start outcome is ambiguous (turn/start); not "
                f"replayed ({detail}) -- reconcile against thread "
                f"{client.thread_id}"
            )
        elif outcome == "overflow":
            yield error_chunk("Codex event stream overflowed")
        else:  # terminal
            yield error_chunk(
                f"Codex runtime terminated before turn/start completed: {detail}"
            )

    def _detach_orphan_owner(self, client: AppServerSessionClient) -> None:
        """Move ownership of a committed-but-unresolved ``turn/start`` off the
        (cancelled) generator and onto a detached, handle-owned task.

        The subscription is transferred to the task and cleared from the handle,
        so the generator's outer ``except BaseException`` cannot unsubscribe it
        out from under the owner; ``disconnect()`` awaits the task instead.
        """
        subscription = client._subscription
        client._subscription = None
        client._mapper = None
        client.current_turn_id = None
        self._clear_pending_interaction(client)
        if subscription is None:
            return
        client._reconcile_task = asyncio.get_running_loop().create_task(
            self._own_orphaned_turn_start(client, subscription),
            name="codex-orphan-turn-start-owner",
        )

    async def _own_orphaned_turn_start(
        self, client: AppServerSessionClient, subscription: Subscription
    ) -> None:
        """Detached owner for a ``turn/start`` orphaned by caller cancellation.

        Independent of the cancelled SSE generator: it drives the subscription to
        a definitive outcome, retires a recovered turn (the HTTP caller is gone,
        so the accepted turn must not run detached), observes an
        ambiguous/terminal outcome, never replays, and releases the subscription.
        ``disconnect()`` coordinates by closing the transport (which terminalizes
        and unblocks this owner) and then awaiting it.
        """
        try:
            # No local health bound here: the cancel path is always followed by
            # disconnect() (transport.close), which terminalizes and delivers a
            # definitive outcome; bounding here could terminalize a runtime the
            # teardown is about to close anyway.
            outcome, detail = await self._await_turn_start_outcome(
                client, subscription, health_bound_s=None
            )
            if outcome == "recovered":
                await self._interrupt_turn(client, detail)
        except Exception:  # noqa: BLE001 - a detached owner must never escape
            logger.debug("codex orphan turn/start owner failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                client.transport.unsubscribe(subscription)
            if client._reconcile_task is asyncio.current_task():
                client._reconcile_task = None

    async def _interrupt_turn(
        self, client: AppServerSessionClient, turn_id: Optional[str]
    ) -> None:
        """Best-effort interrupt of a specific turn id (never raises)."""
        if not turn_id:
            return
        try:
            await client.transport.interrupt(
                client.thread_id, turn_id, timeout=_INTERRUPT_TIMEOUT_S
            )
        except (RuntimeLost, RpcError, StaleAnswer, TimeoutError, asyncio.TimeoutError):
            pass

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
        canonical_id = client.pending_interaction_call_id
        native_id = client.pending_interaction_native_id
        method = client.pending_interaction_method or ""
        params = client.pending_interaction_params or {}
        token = client.pending_interaction_token
        self._clear_pending_interaction(client)

        if canonical_id is None:
            yield error_chunk("Codex interaction continuation has no pending request")
            self._end_turn(client)
            return
        # The UI submits the opaque canonical id; it must match the exact parked
        # occurrence. A stale card for a retired occurrence (even one whose
        # native id was later reused) never matches, so its answer can never
        # reach a replacement interaction.
        if str(canonical_id) != str(call_id):
            yield error_chunk(
                f"Codex interaction id mismatch: pending {canonical_id!r}, "
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
                native_id,
                result,
                generation=client.generation,
                token=token,
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
            if client.pending_interaction_call_id is None:
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
                # Tool-policy enforcement (frozen parity, #6): auto-decide the
                # approval BEFORE bridging to the user. A tool the policy does
                # not permit is auto-denied; acceptEdits auto-accepts file
                # changes. Only an interaction with no policy verdict is parked
                # into the AskUserQuestion / approval UX.
                if await self._auto_decide_interaction(client, item):
                    continue
                park_chunk = await self._park_interaction(client, session, item)
                if park_chunk is None:
                    # The interaction could not be represented as a completable
                    # card (e.g. a free-text/secret requestUserInput the current
                    # renderer can't answer); it was failed closed, so keep
                    # consuming the same turn instead of emitting a dead card.
                    continue
                yield park_chunk
                return

            if isinstance(item, OrphanedResponse):
                # A turn/start orphaned by a response-deadline expiry is consumed
                # by _reconcile_orphaned_turn_start BEFORE the turn is driven, so
                # any orphan reaching this loop is for a different in-flight
                # request (or a duplicate); log and ignore it.
                logger.debug("appserver codex orphaned response: %s", item.method)
                continue

            if isinstance(item, AmbiguousRequest):
                # A request whose bytes were accepted but never answered before
                # the generation ended. The work may or may not have happened;
                # never blindly replay -- surface a terminal error so the turn
                # ends deterministically (the transport also fans a TerminalEvent
                # right after, which is handled below/next).
                for chunk in mapper.drain_open_subagents("failed"):
                    yield chunk
                self._end_turn(client)
                yield error_chunk(
                    f"Codex request outcome is ambiguous ({item.method}); "
                    f"not replayed ({item.terminal_reason})"
                )
                return

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

    async def _auto_decide_interaction(
        self, client: AppServerSessionClient, interaction: PendingInteraction
    ) -> bool:
        """Auto-answer an approval by tool policy (frozen parity, #6).

        Returns True if the interaction was decided here (answered) and must NOT
        be bridged to the user; False to bridge it. A tool the policy forbids is
        auto-denied; ``acceptEdits`` auto-accepts file-change approvals.
        """
        method = interaction.method
        params = interaction.params if isinstance(interaction.params, dict) else {}
        deny = should_auto_deny_approval(
            method,
            params,
            allowed_tools=client.allowed_tools,
            disallowed_tools=client.disallowed_tools,
        )
        accept = (not deny) and should_auto_accept_approval(
            method, permission_mode=client.permission_mode
        )
        if not deny and not accept:
            return False
        if method == "item/permissions/requestApproval":
            result: Dict[str, Any] = {"permissions": {}, "scope": "turn"}
        else:
            result = {"decision": "accept" if accept else "decline"}
        try:
            await client.transport.answer(
                interaction.id,
                result,
                generation=client.generation,
                token=interaction.token,
            )
        except (StaleAnswer, RuntimeLost):
            pass
        return True

    async def _park_interaction(
        self,
        client: AppServerSessionClient,
        session: Any,
        interaction: PendingInteraction,
    ) -> Optional[Dict[str, Any]]:
        """Park an interaction as an AskUserQuestion card, or fail it closed.

        Returns the ``codex_approval`` chunk the route detects
        (``_is_codex_pending_approval_chunk``) to pause into ``requires_action``,
        or ``None`` when the interaction cannot be represented as a card the
        current renderer can complete (#174 review §1): rather than emit an
        apparently-actionable card with no answerable control, the interaction is
        failed closed so the turn ends deterministically.

        The UI-facing ``call_id`` is an OPAQUE per-occurrence id, never the native
        JSON-RPC request id (#174 review §3), so a stale card can never be matched
        against a later interaction that reused the native id.
        """
        params = interaction.params if isinstance(interaction.params, dict) else {}
        arguments = interaction_arguments(interaction.method, params)

        if not _card_is_completable(arguments):
            # The current AskUserQuestion card only renders option buttons and
            # requires a selection per question; a free-text / secret / "Other"
            # / optionless question would render an un-submittable card. Fail
            # closed instead (a canonical free-text/secret capability is tracked
            # as review §1 follow-up).
            await self._fail_interaction_closed(
                client,
                interaction,
                "interaction form not supported by the current UI",
            )
            return None

        canonical_id = uuid.uuid4().hex
        client.pending_interaction_call_id = canonical_id
        client.pending_interaction_native_id = interaction.id
        client.pending_interaction_method = interaction.method
        client.pending_interaction_params = params
        client.pending_interaction_token = interaction.token
        client.pending_interaction_generation = interaction.generation
        session.pending_tool_call = {
            "call_id": canonical_id,
            "name": ASK_USER_QUESTION_TOOL_NAME,
            "arguments": arguments,
            "backend": BACKEND_NAME,
            "codex_resume": "approval",
        }
        tool_block = {
            "type": "tool_use",
            "id": canonical_id,
            "name": "codex_approval",
            "input": arguments,
            "metadata": {
                # The canonical (opaque) id the route/UI key on. The native
                # request id and occurrence token stay private to the adapter.
                "codex_approval_request_id": canonical_id,
                "codex_approval_method": interaction.method,
                "codex_thread_id": str(client.thread_id or ""),
                "codex_turn_id": str(client.current_turn_id or ""),
            },
        }
        return {"type": "assistant", "content": [tool_block]}

    async def _fail_interaction_closed(
        self,
        client: AppServerSessionClient,
        interaction: PendingInteraction,
        message: str,
    ) -> None:
        try:
            await client.transport.fail_interaction(
                interaction.id,
                generation=client.generation,
                token=interaction.token,
                message=message,
            )
        except (StaleAnswer, RuntimeLost):
            pass

    def _clear_pending_interaction(self, client: AppServerSessionClient) -> None:
        client.pending_interaction_call_id = None
        client.pending_interaction_native_id = None
        client.pending_interaction_method = None
        client.pending_interaction_params = None
        client.pending_interaction_token = None
        client.pending_interaction_generation = None

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
        await self._interrupt_turn(client, client.current_turn_id)

    def update_request_policy(
        self,
        client: AppServerSessionClient,
        *,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        model_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Apply a continuation turn's policy to the reused handle (#174 §6).

        The gateway reuses ``session.client`` across turns, so without this the
        first turn's ``allowed_tools``/``disallowed_tools``/``permission_mode``/
        ``model_params`` would stick and a continuation that tightens policy would
        silently run under the old one. Fail closed (``UnsupportedContinuationPolicy``
        -> HTTP 400) for any change that cannot be applied safely to a live
        thread: a deny the runtime can't enforce, or a sandbox change (the
        sandbox is fixed at ``thread/start`` and cannot be tightened mid-thread).
        """
        # Validate the WHOLE requested policy through the same fail-closed
        # capability validator (#174 §B4) BEFORE mutating any handle field, so a
        # continuation that tightens to an unenforceable policy is rejected with
        # the handle's previously stored policy left untouched.
        try:
            new_policy = resolve_runtime_policy(
                default_sandbox=sandbox_mode(),
                default_approval=approval_policy(),
                permission_mode=permission_mode,
                allowed_tools=allowed_tools,
                disallowed_tools=disallowed_tools,
            )
            _validate_model_params(model_params)
        except CapabilityError as exc:
            raise UnsupportedContinuationPolicy(str(exc)) from exc

        if new_policy["sandbox"] != client.sandbox:
            raise UnsupportedContinuationPolicy(
                "Codex sandbox cannot change mid-session (it is fixed at "
                f"thread start: {client.sandbox!r} -> {new_policy['sandbox']!r}); "
                "start a new session to change it"
            )

        client.allowed_tools = (
            list(allowed_tools) if allowed_tools is not None else None
        )
        client.disallowed_tools = (
            list(disallowed_tools) if disallowed_tools is not None else None
        )
        client.permission_mode = permission_mode
        client.model_params = dict(model_params) if model_params else None
        client.approval_policy = new_policy["approvalPolicy"]

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

    def _metadata_allowlist(self) -> frozenset:
        from src.constants import METADATA_ENV_ALLOWLIST

        return frozenset(METADATA_ENV_ALLOWLIST)

    def _thread_params(
        self,
        *,
        model: Optional[str],
        cwd: Optional[str],
        system_prompt: Optional[str],
        runtime_policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        # MCP configuration is deliberately NOT emitted here: gateway-supplied
        # MCP servers are rejected at create_client (#174 §B3) until the exact
        # app-server ``config`` shape and per-tool filtering are certified on the
        # pinned runtime (checkpoint-2). No unverified config surface is sent.
        params: Dict[str, Any] = {
            "approvalPolicy": runtime_policy["approvalPolicy"],
            "sandbox": runtime_policy["sandbox"],
        }
        if model:
            params["model"] = model
        if cwd:
            params["cwd"] = cwd
        if system_prompt:
            params["developerInstructions"] = system_prompt
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
