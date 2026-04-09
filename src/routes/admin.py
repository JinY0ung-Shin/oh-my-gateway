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
Session stats:  GET  /admin/api/sessions/stats
Session clean:  POST /admin/api/sessions/cleanup
Bulk delete:    POST /admin/api/sessions/bulk-delete
Server info:    GET  /admin/api/server-info
Logs:           GET  /admin/api/logs
Rate limits:    GET  /admin/api/rate-limits
Session msgs:   GET  /admin/api/sessions/{session_id}/messages
Skills:         GET/PUT/DELETE /admin/api/skills/{name}
System prompt:  GET/PUT/DELETE /admin/api/system-prompt
Named prompts:  GET/PUT/DELETE /admin/api/prompts/{name}
                POST /admin/api/prompts/{name}/activate
Plugins:        GET  /admin/api/plugins
Plugin detail:  GET  /admin/api/plugins/{id}
Plugin skills:  GET  /admin/api/plugins/{id}/skills/{name}
Marketplaces:   GET  /admin/api/marketplaces
Blocklist:      GET  /admin/api/plugins/blocklist
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Request, Response
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


class NamedPromptWrite(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# HTML page (no auth — page itself handles login UI)
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def admin_page():
    """Serve the admin dashboard HTML."""
    from src.admin_page import build_admin_page

    return HTMLResponse(build_admin_page())


@router.get("/chat", response_class=HTMLResponse)
async def admin_chat_page(_=Depends(require_admin)):
    """Serve the chat UI page (requires admin authentication)."""
    from src.chat_page import build_chat_page

    return HTMLResponse(build_chat_page())


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


@router.get("/api/server-info")
async def get_server_info(request: Request, _=Depends(require_admin)):
    """Return server uptime, version, and basic runtime info."""
    import time

    from src import __version__
    from src.session_manager import session_manager

    started_at = getattr(request.app.state, "started_at", None)
    uptime_seconds = round(time.time() - started_at, 1) if started_at else None

    return {
        "version": __version__,
        "started_at": started_at,
        "uptime_seconds": uptime_seconds,
        "session_stats": session_manager.get_stats(),
        "cleanup_task_alive": session_manager._cleanup_task is not None
        and not session_manager._cleanup_task.done(),
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
# Session stats & bulk operations (must precede {session_id} routes)
# ---------------------------------------------------------------------------


@router.get("/api/sessions/stats")
async def get_session_stats(_=Depends(require_admin)):
    """Return session statistics (active, expired, total messages)."""
    from src.session_manager import session_manager

    return session_manager.get_stats()


@router.post("/api/sessions/cleanup")
async def trigger_session_cleanup(_=Depends(require_admin)):
    """Manually trigger expired session cleanup."""
    from src.session_manager import session_manager

    removed = await session_manager.cleanup_expired_sessions()
    stats = session_manager.get_stats()
    return {"removed": removed, **stats}


class BulkDeleteRequest(BaseModel):
    session_ids: Optional[list[str]] = None
    expired_only: bool = False


@router.post("/api/sessions/bulk-delete")
async def bulk_delete_sessions(body: BulkDeleteRequest, _=Depends(require_admin)):
    """Delete multiple sessions or all expired sessions."""
    from src.session_manager import session_manager

    deleted_ids: list[str] = []

    if body.expired_only:
        removed = await session_manager.cleanup_expired_sessions()
        return {"deleted_count": removed, "mode": "expired_only"}

    if body.session_ids:
        for sid in body.session_ids:
            if session_manager.delete_session(sid):
                deleted_ids.append(sid)
        return {
            "deleted_count": len(deleted_ids),
            "deleted_ids": deleted_ids,
            "not_found": [sid for sid in body.session_ids if sid not in deleted_ids],
        }

    return JSONResponse(
        status_code=400,
        content={"error": "Provide session_ids or set expired_only=true"},
    )


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
    session_id: Optional[str] = None,
    backend: Optional[str] = None,
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
        session_id=session_id,
        backend=backend,
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


@router.get("/api/system-prompt/templates")
async def list_prompt_templates(_=Depends(require_admin)):
    """List available system prompt template files from docs/."""
    import re as _re
    from pathlib import Path as _Path

    docs_dir = _Path(__file__).resolve().parent.parent.parent / "docs"
    templates = []
    for f in sorted(docs_dir.glob("*system-prompt*.md")):
        raw = f.read_text(encoding="utf-8")
        body = _re.sub(r"\A#[^\n]*\n+(?:>[^\n]*\n)*\n*---\n*", "", raw).strip()
        templates.append({"name": f.stem, "filename": f.name, "content": body})
    return {"templates": templates}


@router.get("/api/system-prompt")
async def get_system_prompt_endpoint(_=Depends(require_admin)):
    """Return the current system prompt and its mode."""
    from src.system_prompt import (
        get_active_prompt_name,
        get_preset_text,
        get_prompt_mode,
        get_raw_system_prompt,
        get_system_prompt,
    )

    raw = get_raw_system_prompt()
    resolved = get_system_prompt()
    return {
        "mode": get_prompt_mode(),
        "prompt": raw,
        "resolved_prompt": resolved,
        "preset_text": get_preset_text(),
        "char_count": len(resolved) if resolved else 0,
        "active_name": get_active_prompt_name(),
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


# ---------------------------------------------------------------------------
# Named Prompts
# ---------------------------------------------------------------------------


@router.get("/api/prompts")
def list_prompts_endpoint(_=Depends(require_admin)):
    """List all saved named prompts."""
    from src.system_prompt import get_active_prompt_name, list_named_prompts

    return {
        "prompts": list_named_prompts(),
        "active_name": get_active_prompt_name(),
    }


@router.get("/api/prompts/{name}")
def get_prompt_endpoint(name: str, _=Depends(require_admin)):
    """Get a single named prompt by name."""
    from src.system_prompt import get_named_prompt

    try:
        data = get_named_prompt(name)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    if data is None:
        return JSONResponse(status_code=404, content={"error": f"Prompt not found: {name}"})
    return data


@router.put("/api/prompts/{name}")
def save_prompt_endpoint(name: str, body: NamedPromptWrite, _=Depends(require_admin)):
    """Create or update a named prompt."""
    from src.system_prompt import save_named_prompt

    try:
        data = save_named_prompt(name, body.content)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to save: {e}"})
    return data


@router.delete("/api/prompts/{name}")
def delete_prompt_endpoint(name: str, _=Depends(require_admin)):
    """Delete a named prompt."""
    from src.system_prompt import delete_named_prompt

    try:
        deleted = delete_named_prompt(name)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to delete: {e}"})
    if not deleted:
        return JSONResponse(status_code=404, content={"error": f"Prompt not found: {name}"})
    return {"status": "deleted", "name": name}


@router.post("/api/prompts/{name}/activate")
def activate_prompt_endpoint(name: str, _=Depends(require_admin)):
    """Activate a named prompt as the current system prompt."""
    from src.system_prompt import activate_named_prompt, get_prompt_mode

    try:
        activate_named_prompt(name)
    except ValueError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to activate: {e}"})
    return {"status": "activated", "name": name, "mode": get_prompt_mode()}


# ---------------------------------------------------------------------------
# Plugins (read-only)
#
# Route ordering matters: static paths (/blocklist) MUST be declared before
# the catch-all {plugin_id:path} parameter, otherwise FastAPI would capture
# "blocklist" as a plugin_id.
# ---------------------------------------------------------------------------


@router.get("/api/plugins")
async def list_plugins_endpoint(_=Depends(require_admin)):
    """List all installed Claude Code plugins with metadata."""
    from src.plugin_service import list_plugins

    return {"plugins": list_plugins()}


@router.get("/api/plugins/blocklist")
async def get_blocklist_endpoint(_=Depends(require_admin)):
    """Return the plugin blocklist."""
    from src.plugin_service import get_plugin_blocklist

    return {"blocklist": get_plugin_blocklist()}


@router.get("/api/plugins/{plugin_id:path}/skills/{skill_name}")
async def get_plugin_skill_endpoint(
    plugin_id: str,
    skill_name: str,
    _=Depends(require_admin),
):
    """Read a specific skill's content from an installed plugin."""
    from src.plugin_service import get_plugin_skill_content

    result = get_plugin_skill_content(plugin_id, skill_name)
    if result is None:
        return JSONResponse(status_code=404, content={"error": "Plugin or skill not found"})
    return result


@router.get("/api/plugins/{plugin_id:path}")
async def get_plugin_detail_endpoint(plugin_id: str, _=Depends(require_admin)):
    """Return full detail for a single installed plugin."""
    from src.plugin_service import get_plugin_detail

    detail = get_plugin_detail(plugin_id)
    if detail is None:
        return JSONResponse(status_code=404, content={"error": "Plugin not found"})
    return detail


@router.get("/api/marketplaces")
async def list_marketplaces_endpoint(_=Depends(require_admin)):
    """Return registered plugin marketplace sources."""
    from src.plugin_service import list_marketplaces

    return {"marketplaces": list_marketplaces()}
