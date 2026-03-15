"""Admin API routes — thin handlers that delegate to admin_service and admin_auth.

All endpoints live under ``/admin`` and require admin authentication
(separate ``ADMIN_API_KEY``, not the regular gateway ``API_KEY``).

HTML page:      GET  /admin
Login:          POST /admin/api/login
Logout:         POST /admin/api/logout
Dashboard:      GET  /admin/api/summary
File tree:      GET  /admin/api/files
File read:      GET  /admin/api/files/{path:path}
File write:     PUT  /admin/api/files/{path:path}
Config:         GET  /admin/api/config
Session delete: DELETE /admin/api/sessions/{session_id}
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.admin_auth import (
    get_admin_status,
    login,
    logout,
    require_admin,
)
from src.admin_service import (
    get_redacted_config,
    list_workspace_files,
    read_file,
    write_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    api_key: str


class FileWriteRequest(BaseModel):
    content: str
    etag: Optional[str] = None


# ---------------------------------------------------------------------------
# HTML page (no auth — page itself handles login UI)
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def admin_page():
    """Serve the admin dashboard HTML."""
    from src.admin_page import build_admin_page

    return HTMLResponse(build_admin_page())


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@router.post("/api/login")
async def admin_login(body: LoginRequest, response: Response):
    """Authenticate with admin API key and receive a session cookie."""
    return login(body.api_key, response)


@router.post("/api/logout")
async def admin_logout(response: Response, _=Depends(require_admin)):
    """Clear admin session."""
    return logout(response)


@router.get("/api/status")
async def admin_status():
    """Return admin UI status (enabled, configured — no secrets)."""
    return get_admin_status()


# ---------------------------------------------------------------------------
# Dashboard summary (aggregates multiple existing endpoints)
# ---------------------------------------------------------------------------


@router.get("/api/summary")
async def admin_summary(_=Depends(require_admin)):
    """Single endpoint to bootstrap the admin dashboard.

    Aggregates health, models, sessions, and auth into one response
    to minimise client round-trips on page load.
    """
    from src.backends.base import BackendRegistry
    from src.session_manager import session_manager
    from src.auth import get_all_backends_auth_info

    # Models
    models = []
    for name, backend in BackendRegistry.all_backends().items():
        for m in backend.supported_models():
            models.append({"id": m, "backend": name})

    # Sessions
    sessions_data = session_manager.list_sessions()

    # Auth
    auth_info = get_all_backends_auth_info()

    # Health (lightweight — just check backend registration)
    backends_health = {}
    for name in BackendRegistry.all_backends():
        backends_health[name] = "registered"

    return {
        "health": {"status": "ok", "backends": backends_health},
        "models": models,
        "sessions": {
            "active": len(sessions_data),
            "sessions": sessions_data[:50],  # Cap for dashboard
        },
        "auth": auth_info,
        "admin": get_admin_status(),
    }


# ---------------------------------------------------------------------------
# Workspace file management
# ---------------------------------------------------------------------------


@router.get("/api/files")
async def list_files(_=Depends(require_admin)):
    """List allowlisted workspace files."""
    try:
        return {"files": list_workspace_files()}
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@router.get("/api/files/{file_path:path}")
async def get_file(file_path: str, _=Depends(require_admin)):
    """Read a workspace file. Returns content and ETag."""
    try:
        content, etag = read_file(file_path)
        return {"path": file_path, "content": content, "etag": etag}
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})


@router.put("/api/files/{file_path:path}")
async def put_file(
    file_path: str,
    body: FileWriteRequest,
    _=Depends(require_admin),
    if_match: Optional[str] = Header(None),
):
    """Write a workspace file. Supports If-Match for optimistic concurrency."""
    expected_etag = body.etag or if_match
    try:
        new_etag = write_file(file_path, body.content, expected_etag)
        return {"path": file_path, "etag": new_etag, "status": "saved"}
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        error_msg = str(e)
        if "ETag mismatch" in error_msg:
            return JSONResponse(status_code=409, content={"error": error_msg})
        return JSONResponse(status_code=400, content={"error": error_msg})


# ---------------------------------------------------------------------------
# Session management (proxied through admin auth boundary)
# ---------------------------------------------------------------------------


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, _=Depends(require_admin)):
    """Delete a session. Proxied from admin so it stays within admin auth."""
    from src.session_manager import session_manager

    deleted = session_manager.delete_session(session_id)
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return {"status": "deleted", "session_id": session_id}


# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------


@router.get("/api/config")
async def get_config(_=Depends(require_admin)):
    """Return redacted runtime configuration."""
    return get_redacted_config()
