"""Direct stdio JSON-RPC transport for ``codex app-server`` (C0 core; #163, #170).

This is the topology-agnostic primitive selected after the public Python SDK
failed the B0 hard-correctness fixtures (#166). It owns exactly one process and
nothing else:

- exactly ONE stdout reader per process; server requests are dispatched to
  their own tasks so a parked human interaction never blocks the reader
- serialized stdin writes
- JSON-RPC request id -> waiter future
- notification fanout to every subscriber, in arrival order, with no
  registration window (a notification written before or right after a
  response is delivered like any other)
- server request -> generation-bound ``PendingInteraction``; unsupported
  server requests are answered with a JSON-RPC error (fail-closed), never a
  permissive result
- EOF / parse error / process death -> bounded fanout: every waiter fails
  exactly once, every pending interaction is invalidated, every subscriber
  receives one ``TerminalEvent``
- late, wrong-generation, or wrong-OCCURRENCE interaction answers are
  rejected (``StaleAnswer``) and never reach the process: every answer carries
  the immutable token of the occurrence it answers, so a stale decision can
  never authorize a new server request that reused a settled JSON-RPC id
- interrupt routing
- process-group teardown (EOF -> SIGTERM -> SIGKILL) with descendant reap and
  an idempotent, deterministic ``close()`` report
- a bounded handshake: ``start()`` fails within ``initialize_timeout_s`` and
  tears the runtime down on any handshake failure before re-raising
- bounded writes: one absolute deadline covers write + response, and a
  runtime that stops reading stdin is terminalized (``write_timeout``) and
  torn down instead of pinning the caller
- transport-owned teardown on ANY unexpected loss: the process group is
  reaped (SIGTERM -> SIGKILL, bounded) without waiting for ``close()``
- transport-owned, exactly-once commit of interaction answers: a cancelled
  caller never leaves an interaction "answered" with nothing on the wire, and
  the WIRE is the boundary against ``serverRequest/resolved``: an answer that
  has not begun reaching the process loses to it and writes nothing
- a caller that leaves after its request's bytes were accepted (cancelled,
  or its response deadline expired after a clean send) does not release the
  writer mid-drain and does not lose the outcome: the transport finishes the
  write and fans the response out as ``OrphanedResponse`` -- whether it had
  already landed on the abandoned waiter or arrives later -- so the owner
  layer can reconcile a stateful call such as ``turn/start``; if the
  generation ends first, that request is surfaced ONCE as
  ``AmbiguousRequest`` (before the ``TerminalEvent``) -- accepted work with
  no observed outcome, to be reconciled against durable state, never replayed
- a live caller learns whether its request crossed the wire: generation loss
  before the bytes were accepted is a plain ``RuntimeLost`` (known not sent);
  loss after acceptance and before the response is ``RequestOutcomeUnknown``
  (accepted-work ambiguity carrying request id/method, to be reconciled)
- bounded subscribers: streaming deltas are droppable for a slow consumer,
  lossless items are not, and a consumer that falls too far behind on
  lossless items is disconnected instead of growing the process
- owner close owns every accepted answer: one whose bytes were not yet
  accepted is invalidated (``OWNER_CLOSED``) and writes nothing; one whose
  bytes were accepted settles before shutdown; ``close()`` awaits all of them
  (bounded) and reports unsettled interactions, not just pending ones
- ``close()`` is the admission barrier: from its first call no new request,
  notification, or answer is admitted (``RuntimeLost("closing")`` /
  ``StaleAnswer``, zero bytes), and concurrent closers share one teardown

Deliberately NOT here (supervisor policy, gated on #165's production-mount and
budget evidence): session<->process placement, pooling or sharding, capacity,
resume placement, blind replay, or any override of Codex's live writer
ownership. Nothing in this module reaches into the frozen ``src/backends/codex``
implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
)

logger = logging.getLogger(__name__)

# Server requests the gateway knows how to surface as interactions -- the
# certified subset for the C0 rich-interaction surface. The set is injectable
# per transport (``supported_server_requests=``) so a product layer can widen
# or narrow it; anything outside the active set is answered with a JSON-RPC
# "method not found" error. An unknown request must never be satisfied by
# accident.
DEFAULT_SUPPORTED_SERVER_REQUESTS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
        "item/tool/call",  # dynamic tool call
        "mcpServer/elicitation/request",
    }
)
SUPPORTED_SERVER_REQUESTS = (
    DEFAULT_SUPPORTED_SERVER_REQUESTS  # backwards-compatible alias
)

# Upstream retires a pending server request (turn interrupted, turn finished,
# request cleared) with this notification; the matching interaction must
# become non-actionable at that moment, independent of any human TTL.
SERVER_REQUEST_RESOLVED = "serverRequest/resolved"

JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_INTERNAL_ERROR = -32603

RUNTIME_LOST = "RUNTIME_LOST"
SERVER_RESOLVED = "SERVER_RESOLVED"
OWNER_CLOSED = "OWNER_CLOSED"

DEFAULT_CLIENT_INFO = {
    "name": "oh_my_gateway",
    "title": "Oh My Gateway",
    "version": "0",
}
DEFAULT_CAPABILITIES = {"experimentalApi": True}

# asyncio's StreamReader default line limit is 64 KiB; app-server item payloads
# (tool output, file diffs) can exceed that on one line.
STDOUT_LINE_LIMIT = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# errors and events
# ---------------------------------------------------------------------------


class TransportError(RuntimeError):
    """Base class for every error this transport raises."""


class RuntimeLost(TransportError):
    """The process behind this transport is gone (EOF, exit, protocol error, close).

    Raised to every waiter exactly once at the moment of loss, and to every
    later ``request``/``notify`` immediately -- a call after loss never blocks.
    """

    def __init__(
        self,
        reason: str,
        *,
        generation: int,
        exit_code: Optional[int],
        detail: str = "",
    ):
        super().__init__(
            f"app-server runtime lost ({reason}; generation={generation}; exit={exit_code}) {detail}".rstrip()
        )
        self.reason = reason
        self.generation = generation
        self.exit_code = exit_code
        self.detail = detail


class RequestOutcomeUnknown(RuntimeLost):
    """The generation ended after THIS request's bytes were accepted by the
    runtime and before its JSON-RPC response was observed. The server may have
    performed the work (e.g. created the turn). Raised to the caller that still
    owns the RPC in place of a plain ``RuntimeLost`` -- which is reserved for
    requests that never crossed the wire -- so a supervisor can branch: plain
    loss is known-not-sent; this is accepted-work ambiguity that must be
    reconciled against durable state, never blind-replayed.
    """

    def __init__(
        self,
        *,
        request_id: str,
        method: str,
        generation: int,
        terminal_reason: str,
        exit_code: Optional[int],
        detail: str = "",
    ):
        super().__init__(
            terminal_reason, generation=generation, exit_code=exit_code, detail=detail
        )
        self.request_id = request_id
        self.method = method
        self.terminal_reason = terminal_reason


class CommittedRequestCancelled(asyncio.CancelledError):
    """A committed request's caller was cancelled after the bytes crossed the wire.

    Raised in place of a plain ``CancelledError`` whenever the request's bytes
    were accepted before the caller was cancelled -- INCLUDING the
    response-beats-cancellation race, where the transport has already fanned the
    ``OrphanedResponse`` and no unresolved-orphan registry entry remains. The
    owner layer keys ownership transfer on THIS type (atomic, explicit), never on
    the current unresolved-orphan registry (which answers a different question:
    "is an outcome still pending?"). A plain ``CancelledError`` is known-not-sent.

    It subclasses ``asyncio.CancelledError`` so asyncio still treats the task as
    cancelled and the cancellation still propagates.
    """

    def __init__(self, *, request_id: str, method: str, generation: int):
        super().__init__(
            f"committed request cancelled ({method}; id={request_id}; "
            f"generation={generation})"
        )
        self.request_id = request_id
        self.method = method
        self.generation = generation


class RpcError(TransportError):
    """The process answered a request with a JSON-RPC error object."""

    def __init__(self, method: str, error: Any):
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error)
        super().__init__(f"{method}: {message} (code={code})")
        self.method = method
        self.code = code
        self.rpc_message = message
        self.data = error.get("data") if isinstance(error, dict) else None


class HandshakeError(TransportError):
    """``start()`` could not complete ``initialize``; the runtime was torn down.

    ``cause`` carries the underlying failure (``asyncio.TimeoutError`` for a
    live-but-silent child, ``RuntimeLost`` for one that died mid-handshake).
    An ``RpcError`` from ``initialize`` is re-raised as itself after cleanup.
    """

    def __init__(self, message: str, cause: BaseException):
        super().__init__(f"{message}: {cause!r}")
        self.cause = cause


class StaleAnswer(TransportError):
    """An interaction answer arrived for a generation, process, or interaction
    that can no longer accept it. The answer is dropped; nothing is written."""


@dataclass(frozen=True)
class Notification:
    method: str
    params: Any


@dataclass(frozen=True)
class TerminalEvent:
    """Delivered once to every subscriber when the runtime is lost."""

    reason: str  # eof | exit | protocol_error | write_failed | write_timeout | closed
    generation: int
    exit_code: Optional[int]
    detail: str = ""


@dataclass
class PendingInteraction:
    """A server request awaiting an answer from outside the transport.

    Bound to the process generation that raised it: an answer carrying a
    different generation, or arriving after the runtime was lost or the
    interaction already answered, is rejected as ``StaleAnswer``.
    """

    id: Any
    method: str
    params: Any
    generation: int
    # Immutable occurrence identity. Upstream may reuse a JSON-RPC server
    # request id after the previous occurrence settled, so `(generation, id)`
    # is NOT unique; every answer must present the token of the occurrence it
    # is answering and it is checked against the registry's current entry.
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.monotonic)
    state: str = (
        "pending"  # pending | resolving | answered | failed | resolved | invalidated
    )
    invalidation_reason: Optional[str] = None  # RUNTIME_LOST | SERVER_RESOLVED
    # Transport-owned commit of the response; set once the first answer is
    # accepted. Await it to learn the final state even if your own call was
    # cancelled.
    commit: Optional["asyncio.Future[None]"] = None
    # True from the instant the response's bytes begin reaching the process.
    # Before it, `serverRequest/resolved` still wins and the answer is dropped;
    # after it, the answer stands and a late `resolved` is only recorded.
    wire_committed: bool = False
    resolved_after_commit: bool = False

    @property
    def open(self) -> bool:
        """Answerable: exactly ``pending``. A ``resolving`` interaction already
        has an accepted answer and rejects a second one."""
        return self.state == "pending"

    @property
    def settled(self) -> bool:
        """No transport-owned work remains: neither awaiting an answer nor
        carrying an accepted answer that has not finished committing."""
        return self.state not in ("pending", "resolving")


@dataclass(frozen=True)
class OrphanedResponse:
    """A response to a request whose caller was cancelled AFTER its bytes had
    begun reaching the process. The transport completed the write (a partial
    line must never be left in the pipe), so the request was really sent; the
    owner layer sees the outcome here and reconciles (e.g. interrupts a turn
    it no longer wants)."""

    request_id: str
    method: str
    result: Any
    error: Any
    generation: int


@dataclass(frozen=True)
class AmbiguousRequest:
    """A request whose bytes were accepted by the runtime and whose caller had
    already left (cancelled, or response deadline expired) received NO JSON-RPC
    response before the generation ended. The work may or may not have
    happened. This is not an ordinary RPC failure: the owner must reconcile
    against durable state (read the thread, resume) and never blindly replay.
    Delivered exactly once, before the ``TerminalEvent``."""

    request_id: str
    method: str
    generation: int
    terminal_reason: str


@dataclass(frozen=True)
class SubscriberOverflow:
    """Delivered once to a subscriber that fell too far behind on lossless
    items; the subscription is disconnected and receives nothing further."""

    pending: int
    dropped_deltas: int
    generation: int


SubscriberItem = Union[
    Notification,
    PendingInteraction,
    TerminalEvent,
    SubscriberOverflow,
    OrphanedResponse,
    AmbiguousRequest,
]
InteractionHandler = Callable[[PendingInteraction], Awaitable[Dict[str, Any]]]


def is_best_effort(item: SubscriberItem) -> bool:
    """Streaming deltas may be dropped for a slow subscriber; everything else
    (lifecycle notifications, server requests, terminal events) is lossless."""
    if not isinstance(item, Notification):
        return False
    method = item.method
    return method.endswith("Delta") or method.endswith("/delta")


class Subscription:
    """One subscriber's bounded view of the transport's inbound stream.

    Reader progress never depends on a subscriber: enqueueing is non-blocking.
    Memory is bounded instead by policy -- best-effort deltas are dropped once
    ``max_pending`` items are waiting (counted in ``dropped_deltas``), and a
    subscriber that lets lossless items pile past ``hard_limit`` is
    disconnected with one ``SubscriberOverflow`` rather than allowed to grow
    the gateway process without bound.
    """

    def __init__(self, *, max_pending: int, hard_limit: int, generation: int) -> None:
        self.max_pending = max_pending
        self.hard_limit = hard_limit
        self.generation = generation
        self.dropped_deltas = 0
        self.disconnected = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def get(self) -> SubscriberItem:
        return await self._queue.get()

    def get_nowait(self) -> SubscriberItem:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def _offer(self, item: SubscriberItem) -> None:
        if self.disconnected:
            return
        pending = self._queue.qsize()
        if is_best_effort(item):
            if pending >= self.max_pending:
                self.dropped_deltas += 1
                return
        elif pending >= self.hard_limit:
            self.disconnected = True
            self._queue.put_nowait(
                SubscriberOverflow(
                    pending=pending,
                    dropped_deltas=self.dropped_deltas,
                    generation=self.generation,
                )
            )
            return
        self._queue.put_nowait(item)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class AppServerTransport:
    """One ``codex app-server --listen stdio://`` process, owned end to end."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        generation: int = 1,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Mapping[str, str]] = None,
        env_remove: Optional[Iterable[str]] = None,
        interaction_handler: Optional[InteractionHandler] = None,
        client_info: Optional[Dict[str, Any]] = None,
        capabilities: Optional[Dict[str, Any]] = None,
        supported_server_requests: Optional[Iterable[str]] = None,
        stderr_tail_lines: int = 400,
        exit_drain_grace_s: float = 2.0,
        initialize_timeout_s: float = 10.0,
        write_timeout_s: float = 10.0,
        loss_teardown_grace_s: float = 2.0,
    ) -> None:
        self.argv = list(argv)
        self.generation = generation
        self.supported_server_requests = frozenset(
            DEFAULT_SUPPORTED_SERVER_REQUESTS
            if supported_server_requests is None
            else supported_server_requests
        )
        self.exit_drain_grace_s = exit_drain_grace_s
        self.initialize_timeout_s = initialize_timeout_s
        self.write_timeout_s = write_timeout_s
        self.loss_teardown_grace_s = loss_teardown_grace_s
        self._reap_task: Optional[asyncio.Task] = None
        self._reaped: Optional[asyncio.Future] = None
        self.cwd = str(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        # Keys removed from the child's environment AFTER the os.environ merge.
        # An override cannot unset an inherited var, so isolation (e.g. keeping a
        # sibling backend's auth token out of a Codex subprocess) needs an
        # explicit removal step -- see start().
        self.env_remove = (
            frozenset(env_remove) if env_remove is not None else frozenset()
        )
        self.client_info = client_info or dict(DEFAULT_CLIENT_INFO)
        self.capabilities = capabilities or dict(DEFAULT_CAPABILITIES)
        self._interaction_handler = interaction_handler

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pgid: Optional[int] = None
        self._write_lock = asyncio.Lock()
        self._writer_since: Optional[float] = (
            None  # when the current lock holder took the writer
        )
        self._orphaned_requests: Dict[str, str] = (
            {}
        )  # request id -> method, caller gone after bytes began
        self._orphan_delivered: set[str] = (
            set()
        )  # ids whose orphan the reader already fanned out
        self._interaction_commits: set[asyncio.Task] = (
            set()
        )  # accepted answers not yet settled
        self._close_task: Optional[asyncio.Task] = None
        self._committed_writes: set[asyncio.Task] = set()
        self._waiters: Dict[str, asyncio.Future] = {}
        self._waiter_methods: Dict[str, str] = {}
        self._interactions: Dict[Any, PendingInteraction] = {}
        self._subscribers: List[Subscription] = []
        self._tasks: List[asyncio.Task] = []
        self._handler_tasks: set[asyncio.Task] = set()
        self._interaction_tasks: Dict[Any, asyncio.Task] = {}
        self._stdout_eof: Optional[asyncio.Event] = None
        self._stderr_tail: deque[str] = deque(maxlen=stderr_tail_lines)
        self._terminal: Optional[TerminalEvent] = None
        self._terminal_waiter: Optional[asyncio.Future] = None
        self._closing = False
        self._close_report: Optional[Dict[str, Any]] = None
        self.rejected_server_requests: List[Dict[str, Any]] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self, *, initialize: bool = True) -> None:
        """Spawn the process, start the single reader, run the handshake.

        The handshake is bounded by ``initialize_timeout_s`` (a live child that
        never answers ``initialize`` is the bootstrap form of "alive, no
        progress"), and any handshake failure -- timeout, JSON-RPC error,
        runtime loss, or cancellation of the caller -- tears the spawned
        process group and tasks down deterministically before re-raising, so a
        factory that never returns the handle never leaks the process.
        """
        if self._proc is not None:
            raise TransportError("transport already started")
        loop = asyncio.get_running_loop()
        self._terminal_waiter = loop.create_future()
        self._reaped = loop.create_future()
        self._stdout_eof = asyncio.Event()
        proc_env = os.environ.copy()
        if self.env:
            proc_env.update(self.env)
        for key in self.env_remove:
            proc_env.pop(key, None)
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=proc_env,
            start_new_session=True,  # own process group: teardown can reach every descendant
            limit=STDOUT_LINE_LIMIT,
        )
        with contextlib.suppress(OSError):
            self._pgid = os.getpgid(self._proc.pid)
        self._tasks = [
            asyncio.create_task(self._read_stdout(), name="appserver-stdout"),
            asyncio.create_task(self._read_stderr(), name="appserver-stderr"),
            asyncio.create_task(self._watch_exit(), name="appserver-exit"),
        ]
        if not initialize:
            return
        try:
            await self.request(
                "initialize",
                {"clientInfo": self.client_info, "capabilities": self.capabilities},
                timeout=self.initialize_timeout_s,
            )
            await self.notify("initialized", {})
        except BaseException as exc:  # noqa: BLE001 - cleanup, then re-raise
            await asyncio.shield(self.close(grace_s=1.0))
            if isinstance(exc, RpcError):
                raise
            if isinstance(exc, asyncio.TimeoutError):
                raise HandshakeError(
                    f"initialize not answered within {self.initialize_timeout_s}s", exc
                ) from exc
            if isinstance(exc, RuntimeLost):
                raise HandshakeError("runtime lost during initialize", exc) from exc
            raise

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._terminal is None
            and self._proc.returncode is None
        )

    @property
    def terminal(self) -> Optional[TerminalEvent]:
        return self._terminal

    @property
    def exit_code(self) -> Optional[int]:
        return self._proc.returncode if self._proc is not None else None

    async def wait_terminal(self, timeout: Optional[float] = None) -> TerminalEvent:
        if self._terminal is not None:
            return self._terminal
        assert self._terminal_waiter is not None, "transport not started"
        return await asyncio.wait_for(asyncio.shield(self._terminal_waiter), timeout)

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # -- outbound ------------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """Send a request and await its response.

        Raises ``RuntimeLost`` immediately if the runtime is already gone, or
        at the moment it goes while the request is in flight; ``RpcError`` for
        a JSON-RPC error response; ``asyncio.TimeoutError`` after ``timeout``.
        """
        self._raise_if_lost()
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        request_id = str(uuid.uuid4())
        future: asyncio.Future = loop.create_future()
        self._waiters[request_id] = future
        self._waiter_methods[request_id] = method
        committed = False
        orphaned_inline = False

        def _mark_committed() -> None:
            nonlocal committed
            committed = True

        try:
            # One absolute deadline covers the write AND the response: a live
            # runtime that stopped reading stdin cannot pin the caller before
            # its timeout even starts.
            await self._write(
                {"id": request_id, "method": method, "params": params or {}},
                deadline=deadline,
                on_committed=_mark_committed,
            )
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            result = await asyncio.wait_for(future, remaining)
            current = asyncio.current_task()
            if (
                committed
                and current is not None
                and getattr(current, "cancelling", lambda: 0)()
            ):
                # The response landed in the same instant the caller was being
                # cancelled; asyncio hands the result to a task that is leaving.
                # Honor the cancellation and own the outcome instead: exactly
                # one OrphanedResponse, never a result nobody reads.
                orphaned_inline = True
                self._fanout(
                    OrphanedResponse(
                        request_id=request_id,
                        method=method,
                        result=result,
                        error=None,
                        generation=self.generation,
                    )
                )
                raise asyncio.CancelledError()
            return result
        except RuntimeLost as exc:
            if committed and not isinstance(exc, RequestOutcomeUnknown):
                # Bytes were accepted, no response was observed, the generation
                # is gone: the live caller learns THAT, not a generic loss.
                raise RequestOutcomeUnknown(
                    request_id=request_id,
                    method=method,
                    generation=self.generation,
                    terminal_reason=exc.reason,
                    exit_code=exc.exit_code,
                    detail=exc.detail,
                ) from exc
            raise
        except asyncio.TimeoutError:
            if committed and not orphaned_inline:
                # The bytes were accepted before the response deadline expired
                # after a clean send: the request WAS sent, so the transport owns
                # its outcome. Ownership moves from the waiter to the orphan
                # registry atomically -- a response that already landed on the
                # waiter becomes an OrphanedResponse right now; a later one is
                # routed there.
                self._orphan(request_id, method, future)
            raise
        except asyncio.CancelledError as exc:
            if committed and not orphaned_inline:
                # Bytes accepted before the caller was cancelled: transfer
                # ownership to the orphan registry (a later response/terminal is
                # routed there).
                self._orphan(request_id, method, future)
            if committed:
                # EVERY committed cancellation is surfaced to the owner as a
                # CommittedRequestCancelled -- including the response-beats-
                # cancellation inline case, where the OrphanedResponse was already
                # fanned out and no unresolved-orphan registry entry remains. The
                # owner must transfer ownership atomically on THIS type, never by
                # inspecting the (possibly-empty) unresolved-orphan registry.
                raise CommittedRequestCancelled(
                    request_id=request_id,
                    method=method,
                    generation=self.generation,
                ) from exc
            raise
        finally:
            self._waiters.pop(request_id, None)
            self._waiter_methods.pop(request_id, None)
            if future.done() and not future.cancelled():
                # Terminalization may have failed this waiter while the write
                # itself was raising; mark that exception retrieved.
                future.exception()

    def _orphan(
        self, request_id: str, method: str, future: "asyncio.Future[Any]"
    ) -> None:
        """Transfer ownership of a sent request from its departed caller to the
        transport. Exactly one OrphanedResponse results, whether the response
        beat the caller's departure (already on the waiter) or arrives later."""
        self._waiters.pop(request_id, None)
        self._waiter_methods.pop(request_id, None)
        if request_id in self._orphan_delivered:
            # The reader already fanned this outcome out while we were leaving.
            self._orphan_delivered.discard(request_id)
            return
        if future.done() and not future.cancelled():
            error = None
            result = None
            exc = future.exception()
            if exc is None:
                result = future.result()
            elif isinstance(exc, RpcError):
                error = {"code": exc.code, "message": exc.rpc_message, "data": exc.data}
            else:
                # RuntimeLost while the caller was leaving: the request was
                # committed and the generation is gone -- ambiguous accepted
                # work, surfaced as such (the caller itself sees RuntimeLost).
                self._fanout(
                    AmbiguousRequest(
                        request_id=request_id,
                        method=method,
                        generation=self.generation,
                        terminal_reason=(
                            self._terminal.reason if self._terminal else "lost"
                        ),
                    )
                )
                return
            self._fanout(
                OrphanedResponse(
                    request_id=request_id,
                    method=method,
                    result=result,
                    error=error,
                    generation=self.generation,
                )
            )
            return
        if self._terminal is not None:
            # No response can arrive any more: terminal outcome right now.
            self._fanout(
                AmbiguousRequest(
                    request_id=request_id,
                    method=method,
                    generation=self.generation,
                    terminal_reason=self._terminal.reason,
                )
            )
            return
        self._orphaned_requests[request_id] = method

    async def notify(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> None:
        self._raise_if_lost()
        await self._write({"method": method, "params": params or {}})

    async def interrupt(
        self, thread_id: str, turn_id: str, *, timeout: Optional[float] = None
    ) -> Any:
        """Route ``turn/interrupt``; must progress even while an interaction is parked."""
        return await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            timeout=timeout,
        )

    # -- inbound fanout ------------------------------------------------------

    def subscribe(
        self, *, max_pending: int = 1000, hard_limit: Optional[int] = None
    ) -> Subscription:
        """Receive every notification and interaction in arrival order, then one
        ``TerminalEvent``.

        Enqueueing never blocks the reader. Memory is bounded by policy: once
        ``max_pending`` items wait, streaming deltas are dropped (lossless
        items still land); once lossless items alone exceed ``hard_limit``
        (default ``4 * max_pending``) the subscriber is disconnected with one
        ``SubscriberOverflow``. See ``Subscription``.
        """
        subscription = Subscription(
            max_pending=max_pending,
            hard_limit=hard_limit if hard_limit is not None else 4 * max_pending,
            generation=self.generation,
        )
        if self._terminal is not None:
            subscription._offer(self._terminal)
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.disconnected = True
        with contextlib.suppress(ValueError):
            self._subscribers.remove(subscription)

    # -- interactions --------------------------------------------------------

    @property
    def pending_interactions(self) -> List[PendingInteraction]:
        """Interactions still awaiting an answer."""
        return [i for i in self._interactions.values() if i.open]

    @property
    def unsettled_interactions(self) -> List[PendingInteraction]:
        """Interactions with transport-owned work outstanding: awaiting an
        answer, or carrying an accepted answer whose commit has not settled.
        This is what teardown must drive to zero."""
        return [i for i in self._interactions.values() if not i.settled]

    @property
    def pending_waiters(self) -> int:
        """Requests that have not yet been given a result or a terminal error.
        A waiter already failed by terminalization but not yet unwound by its
        (scheduled) caller task is settled, not pending."""
        return sum(1 for future in self._waiters.values() if not future.done())

    def interaction(self, interaction_id: Any) -> Optional[PendingInteraction]:
        return self._interactions.get(interaction_id)

    async def answer(
        self,
        interaction_id: Any,
        result: Dict[str, Any],
        *,
        generation: int,
        token: str,
    ) -> None:
        """Deliver a human/app decision for a server request.

        Fenced four ways: the transport must not be closing, the occurrence
        identified by ``(interaction_id, token)`` must be the registry's
        current entry and still open, its generation must match the caller's,
        and the runtime must still be alive. Otherwise ``StaleAnswer`` -- and
        nothing is written. The token is what makes a stale card or a late
        handler for an OLD occurrence unable to authorize a NEW server request
        that reused the same JSON-RPC id.
        """
        interaction = self._fence(interaction_id, generation, token)
        await self._commit(
            interaction, {"id": interaction.id, "result": result}, "answered"
        )

    async def fail_interaction(
        self,
        interaction_id: Any,
        *,
        generation: int,
        token: str,
        code: int = JSONRPC_INTERNAL_ERROR,
        message: str = "interaction failed",
    ) -> None:
        """Answer a server request with a JSON-RPC error (the fail-closed path)."""
        interaction = self._fence(interaction_id, generation, token)
        await self._commit(
            interaction,
            {"id": interaction.id, "error": {"code": code, "message": message}},
            "failed",
        )

    async def _commit(
        self, interaction: PendingInteraction, payload: Dict[str, Any], final_state: str
    ) -> None:
        """Transport-owned, exactly-once commit of an interaction's response.

        The caller (an HTTP continuation, a UI task) may be cancelled at any
        point; the ownership transition must not be half-cancelled. So the
        interaction moves to ``resolving`` synchronously (a concurrent second
        answer is already stale), the write runs in a task the transport owns,
        and the caller merely waits on a shield of it. Whatever happens to the
        caller, the commit task records the final state exactly once:
        ``answered``/``failed`` when the bytes are on the wire, ``invalidated``
        (RUNTIME_LOST) if the runtime was lost before they could be.
        """
        interaction.state = "resolving"
        commit = asyncio.ensure_future(
            self._commit_task(interaction, payload, final_state)
        )
        interaction.commit = commit
        # Owned by the transport from this instant: close() drives it to a
        # settled state before shutting the runtime down.
        self._interaction_commits.add(commit)
        commit.add_done_callback(self._interaction_commits.discard)
        await asyncio.shield(commit)

    async def _commit_task(
        self, interaction: PendingInteraction, payload: Dict[str, Any], final_state: str
    ) -> None:
        def _gate() -> None:
            # Runs under the writer lock, immediately before the first byte.
            # If upstream retired the interaction while this answer was queued,
            # nothing may be written; the wire, not the scheduling, decides.
            if interaction.state != "resolving":
                raise StaleAnswer(
                    f"interaction {interaction.id!r} was {interaction.state} "
                    f"({interaction.invalidation_reason}) before its answer reached the wire"
                )
            interaction.wire_committed = True

        try:
            await self._write(payload, before_write=_gate)
        except RuntimeLost as exc:
            if interaction.state == "resolving":
                # Not already retired by close()/terminalize: record why the
                # accepted answer never reached the wire.
                interaction.state = "invalidated"
                interaction.invalidation_reason = (
                    OWNER_CLOSED if exc.reason == "closing" else RUNTIME_LOST
                )
            raise StaleAnswer(
                f"runtime {exc.reason} before the response for {interaction.method} could be written"
            )
        interaction.state = final_state

    def _fence(
        self, interaction_id: Any, generation: int, token: str
    ) -> PendingInteraction:
        if self._closing and self._terminal is None:
            raise StaleAnswer(
                f"owner close in progress; answer for {interaction_id!r} not admitted"
            )
        interaction = self._interactions.get(interaction_id)
        if interaction is None:
            raise StaleAnswer(f"unknown interaction {interaction_id!r}")
        if interaction.token != token:
            # The registry's current occurrence of this id is not the one the
            # caller is answering: upstream reused the id after the caller's
            # occurrence settled. A stale decision must never authorize it.
            raise StaleAnswer(
                f"answer for a previous occurrence of interaction {interaction_id!r} rejected; "
                f"the id now belongs to a different server request"
            )
        if generation != self.generation or generation != interaction.generation:
            raise StaleAnswer(
                f"answer for generation {generation} rejected; interaction {interaction_id!r} "
                f"belongs to generation {interaction.generation} on transport generation {self.generation}"
            )
        if self._terminal is not None:
            raise StaleAnswer(
                f"runtime lost ({self._terminal.reason}); interaction {interaction_id!r} is {interaction.state}"
            )
        if not interaction.open:
            raise StaleAnswer(
                f"interaction {interaction_id!r} already {interaction.state}"
            )
        return interaction

    # -- teardown ------------------------------------------------------------

    async def close(self, *, grace_s: float = 2.0) -> Dict[str, Any]:
        """Deterministic, idempotent teardown -- and the admission barrier.

        From the first call, no new request/notify/answer is admitted (see
        ``_raise_if_lost`` / ``_fence``); pre-existing work is settled or
        retired; then stdin EOF -> SIGTERM (process group) -> SIGKILL (process
        group), each bounded by ``grace_s``; then every waiter/interaction/
        subscriber is terminalized (if the process had not already died) and
        the process group is checked for running descendants. Concurrent and
        repeated callers share ONE transport-owned close task and receive the
        same report; a cancelled closer does not abort the teardown.
        """
        if self._close_report is not None:
            return self._close_report
        if self._close_task is None:
            self._closing = True  # admission barrier: set before the first await
            self._close_task = asyncio.ensure_future(self._close_impl(grace_s))
        return await asyncio.shield(self._close_task)

    async def _close_impl(self, grace_s: float) -> Dict[str, Any]:
        proc = self._proc
        exit_code: Optional[int] = None
        # Policy for accepted answers at owner close (explicit, tested):
        #   * an answer whose bytes have NOT been accepted yet loses to the
        #     close -- it is invalidated (OWNER_CLOSED) and its commit task,
        #     which is still queued for the writer, finds the gate shut and
        #     writes nothing;
        #   * an answer whose bytes HAVE been accepted settles (drain completes
        #     or terminalizes) before the runtime is shut down.
        # Either way every accepted commit is awaited here, bounded, so no
        # interaction-owned task outlives close() and no late write can race
        # the closed runtime.
        self._retire_unsent_answers(OWNER_CLOSED)
        if self._interaction_commits:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._interaction_commits, return_exceptions=True),
                    self.write_timeout_s + 1.0,
                )
        if self._committed_writes:
            # A write that has begun finishes (or terminalizes) before teardown
            # starts pulling the process out from under it. Bounded: a committed
            # write is itself bounded by its drain deadline.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._committed_writes, return_exceptions=True),
                    self.write_timeout_s + 1.0,
                )
        if self._reap_task is not None:
            # An unexpected loss already started transport-owned teardown; let
            # it finish rather than racing two signal sequences.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(self._reap_task),
                    2 * self.loss_teardown_grace_s + 1.0,
                )
        if proc is not None:
            if proc.returncode is None and proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
                await self._wait_exit(proc, grace_s)
            if proc.returncode is None:
                self._signal_group(signal.SIGTERM)
                await self._wait_exit(proc, grace_s)
            if proc.returncode is None:
                self._signal_group(signal.SIGKILL)
                await self._wait_exit(proc, grace_s)
            exit_code = proc.returncode
        self._terminalize("closed", exit_code=exit_code, detail="closed by owner")
        for task in self._tasks:
            task.cancel()
        for task in list(self._handler_tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, *self._handler_tasks, return_exceptions=True)
        running = self._running_group_members()
        if running:
            # Descendants that ignored the group signal: last resort.
            self._signal_group(signal.SIGKILL)
            await asyncio.sleep(0.1)
            running = self._running_group_members()
        if self._reaped is not None and not self._reaped.done():
            self._reaped.set_result(None)
        self._close_report = {
            "generation": self.generation,
            "exit_code": exit_code,
            "reason": self._terminal.reason if self._terminal else "closed",
            "pending_waiters": self.pending_waiters,
            # Counts unsettled interactions: awaiting an answer OR carrying an
            # accepted answer that never settled. Must be 0 after close().
            "pending_interactions": len(self.unsettled_interactions),
            "unsettled_interaction_commits": len(self._interaction_commits),
            # Committed-but-unanswered requests still unaccounted for; every one
            # was surfaced as AmbiguousRequest at terminalization, so this is 0.
            "unresolved_orphans": len(self._orphaned_requests),
            # Definitive outcomes already delivered whose departed caller has
            # not yet consumed the tombstone (it will, on its next step).
            "unconsumed_orphan_tombstones": len(self._orphan_delivered),
            "running_descendants": len(running),
            "running_descendant_pids": running,
        }
        return self._close_report

    # -- internals: writing ----------------------------------------------------

    async def _write(
        self,
        payload: Dict[str, Any],
        *,
        deadline: Optional[float] = None,
        before_write: Optional[Callable[[], None]] = None,
        on_committed: Optional[Callable[[], None]] = None,
    ) -> None:
        """Bounded write with two clocks and two cancellation phases.

        Clocks:
        - The **caller's** ``deadline`` expiring while this request is still
          queued for the writer is local: zero bytes of it were written, so it
          raises ``asyncio.TimeoutError`` for this request only.
        - The **transport health** bound ``write_timeout_s`` is judged against
          the *current lock holder*: if the writer that owns the lock has held
          it that long, the runtime has stopped consuming stdin and the
          generation is terminalized (``write_timeout``).
        - Once bytes are in flight, the caller's deadline expiring during
          ``drain()`` leaves a partial, ambiguous line, so that too terminalizes.

        Cancellation phases:
        - Cancelled while **queued** (before ``before_write``): local and safe,
          nothing written, the lock was never taken.
        - Cancelled after the write has **begun**: the write is owned by a
          transport task that finishes the drain and only then releases the
          writer -- a partial line is never left in the pipe and another writer
          never interleaves. ``on_committed`` tells the caller this phase was
          reached so it can own the outcome (``request()`` registers the id as
          orphaned and its response is fanned out as ``OrphanedResponse``).
        ``before_write`` runs under the lock immediately before the first byte
        and may raise to abort with nothing written.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TransportError("transport not started")
        loop = asyncio.get_running_loop()
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

        # --- acquire the writer (cancellable, nothing written yet) ------------
        while True:
            now = loop.time()
            holder_since = self._writer_since if self._writer_since is not None else now
            health_deadline = holder_since + self.write_timeout_s
            wait_until = (
                health_deadline if deadline is None else min(health_deadline, deadline)
            )
            acquire = asyncio.ensure_future(self._write_lock.acquire())
            waits: set = {acquire}
            if self._terminal_waiter is not None and not self._terminal_waiter.done():
                # Stop queueing the moment the generation ends.
                waits.add(asyncio.shield(self._terminal_waiter))
            try:
                await asyncio.wait(
                    waits,
                    timeout=max(0.0, wait_until - now),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                # Caller cancelled while queued: nothing written, never keep the lock.
                if await self._settle_acquire(acquire):
                    self._write_lock.release()
                raise
            if await self._settle_acquire(acquire):
                break
            if self._terminal is not None:
                self._raise_if_lost()
            now = loop.time()
            if deadline is not None and now >= deadline:
                raise asyncio.TimeoutError(
                    "request deadline expired while queued for the writer; nothing was written"
                )
            if (
                self._writer_since is not None
                and now - self._writer_since >= self.write_timeout_s
            ):
                self._terminalize(
                    "write_timeout",
                    exit_code=proc.returncode,
                    detail=f"writer held the transport for {now - self._writer_since:.2f}s; runtime not consuming stdin",
                )
                self._raise_if_lost()
            # The holder changed under us before either clock ran out: re-arm.

        # --- own the writer: hand it to a transport-owned committed write --------
        # The gate and the first byte run INSIDE that task, in one synchronous
        # section with no await between them, so the reader can never
        # interleave `serverRequest/resolved` between "gate passed" and "bytes
        # accepted": either the gate sees the retirement and writes nothing, or
        # the bytes are in the stdin buffer before the reader runs again.
        self._writer_since = loop.time()
        drain_deadline = self._writer_since + self.write_timeout_s
        if deadline is not None:
            drain_deadline = min(drain_deadline, deadline)
        committed = asyncio.ensure_future(
            self._committed_write(
                proc,
                line,
                drain_deadline,
                caller_deadline=deadline,
                before_write=before_write,
                on_committed=on_committed,
            )
        )
        self._committed_writes.add(committed)
        committed.add_done_callback(self._committed_writes.discard)
        await asyncio.shield(committed)

    @staticmethod
    async def _settle_acquire(acquire: "asyncio.Future[bool]") -> bool:
        """Resolve an in-flight ``Lock.acquire()``; True iff the lock is now ours.

        A cancel that lands after the lock was granted leaves the future
        completed with True (asyncio.Lock hands an unfinished, cancelled
        acquire on to the next waiter), so the answer is read from the future,
        never assumed.
        """
        if not acquire.done():
            acquire.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await acquire
        return (
            acquire.done() and not acquire.cancelled() and acquire.exception() is None
        )

    async def _committed_write(
        self,
        proc: asyncio.subprocess.Process,
        line: bytes,
        drain_deadline: float,
        *,
        caller_deadline: Optional[float],
        before_write: Optional[Callable[[], None]] = None,
        on_committed: Optional[Callable[[], None]] = None,
    ) -> None:
        """The part of a write that must finish once begun. Holds the writer
        until the drain completes or the generation is terminalized; releases
        it in every case.

        ``before_write`` -> ``stdin.write`` -> ``on_committed`` execute with no
        await between them (the write itself only buffers), which is what makes
        the gate and the acceptance of the bytes one indivisible step.
        """
        loop = asyncio.get_running_loop()
        try:
            # Gate first, outside the write-failure handling: a gate rejection
            # (StaleAnswer -- a TransportError, hence a RuntimeError) means zero
            # bytes were written and must propagate as itself, never be
            # mistaken for a broken pipe.
            self._raise_if_lost()
            if before_write is not None:
                before_write()
            try:
                proc.stdin.write(line)  # type: ignore[union-attr]
                if on_committed is not None:
                    on_committed()  # bytes accepted into the stdin transport buffer
                await asyncio.wait_for(proc.stdin.drain(), max(0.0, drain_deadline - loop.time()))  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                which = (
                    "caller deadline"
                    if caller_deadline is not None and drain_deadline == caller_deadline
                    else "write_timeout_s"
                )
                self._terminalize(
                    "write_timeout",
                    exit_code=proc.returncode,
                    detail=f"{which} expired mid-write with {len(line)} bytes not drained; pipe contents ambiguous",
                )
                self._raise_if_lost()
            except TransportError:
                raise
            except (
                BrokenPipeError,
                ConnectionResetError,
                RuntimeError,
                OSError,
            ) as exc:
                self._terminalize(
                    "write_failed", exit_code=proc.returncode, detail=repr(exc)
                )
                self._raise_if_lost()
        finally:
            self._writer_since = None
            self._write_lock.release()

    def _raise_if_lost(self) -> None:
        """Admission fence for every outbound path.

        Raises once the runtime is lost, and ALSO from the instant owner close
        begins: ``close()`` is the linearization point after which no new
        request, notification, or interaction answer is admitted -- it only
        settles or retires work that existed before it. Bytes already accepted
        by the runtime are never subject to this check (nothing calls it after
        ``stdin.write``), so an in-flight committed write still drains.
        """
        if self._terminal is not None:
            raise RuntimeLost(
                self._terminal.reason,
                generation=self.generation,
                exit_code=self._terminal.exit_code,
                detail=self._terminal.detail,
            )
        if self._closing:
            raise RuntimeLost(
                "closing",
                generation=self.generation,
                exit_code=self._proc.returncode if self._proc is not None else None,
                detail="owner close in progress; no new work admitted",
            )

    # -- internals: the one reader -------------------------------------------

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                self._terminalize(
                    "protocol_error",
                    exit_code=proc.returncode,
                    detail=f"oversized line: {exc!r}",
                )
                self._signal_group(signal.SIGKILL)
                return
            if not raw:
                # Every buffered message has now been routed: the reader owns
                # terminalization. Give the exit status a bounded moment to
                # land so every RuntimeLost and the TerminalEvent carry it. A
                # process that closed stdout but lives on is reported as "eof"
                # with exit_code None.
                assert self._stdout_eof is not None
                self._stdout_eof.set()
                await self._exit_status(proc, self.exit_drain_grace_s)
                reason = "exit" if proc.returncode is not None else "eof"
                self._terminalize(
                    reason, exit_code=proc.returncode, detail=self._stderr_excerpt()
                )
                return
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                self._terminalize(
                    "protocol_error",
                    exit_code=proc.returncode,
                    detail=f"unparsable stdout line: {exc}; line={text[:200]!r}",
                )
                self._signal_group(signal.SIGKILL)
                return
            if not isinstance(message, dict):
                self._terminalize(
                    "protocol_error",
                    exit_code=proc.returncode,
                    detail=f"stdout line is not an object: {text[:200]!r}",
                )
                self._signal_group(signal.SIGKILL)
                return
            self._dispatch(message)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        has_id = "id" in message
        method = message.get("method")
        if has_id and isinstance(method, str):
            self._on_server_request(message["id"], method, message.get("params"))
            return
        if isinstance(method, str):
            params = message.get("params")
            if method == SERVER_REQUEST_RESOLVED:
                # Retire the interaction BEFORE anyone hears about it, so no
                # subscriber can race a late answer past the fence.
                self._resolve_interaction(params)
            self._fanout(Notification(method, params))
            return
        if has_id:
            self._on_response(message)
            return
        # Neither a request, a notification nor a response: the stream is not
        # speaking JSON-RPC any more.
        self._terminalize(
            "protocol_error",
            exit_code=self._proc.returncode if self._proc else None,
            detail=f"unroutable message: {str(message)[:200]!r}",
        )
        self._signal_group(signal.SIGKILL)

    def _resolve_interaction(self, params: Any) -> None:
        """Upstream cleared a server request: the interaction is no longer
        actionable. A later ``answer()`` raises ``StaleAnswer`` and writes
        nothing; a handler still parked on it is cancelled."""
        request_id = params.get("requestId") if isinstance(params, dict) else None
        interaction = self._interactions.get(request_id)
        if interaction is None:
            return
        if interaction.state == "resolving" and interaction.wire_committed:
            # The answer's bytes have begun reaching the process; upstream
            # retiring it afterwards is recorded, not allowed to unsend them.
            interaction.resolved_after_commit = True
            return
        if interaction.state not in ("pending", "resolving"):
            if interaction.state in ("answered", "failed"):
                interaction.resolved_after_commit = True
            return
        # Pending, or resolving with nothing on the wire yet: upstream wins. The
        # commit task's pre-write gate sees `resolved` under the writer lock and
        # writes nothing.
        interaction.state = "resolved"
        interaction.invalidation_reason = SERVER_RESOLVED
        task = self._interaction_tasks.pop(request_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _on_response(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._waiters.get(str(request_id))
        if future is not None and future.cancelled():
            # The caller is being cancelled but has not run its handler yet
            # (Task.cancel() cancels the awaited future synchronously); the
            # request was sent, so this response is its orphaned outcome. Fan
            # it out now and tell the handler delivery already happened.
            self._orphan_delivered.add(str(request_id))
            self._fanout(
                OrphanedResponse(
                    request_id=str(request_id),
                    method=self._waiter_methods.get(str(request_id), "?"),
                    result=message.get("result"),
                    error=message.get("error"),
                    generation=self.generation,
                )
            )
            return
        if future is None or future.done():
            method = self._orphaned_requests.pop(str(request_id), None)
            if method is not None:
                self._fanout(
                    OrphanedResponse(
                        request_id=str(request_id),
                        method=method,
                        result=message.get("result"),
                        error=message.get("error"),
                        generation=self.generation,
                    )
                )
                return
            logger.debug(
                "app-server response for unknown/finished request %r", request_id
            )
            return
        if "error" in message:
            future.set_exception(
                RpcError(
                    self._waiter_methods.get(str(request_id), "?"), message["error"]
                )
            )
        else:
            future.set_result(message.get("result"))

    def _on_server_request(self, request_id: Any, method: str, params: Any) -> None:
        live = self._interactions.get(request_id)
        if live is not None and live.state in ("pending", "resolving"):
            # A JSON-RPC peer must not reuse a live id. Never replace the
            # existing interaction (an old handler could then resolve the new
            # one under the same key): answer the newcomer with an error.
            self.rejected_server_requests.append(
                {"id": request_id, "method": method, "reason": "duplicate_live_id"}
            )
            logger.warning(
                "rejecting server request with live duplicate id %r (%s)",
                request_id,
                method,
            )
            self._spawn_handler_task(
                self._write(
                    {
                        "id": request_id,
                        "error": {
                            "code": JSONRPC_INVALID_REQUEST,
                            "message": f"duplicate live server request id {request_id!r}",
                        },
                    }
                )
            )
            return
        if method not in self.supported_server_requests:
            # Fail closed: answer with a protocol error right away, record it,
            # and keep reading. Never a permissive result, never an interaction.
            self.rejected_server_requests.append({"id": request_id, "method": method})
            logger.warning(
                "rejecting unsupported app-server request %r (id=%r)",
                method,
                request_id,
            )
            self._spawn_handler_task(
                self._write(
                    {
                        "id": request_id,
                        "error": {
                            "code": JSONRPC_METHOD_NOT_FOUND,
                            "message": f"unsupported server request: {method}",
                        },
                    }
                )
            )
            return
        interaction = PendingInteraction(
            id=request_id, method=method, params=params, generation=self.generation
        )
        self._interactions[request_id] = interaction
        self._fanout(interaction)
        if self._interaction_handler is not None:
            # Off the reader: a handler that parks on a human must not stop
            # responses, notifications, or death detection.
            task = self._spawn_handler_task(self._run_handler(interaction))
            self._interaction_tasks[request_id] = task
            task.add_done_callback(
                lambda done, rid=request_id: self._forget_interaction_task(rid, done)
            )

    def _forget_interaction_task(self, request_id: Any, task: asyncio.Task) -> None:
        """Drop the registry entry only if it still points at THIS task.

        A settled server-request id may be reused by upstream; a new
        interaction's parked handler must not be evicted by the old handler's
        completion callback, or `serverRequest/resolved` / terminalization
        would no longer find it.
        """
        if self._interaction_tasks.get(request_id) is task:
            self._interaction_tasks.pop(request_id, None)

    async def _run_handler(self, interaction: PendingInteraction) -> None:
        assert self._interaction_handler is not None
        try:
            result = await self._interaction_handler(interaction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failing handler must fail closed
            logger.warning(
                "interaction handler failed for %s: %r", interaction.method, exc
            )
            with contextlib.suppress(StaleAnswer):
                await self.fail_interaction(
                    interaction.id,
                    generation=interaction.generation,
                    token=interaction.token,
                    message=f"handler error: {type(exc).__name__}",
                )
            return
        try:
            await self.answer(
                interaction.id,
                result,
                generation=interaction.generation,
                token=interaction.token,
            )
        except StaleAnswer as exc:
            logger.info("dropping late interaction answer: %s", exc)

    def _spawn_handler_task(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)
        return task

    def _fanout(self, item: SubscriberItem) -> None:
        for subscription in list(self._subscribers):
            subscription._offer(item)
            if subscription.disconnected:
                with contextlib.suppress(ValueError):
                    self._subscribers.remove(subscription)

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            try:
                raw = await proc.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                return
            self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip("\n"))

    async def _watch_exit(self) -> None:
        """Process exit never terminalizes ahead of the stdout drain.

        A child can flush its final response and exit while those bytes are
        still in the pipe; ``proc.wait()`` returning says nothing about
        whether the reader has consumed them. So after exit the watcher waits
        for the reader's EOF (bounded by ``exit_drain_grace_s``) and lets the
        reader terminalize after routing everything. Only when EOF cannot
        arrive -- a descendant inherited stdout and keeps it open -- does the
        watcher terminalize itself, after the grace.
        """
        proc = self._proc
        assert proc is not None and self._stdout_eof is not None
        code = await self._exit_status(proc, None)
        try:
            await asyncio.wait_for(self._stdout_eof.wait(), self.exit_drain_grace_s)
        except asyncio.TimeoutError:
            self._terminalize(
                "exit",
                exit_code=code,
                detail=f"stdout held open {self.exit_drain_grace_s}s past exit; {self._stderr_excerpt()}",
            )

    @staticmethod
    async def _exit_status(
        proc: asyncio.subprocess.Process, timeout: Optional[float]
    ) -> Optional[int]:
        """Exit status as soon as the child is reaped, pipes notwithstanding.

        ``Process.wait()`` only resolves once every inherited pipe has also
        closed, so a descendant holding stdout would hide the parent's exit
        from a watcher built on it. ``returncode`` is set the moment the child
        watcher reaps the process, so poll that (50 ms) instead.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while proc.returncode is None:
            if deadline is not None and loop.time() >= deadline:
                break
            await asyncio.sleep(0.05)
        return proc.returncode

    def _stderr_excerpt(self) -> str:
        tail = list(self._stderr_tail)[-5:]
        return " | ".join(tail)

    # -- internals: terminalization --------------------------------------------

    def _terminalize(
        self, reason: str, *, exit_code: Optional[int], detail: str = ""
    ) -> None:
        """Generation-wide, exactly once. Every waiter, interaction and
        subscriber learns about the loss in this call; nothing is left to be
        discovered by a later read."""
        if self._terminal is not None:
            return
        if self._closing and reason in ("eof", "exit", "write_failed"):
            # The owner asked for this shutdown; the process leaving is the
            # expected consequence, not a loss to be diagnosed.
            reason = "closed"
        event = TerminalEvent(
            reason=reason,
            generation=self.generation,
            exit_code=exit_code,
            detail=detail,
        )
        self._terminal = event
        if self._terminal_waiter is not None and not self._terminal_waiter.done():
            self._terminal_waiter.set_result(event)
        for request_id, future in list(self._waiters.items()):
            if not future.done():
                future.set_exception(
                    RuntimeLost(
                        reason,
                        generation=self.generation,
                        exit_code=exit_code,
                        detail=detail,
                    )
                )
        for interaction in self._interactions.values():
            if interaction.open:
                interaction.state = "invalidated"
                interaction.invalidation_reason = RUNTIME_LOST
        # An accepted answer whose bytes were never accepted by the runtime can
        # no longer be delivered either; its commit task's gate finds it shut.
        self._retire_unsent_answers(RUNTIME_LOST)
        for task in list(self._interaction_tasks.values()):
            if not task.done():
                task.cancel()
        self._interaction_tasks.clear()
        # Every committed request whose caller left and that never got its
        # response reaches ONE terminal outcome, delivered before the terminal
        # event (a subscriber may stop consuming after it): ambiguous accepted
        # work for the owner to reconcile, never silently dropped.
        for request_id, method in list(self._orphaned_requests.items()):
            self._fanout(
                AmbiguousRequest(
                    request_id=request_id,
                    method=method,
                    generation=self.generation,
                    terminal_reason=reason,
                )
            )
        self._orphaned_requests.clear()
        # `_orphan_delivered` is deliberately NOT cleared here: it is the
        # tombstone proving a definitive OrphanedResponse was already fanned
        # out for a request whose cancelled caller has not yet run its
        # ownership-transfer handler. That handler consumes it; erasing it at
        # terminalization would let the same request also be reported as
        # AmbiguousRequest -- two terminal outcomes for one request.
        self._fanout(event)
        if reason != "closed" and self._reap_task is None:
            # Unexpected loss: once stdout/stdin is unusable the gateway can no
            # longer supervise approvals, tool events or completion, so the
            # owned process group must not keep running until some caller
            # remembers close(). Teardown is transport-owned and bounded.
            self._reap_task = asyncio.ensure_future(self._reap_after_loss())

    def _retire_unsent_answers(self, reason: str) -> None:
        for interaction in self._interactions.values():
            if interaction.state == "resolving" and not interaction.wire_committed:
                interaction.state = "invalidated"
                interaction.invalidation_reason = reason

    async def _reap_after_loss(self) -> None:
        try:
            if self._running_group_members():
                self._signal_group(signal.SIGTERM)
                await self._wait_group_empty(self.loss_teardown_grace_s)
            if self._running_group_members():
                self._signal_group(signal.SIGKILL)
                await self._wait_group_empty(self.loss_teardown_grace_s)
        finally:
            if self._reaped is not None and not self._reaped.done():
                self._reaped.set_result(None)

    async def _wait_group_empty(self, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._running_group_members() and loop.time() < deadline:
            await asyncio.sleep(0.05)

    async def wait_reaped(self, timeout: Optional[float] = None) -> None:
        """Wait until the transport-owned teardown after an unexpected loss has
        run (no-op once ``close()`` has completed)."""
        if self._close_report is not None:
            return
        assert self._reaped is not None, "transport not started"
        await asyncio.wait_for(asyncio.shield(self._reaped), timeout)

    def running_group_members(self) -> List[int]:
        """PIDs still running in this transport's process group (Linux /proc)."""
        return self._running_group_members()

    # -- internals: process group --------------------------------------------

    def _signal_group(self, sig: signal.Signals) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if self._pgid is not None:
                os.killpg(self._pgid, sig)
            elif proc.returncode is None:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.debug("signal %s to app-server group failed: %r", sig, exc)

    async def _wait_exit(
        self, proc: asyncio.subprocess.Process, timeout: float
    ) -> None:
        await self._exit_status(proc, timeout)

    def _running_group_members(self) -> List[int]:
        """PIDs still running (not zombies) in this transport's process group.

        Linux-only via /proc; returns [] elsewhere (unknown, not zero -- the
        report's ``running_descendants`` is only meaningful where /proc exists).
        """
        if self._pgid is None or not Path("/proc").exists():
            return []
        running: List[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                rest = stat[stat.rindex(")") + 2 :].split()
                state, pgid = rest[0], int(rest[2])
            except (ValueError, IndexError):
                continue
            if pgid == self._pgid and not state.startswith("Z"):
                running.append(int(entry.name))
        return sorted(running)
