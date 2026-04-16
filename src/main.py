import os
import json
import asyncio
import logging
import secrets
import string
import time
import uuid
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from src.auth import (
    validate_claude_code_auth,
    auth_manager,
)
from src.session_manager import session_manager
from src.backends import (
    BackendRegistry,
    discover_backends,
)
from src.rate_limiter import (
    limiter,
    rate_limit_exceeded_handler,
)
from src.constants import (
    DEBUG_MODE,
    VERBOSE,
    DEFAULT_TIMEOUT_MS,
    DEFAULT_PORT,
    DEFAULT_HOST,
    MAX_REQUEST_SIZE,
    DOCKER_SANDBOX_ENABLED,
    DOCKER_SANDBOX_ROLE,
)
from src import __version__
from src.mcp_config import get_mcp_servers
from src.request_logger import request_logger, RequestLogEntry
from src.routes.deps import truncate_image_data

log_level = logging.DEBUG if (DEBUG_MODE or VERBOSE) else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

runtime_api_key = None


def _sanitize_validation_errors(errors: list[dict]) -> list[dict[str, str]]:
    sanitized = []
    for error in errors:
        sanitized.append(
            {
                "field": " -> ".join(str(loc) for loc in error.get("loc", [])),
                "message": error.get("msg", "Unknown validation error"),
                "type": error.get("type", "validation_error"),
            }
        )
    return sanitized


def generate_secure_token(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def prompt_for_api_protection() -> Optional[str]:
    if os.getenv("API_KEY"):
        return None

    print("\n" + "=" * 60)
    print("API Endpoint Security Configuration")
    print("=" * 60)
    print("Would you like to protect your API endpoint with an API key?")
    print("")

    while True:
        try:
            choice = input("Enable API key protection? (y/N): ").strip().lower()

            if choice in ["", "n", "no"]:
                print("API endpoint will be accessible without authentication")
                print("=" * 60)
                return None

            elif choice in ["y", "yes"]:
                token = generate_secure_token()
                print("")
                print("API Key Generated!")
                print("=" * 60)
                print("API Key: " + token)
                print("=" * 60)
                print("IMPORTANT: Save this key - you'll need it for API calls!")
                print("   Example usage:")
                print("   curl -H 'Authorization: Bearer " + token + "'")
                print("        http://localhost:" + str(DEFAULT_PORT) + "/v1/models")
                print("=" * 60)
                return token

            else:
                print("Please enter 'y' for yes or 'n' for no (or press Enter for no)")

        except (EOFError, KeyboardInterrupt):
            print("\nDefaulting to no authentication")
            return None


async def _verify_backends() -> None:
    for name, backend in BackendRegistry.all_backends().items():
        try:
            logger.info("Verifying %s backend...", name)
            verified = await asyncio.wait_for(backend.verify(), timeout=30.0)
            if verified:
                logger.info("%s backend verified successfully", name)
            else:
                logger.warning("%s backend verification returned False", name)
        except asyncio.TimeoutError:
            logger.warning("%s backend verification timed out (30s)", name)
        except Exception as e:
            logger.error("%s backend verification failed: %s", name, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing backend registry...")

    from src.admin_auth import validate_admin_config
    validate_admin_config()

    auth_manager.clean_stale_env_vars()

    auth_valid, auth_info = validate_claude_code_auth()

    if not auth_valid:
        logger.error("Claude Code authentication failed!")
        for error in auth_info.get("errors", []):
            logger.error("  - %s", error)
        logger.warning("Authentication setup guide:")
        logger.warning("  1. For Anthropic API: Set ANTHROPIC_AUTH_TOKEN")
        logger.warning("  2. For CLI auth: Run 'claude auth login'")
    else:
        logger.info("Claude Code authentication validated: %s", auth_info["method"])

    from src.system_prompt import load_default_prompt
    from src.constants import SYSTEM_PROMPT_FILE
    load_default_prompt(SYSTEM_PROMPT_FILE)

    discover_backends()
    await _verify_backends()

    if DEBUG_MODE or VERBOSE:
        logger.debug("Debug mode enabled - Enhanced logging active")
        logger.debug("Environment variables:")
        logger.debug("   DEBUG_MODE: %s", DEBUG_MODE)
        logger.debug("   VERBOSE: %s", VERBOSE)
        logger.debug("   PORT: %s", DEFAULT_PORT)
        cors_origins_val = os.getenv("CORS_ORIGINS", '["*"]')
        logger.debug("   CORS_ORIGINS: %s", cors_origins_val)
        logger.debug("   MAX_TIMEOUT: %s", DEFAULT_TIMEOUT_MS)
        logger.debug("   CLAUDE_CWD: %s", os.getenv("CLAUDE_CWD", "Not set"))
        logger.debug("Available endpoints:")
        logger.debug("   POST /v1/responses - Responses API endpoint")
        logger.debug("   GET  /v1/models - List available models")
        logger.debug("   GET  /v1/auth/status - Authentication status")
        logger.debug("   GET  /health - Health check")
        logger.debug(
            "API Key protection: %s",
            "Enabled" if auth_manager.get_api_key() else "Disabled",
        )

    logger.info("Responses API parameters:")
    logger.info(
        "  Supported: model, input, instructions, previous_response_id, stream, allowed_tools, tools, metadata"
    )
    logger.info("  See README.md for details")

    mcp_servers = get_mcp_servers()
    if mcp_servers:
        logger.info("MCP servers configured: %s", list(mcp_servers.keys()))
    else:
        logger.info("No MCP servers configured (set MCP_CONFIG to enable)")

    session_manager.start_cleanup_task()

    # --- Docker per-user sandbox ---
    if DOCKER_SANDBOX_ENABLED and DOCKER_SANDBOX_ROLE != "worker":
        from src import docker_sandbox as _dsb

        _dsb.init_sandbox()
        if _dsb.sandbox_manager:
            _dsb.sandbox_manager.start_cleanup_task()
        logger.info("Docker per-user sandbox enabled (orchestrator mode)")
    elif DOCKER_SANDBOX_ENABLED:
        logger.info("Docker sandbox worker mode (single-user container)")

    app.state.started_at = time.time()

    yield

    # --- Shutdown Docker sandbox containers ---
    if DOCKER_SANDBOX_ENABLED and DOCKER_SANDBOX_ROLE != "worker":
        from src.docker_sandbox import shutdown_sandbox

        logger.info("Shutting down Docker sandbox containers...")
        await shutdown_sandbox()

    logger.info("Shutting down session manager...")
    await session_manager.async_shutdown()


app = FastAPI(
    title="Claude Code Gateway",
    description="API gateway for Claude Code",
    version=__version__,
    lifespan=lifespan,
)

cors_origins = json.loads(os.getenv("CORS_ORIGINS", '["*"]'))
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if limiter:
    app.state.limiter = limiter
    app.add_exception_handler(429, rate_limit_exceeded_handler)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "message": "Request body too large. Maximum size is %d bytes." % MAX_REQUEST_SIZE,
                        "type": "request_too_large",
                        "code": 413,
                    }
                },
            )
        return await call_next(request)


class DebugLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = getattr(request.state, "request_id", "unknown")

        if not (DEBUG_MODE or VERBOSE):
            return await call_next(request)

        start_time = asyncio.get_event_loop().time()

        logger.debug("[%s] Incoming request: %s %s", request_id, request.method, request.url)
        _SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
        safe_headers = {
            k: "***" if k.lower() in _SENSITIVE_HEADERS else v for k, v in request.headers.items()
        }
        logger.debug("[%s] Headers: %s", request_id, safe_headers)

        body_logged = False
        if request.method == "POST" and request.url.path.startswith("/v1/"):
            try:
                content_length = request.headers.get("content-length")
                if content_length and int(content_length) < 100000:
                    body = await request.body()
                    if body:
                        try:
                            parsed_body = json.loads(body.decode())
                            logged_body = truncate_image_data(parsed_body)
                            logger.debug("Request body: %s", json.dumps(logged_body, indent=2))
                            body_logged = True
                        except Exception:
                            logger.debug(
                                "Request body: [non-JSON, %d bytes, content-type: %s]",
                                len(body),
                                request.headers.get("content-type", "unknown"),
                            )
                            body_logged = True
            except Exception as e:
                logger.debug("Could not read request body: %s", e)

        if not body_logged and request.method == "POST":
            logger.debug("Request body: [not logged - streaming or large payload]")

        try:
            response = await call_next(request)
            end_time = asyncio.get_event_loop().time()
            duration = (end_time - start_time) * 1000
            logger.debug("Response: %s in %.2fms", response.status_code, duration)
            return response

        except Exception as e:
            end_time = asyncio.get_event_loop().time()
            duration = (end_time - start_time) * 1000
            logger.debug("Request failed after %.2fms: %s", duration, e)
            raise


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not request_logger.should_log(path):
            return await call_next(request)

        start = asyncio.get_event_loop().time()
        model: Optional[str] = None
        session_id: Optional[str] = None
        backend: Optional[str] = None

        if request.method == "POST" and path.startswith("/v1/"):
            try:
                content_length = request.headers.get("content-length")
                if content_length and int(content_length) < 100_000:
                    body = await request.body()
                    if body:
                        parsed = json.loads(body.decode())
                        model = parsed.get("model")
                        session_id = parsed.get("session_id")
                        if model:
                            try:
                                from src.backends import resolve_model
                                resolved = resolve_model(model)
                                if resolved:
                                    backend = resolved.backend
                            except Exception as exc:
                                logger.debug(
                                    "Could not resolve backend for request logging: %s", exc
                                )
            except Exception as exc:
                logger.debug("Could not inspect request body for request logging: %s", exc)

        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            raise
        finally:
            elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
            client_ip = request.client.host if request.client else "unknown"
            entry = RequestLogEntry(
                timestamp=time.time(),
                method=request.method,
                path=path,
                status_code=status_code,
                response_time_ms=round(elapsed_ms, 2),
                client_ip=client_ip,
                model=model,
                backend=backend,
                session_id=session_id,
            )
            request_logger.log(entry)


app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(DebugLoggingMiddleware)
app.add_middleware(RequestLoggingMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    sanitized_errors = _sanitize_validation_errors(exc.errors())
    logger.error("Request validation failed for %s %s", request.method, request.url)
    logger.error("Validation errors: %s", sanitized_errors)

    error_response = {
        "error": {
            "message": "Request validation failed - the request body doesn't match the expected format",
            "type": "validation_error",
            "code": "invalid_request_error",
            "details": sanitized_errors,
            "help": {
                "common_issues": [
                    "Missing required fields (model, input)",
                    "Invalid field types (e.g. input should be an array or string)",
                    "Invalid role values (must be 'system', 'user', or 'assistant')",
                    "Invalid previous_response_id format",
                ],
                "debug_tip": "Set DEBUG_MODE=true or VERBOSE=true environment variable for more detailed logging",
            },
        }
    }
    return JSONResponse(status_code=422, content=error_response)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"message": exc.detail, "type": "api_error", "code": str(exc.status_code)}
        },
    )


# ==================== Register Routers ====================

from src.routes import (  # noqa: E402
    responses_router,
    sessions_router,
    general_router,
    admin_router,
)

if DOCKER_SANDBOX_ENABLED and DOCKER_SANDBOX_ROLE != "worker":
    from src.sandbox_route import router as sandbox_responses_router  # noqa: E402

    app.include_router(sandbox_responses_router)
    logger.info("Registered sandbox responses router (orchestrator mode)")
else:
    app.include_router(responses_router)

app.include_router(sessions_router)
app.include_router(general_router)
app.include_router(admin_router)


# ==================== Backward-compat re-exports ====================

from src.routes.responses import (  # noqa: E402, F401, F811
    _generate_msg_id,
    _make_response_id,
    _parse_response_id,
    _responses_streaming_preflight,
)
from src.session_manager import session_manager  # noqa: E402, F401, F811
from src.backends.claude.constants import DEFAULT_ALLOWED_TOOLS  # noqa: E402, F401, F811
from src.constants import PERMISSION_MODE_BYPASS  # noqa: E402, F401, F811
from src.backend_registry import ResolvedModel  # noqa: E402, F401, F811
from src import streaming_utils  # noqa: E402, F401, F811


# ==================== Server Startup ====================


def find_available_port(start_port: int = 8000, max_attempts: int = 10) -> int:
    import socket

    for port in range(start_port, start_port + max_attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(("127.0.0.1", port))
            if result != 0:
                return port
        except Exception:
            return port
        finally:
            sock.close()

    raise RuntimeError(
        "No available ports found in range %d-%d" % (start_port, start_port + max_attempts - 1)
    )


def run_server(port: int = None, host: str = None):
    import uvicorn

    global runtime_api_key
    runtime_api_key = prompt_for_api_protection()
    auth_manager.runtime_api_key = runtime_api_key

    if port is None:
        port = DEFAULT_PORT
    if host is None:
        host = DEFAULT_HOST
    preferred_port = port

    try:
        uvicorn.run(app, host=host, port=preferred_port)  # nosec B104
    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 48:
            logger.warning("Port %d is already in use. Finding alternative port...", preferred_port)
            try:
                available_port = find_available_port(preferred_port + 1)
                logger.info("Starting server on alternative port %d", available_port)
                print("Server starting on http://localhost:%d" % available_port)
                print("Update your client base_url to: http://localhost:%d/v1" % available_port)
                uvicorn.run(app, host=host, port=available_port)  # nosec B104
            except RuntimeError as port_error:
                logger.error("Could not find available port: %s", port_error)
                raise
        else:
            raise


if __name__ == "__main__":
    import sys

    port = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            print("Using port from command line: %d" % port)
        except ValueError:
            print("Invalid port number: %s. Using default." % sys.argv[1])

    run_server(port)
