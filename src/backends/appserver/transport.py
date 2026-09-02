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
- late or wrong-generation interaction answers are rejected (``StaleAnswer``)
  and never reach the process
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
  caller never leaves an interaction "answered" with nothing on the wire
- bounded subscribers: streaming deltas are droppable for a slow consumer,
  lossless items are not, and a consumer that falls too far behind on
  lossless items is disconnected instead of growing the process

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
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Union

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
SUPPORTED_SERVER_REQUESTS = DEFAULT_SUPPORTED_SERVER_REQUESTS  # backwards-compatible alias

# Upstream retires a pending server request (turn interrupted, turn finished,
# request cleared) with this notification; the matching interaction must
# become non-actionable at that moment, independent of any human TTL.
SERVER_REQUEST_RESOLVED = "serverRequest/resolved"

JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603

RUNTIME_LOST = "RUNTIME_LOST"
SERVER_RESOLVED = "SERVER_RESOLVED"

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

    def __init__(self, reason: str, *, generation: int, exit_code: Optional[int], detail: str = ""):
        super().__init__(f"app-server runtime lost ({reason}; generation={generation}; exit={exit_code}) {detail}".rstrip())
        self.reason = reason
        self.generation = generation
        self.exit_code = exit_code
        self.detail = detail


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
    created_at: float = field(default_factory=time.monotonic)
    state: str = "pending"  # pending | resolving | answered | failed | resolved | invalidated
    invalidation_reason: Optional[str] = None  # RUNTIME_LOST | SERVER_RESOLVED
    # Transport-owned commit of the response; set once the first answer is
    # accepted. Await it to learn the final state even if your own call was
    # cancelled.
    commit: Optional["asyncio.Future[None]"] = None

    @property
    def open(self) -> bool:
        return self.state == "pending"


@dataclass(frozen=True)
class SubscriberOverflow:
    """Delivered once to a subscriber that fell too far behind on lossless
    items; the subscription is disconnected and receives nothing further."""

    pending: int
    dropped_deltas: int
    generation: int


SubscriberItem = Union[Notification, PendingInteraction, TerminalEvent, SubscriberOverflow]
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
                SubscriberOverflow(pending=pending, dropped_deltas=self.dropped_deltas, generation=self.generation)
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
        self.client_info = client_info or dict(DEFAULT_CLIENT_INFO)
        self.capabilities = capabilities or dict(DEFAULT_CAPABILITIES)
        self._interaction_handler = interaction_handler

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pgid: Optional[int] = None
        self._write_lock = asyncio.Lock()
        self._writer_since: Optional[float] = None  # when the current lock holder took the writer
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
        return self._proc is not None and self._terminal is None and self._proc.returncode is None

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

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, *, timeout: Optional[float] = None) -> Any:
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
        try:
            # One absolute deadline covers the write AND the response: a live
            # runtime that stopped reading stdin cannot pin the caller before
            # its timeout even starts.
            await self._write(
                {"id": request_id, "method": method, "params": params or {}},
                deadline=deadline,
            )
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            return await asyncio.wait_for(future, remaining)
        finally:
            self._waiters.pop(request_id, None)
            self._waiter_methods.pop(request_id, None)
            if future.done() and not future.cancelled():
                # Terminalization may have failed this waiter while the write
                # itself was raising; mark that exception retrieved.
                future.exception()

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._raise_if_lost()
        await self._write({"method": method, "params": params or {}})

    async def interrupt(self, thread_id: str, turn_id: str, *, timeout: Optional[float] = None) -> Any:
        """Route ``turn/interrupt``; must progress even while an interaction is parked."""
        return await self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=timeout
        )

    # -- inbound fanout ------------------------------------------------------

    def subscribe(self, *, max_pending: int = 1000, hard_limit: Optional[int] = None) -> Subscription:
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
        return [i for i in self._interactions.values() if i.open]

    @property
    def pending_waiters(self) -> int:
        return len(self._waiters)

    def interaction(self, interaction_id: Any) -> Optional[PendingInteraction]:
        return self._interactions.get(interaction_id)

    async def answer(self, interaction_id: Any, result: Dict[str, Any], *, generation: int) -> None:
        """Deliver a human/app decision for a server request.

        Fenced three ways: the interaction must still be open, its generation
        must match the caller's, and the runtime must still be alive.
        Otherwise ``StaleAnswer`` -- and nothing is written.
        """
        interaction = self._fence(interaction_id, generation)
        await self._commit(interaction, {"id": interaction.id, "result": result}, "answered")

    async def fail_interaction(self, interaction_id: Any, *, generation: int, code: int = JSONRPC_INTERNAL_ERROR, message: str = "interaction failed") -> None:
        """Answer a server request with a JSON-RPC error (the fail-closed path)."""
        interaction = self._fence(interaction_id, generation)
        await self._commit(
            interaction, {"id": interaction.id, "error": {"code": code, "message": message}}, "failed"
        )

    async def _commit(self, interaction: PendingInteraction, payload: Dict[str, Any], final_state: str) -> None:
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
        interaction.commit = asyncio.ensure_future(self._commit_task(interaction, payload, final_state))
        await asyncio.shield(interaction.commit)

    async def _commit_task(self, interaction: PendingInteraction, payload: Dict[str, Any], final_state: str) -> None:
        try:
            await self._write(payload)
        except RuntimeLost:
            interaction.state = "invalidated"
            interaction.invalidation_reason = RUNTIME_LOST
            raise StaleAnswer(f"runtime lost before the response for {interaction.method} could be written")
        interaction.state = final_state

    def _fence(self, interaction_id: Any, generation: int) -> PendingInteraction:
        interaction = self._interactions.get(interaction_id)
        if interaction is None:
            raise StaleAnswer(f"unknown interaction {interaction_id!r}")
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
            raise StaleAnswer(f"interaction {interaction_id!r} already {interaction.state}")
        return interaction

    # -- teardown ------------------------------------------------------------

    async def close(self, *, grace_s: float = 2.0) -> Dict[str, Any]:
        """Deterministic, idempotent teardown.

        stdin EOF -> SIGTERM (process group) -> SIGKILL (process group), each
        bounded by ``grace_s``; then every waiter/interaction/subscriber is
        terminalized (if the process had not already died) and the process
        group is checked for running descendants. Returns the same report on
        every call.
        """
        if self._close_report is not None:
            return self._close_report
        self._closing = True
        proc = self._proc
        exit_code: Optional[int] = None
        if self._reap_task is not None:
            # An unexpected loss already started transport-owned teardown; let
            # it finish rather than racing two signal sequences.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._reap_task), 2 * self.loss_teardown_grace_s + 1.0)
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
            "pending_waiters": len(self._waiters),
            "pending_interactions": len(self.pending_interactions),
            "running_descendants": len(running),
            "running_descendant_pids": running,
        }
        return self._close_report

    # -- internals: writing ----------------------------------------------------

    async def _write(self, payload: Dict[str, Any], *, deadline: Optional[float] = None) -> None:
        """Bounded write with two separate clocks.

        - The **caller's** ``deadline`` expiring while this request is still
          queued for the writer is local: zero bytes of it were written, so it
          raises ``asyncio.TimeoutError`` for this request only and the
          transport stays alive. Being queued behind another, healthy write is
          not evidence about the runtime.
        - The **transport health** bound ``write_timeout_s`` is judged against
          the *current lock holder*: if the writer that owns the lock has held
          it that long, the runtime has stopped consuming stdin and the
          generation is terminalized (``write_timeout``). Contention between
          several fast writes never trips it.
        - Once this request has written bytes, the caller's deadline expiring
          during ``drain()`` leaves a partial, ambiguous line in the pipe, so
          that too terminalizes the generation rather than being abandoned.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TransportError("transport not started")
        loop = asyncio.get_running_loop()
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

        # --- acquire the writer -------------------------------------------
        while True:
            now = loop.time()
            holder_since = self._writer_since if self._writer_since is not None else now
            health_deadline = holder_since + self.write_timeout_s
            wait_until = health_deadline if deadline is None else min(health_deadline, deadline)
            try:
                await asyncio.wait_for(self._write_lock.acquire(), max(0.0, wait_until - now))
                break
            except asyncio.TimeoutError:
                now = loop.time()
                if deadline is not None and now >= deadline:
                    raise asyncio.TimeoutError(
                        "request deadline expired while queued for the writer; nothing was written"
                    )
                if self._writer_since is not None and now - self._writer_since >= self.write_timeout_s:
                    self._terminalize(
                        "write_timeout",
                        exit_code=proc.returncode,
                        detail=f"writer held the transport for {now - self._writer_since:.2f}s; runtime not consuming stdin",
                    )
                    self._raise_if_lost()
                # The holder changed under us before either clock ran out: re-arm.

        # --- own the writer -------------------------------------------------
        self._writer_since = loop.time()
        try:
            self._raise_if_lost()
            drain_deadline = self._writer_since + self.write_timeout_s
            if deadline is not None:
                drain_deadline = min(drain_deadline, deadline)
            try:
                proc.stdin.write(line)
                await asyncio.wait_for(proc.stdin.drain(), max(0.0, drain_deadline - loop.time()))
            except asyncio.TimeoutError:
                which = "caller deadline" if deadline is not None and drain_deadline == deadline else "write_timeout_s"
                self._terminalize(
                    "write_timeout",
                    exit_code=proc.returncode,
                    detail=f"{which} expired mid-write with {len(line)} bytes not drained; pipe contents ambiguous",
                )
                self._raise_if_lost()
            except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError) as exc:
                self._terminalize("write_failed", exit_code=proc.returncode, detail=repr(exc))
                self._raise_if_lost()
        finally:
            self._writer_since = None
            self._write_lock.release()

    def _raise_if_lost(self) -> None:
        if self._terminal is not None:
            raise RuntimeLost(
                self._terminal.reason,
                generation=self.generation,
                exit_code=self._terminal.exit_code,
                detail=self._terminal.detail,
            )

    # -- internals: the one reader -------------------------------------------

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                self._terminalize("protocol_error", exit_code=proc.returncode, detail=f"oversized line: {exc!r}")
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
                self._terminalize(reason, exit_code=proc.returncode, detail=self._stderr_excerpt())
                return
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                self._terminalize("protocol_error", exit_code=proc.returncode, detail=f"unparsable stdout line: {exc}; line={text[:200]!r}")
                self._signal_group(signal.SIGKILL)
                return
            if not isinstance(message, dict):
                self._terminalize("protocol_error", exit_code=proc.returncode, detail=f"stdout line is not an object: {text[:200]!r}")
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
        self._terminalize("protocol_error", exit_code=self._proc.returncode if self._proc else None, detail=f"unroutable message: {str(message)[:200]!r}")
        self._signal_group(signal.SIGKILL)

    def _resolve_interaction(self, params: Any) -> None:
        """Upstream cleared a server request: the interaction is no longer
        actionable. A later ``answer()`` raises ``StaleAnswer`` and writes
        nothing; a handler still parked on it is cancelled."""
        request_id = params.get("requestId") if isinstance(params, dict) else None
        interaction = self._interactions.get(request_id)
        if interaction is None or not interaction.open:
            return
        interaction.state = "resolved"
        interaction.invalidation_reason = SERVER_RESOLVED
        task = self._interaction_tasks.pop(request_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _on_response(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._waiters.get(str(request_id))
        if future is None or future.done():
            logger.debug("app-server response for unknown/finished request %r", request_id)
            return
        if "error" in message:
            future.set_exception(RpcError(self._waiter_methods.get(str(request_id), "?"), message["error"]))
        else:
            future.set_result(message.get("result"))

    def _on_server_request(self, request_id: Any, method: str, params: Any) -> None:
        if method not in self.supported_server_requests:
            # Fail closed: answer with a protocol error right away, record it,
            # and keep reading. Never a permissive result, never an interaction.
            self.rejected_server_requests.append({"id": request_id, "method": method})
            logger.warning("rejecting unsupported app-server request %r (id=%r)", method, request_id)
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
        interaction = PendingInteraction(id=request_id, method=method, params=params, generation=self.generation)
        self._interactions[request_id] = interaction
        self._fanout(interaction)
        if self._interaction_handler is not None:
            # Off the reader: a handler that parks on a human must not stop
            # responses, notifications, or death detection.
            task = self._spawn_handler_task(self._run_handler(interaction))
            self._interaction_tasks[request_id] = task
            task.add_done_callback(lambda _t, rid=request_id: self._interaction_tasks.pop(rid, None))

    async def _run_handler(self, interaction: PendingInteraction) -> None:
        assert self._interaction_handler is not None
        try:
            result = await self._interaction_handler(interaction)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failing handler must fail closed
            logger.warning("interaction handler failed for %s: %r", interaction.method, exc)
            with contextlib.suppress(StaleAnswer):
                await self.fail_interaction(
                    interaction.id, generation=interaction.generation, message=f"handler error: {type(exc).__name__}"
                )
            return
        try:
            await self.answer(interaction.id, result, generation=interaction.generation)
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
    async def _exit_status(proc: asyncio.subprocess.Process, timeout: Optional[float]) -> Optional[int]:
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

    def _terminalize(self, reason: str, *, exit_code: Optional[int], detail: str = "") -> None:
        """Generation-wide, exactly once. Every waiter, interaction and
        subscriber learns about the loss in this call; nothing is left to be
        discovered by a later read."""
        if self._terminal is not None:
            return
        if self._closing and reason in ("eof", "exit", "write_failed"):
            # The owner asked for this shutdown; the process leaving is the
            # expected consequence, not a loss to be diagnosed.
            reason = "closed"
        event = TerminalEvent(reason=reason, generation=self.generation, exit_code=exit_code, detail=detail)
        self._terminal = event
        if self._terminal_waiter is not None and not self._terminal_waiter.done():
            self._terminal_waiter.set_result(event)
        for request_id, future in list(self._waiters.items()):
            if not future.done():
                future.set_exception(
                    RuntimeLost(reason, generation=self.generation, exit_code=exit_code, detail=detail)
                )
        for interaction in self._interactions.values():
            if interaction.open:
                interaction.state = "invalidated"
                interaction.invalidation_reason = RUNTIME_LOST
        for task in list(self._interaction_tasks.values()):
            if not task.done():
                task.cancel()
        self._interaction_tasks.clear()
        self._fanout(event)
        if reason != "closed" and self._reap_task is None:
            # Unexpected loss: once stdout/stdin is unusable the gateway can no
            # longer supervise approvals, tool events or completion, so the
            # owned process group must not keep running until some caller
            # remembers close(). Teardown is transport-owned and bounded.
            self._reap_task = asyncio.ensure_future(self._reap_after_loss())

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

    async def _wait_exit(self, proc: asyncio.subprocess.Process, timeout: float) -> None:
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
