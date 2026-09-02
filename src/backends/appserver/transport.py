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

    reason: str  # "eof" | "exit" | "protocol_error" | "write_failed" | "closed"
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
    state: str = "pending"  # "pending" | "answered" | "failed" | "resolved" | "invalidated"
    invalidation_reason: Optional[str] = None  # RUNTIME_LOST | SERVER_RESOLVED

    @property
    def open(self) -> bool:
        return self.state == "pending"


SubscriberItem = Union[Notification, PendingInteraction, TerminalEvent]
InteractionHandler = Callable[[PendingInteraction], Awaitable[Dict[str, Any]]]


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
    ) -> None:
        self.argv = list(argv)
        self.generation = generation
        self.supported_server_requests = frozenset(
            DEFAULT_SUPPORTED_SERVER_REQUESTS
            if supported_server_requests is None
            else supported_server_requests
        )
        self.exit_drain_grace_s = exit_drain_grace_s
        self.cwd = str(cwd) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        self.client_info = client_info or dict(DEFAULT_CLIENT_INFO)
        self.capabilities = capabilities or dict(DEFAULT_CAPABILITIES)
        self._interaction_handler = interaction_handler

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pgid: Optional[int] = None
        self._write_lock = asyncio.Lock()
        self._waiters: Dict[str, asyncio.Future] = {}
        self._waiter_methods: Dict[str, str] = {}
        self._interactions: Dict[Any, PendingInteraction] = {}
        self._subscribers: List[asyncio.Queue] = []
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
        """Spawn the process, start the single reader, run the handshake."""
        if self._proc is not None:
            raise TransportError("transport already started")
        loop = asyncio.get_running_loop()
        self._terminal_waiter = loop.create_future()
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
        if initialize:
            await self.request(
                "initialize",
                {"clientInfo": self.client_info, "capabilities": self.capabilities},
            )
            await self.notify("initialized", {})

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
        request_id = str(uuid.uuid4())
        future: asyncio.Future = loop.create_future()
        self._waiters[request_id] = future
        self._waiter_methods[request_id] = method
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._waiters.pop(request_id, None)
            self._waiter_methods.pop(request_id, None)

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._raise_if_lost()
        await self._write({"method": method, "params": params or {}})

    async def interrupt(self, thread_id: str, turn_id: str, *, timeout: Optional[float] = None) -> Any:
        """Route ``turn/interrupt``; must progress even while an interaction is parked."""
        return await self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=timeout
        )

    # -- inbound fanout ------------------------------------------------------

    def subscribe(self) -> "asyncio.Queue[SubscriberItem]":
        """Receive every notification and interaction in arrival order, then one
        ``TerminalEvent``. Queues are unbounded so the reader never blocks on a
        slow consumer; a subscriber that stops draining leaks its own memory,
        not everyone's progress."""
        queue: asyncio.Queue = asyncio.Queue()
        if self._terminal is not None:
            queue.put_nowait(self._terminal)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

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
        interaction.state = "answered"
        try:
            await self._write({"id": interaction.id, "result": result})
        except RuntimeLost:
            interaction.state = "invalidated"
            interaction.invalidation_reason = RUNTIME_LOST
            raise StaleAnswer(f"runtime lost before answer for {interaction.method} could be written")

    async def fail_interaction(self, interaction_id: Any, *, generation: int, code: int = JSONRPC_INTERNAL_ERROR, message: str = "interaction failed") -> None:
        """Answer a server request with a JSON-RPC error (the fail-closed path)."""
        interaction = self._fence(interaction_id, generation)
        interaction.state = "failed"
        try:
            await self._write({"id": interaction.id, "error": {"code": code, "message": message}})
        except RuntimeLost:
            interaction.state = "invalidated"
            interaction.invalidation_reason = RUNTIME_LOST
            raise StaleAnswer(f"runtime lost before error for {interaction.method} could be written")

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

    async def _write(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TransportError("transport not started")
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self._raise_if_lost()
            try:
                proc.stdin.write(line)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError, OSError) as exc:
                self._terminalize("write_failed", exit_code=proc.returncode, detail=repr(exc))
                self._raise_if_lost()

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
        for queue in list(self._subscribers):
            queue.put_nowait(item)

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
