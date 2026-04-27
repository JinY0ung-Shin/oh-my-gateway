"""Session management endpoints (/v1/sessions)."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials

from src.models import SessionListResponse
from src.auth import verify_api_key, security
from src.session_manager import session_manager

router = APIRouter()


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
    """List all active sessions."""
    await verify_api_key(request, credentials)
    sessions = session_manager.list_sessions()
    return SessionListResponse(sessions=sessions, total=len(sessions))


@router.get("/v1/sessions/{session_id}")
async def get_session(
    request: Request,
    session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Get information about a specific session."""
    await verify_api_key(request, credentials)
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session.to_session_info()


@router.delete("/v1/sessions/{session_id}")
async def delete_session(
    request: Request,
    session_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Delete a specific session."""
    await verify_api_key(request, credentials)
    deleted = await session_manager.delete_session_async(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"message": f"Session {session_id} deleted successfully"}
