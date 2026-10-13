"""Admission control for concurrent agent runs.

Every ``/v1/responses`` turn and every ``/v1/agents/messages`` call drives a
Claude CLI subprocess.  A measured live subprocess holds roughly 400 MB RSS
(CLI 2.1.220), so an unbounded gateway is one traffic burst away from OOM.
Two independent limits guard two independent resources:

``MAX_LIVE_SESSIONS``
    The memory ceiling, and the one that actually prevents OOM.
    :attr:`src.session_manager.Session.client` holds a *persistent* SDK
    client for the whole session TTL (default 60 minutes), so a subprocess
    outlives the turn that created it.  Capping in-flight turns alone does
    nothing here: fifty idle sessions cost ~20 GB with zero turns running.

``MAX_CONCURRENT_TURNS``
    The CPU and upstream-API ceiling — how many turns may execute at once.
    Against a hostile caller this is the *only* load-bearing limit.

``MAX_CONCURRENT_TURNS_PER_USER`` is a **fairness hint, not a security
control.** It keys on the request's ``user`` field, which is caller-supplied
and bound to no credential: rotating the value evades it entirely, and
claiming someone else's value burns their share. It keeps well-behaved
clients from starving each other; it stops no one who does not want to be
stopped.

It is enforced only for requests that actually carry a ``user``. Unidentified
callers share one bucket, so enforcing there would cap a whole endpoint at
``per_user`` rather than share it — ``/v1/agents/messages`` forbids the field
outright and would have been pinned at three concurrent turns for every
caller combined. The trade-off is that opting out is trivial: omit ``user``,
or send the body with ``Transfer-Encoding: chunked`` so there is no
content-length to peek at. That costs nothing extra, because the cap was
already advisory. ``MAX_CONCURRENT_TURNS`` is the real bound.

Both default to values that are safe on an 8 GB box at roughly 0.5 GB per
live session (measured 400 MB plus headroom for MCP child processes).
Operators on larger hosts should raise them; see ``.env.example``.

Set either to ``0`` to disable that limit entirely.

Admission **waits briefly, then fails fast**: a turn that finds either cap
full joins a FIFO queue for up to ``TURN_QUEUE_TIMEOUT_SECONDS`` (default 30;
``0`` disables the queue) and gets ``503`` with ``Retry-After`` only if no
slot frees in time or ``TURN_QUEUE_MAX_WAITERS`` are already waiting. Nearly
every collision is one turn ending moments later, so a short wait turns an
error the user sees into a pause. The wait stays short on purpose: turns run
for up to ``MAX_TIMEOUT`` (default 10 minutes), so an unbounded queue would
tie up a connection far longer than any client is willing to wait.

A freed slot goes to the **oldest waiter that can take it** — one blocked
only by its own per-user cap does not hold up anyone else's turn — and a new
arrival never jumps ahead of a waiter that could have taken the slot.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Awaitable, Callable, Deque, Dict, Optional, Tuple

from src.constants import (
    MAX_CONCURRENT_TURNS,
    MAX_CONCURRENT_TURNS_PER_USER,
    MAX_LIVE_SESSIONS,
    TURN_QUEUE_MAX_WAITERS,
    TURN_QUEUE_TIMEOUT_SECONDS,
)

__all__ = [
    "SessionLimitExceeded",
    "TurnLimiter",
    "TurnSlot",
    "check_session_limit_fits_memory",
    "detect_memory_limit_bytes",
    "peak_subprocess_count",
    "turn_limiter",
]

logger = logging.getLogger(__name__)

_ANONYMOUS = "__anonymous__"


class SessionLimitExceeded(RuntimeError):
    """Raised when creating a session would exceed ``MAX_LIVE_SESSIONS``."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            f"Gateway is at its live-session capacity ({limit}). "
            "Retry once an existing session ends or expires."
        )


class TurnSlot:
    """A held admission slot.  ``release()`` is idempotent."""

    __slots__ = ("_limiter", "_user", "_released")

    def __init__(self, limiter: "TurnLimiter", user: str) -> None:
        self._limiter = limiter
        self._user = user
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release(self._user)

    def __enter__(self) -> "TurnSlot":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class _Waiter:
    __slots__ = ("key", "identified", "future", "loop")

    def __init__(
        self, key: str, identified: bool, loop: asyncio.AbstractEventLoop
    ) -> None:
        self.key = key
        self.identified = identified
        self.loop = loop
        self.future: "asyncio.Future[TurnSlot]" = loop.create_future()


class TurnLimiter:
    """Counter for in-flight agent turns, with a short FIFO wait queue.

    Guarded by a plain :class:`threading.Lock` rather than an asyncio
    primitive: the counting itself never blocks, and a sync lock keeps
    :meth:`try_acquire` usable from non-async callers (admin snapshots,
    tests) without an event loop. Only :meth:`acquire` waits, on a future that
    a release resolves through the waiter's own loop.
    """

    def __init__(
        self,
        total: int,
        per_user: int,
        queue_timeout: float = 0.0,
        queue_max: int = 0,
    ) -> None:
        self.total = total
        self.per_user = per_user
        self.queue_timeout = queue_timeout
        self.queue_max = queue_max
        self._lock = threading.Lock()
        self._in_flight = 0
        self._by_user: Dict[str, int] = {}
        self._waiters: Deque[_Waiter] = deque()

    def _blocked(self, key: str, identified: bool) -> str:
        """Which limit stops *key* now — ``""`` when a slot is free. Lock held."""
        if self.total > 0 and self._in_flight >= self.total:
            return "global"
        # Enforced only for callers that actually identified themselves.
        # Unidentified requests all hash to one bucket, so enforcing there
        # would cap an entire endpoint at ``per_user`` rather than share it
        # fairly: /v1/agents/messages forbids a ``user`` field outright, and a
        # chunked request has no content-length to peek. Both would silently
        # collapse to 3 concurrent turns for every caller combined. They are
        # bounded by ``total`` instead.
        if (
            identified
            and self.per_user > 0
            and self._by_user.get(key, 0) >= self.per_user
        ):
            return "per_user"
        return ""

    def _take(self, key: str) -> TurnSlot:
        """Count one slot for *key*. Lock held."""
        self._in_flight += 1
        self._by_user[key] = self._by_user.get(key, 0) + 1
        return TurnSlot(self, key)

    def try_acquire(self, user: Optional[str] = None) -> Tuple[Optional[TurnSlot], str]:
        """Take a slot without blocking.

        Returns ``(slot, "")`` on success, or ``(None, reason)`` when a limit
        is hit.  *reason* is a short scope label for logging and metrics —
        never echoed to the caller, since per-user counts would leak the
        gateway's tenancy shape. A waiter that could take a free slot has it
        already (see :meth:`_grant_waiters`), so this never jumps the queue.
        """
        key = user or _ANONYMOUS
        with self._lock:
            reason = self._blocked(key, bool(user))
            if reason:
                return None, reason
            return self._take(key), ""

    async def acquire(
        self,
        user: Optional[str] = None,
        *,
        abort: Optional[Callable[[], Awaitable[object]]] = None,
    ) -> Tuple[Optional[TurnSlot], str, float]:
        """Take a slot, waiting up to ``queue_timeout`` seconds for one.

        Returns ``(slot, reason, waited_seconds)``. *reason* is the limit that
        refused an immediate admission (``global``/``per_user``), then
        ``queue_full`` or ``queue_timeout`` when waiting did not help, or
        ``aborted`` when *abort* (the client disconnecting) finished first.
        *abort* is a factory, called only if this request actually queues.
        A slot granted in the same instant as a timeout or abort is released,
        never leaked.
        """
        key = user or _ANONYMOUS
        identified = bool(user)
        with self._lock:
            reason = self._blocked(key, identified)
            if not reason:
                return self._take(key), "", 0.0
            if self.queue_timeout <= 0:
                return None, reason, 0.0
            if self.queue_max > 0 and len(self._waiters) >= self.queue_max:
                return None, "queue_full", 0.0
            waiter = _Waiter(key, identified, asyncio.get_running_loop())
            self._waiters.append(waiter)

        started = time.monotonic()
        abort_task = asyncio.ensure_future(abort()) if abort is not None else None
        outcome = "queue_timeout"
        keep = False
        try:
            wait_for = {waiter.future} | ({abort_task} if abort_task else set())
            await asyncio.wait(
                wait_for,
                timeout=self.queue_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort_task is not None and abort_task.done():
                outcome = "aborted"
            keep = outcome != "aborted"
        finally:
            with self._lock:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass  # granted: _grant_waiters already took it out
            if abort_task is not None and not abort_task.done():
                abort_task.cancel()
            if not waiter.future.done():
                # A grant still in flight sees the cancelled future and
                # releases the slot itself (_resolve_waiter).
                waiter.future.cancel()
            elif not waiter.future.cancelled() and not keep:
                # Granted, but we are leaving anyway (abort, or this task was
                # cancelled mid-wait): hand the slot straight back.
                waiter.future.result().release()
        waited = time.monotonic() - started
        if not waiter.future.cancelled() and outcome != "aborted":
            return waiter.future.result(), "", waited
        return None, outcome, waited

    def _grant_waiters(self) -> None:
        """Hand free slots to the oldest waiters that can take them. Lock held."""
        if not self._waiters:
            return
        for waiter in list(self._waiters):
            if self.total > 0 and self._in_flight >= self.total:
                return
            if waiter.future.done() or self._blocked(waiter.key, waiter.identified):
                continue
            self._waiters.remove(waiter)
            slot = self._take(waiter.key)
            waiter.loop.call_soon_threadsafe(_resolve_waiter, waiter.future, slot)

    def _release(self, user: str) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            remaining = self._by_user.get(user, 0) - 1
            if remaining > 0:
                self._by_user[user] = remaining
            else:
                # Drop the key so an idle gateway does not accumulate one
                # dict entry per user seen since boot.
                self._by_user.pop(user, None)
            self._grant_waiters()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self._waiters)

    def snapshot(self) -> Dict[str, int]:
        """Return current occupancy for admin/diagnostic surfaces."""
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "limit": self.total,
                "distinct_users": len(self._by_user),
                "per_user_limit": self.per_user,
                "queued": len(self._waiters),
                "queue_limit": self.queue_max,
            }

    def reset(self) -> None:
        """Drop all accounting.  Tests only."""
        with self._lock:
            self._in_flight = 0
            self._by_user.clear()
            self._waiters.clear()


def _resolve_waiter(future: "asyncio.Future[TurnSlot]", slot: TurnSlot) -> None:
    """Deliver a granted slot on the waiter's loop — or return it if nobody waits."""
    if future.done():
        slot.release()
    else:
        future.set_result(slot)


turn_limiter = TurnLimiter(
    MAX_CONCURRENT_TURNS,
    MAX_CONCURRENT_TURNS_PER_USER,
    queue_timeout=TURN_QUEUE_TIMEOUT_SECONDS,
    queue_max=TURN_QUEUE_MAX_WAITERS,
)


# ---------------------------------------------------------------------------
# Startup sizing check
# ---------------------------------------------------------------------------

# Measured resident size of one live `claude` CLI subprocess, rounded up to
# leave room for the MCP child processes it may spawn.
BYTES_PER_LIVE_SESSION = 500 * 1024 * 1024

# Leave headroom for the gateway process itself, the page cache, and the
# workspace file I/O threadpool.
_SESSION_MEMORY_SHARE = 0.6


def detect_memory_limit_bytes() -> Optional[int]:
    """Return the memory ceiling this process actually runs under.

    Prefers the cgroup v2 limit, which is correct inside a container — unlike
    ``os.cpu_count()``, which reports host cores and silently over-reports
    under a CFS quota.  Falls back to cgroup v1, then to host ``MemTotal``.
    Returns ``None`` when nothing is readable.
    """
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw == "max":
            break  # cgroup exists but is unlimited — fall through to MemTotal
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 reports a sentinel near 2^63 when unlimited.
        if 0 < value < (1 << 62):
            return value

    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def peak_subprocess_count() -> int:
    """Worst-case concurrent Claude CLI subprocesses across both agent paths.

    ``/v1/responses`` turns reuse their session's persistent client, so the
    session cap already covers them. ``/v1/agents/messages`` is stateless:
    every call builds a fresh SDK client, never enters the session manager,
    and is therefore invisible to ``MAX_LIVE_SESSIONS`` and to the
    ``gateway_live_sessions`` gauge. Those runs are bounded only by
    ``MAX_CONCURRENT_TURNS``, so the true ceiling is the sum.

    A limit set to 0 is unbounded and cannot be budgeted; it contributes 0
    here and is warned about separately.
    """
    return max(MAX_LIVE_SESSIONS, 0) + max(MAX_CONCURRENT_TURNS, 0)


def check_session_limit_fits_memory() -> Optional[str]:
    """Warn when the configured limits cannot fit in available memory.

    Returns the warning text (also logged), or ``None`` when the limits fit
    or memory could not be detected.
    """
    if MAX_LIVE_SESSIONS <= 0 or MAX_CONCURRENT_TURNS <= 0:
        logger.warning(
            "MAX_LIVE_SESSIONS=%s / MAX_CONCURRENT_TURNS=%s: a 0 disables that "
            "cap. Each live session and each stateless /v1/agents/messages run "
            "holds a Claude CLI subprocess (~%d MB); with a cap disabled the "
            "gateway can exhaust host memory under load.",
            MAX_LIVE_SESSIONS or "unlimited",
            MAX_CONCURRENT_TURNS or "unlimited",
            BYTES_PER_LIVE_SESSION // (1024**2),
        )
        return None

    total = detect_memory_limit_bytes()
    if not total:
        return None

    affordable = int(total * _SESSION_MEMORY_SHARE) // BYTES_PER_LIVE_SESSION
    peak = peak_subprocess_count()
    if peak <= affordable:
        return None

    message = (
        f"Configured limits can reach {peak} concurrent Claude CLI subprocesses "
        f"(MAX_LIVE_SESSIONS={MAX_LIVE_SESSIONS} + MAX_CONCURRENT_TURNS="
        f"{MAX_CONCURRENT_TURNS} stateless /v1/agents/messages runs, which never "
        f"enter the session manager), but the {total // (1024 ** 3)} GiB detected "
        f"here fits about {affordable} at ~{BYTES_PER_LIVE_SESSION // (1024 ** 2)} MB "
        f"each. Sessions hold their subprocess for the whole TTL, so the gateway "
        f"may OOM before either cap is reached. Lower MAX_LIVE_SESSIONS (or "
        f"MAX_CONCURRENT_TURNS if you serve /v1/agents/messages), or add memory."
    )
    logger.warning(message)
    return message
