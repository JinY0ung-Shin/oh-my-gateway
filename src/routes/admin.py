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
Logs:           GET  /admin/api/logs
Rate limits:    GET  /admin/api/rate-limits
Session msgs:   GET  /admin/api/sessions/{session_id}/messages
Skills:         GET/PUT/DELETE /admin/api/skills/{name}
System prompt:  GET/PUT/DELETE /admin/api/system-prompt
"""

import logging
from typing import Any, Optional

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
    create_or_update_skill,
    delete_skill,
    export_session_json,
    get_backends_health,
    get_mcp_servers_detail,
    get_redacted_config,
    get_sandbox_config,
    get_session_detail,
    get_session_messages,
    get_skill,
    get_tools_registry,
    list_skills,
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


class RuntimeConfigUpdate(BaseModel):
    key: str
    value: Any


class SkillWriteRequest(BaseModel):
    content: str
    etag: Optional[str] = None


class SystemPromptUpdate(BaseModel):
    prompt: str


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
# Backend health & auth
# ---------------------------------------------------------------------------


@router.get("/api/backends")
async def get_backends(_=Depends(require_admin)):
    """Detailed backend health, auth status, and model availability."""
    return {"backends": await get_backends_health()}


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


@router.get("/api/mcp-servers")
async def get_mcp_servers_endpoint(_=Depends(require_admin)):
    """Return detailed MCP server configuration and tool patterns."""
    return {"servers": get_mcp_servers_detail()}


# ---------------------------------------------------------------------------
# Sandbox & permissions
# ---------------------------------------------------------------------------


@router.get("/api/sandbox")
async def get_sandbox(_=Depends(require_admin)):
    """Return sandbox and permission mode configuration."""
    return get_sandbox_config()


# ---------------------------------------------------------------------------
# Tools registry
# ---------------------------------------------------------------------------


@router.get("/api/tools")
async def get_tools(_=Depends(require_admin)):
    """Return available tools per backend and MCP tool patterns."""
    return get_tools_registry()


# ---------------------------------------------------------------------------
# Session detail & export
# ---------------------------------------------------------------------------


@router.get("/api/sessions/{session_id}/detail")
async def get_session_detail_endpoint(session_id: str, _=Depends(require_admin)):
    """Return detailed session metadata (backend, turns, TTL, etc)."""
    detail = get_session_detail(session_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return detail


@router.get("/api/sessions/{session_id}/export")
async def export_session_endpoint(session_id: str, _=Depends(require_admin)):
    """Export full session data as JSON."""
    data = export_session_json(session_id)
    if data is None:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return data


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


# ---------------------------------------------------------------------------
# Request logs (Feature 1)
# ---------------------------------------------------------------------------


@router.get("/api/logs")
async def get_logs(
    endpoint: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _=Depends(require_admin),
):
    """Return paginated request logs with summary stats.

    *status* accepts an exact code (``200``) or a class prefix (``4xx``, ``5xx``).
    """
    from src.request_logger import request_logger

    return request_logger.query(
        endpoint=endpoint,
        status=status,
        limit=min(limit, 200),
        offset=max(offset, 0),
    )


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------


@router.get("/api/metrics")
async def get_metrics(_=Depends(require_admin)):
    """Return performance metrics derived from request logs."""
    from src.request_logger import request_logger

    data = request_logger.query(limit=0)  # stats over all buffered entries
    return {
        "stats": data.get("stats", {}),
        "total_logged": data.get("total_logged", 0),
        "buffer_size": data.get("total", 0),
    }


# ---------------------------------------------------------------------------
# Rate limit monitoring (Feature 2)
# ---------------------------------------------------------------------------


@router.get("/api/rate-limits")
async def get_rate_limits(_=Depends(require_admin)):
    """Return approximate rate-limit usage derived from request logs.

    This is an approximation — actual enforcement is handled by slowapi.
    """
    from src.request_logger import request_logger

    return {
        "snapshot": request_logger.get_rate_limit_snapshot(),
        "_note": "Approximate monitoring based on request logs. "
        "Actual enforcement is handled by the rate limiter (slowapi).",
    }


# ---------------------------------------------------------------------------
# Session message history (Feature 3)
# ---------------------------------------------------------------------------


@router.get("/api/sessions/{session_id}/messages")
async def get_session_history(
    session_id: str,
    truncate: int = 500,
    _=Depends(require_admin),
):
    """Return message history for a session (read-only, no TTL refresh).

    Content may contain sensitive user data.
    """
    messages = get_session_messages(session_id, truncate=max(truncate, 0))
    if messages is None:
        return JSONResponse(status_code=404, content={"error": "Session not found"})
    return {
        "session_id": session_id,
        "messages": messages,
        "total": len(messages),
        "_warning": "Message content may contain sensitive user data.",
    }


# ---------------------------------------------------------------------------
# Runtime configuration (hot-reload)
# ---------------------------------------------------------------------------


@router.get("/api/runtime-config")
async def get_runtime_config(_=Depends(require_admin)):
    """Return all editable runtime settings with current values."""
    from src.runtime_config import runtime_config

    return {"settings": runtime_config.get_all()}


@router.patch("/api/runtime-config")
async def update_runtime_config(body: RuntimeConfigUpdate, _=Depends(require_admin)):
    """Update a single runtime setting. Takes effect on next request."""
    from src.runtime_config import runtime_config

    try:
        runtime_config.set(body.key, body.value)
        return {
            "status": "updated",
            "key": body.key,
            "value": runtime_config.get(body.key),
        }
    except KeyError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except (ValueError, TypeError) as e:
        return JSONResponse(status_code=422, content={"error": f"Invalid value: {e}"})


@router.post("/api/runtime-config/reset")
async def reset_runtime_config(
    key: Optional[str] = None,
    _=Depends(require_admin),
):
    """Reset runtime overrides. If *key* is given, reset that key only."""
    from src.runtime_config import runtime_config

    if key:
        try:
            runtime_config.reset(key)
        except KeyError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"status": "reset", "key": key, "value": runtime_config.get(key)}
    runtime_config.reset_all()
    return {"status": "all_reset"}


# ---------------------------------------------------------------------------
# Skills management
# ---------------------------------------------------------------------------


@router.get("/api/skills")
async def list_skills_endpoint(_=Depends(require_admin)):
    """List all skills with parsed metadata."""
    try:
        return {"skills": list_skills()}
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@router.get("/api/skills/{name}")
async def get_skill_endpoint(name: str, _=Depends(require_admin)):
    """Read a skill's SKILL.md content and parsed metadata."""
    try:
        meta, content, etag = get_skill(name)
        return {"name": name, "metadata": meta, "content": content, "etag": etag}
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.put("/api/skills/{name}")
async def put_skill_endpoint(
    name: str,
    body: SkillWriteRequest,
    _=Depends(require_admin),
    if_match: Optional[str] = Header(None),
):
    """Create or update a skill. Supports If-Match for optimistic concurrency."""
    expected_etag = body.etag or if_match
    try:
        new_etag, created = create_or_update_skill(name, body.content, expected_etag)
        return JSONResponse(
            status_code=201 if created else 200,
            content={"name": name, "etag": new_etag, "status": "created" if created else "updated"},
        )
    except ValueError as e:
        error_msg = str(e)
        if "ETag mismatch" in error_msg:
            return JSONResponse(status_code=409, content={"error": error_msg})
        return JSONResponse(status_code=400, content={"error": error_msg})


@router.delete("/api/skills/{name}")
async def delete_skill_endpoint(name: str, _=Depends(require_admin)):
    """Delete a skill and its directory."""
    try:
        delete_skill(name)
        return {"name": name, "status": "deleted"}
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


# ---------------------------------------------------------------------------
# System Prompt Management
# ---------------------------------------------------------------------------


@router.get("/api/system-prompt")
async def get_system_prompt_endpoint(_=Depends(require_admin)):
    """Return the current system prompt and its mode."""
    from src.system_prompt import (
        get_default_prompt,
        get_preset_text,
        get_prompt_mode,
        get_system_prompt,
    )

    prompt = get_system_prompt()
    return {
        "mode": get_prompt_mode(),
        "prompt": prompt,
        "default_prompt": get_default_prompt(),
        "preset_text": get_preset_text(),
        "char_count": len(prompt) if prompt else 0,
    }


@router.put("/api/system-prompt")
async def set_system_prompt_endpoint(
    body: SystemPromptUpdate,
    _=Depends(require_admin),
):
    """Set a custom system prompt. Only affects new sessions."""
    from src.system_prompt import get_prompt_mode, set_system_prompt

    try:
        set_system_prompt(body.prompt)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to persist: {e}"})
    return {
        "status": "updated",
        "mode": get_prompt_mode(),
        "char_count": len(body.prompt.strip()),
    }


@router.delete("/api/system-prompt")
async def reset_system_prompt_endpoint(_=Depends(require_admin)):
    """Reset to file default or claude_code preset."""
    from src.system_prompt import get_prompt_mode, reset_system_prompt

    try:
        reset_system_prompt()
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to persist: {e}"})
    return {"status": "reset", "mode": get_prompt_mode()}
