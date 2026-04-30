"""General utility endpoints (models, health, version, root, auth, MCP)."""

import os
import logging
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse

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
from src.backends import BackendRegistry
from src.rate_limiter import rate_limit_endpoint
from src.constants import DEFAULT_PORT
from src.mcp_config import get_mcp_servers

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
            pass

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
