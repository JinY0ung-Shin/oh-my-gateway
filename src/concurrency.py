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
stopped. It is enforced only for requests that actually carry a ``user`` —
unidentified callers share one bucket, so enforcing there would cap a whole
endpoint at ``per_user`` instead of sharing it.

Both default to values that are safe on an 8 GB box at roughly 0.5 GB per
live session (measured 400 MB plus headroom for MCP child processes).
Operators on larger hosts should raise them; see ``.env.example``.

Set either to ``0`` to disable that limit entirely.

Admission is **fail-fast**: a rejected caller gets ``503`` with
``Retry-After`` rather than queueing.  Turns run for up to ``MAX_TIMEOUT``
(default 10 minutes), so blocking a waiter on a full gateway would tie up a
connection far longer than any client is willing to wait.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Tuple

from src.constants import (
    MAX_CONCURRENT_TURNS,
    MAX_CONCURRENT_TURNS_PER_USER,
    MAX_LIVE_SESSIONS,
)

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


class TurnLimiter:
    """Fail-fast counter for in-flight agent turns.

    Guarded by a plain :class:`threading.Lock` rather than an asyncio
    primitive: acquisition never blocks, so there is nothing to await, and a
    sync lock keeps the limiter usable from non-async callers (admin
    snapshots, tests) without an event loop.
    """

    def __init__(self, total: int, per_user: int) -> None:
        self.total = total
        self.per_user = per_user
        self._lock = threading.Lock()
        self._in_flight = 0
        self._by_user: Dict[str, int] = {}

    def try_acquire(self, user: Optional[str] = None) -> Tuple[Optional[TurnSlot], str]:
        """Take a slot without blocking.

        Returns ``(slot, "")`` on success, or ``(None, reason)`` when a limit
        is hit.  *reason* is a short scope label for logging and metrics —
        never echoed to the caller, since per-user counts would leak the
        gateway's tenancy shape.
        """
        key = user or _ANONYMOUS
        with self._lock:
            if self.total > 0 and self._in_flight >= self.total:
                return None, "global"
            # Enforced only for callers that actually identified themselves.
            # Unidentified requests all hash to one bucket, so enforcing there
            # would cap an entire endpoint at ``per_user`` rather than share
            # it fairly: /v1/agents/messages forbids a ``user`` field
            # outright, and a chunked request has no content-length to peek.
            # Both would silently collapse to 3 concurrent turns for every
            # caller combined. They are bounded by ``total`` instead.
            if user and self.per_user > 0 and self._by_user.get(key, 0) >= self.per_user:
                return None, "per_user"
            self._in_flight += 1
            self._by_user[key] = self._by_user.get(key, 0) + 1
        return TurnSlot(self, key), ""

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

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def snapshot(self) -> Dict[str, int]:
        """Return current occupancy for admin/diagnostic surfaces."""
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "limit": self.total,
                "distinct_users": len(self._by_user),
                "per_user_limit": self.per_user,
            }

    def reset(self) -> None:
        """Drop all accounting.  Tests only."""
        with self._lock:
            self._in_flight = 0
            self._by_user.clear()


turn_limiter = TurnLimiter(MAX_CONCURRENT_TURNS, MAX_CONCURRENT_TURNS_PER_USER)


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


def check_session_limit_fits_memory() -> Optional[str]:
    """Warn when ``MAX_LIVE_SESSIONS`` cannot fit in available memory.

    Returns the warning text (also logged), or ``None`` when the configured
    limit fits or memory could not be detected.
    """
    if MAX_LIVE_SESSIONS <= 0:
        logger.warning(
            "MAX_LIVE_SESSIONS=0 disables the live-session cap. Each session "
            "holds a persistent Claude CLI subprocess (~500 MB); the gateway "
            "can exhaust host memory under load."
        )
        return None

    total = detect_memory_limit_bytes()
    if not total:
        return None

    affordable = int(total * _SESSION_MEMORY_SHARE) // BYTES_PER_LIVE_SESSION
    if MAX_LIVE_SESSIONS <= affordable:
        return None

    message = (
        f"MAX_LIVE_SESSIONS={MAX_LIVE_SESSIONS} exceeds what this host can hold: "
        f"{total // (1024 ** 3)} GiB detected fits about {affordable} live session(s) "
        f"at ~{BYTES_PER_LIVE_SESSION // (1024 ** 2)} MB each. Sessions keep their "
        f"Claude CLI subprocess alive for the whole TTL, so the gateway may OOM "
        f"before the cap is reached. Lower MAX_LIVE_SESSIONS or give the host more memory."
    )
    logger.warning(message)
    return message
