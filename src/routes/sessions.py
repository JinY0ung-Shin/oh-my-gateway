"""Session management endpoints (/v1/sessions)."""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials

from src.models import SessionListResponse
from src.auth import get_authenticated_user, verify_api_key, security
from src.backends import BackendRegistry
from src.backends.claude.client import TaskStopClientUnavailable, TaskStopRejected
from src.rate_limiter import rate_limit_endpoint
from src.session_manager import session_manager
from src.session_outbox import (
    get_outbox,
    idle_reader_running,
    resume_idle_reader_between_turns,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# One stop_task control round-trip. Well under the SDK's own 60 s control
# timeout: the CLI answers in milliseconds, so a reply this late means a
# wedged CLI, and a stop button should fail fast instead of hanging.
TASK_STOP_TIMEOUT_S = 10.0


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
    """Get gateway-wide session manager statistics for an unscoped service key."""
    await verify_api_key(request, credentials)
    if get_authenticated_user(request) is not None:
        raise HTTPException(
            status_code=403,
            detail="Session statistics require an unscoped service key",
        )
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
    # gateway that just processed its first poll) starts it here. Gated — never
    # touches a client mid-turn, non-streaming turns (session lock) included.
    resume_idle_reader_between_turns(session)

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


@router.post("/v1/sessions/{session_id}/tasks/{task_id}/stop", status_code=202)
@rate_limit_endpoint("responses")
async def stop_session_task(
    request: Request,
    session_id: str,
    task_id: str,
    user: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Stop one running task (subagent, background shell) of a session.

    ``task_id`` must be listed in the session's ``active_tasks`` (see
    ``pending-events``). The stop goes to the session's live CLI mid-turn or
    between turns, and the turn itself keeps running. 202 means the backend
    accepted the request: the task's end arrives asynchronously as
    ``task_updated`` (status ``killed``) and/or ``task_notification`` (status
    ``stopped``) in the turn stream or the outbox, which also drops it from
    ``active_tasks``. 504 leaves the outcome unknown: the request may already
    be with the CLI and still take effect, so re-poll ``active_tasks``.

    Scoping matches ``pending-events``: credential-scoped callers are bound to
    their authenticated user; legacy service-key callers may scope with
    ``user``.
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

    # The CLI answers success for ids it does not know or that already ended,
    # so the registry pending-events serves is the only "not found" signal —
    # and it keeps arbitrary ids from reaching the CLI at all.
    if task_id not in get_outbox(session).active_tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        backend = BackendRegistry.get(session.backend)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Backend unavailable") from exc
    stop_task_client = getattr(backend, "stop_task_client", None)
    if not callable(stop_task_client):
        raise HTTPException(
            status_code=400,
            detail=f"Backend '{session.backend}' does not support task stop",
        )
    # The persistent client owns the CLI process and with it every task: a
    # turn publishes this same object as ``active_response_client``, and the
    # idle reader drains it between turns.
    client = session.client
    if client is None:
        raise HTTPException(status_code=409, detail="Session has no live client")

    # Start the idle reader BEFORE sending (gated; never touches a client
    # mid-turn — a turn's own reader drains the stream then). The SDK routes
    # the control reply through the same read loop that feeds its bounded
    # message stream, so between turns an undrained stream would hold the
    # reply back until the timeout; the reader also captures the terminal
    # task_updated.
    resume_idle_reader_between_turns(session)
    logger.info("Stopping task %s in session %s", task_id, session_id)
    try:
        await asyncio.wait_for(
            stop_task_client(client, task_id), timeout=TASK_STOP_TIMEOUT_S
        )
    except TaskStopRejected as exc:
        logger.warning(
            "Backend rejected stop of task %s in session %s: %s",
            task_id,
            session_id,
            exc,
        )
        raise HTTPException(
            status_code=409, detail=f"Task could not be stopped: {exc}"
        ) from exc
    except TaskStopClientUnavailable as exc:
        logger.warning(
            "Cannot stop task %s in session %s: client not connected (%s)",
            task_id,
            session_id,
            exc,
        )
        raise HTTPException(
            status_code=409, detail="Session has no live client"
        ) from exc
    except (asyncio.TimeoutError, TimeoutError) as exc:
        logger.warning("Timed out stopping task %s in session %s", task_id, session_id)
        raise HTTPException(
            status_code=504, detail="Timed out waiting for the task stop"
        ) from exc
    except Exception as exc:
        logger.warning(
            "Failed to stop task %s in session %s",
            task_id,
            session_id,
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Failed to stop task") from exc

    return {"session_id": session_id, "task_id": task_id, "status": "stop_requested"}


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
