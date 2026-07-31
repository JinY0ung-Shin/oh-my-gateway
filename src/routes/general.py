"""General utility endpoints (models, health, version, root, auth, MCP)."""

import os
import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, Response, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse, JSONResponse

from src.landing_page import build_root_page
from src.auth import (
    verify_api_key,
    security,
    auth_manager,
    get_claude_code_auth_info,
    get_all_backends_auth_info,
    validate_backend_auth,
)
from src import __version__
from src import metrics
from src.backends import BackendRegistry
from src.rate_limiter import rate_limit_endpoint
from src.constants import DEFAULT_PORT
from src.mcp_config import get_mcp_servers
from src.usage_logger import usage_logger

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/v1/models")
async def list_models(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """List available models from all registered backends."""
    await verify_api_key(request, credentials)

    return {
        "object": "list",
        "data": BackendRegistry.available_models(),
    }


@router.get("/v1/slash-commands")
@rate_limit_endpoint("general")
async def list_slash_commands(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """List slash commands the Claude backend will accept on /v1/responses.

    Clients (e.g. the ChatDRAGON composer) use this for CLI-style `/`
    completion, so each entry carries the SDK's description/argument hint.
    Blocked names are excluded — sending one returns 400 blocked_command, so
    they must never be offered for completion.
    """
    await verify_api_key(request, credentials)

    from src.backends.claude import slash_commands

    try:
        details = await slash_commands.get_command_details()
    except Exception:  # noqa: BLE001 — SDK 조회 실패는 빈 목록으로 응답
        details = {}
    allowed = [
        {
            "name": name,
            "description": meta.get("description", ""),
            "argument_hint": meta.get("argument_hint", ""),
        }
        for name, meta in sorted(details.items())
        if name not in slash_commands.BLOCKED_COMMANDS
    ]
    return {"commands": allowed, "total": len(allowed)}


@router.get("/v1/mcp/servers")
@rate_limit_endpoint("general")
async def list_mcp_servers(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """List available MCP servers configured on this gateway instance."""
    await verify_api_key(request, credentials)

    mcp_servers = get_mcp_servers()
    servers = []
    for name, config in mcp_servers.items():
        safe_config = {"type": config.get("type", "stdio")}
        if "url" in config:
            safe_config["url"] = config["url"]
        if "command" in config:
            safe_config["command"] = config["command"]
        if "args" in config:
            safe_config["args"] = config["args"]
        servers.append({"name": name, "config": safe_config})

    return {"servers": servers, "total": len(servers)}


@router.get("/health")
@rate_limit_endpoint("health")
async def health_check(request: Request):
    """Health check endpoint."""
    _ = request  # slowapi requires a request parameter in decorated handlers.
    return {
        "status": "healthy",
        "service": "oh-my-gateway",
        "backends": list(BackendRegistry.all_backends().keys()),
    }


# Per-check timeout for the readiness probe. Checks run concurrently, so the
# endpoint responds in roughly one timeout even when several checks hang.
READINESS_CHECK_TIMEOUT_SECONDS = 3.0


async def _check_backend_auth(backend_name: str) -> Dict[str, Any]:
    """Validate one backend's auth (env vars / credentials), bounded by timeout."""
    try:
        valid, info = await asyncio.wait_for(
            asyncio.to_thread(validate_backend_auth, backend_name),
            timeout=READINESS_CHECK_TIMEOUT_SECONDS,
        )
        result: Dict[str, Any] = {"ok": bool(valid)}
        if not valid:
            result["errors"] = info.get("errors", [])
        return result
    except asyncio.TimeoutError:
        return {"ok": False, "errors": ["auth check timed out"]}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}


async def _check_opencode_server() -> Dict[str, Any]:
    """Probe the OpenCode server (managed or external) via its health endpoint."""
    backend = BackendRegistry.all_backends().get("opencode")
    if backend is None:
        return {
            "ok": True,
            "skipped": True,
            "reason": "opencode backend not registered",
        }
    try:
        reachable = await asyncio.wait_for(
            backend.verify(), timeout=READINESS_CHECK_TIMEOUT_SECONDS
        )
        result: Dict[str, Any] = {"ok": bool(reachable)}
        if not reachable:
            result["errors"] = ["OpenCode server health check failed"]
        return result
    except asyncio.TimeoutError:
        return {"ok": False, "errors": ["OpenCode server health check timed out"]}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}


async def _check_usage_log_db() -> Dict[str, Any]:
    """Probe usage-log DB connectivity when usage logging is enabled."""
    if not usage_logger.enabled:
        return {"ok": True, "skipped": True, "reason": "usage logging disabled"}
    try:
        rows = await asyncio.wait_for(
            usage_logger.fetch_rows("SELECT 1"),
            timeout=READINESS_CHECK_TIMEOUT_SECONDS,
        )
        if rows is None:
            return {"ok": False, "errors": ["usage-log DB probe failed"]}
        return {"ok": True}
    except asyncio.TimeoutError:
        return {"ok": False, "errors": ["usage-log DB probe timed out"]}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}


@router.get("/health/ready")
@rate_limit_endpoint("health")
async def readiness_check(request: Request):
    """Readiness probe: backend auth, OpenCode reachability, usage-log DB.

    Returns 200 with per-check results when every check passes, 503 with the
    failing checks otherwise. All checks run concurrently with short timeouts.
    """
    _ = request  # slowapi requires a request parameter in decorated handlers.
    backend_names = sorted(BackendRegistry.all_backends().keys())

    results = await asyncio.gather(
        *(_check_backend_auth(name) for name in backend_names),
        _check_opencode_server(),
        _check_usage_log_db(),
    )

    checks: Dict[str, Any] = {
        f"{name}_auth": results[i] for i, name in enumerate(backend_names)
    }
    checks["opencode_server"] = results[len(backend_names)]
    checks["usage_log_db"] = results[len(backend_names) + 1]

    ready = all(check.get("ok") for check in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus scrape endpoint.

    Unauthenticated by design (standard scrape target), like /health.
    Not rate-limited so periodic scrapes are never dropped.
    """
    payload, content_type = metrics.render_latest()
    return Response(content=payload, media_type=content_type)


@router.get("/version")
@rate_limit_endpoint("health")
async def version_info(request: Request):
    """Version information endpoint."""
    _ = request  # slowapi requires a request parameter in decorated handlers.
    from src import __version__

    return {
        "version": __version__,
        "service": "oh-my-gateway",
        "api_version": "v1",
    }


@router.get("/", response_class=HTMLResponse)
async def root():
    """Landing page with API documentation."""
    from src import __version__

    # Build aggregated auth status across all registered backends
    registered = list(BackendRegistry.all_backends().keys())
    any_valid = False
    auth_method_parts = []
    for backend_name in registered:
        try:
            valid, _info = validate_backend_auth(backend_name)
            if valid:
                any_valid = True
                auth_method_parts.append(backend_name)
        except Exception:
            logger.debug("root auth status check failed for %s", backend_name, exc_info=True)

    auth_info = {
        "method": ", ".join(auth_method_parts) if auth_method_parts else "none",
        "status": {"valid": any_valid},
    }
    return HTMLResponse(content=build_root_page(__version__, auth_info, DEFAULT_PORT))


@router.get("/v1/auth/status")
@rate_limit_endpoint("auth")
async def get_auth_status(request: Request):
    """Get authentication status for all backends."""
    _ = request  # slowapi requires a request parameter in decorated handlers.
    active_api_key = auth_manager.get_api_key()

    backends_auth = get_all_backends_auth_info()
    registered_backends = list(BackendRegistry.all_backends().keys())

    return {
        "claude_code_auth": get_claude_code_auth_info(),
        "backends": {
            name: {**info, "registered": name in registered_backends}
            for name, info in backends_auth.items()
        },
        "server_info": {
            "api_key_required": bool(active_api_key),
            "api_key_source": (
                "environment"
                if os.getenv("API_KEY")
                else ("runtime" if auth_manager.runtime_api_key else "none")
            ),
            "registered_backends": registered_backends,
            "version": __version__,
        },
    }
