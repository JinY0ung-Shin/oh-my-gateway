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

from src.models import Message, SessionInfo
from src.constants import SESSION_CLEANUP_INTERVAL_MINUTES, SESSION_MAX_AGE_MINUTES

logger = logging.getLogger(__name__)

_CWD_ENCODE_RE = re.compile(r"[/_.]")
_PROJECTS_ROOT: Path = Path.home() / ".claude" / "projects"


def _encode_cwd(cwd) -> str:
    """Encode a workspace cwd to its on-disk Claude SDK directory name.

    The Claude SDK stores per-project transcripts under
    ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``. The encoding
    rule observed across recorded sessions: every ``/``, ``_`` and ``.``
    is replaced with ``-``. (To be re-verified against SDK source if the
    rule changes — see plan Task C-followup.)
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


def _try_rehydrate_from_jsonl(
    session_id: str, *, user: Optional[str], cwd
) -> Optional["Session"]:
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
        user_msg_count = 0
        with jsonl_path.open("r") as fh:
            for raw in fh:
                try:
                    line = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    return None  # corrupt — refuse to guess
                if line.get("type") == "user":
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

    def is_expired(self) -> bool:
        """Check if the session has expired."""
        return _utcnow() > self.expires_at

    def to_session_info(self) -> SessionInfo:
        """Convert to SessionInfo model for API responses."""
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
        self._rehydrate_hits: int = 0
        self._rehydrate_misses: int = 0

    # ------------------------------------------------------------------
    # Internal helpers (caller must hold self.lock)
    # ------------------------------------------------------------------

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
            expired = [
                (sid, self.sessions[sid]) for sid, s in self.sessions.items() if s.is_expired()
            ]
        count = 0
        for sid, session in expired:
            if session.client is not None:
                try:
                    await session.client.disconnect()
                except Exception:
                    logger.debug("Client disconnect failed for session %s", sid, exc_info=True)
                session.client = None
            if session.workspace:
                self._cleanup_workspace(session.workspace)
            with self.lock:
                # Re-check: session might have been refreshed since snapshot
                current = self.sessions.get(sid)
                if current is session and current.is_expired():
                    del self.sessions[sid]
                    logger.info(f"Cleaned up expired session: {sid}")
                    count += 1
        return count

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
            pass  # Registry may not be ready during tests/shutdown

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

        with self.lock:
            sessions_snapshot = list(self.sessions.values())

        # Disconnect in parallel with a per-client timeout — ClaudeSDKClient.disconnect()
        # can hang if its internal anyio channel is already dead (common after long-running
        # servers accumulate stale sessions).
        async def _disconnect(session: "Session") -> None:
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
        """
        with self.lock:
            if session_id in self.sessions:
                if self._remove_if_expired(session_id):
                    logger.info(f"Session {session_id} expired, creating new session")
                else:
                    self.sessions[session_id].touch()
                    return self.sessions[session_id]

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
    ) -> Optional[Session]:
        """Return a session by id; rehydrate from jsonl on cache miss when context permits.

        Returns ``None`` when the session does not exist, is expired, and
        cannot be rehydrated from disk.
        """
        with self.lock:
            self._remove_if_expired(session_id)
            session = self.sessions.get(session_id)
            if session is not None:
                session.touch()
                return session
            # Cache miss path — try rehydrate
            session = _try_rehydrate_from_jsonl(session_id, user=user, cwd=cwd)
            if session is not None:
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
            return {
                "active_sessions": len(self.sessions),
                "rehydrate_hits": self._rehydrate_hits,
                "rehydrate_misses": self._rehydrate_misses,
            }


# Global session manager instance
session_manager = SessionManager(
    default_ttl_minutes=SESSION_MAX_AGE_MINUTES,
    cleanup_interval_minutes=SESSION_CLEANUP_INTERVAL_MINUTES,
)
