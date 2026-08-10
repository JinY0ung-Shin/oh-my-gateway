"""
Session management for chat-session history.

This module manages in-memory conversation sessions with TTL-based expiry
and automatic cleanup.  It handles **chat-session message history** only;
the ``previous_response_id`` chaining used by ``/v1/responses`` is managed
in the ``/v1/responses`` endpoint (``src/routes/responses.py``).

Concurrency model
-----------------
* ``SessionManager.lock`` (threading.Lock) guards the ``sessions`` dict for
  thread-safe CRUD.  Dict operations are O(1) so holding the lock briefly
  from async handlers is acceptable under CPython's GIL.
* ``Session.lock`` (asyncio.Lock) is a **per-session** lock that callers
  may acquire for multi-step atomic operations on a single session (e.g.
  read-modify-write across concurrent requests to the same session_id).
"""

import asyncio
import contextlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from threading import Lock

from src import metrics
from src.concurrency import SessionLimitExceeded
from src.models import Message, SessionInfo
from src.constants import SESSION_CLEANUP_INTERVAL_MINUTES, SESSION_MAX_AGE_MINUTES

logger = logging.getLogger(__name__)

_CWD_ENCODE_RE = re.compile(r"[/_.]")
_PROJECTS_ROOT: Path = Path.home() / ".claude" / "projects"


def _encode_cwd(cwd) -> str:
    """Encode a workspace cwd to its on-disk Claude SDK directory name.

    The Claude SDK stores per-project transcripts under
    ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``. The encoding
    rule (verified against on-disk transcript directories): every ``/``,
    ``_`` and ``.`` is replaced with ``-``.

    The rule is **lossy** — distinct workspaces such as ``a_b``, ``a.b`` and
    ``a-b`` collapse to the same directory — so ``_try_rehydrate_from_jsonl``
    additionally verifies the transcript's recorded ``cwd`` against the
    requester's workspace before trusting it.
    """
    return _CWD_ENCODE_RE.sub("-", str(cwd))


def _session_jsonl_path(session_id: str, workspace) -> Path:
    """Return the on-disk path the SDK uses for this session's transcript.

    Path layout: ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``.
    """
    return _PROJECTS_ROOT / _encode_cwd(workspace) / f"{session_id}.jsonl"


def _session_jsonl_exists(session: "Session") -> bool:
    """True when the SDK has already written a transcript for *session*."""
    if not session.workspace:
        return False
    return _session_jsonl_path(session.session_id, session.workspace).is_file()


def _try_rehydrate_from_jsonl(session_id: str, *, user: Optional[str], cwd) -> Optional["Session"]:
    """Reconstruct a Session from the Claude SDK on-disk jsonl, if present.

    Returns None when the jsonl file is missing, unreadable, or malformed
    enough that we can't establish a turn count. The caller treats None as
    cache-miss-and-on-disk-miss → existing 404 path.
    """
    if not user or not cwd:
        return None
    try:
        jsonl_path = _session_jsonl_path(session_id, cwd)
        if not jsonl_path.is_file():
            return None
        expected_cwd = Path(cwd)
        user_msg_count = 0
        with jsonl_path.open("r") as fh:
            for raw in fh:
                try:
                    line = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    return None  # corrupt — refuse to guess
                # Ownership guard. ``_encode_cwd`` is lossy ('/', '_', '.' all
                # collapse to '-'), so distinct workspaces — e.g. users "a_b",
                # "a.b" and "a-b" — share one on-disk transcript directory. The
                # SDK records the REAL cwd on each turn line, so refuse to
                # rehydrate a transcript whose recorded cwd does not match the
                # requester's resolved workspace; otherwise a colliding user
                # could rebuild another user's session and history.
                recorded_cwd = line.get("cwd")
                if (
                    isinstance(recorded_cwd, str)
                    and recorded_cwd
                    and Path(recorded_cwd) != expected_cwd
                ):
                    logger.warning(
                        "Refusing to rehydrate session %s: transcript cwd %r "
                        "does not match requester workspace %r",
                        session_id,
                        recorded_cwd,
                        str(cwd),
                    )
                    return None
                if line.get("type") != "user":
                    continue
                # Claude jsonl reuses type="user" for two distinct things:
                # (1) external prompts the gateway exposed as turns, and
                # (2) tool_result blocks plus isMeta system reminders that
                # the SDK injects internally. Only (1) corresponds to a
                # gateway-issued response_id, so only (1) advances the
                # turn counter.
                if line.get("isMeta"):
                    continue
                content = line.get("message", {}).get("content")
                if (
                    isinstance(content, list)
                    and content
                    and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                ):
                    continue
                user_msg_count += 1
        return Session(
            session_id=session_id,
            backend="claude",
            messages=[],
            turn_counter=user_msg_count,
            workspace=str(cwd),
            user=user,
        )
    except OSError:
        return None


def _cancel_idle_reader(session: "Session") -> None:
    """Stop a session's between-turn idle reader without awaiting it.

    Teardown paths call this before disconnecting the SDK client so the
    reader task never outlives the session. Field-level (no
    ``src.session_outbox`` import) to keep this module dependency-free.
    """
    stop_event = getattr(session, "idle_reader_stop", None)
    if stop_event is not None:
        try:
            stop_event.set()
        except Exception:
            pass
    task = getattr(session, "idle_reader_task", None)
    if task is not None:
        if not task.done():
            task.cancel()
        session.idle_reader_task = None


def _max_live_sessions() -> int:
    """Read ``MAX_LIVE_SESSIONS`` fresh so tests can patch the constant."""
    from src.constants import MAX_LIVE_SESSIONS

    return MAX_LIVE_SESSIONS


def _eviction_policy() -> str:
    """Effective ``SESSION_EVICTION_POLICY``: admin runtime override, then env."""
    from src.runtime_config import runtime_config

    return runtime_config.get("session_eviction_policy")


def _eviction_min_idle_seconds() -> int:
    """Read ``SESSION_EVICTION_MIN_IDLE_SECONDS`` fresh so tests can patch it."""
    from src.constants import SESSION_EVICTION_MIN_IDLE_SECONDS

    return SESSION_EVICTION_MIN_IDLE_SECONDS


def _utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize datetimes to UTC while tolerating legacy naive inputs."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Session:
    """Represents a conversation session with message history.

    Each session tracks its own TTL, message history, and turn count.
    The ``lock`` field is an ``asyncio.Lock`` that callers can acquire
    for safe multi-step operations on the session under concurrency.
    """

    session_id: str
    backend: str = "claude"
    ttl_minutes: int = 60
    messages: List[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    last_accessed: datetime = field(default_factory=_utcnow)
    expires_at: Optional[datetime] = field(default=None)
    turn_counter: int = 0
    base_system_prompt: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    user: Optional[str] = None
    workspace: Optional[str] = None

    # ClaudeSDKClient integration
    client: Optional[Any] = None
    input_event: Optional[asyncio.Event] = field(default=None, repr=False, compare=False)
    input_response: Optional[str] = None
    pending_tool_call: Optional[Dict[str, Any]] = None
    stream_break_event: Optional[asyncio.Event] = field(default=None, repr=False, compare=False)

    # Between-turn idle reader + captured-event outbox (src.session_outbox).
    # ``outbox`` is a SessionOutbox created lazily by the reader/endpoint.
    outbox: Optional[Any] = field(default=None, repr=False, compare=False)
    idle_reader_task: Optional[Any] = field(default=None, repr=False, compare=False)
    idle_reader_stop: Optional[Any] = field(default=None, repr=False, compare=False)

    # In-flight Responses API turn.  This state is deliberately guarded by a
    # separate lock from ``lock``: streaming turns hold ``lock`` for their
    # whole lifetime, while a concurrent cancel request must still be able to
    # deliver an interrupt to the active backend client.
    active_response_id: Optional[str] = None
    active_response_turn: Optional[int] = None
    active_response_state: Optional[str] = None
    active_response_client: Optional[Any] = field(default=None, repr=False, compare=False)
    active_response_done: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False, compare=False
    )
    response_control_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )

    # Per-turn stored ResponseObject payloads, keyed by 1-based turn number.
    # Populated by the responses route at turn-commit time and served by
    # ``GET /v1/responses/{response_id}``. Turns created with ``store=false``
    # are never recorded, and sessions rehydrated from the on-disk jsonl
    # transcript start empty (only the turn counter survives rehydration).
    turn_records: Dict[int, Dict[str, Any]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.created_at = _ensure_utc(self.created_at)
        self.last_accessed = _ensure_utc(self.last_accessed)
        if self.expires_at is None:
            self.expires_at = _utcnow() + timedelta(minutes=self.ttl_minutes)
        else:
            self.expires_at = _ensure_utc(self.expires_at)

    def touch(self) -> None:
        """Update last accessed time and extend expiration."""
        now = _utcnow()
        self.last_accessed = now
        self.expires_at = now + timedelta(minutes=self.ttl_minutes)

    def add_messages(self, messages: List[Message]) -> None:
        """Add new messages to the session and refresh TTL."""
        self.messages.extend(messages)
        self.touch()

    def get_all_messages(self) -> List[Message]:
        """Return a shallow copy of the session's message list."""
        return list(self.messages)

    def record_turn_response(self, turn: int, payload: Dict[str, Any]) -> None:
        """Store the response payload for *turn* (``GET /v1/responses/{id}``)."""
        self.turn_records[turn] = payload

    def get_turn_response(self, turn: int) -> Optional[Dict[str, Any]]:
        """Return the stored response payload for *turn*, or ``None``."""
        return self.turn_records.get(turn)

    def is_expired(self) -> bool:
        """Check if the session has expired."""
        assert self.expires_at is not None
        if self.active_response_id is not None:
            # An in-flight turn (streamed or background) pins the session:
            # expiring it would orphan the running SDK client and strand the
            # response id a client may still poll or cancel. The turn's
            # teardown always clears the slot and touches the session.
            return False
        return _utcnow() > self.expires_at

    def to_session_info(self) -> SessionInfo:
        """Convert to SessionInfo model for API responses."""
        assert self.expires_at is not None
        return SessionInfo(
            session_id=self.session_id,
            created_at=self.created_at,
            last_accessed=self.last_accessed,
            message_count=len(self.messages),
            expires_at=self.expires_at,
        )


class SessionManager:
    """Manages conversation sessions with automatic cleanup.

    This class handles chat-session lifecycle (create, access, expire, delete)
    and a periodic background cleanup task.  It does **not** manage the
    ``previous_response_id`` chain used by the Responses API surface.
    """

    def __init__(self, default_ttl_minutes: int = 60, cleanup_interval_minutes: int = 5) -> None:
        self.sessions: Dict[str, Session] = {}
        self.lock: Lock = Lock()
        self.default_ttl_minutes: int = default_ttl_minutes
        self.cleanup_interval_minutes: int = cleanup_interval_minutes
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        # Out-of-band sweep kicked when the live-session cap is hit; see
        # _schedule_expired_sweep.
        self._sweep_task: Optional[asyncio.Task[int]] = None
        # Strong refs to in-flight eviction teardowns (the loop only keeps
        # weak ones, and several evictions can overlap — a single slot like
        # _sweep_task would let earlier tasks be GC-cancelled mid-disconnect).
        self._evict_tasks: set = set()
        self._rehydrate_hits: int = 0
        self._rehydrate_misses: int = 0

    # ------------------------------------------------------------------
    # Internal helpers (caller must hold self.lock)
    # ------------------------------------------------------------------

    def _has_room_for_new_session(self) -> bool:
        """Whether another session may be admitted.  Caller must hold the lock.

        Sweeps expired sessions before answering, because the periodic
        cleanup task only runs every ``SESSION_CLEANUP_INTERVAL_MINUTES``
        (default 5) and the gateway must not refuse while holding dead ones.

        The synchronous sweep can only drop sessions with no live SDK client
        — which in practice is almost none, since any session that has served
        a turn owns one and disconnecting it has to be awaited. So when
        expired-but-client-holding sessions are what's filling the cap, kick
        the async cleanup instead: this request still fails, but the slot is
        freed in about a second rather than up to five minutes.

        When the cap is full of *unexpired* sessions there is nothing to
        sweep; ``SESSION_EVICTION_POLICY=lru`` opts into evicting the
        least-recently-used idle session instead of refusing (see
        ``_try_evict_lru_locked``). The default remains refusal.
        """
        limit = _max_live_sessions()
        if limit <= 0:
            return True
        if len(self.sessions) < limit:
            return True

        self._purge_all_expired_sync()
        if len(self.sessions) < limit:
            return True

        admitted = _eviction_policy() == "lru" and self._try_evict_lru_locked()

        # Kick the async sweep whether or not eviction made room: under
        # sustained eviction pressure the reject path would otherwise never
        # run, leaving expired-but-client-holding sessions to the (much
        # slower) periodic cleanup.
        if any(s.is_expired() for s in self.sessions.values()):
            self._schedule_expired_sweep()
        return admitted

    def _schedule_expired_sweep(self) -> None:
        """Kick an out-of-band async cleanup, at most one at a time.

        ``get_or_create_session`` is synchronous but always reached from an
        async handler, so a loop is normally running. Outside one (unit tests,
        sync callers) this is a no-op — the periodic task still covers it.
        """
        existing = getattr(self, "_sweep_task", None)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweep_task = loop.create_task(self.cleanup_expired_sessions())

    def _try_evict_lru_locked(self) -> bool:
        """Evict the least-recently-accessed idle session to free one slot.

        Caller must hold the lock.  Returns ``True`` when a slot was freed.

        Skips sessions that are mid-turn in either sense: pinned by an
        in-flight response (``active_response_id``, the same guard
        ``is_expired`` uses — evicting one would orphan the running SDK
        client and strand a response id the caller may still poll or cancel)
        or paused on an ``AskUserQuestion`` (``pending_tool_call`` /
        ``input_event``, the same pair ``resume_idle_reader`` and
        ``_clear_stale_pending_tool_call`` gate on — the turn's continuation
        is coming back for this exact session). Also skips sessions accessed
        within ``SESSION_EVICTION_MIN_IDLE_SECONDS`` (without that floor, a
        burst of cap+1 newcomers would cascade-evict each other). When
        nothing qualifies the caller falls through to the normal reject
        path: a gateway full of busy sessions should 503 rather than break a
        live conversation.

        Expired sessions are preferred over live ones regardless of access
        order — an expired-but-client-holding session (which the sync purge
        cannot drop) is always a better victim than someone's live
        conversation, and per-session TTLs can diverge after admin edits, so
        plain LRU order does not guarantee that on its own.

        The slot itself is freed synchronously — the dict entry is gone when
        this returns — but a session holding an SDK client needs its
        disconnect awaited, so the real teardown runs in a fire-and-forget
        task and the subprocess memory is only returned once it completes.
        Without a running loop that task cannot be scheduled, so
        client-holding candidates are skipped rather than orphaned.
        """
        now = _utcnow()
        min_idle = timedelta(seconds=_eviction_min_idle_seconds())
        candidates = sorted(
            self.sessions.values(),
            key=lambda s: (not s.is_expired(), s.last_accessed),
        )
        for session in candidates:
            if session.active_response_id is not None:
                continue
            if session.pending_tool_call is not None or session.input_event is not None:
                continue
            idle = now - session.last_accessed
            if not session.is_expired() and idle < min_idle:
                # The floor only protects *live* sessions (its purpose is to
                # stop cap+1 newcomers from cascade-evicting each other); an
                # expired candidate is already condemned and must not shield
                # itself with it. Expired sessions sort first, so the first
                # live session under the floor ends the scan — everyone after
                # it is live and younger.
                break
            if session.client is not None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    continue
                del self.sessions[session.session_id]
                task = loop.create_task(self._teardown_evicted(session))
                self._evict_tasks.add(task)
                task.add_done_callback(self._evict_tasks.discard)
            else:
                del self.sessions[session.session_id]
                if session.workspace:
                    self._cleanup_workspace(session.workspace)
            metrics.record_session_evicted()
            logger.info(
                "Evicted LRU session %s (idle %.0fs) to admit a new session; "
                "%d live sessions remain",
                session.session_id,
                idle.total_seconds(),
                len(self.sessions),
            )
            return True
        return False

    async def _teardown_evicted(self, session: "Session") -> None:
        """Disconnect and clean up an evicted session outside the lock.

        Mirrors the expiry sweep's sequence in ``_purge_all_expired``, with
        the same 2s disconnect cap as ``async_shutdown`` — disconnect() can
        hang on a dead anyio channel, and a hung fire-and-forget task would
        pin the subprocess and the Session (via ``_evict_tasks``) forever.
        The session is already out of ``self.sessions``; this only releases
        the resources it still owns. Never raises: nothing awaits this task,
        so an escaped exception would only surface as an unretrieved-task
        warning at GC time.
        """
        try:
            _cancel_idle_reader(session)
            if session.client is not None:
                try:
                    await asyncio.wait_for(session.client.disconnect(), timeout=2.0)
                except Exception:
                    logger.debug(
                        "Client disconnect timed out or failed for evicted session %s",
                        session.session_id,
                        exc_info=True,
                    )
                session.client = None
            if session.workspace:
                self._cleanup_workspace(session.workspace)
        except Exception:
            logger.exception(
                "Eviction teardown failed for session %s", session.session_id
            )

    def _remove_if_expired(self, session_id: str) -> bool:
        """Remove *session_id* if present and expired.

        Returns ``True`` when the session was expired and removed.
        """
        session = self.sessions.get(session_id)
        if session is not None and session.is_expired():
            del self.sessions[session_id]
            logger.info(f"Removed expired session: {session_id}")
            return True
        return False

    async def _purge_all_expired(self) -> int:
        """Remove every expired session.  Returns the count removed.

        Takes a snapshot of expired sessions under the manager lock, then
        disconnects clients and cleans workspaces outside the lock.  Before
        deleting each session it re-checks under the lock that the session
        object is still the same instance **and** still expired — this
        prevents a TOCTOU race where a session could be refreshed (TTL
        extended) between the snapshot and the deletion.
        """
        with self.lock:
            expired = [(sid, s) for sid, s in self.sessions.items() if s.is_expired()]
        doomed = []
        with self.lock:
            for sid, session in expired:
                # Re-check: session might have been refreshed since snapshot
                current = self.sessions.get(sid)
                if current is session and current.is_expired():
                    del self.sessions[sid]
                    logger.info(f"Cleaned up expired session: {sid}")
                    doomed.append((sid, session))
        async def _teardown(sid: str, session: "Session") -> None:
            # Never raises (mirrors _teardown_evicted): under the gather's
            # return_exceptions a raise would otherwise vanish unlogged.
            try:
                _cancel_idle_reader(session)
                if session.client is not None:
                    try:
                        # Capped like every other awaited disconnect here (see
                        # async_shutdown): disconnect() can hang on a dead
                        # anyio channel, and a wedged sweep would block all
                        # future out-of-band sweeps AND the periodic cleanup
                        # loop — session expiry would stop for the process
                        # lifetime.
                        await asyncio.wait_for(session.client.disconnect(), timeout=2.0)
                    except Exception:
                        logger.debug(
                            "Client disconnect failed for session %s", sid, exc_info=True
                        )
                    session.client = None
                if session.workspace:
                    self._cleanup_workspace(session.workspace)
            except Exception:
                logger.exception("Expiry teardown failed for session %s", sid)

        if doomed:
            # Concurrent teardown, mirroring async_shutdown's snapshot pass.
            # The doomed list is by now the ONLY reference to these sessions,
            # so async_shutdown must be able to await an in-flight sweep at a
            # ~2s bound rather than MAX_LIVE_SESSIONS x 2s serial.
            await asyncio.gather(
                *(_teardown(sid, s) for sid, s in doomed), return_exceptions=True
            )
        return len(doomed)

    def _purge_all_expired_sync(self) -> int:
        """Synchronous variant: remove expired sessions without client disconnect.

        Used by synchronous callers (e.g. ``list_sessions``) that cannot await.
        Sessions with active clients are left in place so the async cleanup
        cycle can disconnect them safely and then remove them — otherwise the
        SDK client would be orphaned.
        """
        expired = [sid for sid, s in self.sessions.items() if s.is_expired()]
        removed = 0
        for sid in expired:
            session = self.sessions[sid]
            if session.client is not None:
                # Defer to the async cleanup cycle so the client can be awaited.
                continue
            if session.workspace:
                self._cleanup_workspace(session.workspace)
            del self.sessions[sid]
            logger.info(f"Cleaned up expired session: {sid}")
            removed += 1
        return removed

    def _cleanup_workspace(self, workspace_path: str) -> None:
        """Remove temporary workspace directory on session expiry."""
        try:
            from src.workspace_manager import WorkspaceManager

            wm = WorkspaceManager(base_path=Path(workspace_path).parent)
            wm.cleanup_temp_workspace(Path(workspace_path))
        except Exception:
            logger.debug("Workspace cleanup skipped for %s", workspace_path, exc_info=True)

    # ------------------------------------------------------------------
    # Cleanup task
    # ------------------------------------------------------------------

    def start_cleanup_task(self) -> None:
        """Start the automatic cleanup task — call after the event loop is running."""
        if self._cleanup_task is not None:
            return  # Already started

        async def cleanup_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(self.cleanup_interval_minutes * 60)
                    try:
                        await self.cleanup_expired_sessions()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Session cleanup cycle failed, will retry next interval")
            except asyncio.CancelledError:
                logger.info("Session cleanup task cancelled")
                raise

        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(cleanup_loop())
            logger.info(
                f"Started session cleanup task (interval: {self.cleanup_interval_minutes} minutes)"
            )
        except RuntimeError:
            logger.warning("No running event loop, automatic session cleanup disabled")

    async def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions and stale image files.

        Returns the count of sessions removed.
        """
        removed = await self._purge_all_expired()

        # Clean up stale image files from backends that have an image handler
        try:
            from src.backends.base import BackendRegistry

            for _name, backend in BackendRegistry.all_backends().items():
                if hasattr(backend, "cleanup_images"):
                    backend.cleanup_images()
        except Exception:
            logger.debug("backend image cleanup skipped/failed", exc_info=True)

        return removed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_shutdown(self) -> None:
        """Async shutdown: cancel cleanup task, disconnect clients, clean workspaces, clear sessions."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task

        # Settle — never cancel — an in-flight out-of-band sweep:
        # _purge_all_expired deletes every doomed session from self.sessions
        # BEFORE disconnecting it, so a parked sweep is the only reference to
        # those clients, and cancelling it would orphan each one it had not
        # reached — invisibly to the snapshot pass below. Awaiting is
        # bounded: the sweep's teardowns run concurrently, each disconnect
        # capped at 2s.
        sweep = self._sweep_task
        if sweep is not None and not sweep.done():
            with contextlib.suppress(Exception):
                await sweep

        # Evicted sessions are already out of self.sessions, so the snapshot
        # below cannot see them — settle their in-flight teardowns first or
        # their CLI subprocesses outlive the gateway. Loop: a concurrent
        # admission can evict (scheduling another teardown) while we await.
        # Bounded: each teardown caps its disconnect at 2s and never raises,
        # and the loop ends the first round no new eviction lands in.
        while True:
            evict_tasks = list(self._evict_tasks)
            if not evict_tasks:
                break
            await asyncio.gather(*evict_tasks, return_exceptions=True)

        with self.lock:
            sessions_snapshot = list(self.sessions.values())

        # Disconnect in parallel with a per-client timeout — ClaudeSDKClient.disconnect()
        # can hang if its internal anyio channel is already dead (common after long-running
        # servers accumulate stale sessions).
        async def _disconnect(session: "Session") -> None:
            _cancel_idle_reader(session)
            if session.client is None:
                return
            try:
                await asyncio.wait_for(session.client.disconnect(), timeout=2.0)
            except Exception:
                logger.debug("Client disconnect timed out or failed", exc_info=True)
            session.client = None

        if sessions_snapshot:
            await asyncio.gather(
                *(_disconnect(s) for s in sessions_snapshot),
                return_exceptions=True,
            )

        with self.lock:
            self._cleanup_all_temp_workspaces()
            self.sessions.clear()
            logger.info("Session manager async shutdown complete")

    def _cleanup_all_temp_workspaces(self) -> None:
        """Remove temporary workspaces for all active sessions.

        Called during shutdown to prevent ``_tmp_*`` directory leaks.
        Must be called while holding ``self.lock``.
        """
        for session in self.sessions.values():
            if session.workspace:
                self._cleanup_workspace(session.workspace)

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def get_or_create_session(self, session_id: str) -> Session:
        """Get existing session or create a new one.

        If the session exists but is expired it is replaced with a fresh one.

        Raises :class:`~src.concurrency.SessionLimitExceeded` when creating a
        *new* session would exceed ``MAX_LIVE_SESSIONS``.  Reaching an
        existing session never fails: the cap bounds how many Claude CLI
        subprocesses are alive, and an established conversation already owns
        one.
        """
        with self.lock:
            if session_id in self.sessions:
                if self._remove_if_expired(session_id):
                    logger.info(f"Session {session_id} expired, creating new session")
                else:
                    self.sessions[session_id].touch()
                    return self.sessions[session_id]

            if not self._has_room_for_new_session():
                logger.warning(
                    "Refusing new session %s: %d live sessions at MAX_LIVE_SESSIONS=%d",
                    session_id,
                    len(self.sessions),
                    _max_live_sessions(),
                )
                metrics.record_session_rejected()
                raise SessionLimitExceeded(_max_live_sessions())

            # Use runtime override if admin changed it, otherwise honor
            # the constructor-provided default_ttl_minutes so non-global
            # SessionManager instances still work correctly.
            from src.runtime_config import runtime_config

            if runtime_config.is_overridden("session_max_age_minutes"):
                ttl = runtime_config.get("session_max_age_minutes")
            else:
                ttl = self.default_ttl_minutes
            session = Session(session_id=session_id, ttl_minutes=ttl)
            self.sessions[session_id] = session
            logger.info(f"Created new session: {session_id}")
            return session

    def get_session(
        self,
        session_id: str,
        *,
        user: Optional[str] = None,
        cwd: Optional[str] = None,
        max_turn: Optional[int] = None,
    ) -> Optional[Session]:
        """Return a session by id; rehydrate from jsonl on cache miss when context permits.

        Returns ``None`` when the session does not exist, is expired, and
        cannot be rehydrated from disk.

        ``max_turn`` is the highest turn the caller is about to reference
        (from ``previous_response_id``). A rehydrated session that cannot
        serve it is discarded *before* admission — admission is not free
        (at the cap it can reject, or under ``SESSION_EVICTION_POLICY=lru``
        evict someone else's live session), and the route would 404 the
        request right after anyway. Live cache hits are returned regardless;
        the route's own turn validation owns that case.
        """
        with self.lock:
            self._remove_if_expired(session_id)
            session = self.sessions.get(session_id)
            if session is not None:
                session.touch()
                return session
            # Cache miss path — only count rehydrate hit/miss when the
            # caller supplied enough context for a jsonl lookup to be
            # attempted. Generic cache misses (e.g., simple existence
            # checks without user/cwd) must not pollute the metric.
            if not user or not cwd:
                return None
            session = _try_rehydrate_from_jsonl(session_id, user=user, cwd=cwd)
            if session is not None and max_turn is not None and max_turn > session.turn_counter:
                self._rehydrate_misses = getattr(self, "_rehydrate_misses", 0) + 1
                return None
            if session is not None:
                # Rehydration materializes a session that will pin its own
                # Claude CLI subprocess, so it has to respect the same cap as
                # get_or_create_session. Without this, replaying stored
                # previous_response_ids walks straight past MAX_LIVE_SESSIONS
                # and defeats the memory guard.
                if not self._has_room_for_new_session():
                    logger.warning(
                        "Refusing to rehydrate session %s: %d live sessions at "
                        "MAX_LIVE_SESSIONS",
                        session_id,
                        len(self.sessions),
                    )
                    metrics.record_session_rejected()
                    raise SessionLimitExceeded(_max_live_sessions())
                self.sessions[session_id] = session
                self._rehydrate_hits = getattr(self, "_rehydrate_hits", 0) + 1
                return session
            self._rehydrate_misses = getattr(self, "_rehydrate_misses", 0) + 1
            return None

    def peek_session(self, session_id: str) -> Optional[Session]:
        """Read-only session access — does **not** refresh TTL.

        Used by admin endpoints that should observe sessions without
        extending their lifetime.  Returns ``None`` when the session
        does not exist or is expired.
        """
        with self.lock:
            self._remove_if_expired(session_id)
            return self.sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session.  Returns ``True`` if it was found and removed.

        Synchronous callers cannot await SDK client shutdown. Async request
        handlers should use ``delete_session_async`` so active clients are
        disconnected before the session disappears.
        """
        with self.lock:
            session = self.sessions.pop(session_id, None)
            if session is None:
                return False
        _cancel_idle_reader(session)
        if session.client is not None:
            logger.warning(
                "Deleted session %s with an active client; use delete_session_async "
                "from async callers",
                session_id,
            )
        if session.workspace:
            self._cleanup_workspace(session.workspace)
        logger.info(f"Deleted session: {session_id}")
        return True

    async def delete_session_async(self, session_id: str) -> bool:
        """Delete a session, disconnecting its client and cleaning temp workspace."""
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False

        _cancel_idle_reader(session)
        if session.client is not None:
            try:
                await asyncio.wait_for(session.client.disconnect(), timeout=2.0)
            except Exception:
                logger.debug("Client disconnect timed out or failed", exc_info=True)
            session.client = None

        if session.workspace:
            self._cleanup_workspace(session.workspace)

        logger.info(f"Deleted session: {session_id}")
        return True

    def list_sessions(self) -> List[SessionInfo]:
        """List all active (non-expired) sessions."""
        with self.lock:
            self._purge_all_expired_sync()
            return [session.to_session_info() for session in self.sessions.values()]

    def add_assistant_response(self, session_id: Optional[str], assistant_message: Message) -> None:
        """Add assistant response to session if session mode is active."""
        if session_id is None:
            return

        session = self.get_session(session_id)
        if session:
            session.add_messages([assistant_message])
            logger.info(f"Added assistant response to session {session_id}")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, int]:
        """Get session manager statistics."""
        with self.lock:
            active = 0
            expired = 0
            total_messages = 0
            for s in self.sessions.values():
                if s.is_expired():
                    expired += 1
                else:
                    active += 1
                total_messages += len(s.messages)

            return {
                "active_sessions": active,
                "expired_sessions": expired,
                "total_messages": total_messages,
            }

    def stats(self) -> dict:
        """Return a summary dict including rehydrate hit/miss counters."""
        with self.lock:
            active = sum(1 for s in self.sessions.values() if not s.is_expired())
            return {
                "active_sessions": active,
                "rehydrate_hits": self._rehydrate_hits,
                "rehydrate_misses": self._rehydrate_misses,
            }


# Global session manager instance
session_manager = SessionManager(
    default_ttl_minutes=SESSION_MAX_AGE_MINUTES,
    cleanup_interval_minutes=SESSION_CLEANUP_INTERVAL_MINUTES,
)
