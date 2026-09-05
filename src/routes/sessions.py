"""Session management endpoints (/v1/sessions)."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials

from src.models import SessionListResponse
from src.auth import get_authenticated_user, verify_api_key, security
from src.session_manager import session_manager
from src.session_outbox import (
    get_outbox,
    idle_reader_running,
    resume_idle_reader,
)

router = APIRouter()


def _session_for_request(request: Request, session_id: str):
    """Return a visible session without touching a foreign tenant's TTL."""
    session = session_manager.peek_session(session_id)
    auth_user = get_authenticated_user(request)
    if session is None or (auth_user is not None and session.user != auth_user):
        raise HTTPException(status_code=404, detail="Session not found")
    session.touch()
    return session


@router.get("/v1/sessions/stats")
async def get_session_stats(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Get session manager statistics."""
    await verify_api_key(request, credentials)
    stats = session_manager.get_stats()
    rehydrate_stats = session_manager.stats()
    return {
        "session_stats": stats,
        "cleanup_interval_minutes": session_manager.cleanup_interval_minutes,
        "default_ttl_minutes": session_manager.default_ttl_minutes,
        "rehydrate_hits": rehydrate_stats["rehydrate_hits"],
        "rehydrate_misses": rehydrate_stats["rehydrate_misses"],
    }


@router.get("/v1/sessions")
async def list_sessions(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """List active sessions visible to the authenticated caller."""
    await verify_api_key(request, credentials)
    sessions = session_manager.list_sessions()
    auth_user = get_authenticated_user(request)
    if auth_user is not None:
        sessions = [
            info
            for info in sessions
            if (session := session_manager.peek_session(info.session_id)) is not None
            and session.user == auth_user
        ]
    return SessionListResponse(sessions=sessions, total=len(sessions))


@router.get("/v1/sessions/{session_id}")
async def get_session(
    request: Request,
    session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Get information about a specific visible session."""
    await verify_api_key(request, credentials)
    session = _session_for_request(request, session_id)
    return session.to_session_info()


@router.get("/v1/sessions/{session_id}/pending-events")
async def get_session_pending_events(
    request: Request,
    session_id: str,
    after: int = 0,
    user: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Between-turn events captured by the session's idle reader.

    Serves the outbox populated while no Responses turn is reading the SDK
    client: background task lifecycle (``task_started`` / ``task_progress`` /
    ``task_notification`` / ``task_updated``) and any assistant messages the
    harness produced when a background task finished. ``after`` is the last
    seq the caller has seen; poll again with the returned ``next_after``.

    Polling touches the session TTL, so a watched session (and the SDK client
    owning its background processes) stays alive while a client keeps polling.
    Credential-scoped callers are bound to their authenticated user. Legacy
    service-key callers retain the optional ``user`` query scoping behavior.
    """
    await verify_api_key(request, credentials)
    session = session_manager.peek_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    auth_user = get_authenticated_user(request)
    effective_user = auth_user if auth_user is not None else user
    if effective_user is not None and session.user != effective_user:
        raise HTTPException(status_code=404, detail="Session not found")
    session.touch()

    # Self-heal: a turn path that ended without restarting the reader (or a
    # gateway that just processed its first poll) starts it here. Gated inside
    # resume_idle_reader — never touches a client mid-turn.
    resume_idle_reader(session)

    outbox = get_outbox(session)
    events = outbox.events_after(after)
    # No events: clamp the cursor to the highest seq that exists so a caller
    # holding a stale-high cursor (e.g. after a session rehydrate reset the
    # outbox) recovers instead of polling past the end forever.
    next_after = events[-1]["seq"] if events else min(after, outbox.next_seq - 1)
    return {
        "session_id": session_id,
        "events": events,
        "next_after": next_after,
        "active_tasks": outbox.snapshot_active_tasks(),
        "reader_active": idle_reader_running(session),
        "turn_in_progress": session.active_response_id is not None
        or session.lock.locked(),
        "client_connected": session.client is not None,
    }


@router.delete("/v1/sessions/{session_id}")
async def delete_session(
    request: Request,
    session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Delete a specific visible session."""
    await verify_api_key(request, credentials)
    session = session_manager.peek_session(session_id)
    auth_user = get_authenticated_user(request)
    if session is None or (auth_user is not None and session.user != auth_user):
        raise HTTPException(status_code=404, detail="Session not found")

    deleted = await session_manager.delete_session_async(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"message": f"Session {session_id} deleted successfully"}
