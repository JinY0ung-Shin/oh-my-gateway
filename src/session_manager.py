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
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from threading import Lock

from src import metrics
from src.concurrency import SessionLimitExceeded
from src.models import Message, SessionInfo
from src.constants import (
    CLIENT_DISCONNECT_TIMEOUT_SECONDS,
    SESSION_CLEANUP_INTERVAL_MINUTES,
    SESSION_MAX_AGE_MINUTES,
)

logger = logging.getLogger(__name__)

# Where the SDK stores per-session transcripts: ``<config dir>/projects``.
# Resolved per call (``_projects_root``) via the session's config layout
# (``uid_layout.projects_root``: the shared config dir's, or under uid isolation
# with per-uid config dirs the session uid's own), instead of being frozen at
# import. ``None`` means "resolve from the environment"; a test may set this
# attribute to pin the root (the historical seam
# ``monkeypatch.setattr(session_manager, "_PROJECTS_ROOT", tmp_path)`` keeps
# working).
_PROJECTS_ROOT: Optional[Path] = None


def _projects_root(user: Optional[str] = None, workspace=None) -> Optional[Path]:
    """The ``projects`` root of (*user*, *workspace*), honoring a test override
    of ``_PROJECTS_ROOT``. ``None`` when the session cannot have transcripts
    (``uid_layout.projects_root``: an anonymous run with no uid lease yet)."""
    if _PROJECTS_ROOT is not None:
        return _PROJECTS_ROOT
    from src.backends.claude import uid_layout

    return uid_layout.projects_root(user, workspace)


def _encode_cwd(cwd) -> str:
    """Encode a workspace cwd to its on-disk Claude SDK directory name.

    The Claude SDK stores per-project transcripts under
    ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``. This delegates to
    the CLI's exact naming (``workspace_sandbox._project_dir_name``: every
    non-alphanumeric UTF-16 unit becomes ``-``, a hash suffix past 200 units,
    pinned in ``tests/test_cli_project_dir_name.py``). A hand-rolled
    ``[/_.]`` rule left ``@`` intact, so an e-mail user's transcript lived in
    a directory this lookup never opened and their sessions could not resume.

    The rule is **lossy** — distinct workspaces such as ``a_b``, ``a.b`` and
    ``a-b`` collapse to the same directory — so ``_try_rehydrate_from_jsonl``
    additionally verifies the transcript's recorded ``cwd`` against the
    requester's workspace before trusting it.
    """
    from src.backends.claude.workspace_sandbox import _project_dir_name

    return _project_dir_name(str(cwd))


def _session_jsonl_path(
    session_id: str, workspace, user: Optional[str] = None
) -> Optional[Path]:
    """Return the on-disk path the SDK uses for this session's transcript.

    Path layout: ``<projects root>/<encoded-cwd>/<session_id>.jsonl``, the root
    being the session's (:func:`_projects_root`); ``None`` when it has none.
    """
    root = _projects_root(user, workspace)
    if root is None:
        return None
    return root / _encode_cwd(workspace) / f"{session_id}.jsonl"


def _session_jsonl_exists(session: "Session") -> bool:
    """True when the SDK has already written a transcript for *session*.

    Raises :class:`TranscriptUnreadable` when the gateway may not even look and
    the broker cannot answer for it (``uid_cleanup.transcript_exists``).
    """
    if not session.workspace:
        return False
    from src.backends.claude import uid_cleanup

    return uid_cleanup.transcript_exists(
        session.session_id, user=session.user, workspace=session.workspace
    )


class TranscriptUnreadable(Exception):
    """A session transcript is on disk, but the gateway may not read it.

    Under ``CLAUDE_ISOLATION_MODE=uid`` the CLI runs as the session uid and
    writes ``<session id>.jsonl`` mode 0600, so the gateway's own uid gets
    EACCES. With per-uid config dirs the broker reads it as that uid instead
    (``uid_cleanup.read_transcript_turns``); this is raised when it cannot —
    no per-uid layout, no broker, or the broker refused or failed — instead of
    the silent ``None`` that answered 404 "not found or expired" for a session
    that is on disk.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"session transcript {path} is not readable by the gateway")
        self.path = path


# Transcript paths whose EACCES was logged at WARNING already. Bounded: once
# full it starts over, so each path then warns once more.
_UNREADABLE_WARNED: set = set()
_UNREADABLE_WARNED_MAX = 4096


def _unreadable_reason(cause: Optional[BaseException]) -> str:
    if isinstance(cause, OSError):
        return str(cause.strerror or cause)
    return str(cause) if cause is not None else "permission denied"


def _warn_unreadable_transcript(path: Path, cause: Optional[BaseException]) -> None:
    key = str(path)
    reason = _unreadable_reason(cause)
    if key in _UNREADABLE_WARNED:
        logger.debug("Session transcript %s is still unreadable: %s", path, reason)
        return
    if len(_UNREADABLE_WARNED) >= _UNREADABLE_WARNED_MAX:
        _UNREADABLE_WARNED.clear()
    _UNREADABLE_WARNED.add(key)
    logger.warning(
        "Cannot rehydrate a session from %s: %s. The gateway's uid may not read "
        "the transcript. Under CLAUDE_ISOLATION_MODE=uid the session uid owns "
        "it, and the broker reads it as that uid only with per-uid config dirs "
        "(CLAUDE_ISOLATION_CONFIG_BASE) and a reachable broker.",
        path,
        reason,
    )


def _try_rehydrate_from_jsonl(session_id: str, *, user: Optional[str], cwd) -> Optional["Session"]:
    """Reconstruct a Session from the Claude SDK on-disk jsonl, if present.

    Returns None when the jsonl file is missing, unreadable, or malformed
    enough that we can't establish a turn count. The caller treats None as
    cache-miss-and-on-disk-miss → existing 404 path. A transcript the
    gateway is not *permitted* to read is counted by the broker as the
    session uid (uid isolation with per-uid config dirs); when that is not
    possible either it raises :class:`TranscriptUnreadable` instead (logged
    once per path), so the caller can say so.
    """
    if not user or not cwd:
        return None
    from src.backends.claude import uid_cleanup
    from src.backends.claude.transcript_turns import TRANSCRIPT_MISMATCH

    # One rule (``transcript_turns.count_transcript_turns``) whoever reads the
    # file: the gateway when it may, else the broker as the session uid.
    # Claude jsonl reuses type="user" for external prompts (one per
    # gateway-issued response_id) and for tool_result blocks / isMeta system
    # reminders the SDK injects; only the former advance the turn counter. A
    # corrupt transcript is refused rather than guessed at.
    try:
        turns = uid_cleanup.read_transcript_turns(session_id, user=user, workspace=cwd)
    except TranscriptUnreadable as exc:
        _warn_unreadable_transcript(exc.path, exc.__cause__)
        raise
    if turns == TRANSCRIPT_MISMATCH:
        # Ownership guard. ``_encode_cwd`` is lossy (every non-alphanumeric
        # collapses to '-'), so distinct workspaces — e.g. users "a_b", "a.b"
        # and "a-b" — share one on-disk transcript directory. The SDK records
        # the REAL cwd on each turn line, so a transcript whose recorded cwd
        # does not match the requester's resolved workspace is refused;
        # otherwise a colliding user could rebuild another user's session and
        # history. (The other workspace is not logged: it names another user.)
        logger.warning(
            "Refusing to rehydrate session %s: its transcript records a cwd "
            "other than requester workspace %r",
            session_id,
            str(cwd),
        )
        return None
    if turns < 0:
        return None
    return Session(
        session_id=session_id,
        backend="claude",
        messages=[],
        turn_counter=turns,
        workspace=str(cwd),
        user=user,
    )


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


# How long a CLI that outlived its capped disconnect gets to die once killed.
_KILL_WAIT_SECONDS = 5.0
# How long shutdown waits for the uid reclaimer's queued removals.
_SHUTDOWN_DRAIN_SECONDS = 5.0


def _client_process(client: Any) -> Any:
    """The CLI subprocess behind an SDK client, or ``None``.

    Private SDK attributes, by necessity: nothing public tells whether a
    disconnect that timed out left the process running.
    """
    return getattr(getattr(client, "_transport", None), "_process", None)


async def _stop_client(client: Any, what: str) -> None:
    """Disconnect *client* within the shared cap, and make sure its CLI is gone.

    The SDK's ``close()`` escalates to SIGKILL well inside
    ``CLIENT_DISCONNECT_TIMEOUT_SECONDS`` (#147), so a CLI still running after
    the cap means the disconnect wedged before it got there. Left alone, that
    CLI would outlive its session, while the teardown that follows removes its
    workspace and so (uid isolation) ends its uid lease, which another run could
    then get. So it is killed. Never raises.
    """
    try:
        await asyncio.wait_for(
            client.disconnect(), timeout=CLIENT_DISCONNECT_TIMEOUT_SECONDS
        )
    except Exception:
        logger.debug(
            "Client disconnect timed out or failed for %s", what, exc_info=True
        )
    process = _client_process(client)
    if process is None or getattr(process, "returncode", 0) is not None:
        return
    logger.warning(
        "The CLI (pid %s) of %s outlived its disconnect; killing it",
        getattr(process, "pid", "?"),
        what,
    )
    try:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.wait(), timeout=_KILL_WAIT_SECONDS)
    except Exception:
        logger.warning(
            "Could not confirm that the CLI of %s exited", what, exc_info=True
        )


def _max_live_sessions() -> int:
    """Read ``MAX_LIVE_SESSIONS`` fresh so tests can patch the constant."""
    from src.constants import MAX_LIVE_SESSIONS

    return MAX_LIVE_SESSIONS


def _active_turn_max_age() -> int:
    """Read ``ACTIVE_TURN_MAX_AGE_SECONDS`` fresh so tests can patch it."""
    from src.constants import ACTIVE_TURN_MAX_AGE_SECONDS

    return ACTIVE_TURN_MAX_AGE_SECONDS


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
    # Which live prompt the snapshot above came from (``system_prompt.get_live_ref``):
    # lets the admin prompt library say how many sessions still run an old version.
    base_prompt_ref: Optional[Dict[str, Any]] = field(default=None, compare=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    user: Optional[str] = None
    workspace: Optional[str] = None

    # ClaudeSDKClient integration
    client: Optional[Any] = None
    # True while a request is creating this session's client: ``create_client``
    # is in flight, so a CLI may already run while ``client`` is still None.
    # Eviction skips the session, and a teardown that removes it meanwhile
    # (a delete, an expiry, shutdown) leaves its dirs (and their uid lease) to
    # that request, which stops the new client before releasing them
    # (``SessionManager.finish_client_start``). ``deferred_release`` keeps the
    # deferred teardown's *finished* flag until then.
    client_starting: bool = field(default=False, repr=False, compare=False)
    deferred_release: Optional[bool] = field(default=None, repr=False, compare=False)
    input_event: Optional[asyncio.Event] = field(default=None, repr=False, compare=False)
    input_response: Optional[str] = None
    pending_tool_call: Optional[Dict[str, Any]] = None
    stream_break_event: Optional[asyncio.Event] = field(default=None, repr=False, compare=False)
    # MCP progress relay registration of the current client (src.mcp_progress_relay).
    mcp_progress_relay: Optional[Any] = field(default=None, repr=False, compare=False)

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
    # ``time.monotonic()`` at _begin_active_response, None between turns.
    # Monotonic (not wall clock) so the pin-age safety valve in
    # ``is_expired`` can't misfire on a host clock jump. Never persisted:
    # a rehydrated session starts unpinned, which is correct — its turn
    # died with the old process.
    active_response_started_at: Optional[float] = None
    # ``time.monotonic()`` of the turn's most recent real SDK chunk
    # (stamped by the route's chunk wrapper, seeded to started_at at begin).
    # The pin-age valve measures NO-PROGRESS time from here — not absolute
    # turn age — so a long-running turn that keeps producing chunks
    # (a 30min+ background job inside its 3600s budget, an unbounded
    # streaming agentic turn) is never reclaimed while it is alive.
    active_response_last_progress_at: Optional[float] = None
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
            #
            # ...unless the "turn" is a zombie. The valve measures
            # NO-PROGRESS time (last real SDK chunk), never absolute turn
            # age: streaming turns are intentionally unbounded and background
            # turns may run to their 3600s budget, so a long turn that keeps
            # producing chunks must keep its pin. A turn silent past
            # ACTIVE_TURN_MAX_AGE is a wedge none of the per-path guards
            # reached — left pinned it holds a live CLI worker, a session
            # slot, and a turn slot until the container is recreated.
            # Letting the expiry sweep reclaim it disconnects the client;
            # that kills the CLI, which errors the wedged stream and lets
            # its own teardown release everything.
            age_cap = _active_turn_max_age()
            progressed = (
                self.active_response_last_progress_at
                or self.active_response_started_at
            )
            if age_cap > 0 and progressed is not None:
                return time.monotonic() - progressed > age_cap
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
        is coming back for this exact session), or still creating the client
        a turn will run on (``client_starting``: its CLI may run already, and
        the request would fail once it is up). Also skips sessions accessed
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
            if session.client_starting is True:
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
                self._release_session_dirs(session, locked=True)
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

        Also serves a session a lookup found expired (``_remove_if_expired``).
        Mirrors the expiry sweep's sequence in ``_purge_all_expired``, with
        the same disconnect cap as ``async_shutdown``
        (``CLIENT_DISCONNECT_TIMEOUT_SECONDS`` — sized so the SDK's own
        SIGTERM→SIGKILL escalation finishes, see constants.py) — disconnect() can
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
                await _stop_client(
                    session.client, f"evicted session {session.session_id}"
                )
                session.client = None
            self._release_session_dirs(session)
        except Exception:
            logger.exception(
                "Eviction teardown failed for session %s", session.session_id
            )

    def _remove_if_expired(self, session_id: str) -> bool:
        """Remove *session_id* if present and expired.

        Returns ``True`` when the session was expired and removed. Caller holds
        the lock. What the session holds is released as on the expiry sweep:
        a lookup that found it expired first used to drop it with its CLI still
        running, its temp workspace on disk and, under uid isolation, its
        ephemeral uid leased for good. A client is disconnected in a background
        teardown, since that has to be awaited; without a running loop none can
        be scheduled, and the session is dropped as before.
        """
        session = self.sessions.get(session_id)
        if session is not None and session.is_expired():
            del self.sessions[session_id]
            if session.client is None:
                _cancel_idle_reader(session)
                self._release_session_dirs(session, locked=True)
            else:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    task = loop.create_task(self._teardown_evicted(session))
                    self._evict_tasks.add(task)
                    task.add_done_callback(self._evict_tasks.discard)
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
                    # Capped like every other awaited disconnect here (see
                    # async_shutdown): disconnect() can hang on a dead anyio
                    # channel, and a wedged sweep would block all future
                    # out-of-band sweeps AND the periodic cleanup loop —
                    # session expiry would stop for the process lifetime.
                    await _stop_client(session.client, f"expired session {sid}")
                    session.client = None
                self._release_session_dirs(session)
            except Exception:
                logger.exception("Expiry teardown failed for session %s", sid)

        if doomed:
            # Concurrent teardown, mirroring async_shutdown's snapshot pass.
            # The doomed list is by now the ONLY reference to these sessions,
            # so async_shutdown must be able to await an in-flight sweep at a
            # one disconnect-cap bound rather than MAX_LIVE_SESSIONS x cap serial.
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
            self._release_session_dirs(session, locked=True)
            del self.sessions[sid]
            logger.info(f"Cleaned up expired session: {sid}")
            removed += 1
        return removed

    def _release_session_dirs(
        self, session: "Session", *, finished: bool = False, locked: bool = False
    ) -> None:
        """Remove what a session leaving the manager owns on disk.

        Always its temp workspace (only an anonymous ``_tmp_`` one is removed).
        Its ``session-env/<session id>`` dir (the CLI's ``$CLAUDE_ENV_FILE``
        hook scripts; the CLI's session id is the gateway's) only once the
        session is finished: *finished* (an explicit delete), or any teardown
        of an anonymous session, which nothing can resume. A named session torn
        down by expiry, eviction or shutdown keeps it: a later continuation
        rehydrates the same id, and the resumed CLI fires SessionStart with
        ``source=resume`` only, so startup hooks do not run again to restore
        the env they gave Bash (#244 review). In the confined modes the dir of
        a named session that is never resumed stays behind (a few small files;
        the CLI's own ``cleanupPeriodDays`` sweep cannot reach it from inside
        Landlock, #241).

        Nothing is removed while a request is still creating the session's
        client (``client_starting``): its CLI may run already, in that workspace
        and under that uid lease. The teardown is recorded instead, and the
        request releases the dirs once it has stopped the new client
        (:meth:`finish_client_start`).

        *locked*: the caller holds ``self.lock``, which is not re-entrant.
        """
        if locked:
            deferred = self._defer_release_locked(session, finished)
        else:
            with self.lock:
                deferred = self._defer_release_locked(session, finished)
        if deferred:
            return
        if session.workspace:
            self._cleanup_workspace(session.workspace)
        if session.user and not finished:
            return
        if locked:
            self._remove_session_env_locked(session)
        else:
            with self.lock:
                self._remove_session_env_locked(session)

    @staticmethod
    def _defer_release_locked(session: "Session", finished: bool) -> bool:
        """Record a teardown that has to wait for a client still starting
        (see :meth:`_release_session_dirs`); ``False`` when none is. Caller
        holds ``self.lock``."""
        if getattr(session, "client_starting", False) is not True:
            return False
        previous = getattr(session, "deferred_release", None)
        session.deferred_release = finished or bool(previous)
        logger.info(
            "Session %s ended while its client was starting; its dirs wait for "
            "that client to be stopped",
            session.session_id,
        )
        return True

    def _remove_session_env_locked(self, session: "Session") -> None:
        """Remove *session*'s ``session-env`` dir unless a live session now
        owns its id. Caller holds ``self.lock``.

        A continuation can rehydrate the same id (a new :class:`Session` whose
        CLI is granted that same dir) while the old incarnation's teardown
        awaits its disconnect. Removing the dir then pulls it from under the
        live CLI (#244 review). The check and the removal both run under the
        lock, so no rehydrate can slip in between. Under uid isolation with
        per-uid config dirs, what the gateway's uid could not remove goes to
        the broker in the background; that removal asks the same question
        again before each attempt (``guard``), so it stops once a rehydrated
        session holds the id (it cannot hold the lock across the broker's
        round trip, so a rehydrate in that one window is not covered).
        """
        current = self.sessions.get(session.session_id)
        if current is not None and current is not session:
            return

        def _still_finished() -> bool:
            with self.lock:
                now = self.sessions.get(session.session_id)
            return now is None or now is session

        try:
            from src.backends.claude import uid_cleanup

            uid_cleanup.remove_session_dirs(
                session.session_id,
                user=session.user,
                workspace=session.workspace,
                guard=_still_finished,
            )
        except Exception:
            logger.debug(
                "session-env cleanup skipped for %s", session.session_id, exc_info=True
            )

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
        # capped at CLIENT_DISCONNECT_TIMEOUT_SECONDS.
        sweep = self._sweep_task
        if sweep is not None and not sweep.done():
            with contextlib.suppress(Exception):
                await sweep

        # Evicted sessions are already out of self.sessions, so the snapshot
        # below cannot see them — settle their in-flight teardowns first or
        # their CLI subprocesses outlive the gateway. Loop: a concurrent
        # admission can evict (scheduling another teardown) while we await.
        # Bounded: each teardown caps its disconnect and never raises,
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
            await _stop_client(session.client, f"session {session.session_id}")
            session.client = None

        if sessions_snapshot:
            await asyncio.gather(
                *(_disconnect(s) for s in sessions_snapshot),
                return_exceptions=True,
            )

        with self.lock:
            self._cleanup_all_temp_workspaces()
            self.sessions.clear()

        # Under uid isolation with per-uid config dirs the removals above run
        # on the uid reclaimer's thread (uid_cleanup), which dies with the
        # process: give them a bounded chance to finish. What is left, the next
        # start's orphan sweep reclaims.
        try:
            from src.backends.claude import uid_cleanup

            if uid_cleanup.pending():
                await asyncio.to_thread(uid_cleanup.drain, _SHUTDOWN_DRAIN_SECONDS)
        except Exception:
            logger.debug("uid cleanup drain at shutdown failed", exc_info=True)
        logger.info("Session manager async shutdown complete")

    def _cleanup_all_temp_workspaces(self) -> None:
        """Remove temporary workspaces for all active sessions.

        Called during shutdown to prevent ``_tmp_*`` directory leaks.
        Must be called while holding ``self.lock``.
        """
        for session in self.sessions.values():
            self._release_session_dirs(session, locked=True)

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

        Raises :class:`TranscriptUnreadable` when the transcript is on disk
        but the gateway may not read it (uid isolation).
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
            try:
                session = _try_rehydrate_from_jsonl(session_id, user=user, cwd=cwd)
            except TranscriptUnreadable:
                self._rehydrate_misses = getattr(self, "_rehydrate_misses", 0) + 1
                raise
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
        self._release_session_dirs(session, finished=True)
        logger.info(f"Deleted session: {session_id}")
        return True

    async def delete_session_async(
        self, session_id: str, *, finished: bool = True
    ) -> bool:
        """Delete a session, disconnecting its client and cleaning temp workspace.

        ``finished=False`` is for a caller whose named session a later
        continuation may still resume (a failed client creation): its CLI
        ``session-env`` dir then stays, as on expiry (see
        :meth:`_release_session_dirs`).
        """
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False

        _cancel_idle_reader(session)
        if session.client is not None:
            await _stop_client(session.client, f"session {session_id}")
            session.client = None

        self._release_session_dirs(session, finished=finished)

        logger.info(f"Deleted session: {session_id}")
        return True

    def begin_client_start(self, session: Session) -> None:
        """Mark that the caller is creating *session*'s client (``create_client``).

        Until :meth:`finish_client_start`, eviction skips the session and a
        teardown leaves its dirs alone (see ``Session.client_starting``).
        """
        with self.lock:
            session.client_starting = True

    async def finish_client_start(
        self, session_id: str, session: Session, client: Any
    ) -> bool:
        """End a :meth:`begin_client_start`, handing over the new *client*.

        ``True`` when the manager still holds *session*: *client* (``None``
        when its creation failed) becomes its client, under the lock, so a
        teardown from now on sees and stops it. ``False`` when the session
        ended while the client was starting (a delete, an expiry, shutdown):
        nothing holds the session any more, so *client* is stopped here (the
        disconnect cap, then a kill), and only then are the session's dirs
        released, with the deferred teardown's *finished* flag. Removing the
        workspace (under uid isolation, the uid lease) before that would hand
        the uid of a running CLI to the next run (#244 review). Never raises.
        """
        with self.lock:
            live = self.sessions.get(session_id) is session
            if live:
                if client is not None:
                    session.client = client
                session.client_starting = False
                session.deferred_release = None
                return True
        # The session ended. ``client_starting`` stays set while the new client
        # is stopped: a teardown that only now gets to run (the expiry sweep
        # releases from a later task) must still defer to us, or it would
        # release the workspace and uid lease under a CLI still running.
        if client is not None:
            logger.warning(
                "Session %s ended while its client was starting; stopping the "
                "new client",
                session_id,
            )
            await _stop_client(client, f"ended session {session_id}")
        with self.lock:
            session.client_starting = False
            finished = bool(getattr(session, "deferred_release", None))
            session.deferred_release = None
        self._release_session_dirs(session, finished=finished)
        return False

    def oldest_active_turn_silence(self) -> float:
        """Longest current no-progress stretch across in-flight turns, 0.0 when none.

        Read at metrics scrape time
        (``gateway_oldest_active_turn_silence_seconds``). Measures seconds
        since each turn's last real SDK chunk — NOT absolute turn age, which
        is meaningless as a wedge signal now that healthy turns are unbounded
        while progressing. A value stuck above the stall/pin-age budgets is
        the visible symptom of a wedged turn pinning its session — the
        failure mode that previously accumulated silently until the slot
        caps were exhausted.
        """
        now = time.monotonic()
        with self.lock:
            marks = [
                s.active_response_last_progress_at or s.active_response_started_at
                for s in self.sessions.values()
                if s.active_response_id is not None
            ]
        return max((now - mark for mark in marks if mark is not None), default=0.0)

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
