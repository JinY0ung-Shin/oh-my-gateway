"""General utility endpoints (models, health, version, root, compatibility, debug, auth, MCP)."""

import os
import logging
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from src.models import ChatCompletionRequest
from src.landing_page import build_root_page
from src.auth import (
    verify_api_key,
    security,
    auth_manager,
    get_claude_code_auth_info,
    get_all_backends_auth_info,
    validate_backend_auth,
)
from src.parameter_validator import CompatibilityReporter
from src.backends import BackendRegistry
from src.rate_limiter import rate_limit_endpoint
from src.constants import DEFAULT_PORT, DEBUG_MODE, VERBOSE
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
    """List available MCP servers configured on this wrapper instance."""
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


@router.post("/v1/compatibility")
async def check_compatibility(request_body: ChatCompletionRequest):
    """Check OpenAI API compatibility for a request."""
    report = CompatibilityReporter.generate_compatibility_report(request_body)
    return {
        "compatibility_report": report,
        "claude_agent_sdk_options": {
            "supported": [
                "model",
                "system_prompt",
                "max_turns",
                "allowed_tools",
                "disallowed_tools",
                "permission_mode",
                "continue_conversation",
                "resume",
                "cwd",
            ],
            "custom_headers": [
                "X-Claude-Max-Turns",
                "X-Claude-Allowed-Tools",
                "X-Claude-Disallowed-Tools",
                "X-Claude-Permission-Mode",
            ],
        },
    }


@router.get("/health")
@rate_limit_endpoint("health")
async def health_check(request: Request):
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "claude-code-gateway",
        "backends": list(BackendRegistry.all_backends().keys()),
    }


@router.get("/version")
@rate_limit_endpoint("health")
async def version_info(request: Request):
    """Version information endpoint."""
    from src import __version__

    return {
        "version": __version__,
        "service": "claude-code-gateway",
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


@router.post("/v1/debug/request")
@rate_limit_endpoint("debug")
async def debug_request_validation(request: Request):
    """Debug endpoint to test request validation and see what's being sent."""
    try:
        # Get the raw request body
        body = await request.body()
        raw_body = body.decode() if body else ""

        # Try to parse as JSON
        parsed_body = None
        json_error = None
        try:
            import json as json_lib

            parsed_body = json_lib.loads(raw_body) if raw_body else {}
        except Exception as e:
            json_error = str(e)

        # Try to validate against our model
        validation_result = {"valid": False, "errors": []}
        if parsed_body:
            try:
                chat_request = ChatCompletionRequest(**parsed_body)
                validation_result = {"valid": True, "validated_data": chat_request.model_dump()}
            except ValidationError as e:
                validation_result = {
                    "valid": False,
                    "errors": [
                        {
                            "field": " -> ".join(str(loc) for loc in error.get("loc", [])),
                            "message": error.get("msg", "Unknown error"),
                            "type": error.get("type", "validation_error"),
                            "input": error.get("input"),
                        }
                        for error in e.errors()
                    ],
                }

        return {
            "debug_info": {
                "headers": dict(request.headers),
                "method": request.method,
                "url": str(request.url),
                "raw_body": raw_body,
                "json_parse_error": json_error,
                "parsed_body": parsed_body,
                "validation_result": validation_result,
                "debug_mode_enabled": DEBUG_MODE or VERBOSE,
                "example_valid_request": {
                    "model": "claude-3-sonnet-20240229",
                    "messages": [{"role": "user", "content": "Hello, world!"}],
                    "stream": False,
                },
            }
        }

    except Exception as e:
        return {
            "debug_info": {
                "error": f"Debug endpoint error: {str(e)}",
                "headers": dict(request.headers),
                "method": request.method,
                "url": str(request.url),
            }
        }


@router.get("/v1/auth/status")
@rate_limit_endpoint("auth")
async def get_auth_status(request: Request):
    """Get authentication status for all backends."""
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
            "codex_available": BackendRegistry.is_registered("codex"),
            "version": "1.0.0",
        },
    }
