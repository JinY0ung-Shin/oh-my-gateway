"""Admin API routes — thin handlers that delegate to admin_service and admin_auth.

All endpoints live under ``/admin`` and require admin authentication
(separate ``ADMIN_API_KEY``, not the regular gateway ``API_KEY``).

HTML page:      GET  /admin
Login:          POST /admin/api/login
Logout:         POST /admin/api/logout
Dashboard:      GET  /admin/api/summary
Config:         GET  /admin/api/config
Session delete: DELETE /admin/api/sessions/{session_id}
Session stats:  GET  /admin/api/sessions/stats
Session clean:  POST /admin/api/sessions/cleanup
Bulk delete:    POST /admin/api/sessions/bulk-delete
Server info:    GET  /admin/api/server-info
Logs:           GET  /admin/api/logs
Rate limits:    GET  /admin/api/rate-limits
Session msgs:   GET  /admin/api/sessions/{session_id}/messages
System prompt:  GET/PUT/DELETE /admin/api/system-prompt
Named prompts:  GET/PUT/DELETE /admin/api/prompts/{name}
                POST /admin/api/prompts/{name}/activate
MCP servers:    GET  /admin/api/mcp-servers
                POST /admin/api/mcp-servers
                PUT  /admin/api/mcp-servers/{name}
                DELETE /admin/api/mcp-servers/{name}
MCP validate:   POST /admin/api/mcp-servers/validate
MCP test:       POST /admin/api/mcp-servers/{name}/test
MCP plugin overlay: GET/PUT/DELETE /admin/api/mcp-servers/{name}/plugin-overlay
Plugins:        GET  /admin/api/plugins
Plugin detail:  GET  /admin/api/plugins/{id}
Plugin skills:  GET  /admin/api/plugins/{id}/skills/{name}
Plugin install: POST /admin/api/plugins
Plugin remove:  DELETE /admin/api/plugins/{id}
Plugin manifest: GET /admin/api/plugins/manifest
Auto-refresh:   GET/PUT /admin/api/plugins/auto-refresh
                POST /admin/api/plugins/auto-refresh/run
Marketplaces:   GET  /admin/api/marketplaces
                POST /admin/api/marketplaces
                GET  /admin/api/marketplaces/catalog
                GET  /admin/api/marketplaces/{name}/plugins
                POST /admin/api/marketplaces/{name}/refresh
                DELETE /admin/api/marketplaces/{name}
Blocklist:      GET  /admin/api/plugins/blocklist
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.admin_auth import (
    get_admin_status,
    login,
    logout,
    require_admin,
)
from src.rate_limiter import rate_limit_endpoint
from src.admin_service import (
    export_session_json,
    get_backends_health,
    get_dropped_mcp_servers,
    get_mcp_servers_detail,
    get_plugin_mcp_servers_detail,
    get_redacted_config,
    get_sandbox_config,
    get_session_detail,
    get_session_messages,
    get_tools_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    api_key: str


class RuntimeConfigUpdate(BaseModel):
    key: str
    value: Any


class SystemPromptUpdate(BaseModel):
    prompt: str


class NamedPromptWrite(BaseModel):
    content: str


class MarketplaceAddRequest(BaseModel):
    repo: str
    branch: str = "main"
    scope: str = "user"
    git_token: str = ""


class MarketplaceRefreshRequest(BaseModel):
    scope: str = ""
    git_token: str = ""


class PluginInstallRequest(BaseModel):
    name: str
    marketplace: str = ""
    scope: str = "user"
    repo: str = ""
    branch: str = "main"


class AutoRefreshUpdate(BaseModel):
    enabled: bool
    # None = preserve the stored interval (so a minimal {"enabled": false} body
    # can't silently reset a configured interval back to a default).
    interval_minutes: Optional[int] = None


class McpServerUpsert(BaseModel):
    name: str
    config: dict[str, Any]


class McpServerValidate(BaseModel):
    name: str = ""
    config: dict[str, Any] = {}


class McpPluginOverlayUpsert(BaseModel):
    """Credentials-only overlay for a plugin-provided MCP server."""

    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    plugin_id: Optional[str] = None


# ---------------------------------------------------------------------------
# HTML page (no auth — page itself handles login UI)
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def admin_page():
    """Serve the admin dashboard HTML."""
    from src.admin_page import build_admin_page

    return HTMLResponse(build_admin_page())


@router.get("/chat", response_class=HTMLResponse)
async def admin_chat_page():
    """Serve the chat UI page. Auth gate is handled client-side, matching /admin."""
    from src.chat_page import build_chat_page

    return HTMLResponse(build_chat_page())


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@router.post("/api/login")
@rate_limit_endpoint("admin_login")
async def admin_login(request: Request, body: LoginRequest, response: Response):
    """Authenticate with admin API key and receive a session cookie.

    Strictly rate-limited (``admin_login``) to blunt brute-force attempts
    against ``ADMIN_API_KEY``. slowapi requires the ``request`` parameter.
    """
    _ = request  # slowapi requires a request parameter in decorated handlers.
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
    uptime_seconds = (
        round(time.time() - started_at, 1) if started_at is not None else None
    )

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
    """Return detailed MCP servers (effective) plus any dropped by overlay merge.

    Also appends read-only rows for MCP servers contributed by installed
    plugins (``source='plugin'``): the SDK loads those via ``setting_sources``,
    so they never appear in the effective (env+manifest) config.
    """
    return {
        "servers": get_mcp_servers_detail() + get_plugin_mcp_servers_detail(),
        "dropped": get_dropped_mcp_servers(),
    }


# --- MCP server mutations + diagnostics (admin-managed) -------------------
#
# Route ordering: the literal /api/mcp-servers/validate MUST be declared
# before the parametrised /api/mcp-servers/{name} routes. FastAPI resolves in
# declaration order, so a single-segment {name} would otherwise capture
# "validate". Mirrors the plugin-CRUD shape: lazy run_in_threadpool + service
# import, McpAdminError -> JSONResponse, require_admin dependency last.


@router.post("/api/mcp-servers")
async def create_mcp_server_endpoint(body: McpServerUpsert, _=Depends(require_admin)):
    """Create a new manifest-layer MCP server (env-base servers are immutable)."""
    from fastapi.concurrency import run_in_threadpool

    from src import mcp_admin_service

    try:
        return await run_in_threadpool(
            mcp_admin_service.create_server, body.name, body.config
        )
    except mcp_admin_service.McpAdminError as e:
        code = 409 if "already exists" in str(e) or "not editable" in str(e) else 400
        return JSONResponse(status_code=code, content={"error": str(e)})


@router.post("/api/mcp-servers/validate")
async def validate_mcp_server_endpoint(
    body: McpServerValidate, _=Depends(require_admin)
):
    """Pure preview (never persists): always 200 with a valid:false payload for
    a bad config. Safe to call on every keystroke in the editor UI."""
    from src import mcp_admin_service

    return mcp_admin_service.validate_config(body.name, body.config)


@router.put("/api/mcp-servers/{name}")
async def update_mcp_server_endpoint(
    name: str, body: McpServerUpsert, _=Depends(require_admin)
):
    """Update an existing manifest-layer MCP server in place."""
    from fastapi.concurrency import run_in_threadpool

    from src import mcp_admin_service

    try:
        return await run_in_threadpool(
            mcp_admin_service.update_server, name, body.config
        )
    except mcp_admin_service.McpAdminError as e:
        if "not found" in str(e):
            code = 404
        elif "not editable" in str(e):
            code = 409
        else:
            code = 400
        return JSONResponse(status_code=code, content={"error": str(e)})


@router.delete("/api/mcp-servers/{name}")
async def delete_mcp_server_endpoint(name: str, _=Depends(require_admin)):
    """Delete a manifest-layer MCP server (env-base servers cannot be deleted)."""
    from fastapi.concurrency import run_in_threadpool

    from src import mcp_admin_service

    try:
        return await run_in_threadpool(mcp_admin_service.delete_server, name)
    except mcp_admin_service.McpAdminError as e:
        if "not found" in str(e):
            code = 404
        elif "cannot be deleted" in str(e):
            code = 409
        else:
            code = 400
        return JSONResponse(status_code=code, content={"error": str(e)})


@router.post("/api/mcp-servers/{name}/test")
async def test_mcp_server_endpoint(name: str, _=Depends(require_admin)):
    """Probe an effective MCP server's connectivity. Self-bounded; never raises."""
    from src import mcp_admin_service

    return await mcp_admin_service.test_connection(name)


# --- Plugin MCP credential overlays ---------------------------------------
# Env/headers only; plugin command/url stay owned by the plugin. Hot-reloads
# into NEW Claude sessions (materialize + process env inject).


@router.get("/api/mcp-servers/{name}/plugin-overlay")
async def get_mcp_plugin_overlay_endpoint(name: str, _=Depends(require_admin)):
    """Return the admin credential overlay for a plugin MCP server (redacted)."""
    from fastapi.concurrency import run_in_threadpool

    from src import mcp_plugin_overlay_service

    try:
        return await run_in_threadpool(
            mcp_plugin_overlay_service.get_overlay_detail, name
        )
    except mcp_plugin_overlay_service.McpPluginOverlayError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.put("/api/mcp-servers/{name}/plugin-overlay")
async def put_mcp_plugin_overlay_endpoint(
    name: str, body: McpPluginOverlayUpsert, _=Depends(require_admin)
):
    """Create or replace env/headers overlay for a plugin MCP server."""
    from fastapi.concurrency import run_in_threadpool

    from src import mcp_plugin_overlay_service

    try:
        return await run_in_threadpool(
            mcp_plugin_overlay_service.upsert_overlay,
            name,
            env=body.env,
            headers=body.headers,
            plugin_id=body.plugin_id,
        )
    except mcp_plugin_overlay_service.McpPluginOverlayError as e:
        msg = str(e)
        code = 404 if "not a plugin-provided" in msg else 400
        return JSONResponse(status_code=code, content={"error": msg})


@router.delete("/api/mcp-servers/{name}/plugin-overlay")
async def delete_mcp_plugin_overlay_endpoint(name: str, _=Depends(require_admin)):
    """Remove the admin credential overlay for a plugin MCP server."""
    from fastapi.concurrency import run_in_threadpool

    from src import mcp_plugin_overlay_service

    try:
        return await run_in_threadpool(
            mcp_plugin_overlay_service.delete_overlay, name
        )
    except mcp_plugin_overlay_service.McpPluginOverlayError as e:
        msg = str(e)
        code = 404 if "no overlay" in msg else 400
        return JSONResponse(status_code=code, content={"error": msg})


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
            if await session_manager.delete_session_async(sid):
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
# Session management (proxied through admin auth boundary)
# ---------------------------------------------------------------------------


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, _=Depends(require_admin)):
    """Delete a session. Proxied from admin so it stays within admin auth."""
    from src.session_manager import session_manager

    deleted = await session_manager.delete_session_async(session_id)
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
        return JSONResponse(
            status_code=500, content={"error": f"Failed to persist: {e}"}
        )
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
        return JSONResponse(
            status_code=500, content={"error": f"Failed to persist: {e}"}
        )
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
        return JSONResponse(
            status_code=404, content={"error": f"Prompt not found: {name}"}
        )
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
        return JSONResponse(
            status_code=500, content={"error": f"Failed to delete: {e}"}
        )
    if not deleted:
        return JSONResponse(
            status_code=404, content={"error": f"Prompt not found: {name}"}
        )
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
        return JSONResponse(
            status_code=500, content={"error": f"Failed to activate: {e}"}
        )
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


# --- Plugin / marketplace mutations (admin-managed) -----------------------
#
# These static / method-specific routes MUST be declared before the catch-all
# GET /api/plugins/{plugin_id:path} below. FastAPI matches in declaration
# order: a later GET /api/plugins/{plugin_id:path} would otherwise capture
# "manifest" as a plugin_id. (POST and DELETE are method-distinct and would not
# be shadowed by the GET catch-all, but they are kept here for clarity.)


@router.get("/api/plugins/manifest")
async def get_plugin_manifest_endpoint(_=Depends(require_admin)):
    """Return the admin-managed plugin manifest (added / removed specs)."""
    from src import plugin_manifest

    return {
        "added": plugin_manifest.list_added(),
        "removed": plugin_manifest.list_removed(),
    }


@router.get("/api/plugins/auto-refresh")
async def get_plugin_auto_refresh_endpoint(_=Depends(require_admin)):
    """Return the marketplace auto-refresh config + poller status."""
    from fastapi.concurrency import run_in_threadpool

    from src.plugin_autorefresh import auto_refresher

    return await run_in_threadpool(auto_refresher.status)


@router.put("/api/plugins/auto-refresh")
async def update_plugin_auto_refresh_endpoint(
    body: AutoRefreshUpdate,
    _=Depends(require_admin),
):
    """Persist the auto-refresh config; the poller picks it up next tick."""
    from fastapi.concurrency import run_in_threadpool

    from src import plugin_manifest
    from src.plugin_autorefresh import auto_refresher

    # Omitted interval preserves the stored one rather than snapping to a default.
    if body.interval_minutes is None:
        current = await run_in_threadpool(plugin_manifest.get_auto_refresh)
        interval = current["interval_minutes"]
    else:
        interval = body.interval_minutes

    if not (
        plugin_manifest.AUTO_REFRESH_MIN_MINUTES
        <= interval
        <= plugin_manifest.AUTO_REFRESH_MAX_MINUTES
    ):
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    "interval_minutes must be between "
                    f"{plugin_manifest.AUTO_REFRESH_MIN_MINUTES} and "
                    f"{plugin_manifest.AUTO_REFRESH_MAX_MINUTES}"
                )
            },
        )
    await run_in_threadpool(
        plugin_manifest.set_auto_refresh,
        enabled=body.enabled,
        interval_minutes=interval,
    )
    # Restart the countdown so enabling doesn't fire an immediate cycle and
    # next_run_at reflects the real next run.
    auto_refresher.reset_schedule()
    return await run_in_threadpool(auto_refresher.status)


@router.post("/api/plugins/auto-refresh/run")
async def run_plugin_auto_refresh_endpoint(_=Depends(require_admin)):
    """Trigger an immediate refresh cycle in the background."""
    from src.plugin_autorefresh import auto_refresher

    return auto_refresher.trigger()


@router.post("/api/marketplaces")
async def add_marketplace_endpoint(
    body: MarketplaceAddRequest,
    _=Depends(require_admin),
):
    """Register a plugin marketplace from a repo URL or local path."""
    from fastapi.concurrency import run_in_threadpool

    from src import plugin_admin_service

    try:
        result = await run_in_threadpool(
            plugin_admin_service.add_marketplace,
            body.repo,
            branch=body.branch,
            scope=body.scope,
            git_token=body.git_token,
        )
    except plugin_admin_service.PluginAdminError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return result


@router.delete("/api/marketplaces/{name}")
async def remove_marketplace_endpoint(
    name: str,
    scope: str = "user",
    _=Depends(require_admin),
):
    """Remove a configured marketplace and drop its managed entries."""
    from fastapi.concurrency import run_in_threadpool

    from src import plugin_admin_service

    try:
        result = await run_in_threadpool(
            plugin_admin_service.remove_marketplace,
            name,
            scope=scope,
        )
    except plugin_admin_service.PluginAdminError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return result


@router.post("/api/marketplaces/{name}/refresh")
async def refresh_marketplace_endpoint(
    name: str,
    body: Optional[MarketplaceRefreshRequest] = None,
    _=Depends(require_admin),
):
    """Re-clone/re-register a marketplace and update its installed plugins."""
    from fastapi.concurrency import run_in_threadpool

    from src import plugin_admin_service

    req = body or MarketplaceRefreshRequest()
    try:
        result = await run_in_threadpool(
            plugin_admin_service.refresh_marketplace,
            name,
            scope=req.scope,
            git_token=req.git_token,
        )
    except plugin_admin_service.PluginAdminError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return result


@router.post("/api/plugins")
async def install_plugin_endpoint(
    body: PluginInstallRequest,
    _=Depends(require_admin),
):
    """Install a plugin (optionally registering its marketplace first)."""
    from fastapi.concurrency import run_in_threadpool

    from src import plugin_admin_service

    try:
        result = await run_in_threadpool(
            plugin_admin_service.install_plugin,
            body.name,
            marketplace=body.marketplace,
            scope=body.scope,
            repo=body.repo,
            branch=body.branch,
        )
    except plugin_admin_service.PluginAdminError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return result


@router.delete("/api/plugins/{plugin_id:path}")
async def uninstall_plugin_endpoint(
    plugin_id: str,
    scope: str = "user",
    _=Depends(require_admin),
):
    """Uninstall a plugin by its registry key (e.g. ``octo@nyldn-plugins``)."""
    from fastapi.concurrency import run_in_threadpool

    from src import plugin_admin_service

    try:
        result = await run_in_threadpool(
            plugin_admin_service.uninstall_plugin,
            plugin_id,
            scope=scope,
        )
    except plugin_admin_service.PluginAdminError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return result


@router.get("/api/plugins/{plugin_id:path}/skills/{skill_name}")
async def get_plugin_skill_endpoint(
    plugin_id: str,
    skill_name: str,
    scope: Optional[str] = None,
    _=Depends(require_admin),
):
    """Read a specific skill's content from an installed plugin.

    *scope* picks the right per-scope registry entry when a plugin is installed
    at more than one scope.
    """
    from src.plugin_service import get_plugin_skill_content

    result = get_plugin_skill_content(plugin_id, skill_name, scope)
    if result is None:
        return JSONResponse(
            status_code=404, content={"error": "Plugin or skill not found"}
        )
    return result


@router.get("/api/plugins/{plugin_id:path}")
async def get_plugin_detail_endpoint(
    plugin_id: str, scope: Optional[str] = None, _=Depends(require_admin)
):
    """Return full detail for a single installed plugin (optionally per scope)."""
    from src.plugin_service import get_plugin_detail

    detail = get_plugin_detail(plugin_id, scope)
    if detail is None:
        return JSONResponse(status_code=404, content={"error": "Plugin not found"})
    return detail


@router.get("/api/marketplaces")
async def list_marketplaces_endpoint(_=Depends(require_admin)):
    """Return registered plugin marketplace sources."""
    from src.plugin_service import list_marketplaces

    return {"marketplaces": list_marketplaces()}


# --- Marketplace catalog (read-only, one-click install UI) ----------------
#
# Route ordering: the literal /api/marketplaces/catalog MUST be declared
# before the parametrised /api/marketplaces/{name}/plugins (and before the
# existing DELETE /api/marketplaces/{name}). FastAPI resolves in declaration
# order, so a single-segment {name} would otherwise capture "catalog".


@router.get("/api/marketplaces/catalog")
async def marketplaces_catalog_endpoint(_=Depends(require_admin)):
    """All marketplaces with their offered plugins (one-shot for the UI)."""
    from src.plugin_service import get_marketplaces_with_plugins

    return {"marketplaces": get_marketplaces_with_plugins()}


@router.get("/api/marketplaces/{name}/plugins")
async def marketplace_plugins_endpoint(name: str, _=Depends(require_admin)):
    """Catalog plugins offered by a single marketplace."""
    from src.plugin_service import list_marketplace_plugins

    return {"plugins": list_marketplace_plugins(name)}


# ---------------------------------------------------------------------------
# Usage-log dashboard (MySQL-backed analytics)
# ---------------------------------------------------------------------------


@router.get("/api/usage/summary")
async def usage_summary_endpoint(
    window_days: int = 7,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _=Depends(require_admin),
):
    """Overview counters for the usage tab."""
    from src.usage_queries import get_summary

    window = max(1, min(window_days, 365))
    data = await get_summary(
        window_days=window,
        start_date=start_date,
        end_date=end_date,
    )
    if data is None:
        return {"enabled": False}
    return {"enabled": True, "window_days": window, "summary": data}


@router.get("/api/usage/users")
async def usage_users_endpoint(
    window_days: int = 7,
    limit: int = 20,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _=Depends(require_admin),
):
    """Top users by token usage in the selected range."""
    from src.usage_queries import get_top_users

    rows = await get_top_users(
        window_days=max(1, min(window_days, 365)),
        limit=max(1, min(limit, 500)),
        start_date=start_date,
        end_date=end_date,
    )
    if rows is None:
        return {"enabled": False, "items": []}
    return {"enabled": True, "items": rows}


@router.get("/api/usage/tools")
async def usage_tools_endpoint(
    window_days: int = 7,
    limit: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _=Depends(require_admin),
):
    """Top tools by call count in the selected range."""
    from src.usage_queries import get_top_tools

    rows = await get_top_tools(
        window_days=max(1, min(window_days, 365)),
        limit=max(1, min(limit, 500)),
        start_date=start_date,
        end_date=end_date,
    )
    if rows is None:
        return {"enabled": False, "items": []}
    return {"enabled": True, "items": rows}


@router.get("/api/usage/series")
async def usage_series_endpoint(
    granularity: str = "day",
    buckets: int = 5,
    _=Depends(require_admin),
):
    """Recent bucket aggregates for USAGE time-series charts."""
    from src.usage_queries import get_time_series

    gran = granularity if granularity in ("day", "week", "month") else "day"
    rows = await get_time_series(granularity=gran, buckets=max(1, min(buckets, 60)))
    if rows is None:
        return {"enabled": False, "granularity": gran, "buckets": []}
    return {"enabled": True, "granularity": gran, "buckets": rows}


@router.get("/api/usage/tools-series")
async def usage_tools_series_endpoint(
    granularity: str = "day",
    buckets: int = 5,
    top: int = 5,
    _=Depends(require_admin),
):
    """Per-bucket tool-call breakdown for the grouped TOOL CALLS chart."""
    from src.usage_queries import get_tool_breakdown_series

    gran = granularity if granularity in ("day", "week", "month") else "day"
    data = await get_tool_breakdown_series(
        granularity=gran,
        buckets=max(1, min(buckets, 60)),
        top_n=max(1, min(top, 20)),
    )
    if data is None:
        return {"enabled": False, "granularity": gran, "tools": [], "buckets": []}
    return {"enabled": True, "granularity": gran, **data}


@router.get("/api/usage/turns")
async def usage_turns_endpoint(
    user: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    _=Depends(require_admin),
):
    """Recent turns (newest first), optionally filtered by user."""
    from src.usage_queries import get_recent_turns

    rows = await get_recent_turns(
        user=user,
        limit=max(1, min(limit, 500)),
        offset=max(0, offset),
    )
    if rows is None:
        return {"enabled": False, "items": []}
    return {"enabled": True, "items": rows}
