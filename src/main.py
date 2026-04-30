import os
import json
import asyncio
import inspect
import logging
import secrets
import string
import time
import uuid
from typing import Any, Optional, Sequence, cast
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
)
from src import __version__
from src.mcp_config import get_mcp_servers
from src.request_logger import request_logger, RequestLogEntry
from src.routes.deps import truncate_image_data

# Note: load_dotenv() is called in constants.py at import time
# DEBUG_MODE and VERBOSE are imported from src.constants (single source of truth)

# Set logging level based on debug/verbose mode
log_level = logging.DEBUG if (DEBUG_MODE or VERBOSE) else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Legacy global — auth_manager.runtime_api_key is the authoritative source.
# This variable is kept for backward compat (tests read main.runtime_api_key).
# To set the runtime key, always use: auth_manager.runtime_api_key = token
runtime_api_key = None


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, str]]:
    """Strip raw input payloads from validation errors for logging and responses."""
    sanitized = []
    for error in errors:
        if not isinstance(error, dict):
            sanitized.append(
                {
                    "field": "",
                    "message": str(error),
                    "type": "validation_error",
                }
            )
            continue
        sanitized.append(
            {
                "field": " -> ".join(str(loc) for loc in error.get("loc", [])),
                "message": error.get("msg", "Unknown validation error"),
                "type": error.get("type", "validation_error"),
            }
        )
    return sanitized


def generate_secure_token(length: int = 32) -> str:
    """Generate a secure random token for API authentication."""
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def prompt_for_api_protection() -> Optional[str]:
    """
    Interactively ask user if they want API key protection.
    Returns the generated token if user chooses protection, None otherwise.
    """
    # Don't prompt if API_KEY is already set via environment variable
    if os.getenv("API_KEY"):
        return None

    print("\n" + "=" * 60)
    print("🔐 API Endpoint Security Configuration")
    print("=" * 60)
    print("Would you like to protect your API endpoint with an API key?")
    print("This adds a security layer when accessing your server remotely.")
    print("")

    while True:
        try:
            choice = input("Enable API key protection? (y/N): ").strip().lower()

            if choice in ["", "n", "no"]:
                print("✅ API endpoint will be accessible without authentication")
                print("=" * 60)
                return None

            elif choice in ["y", "yes"]:
                token = generate_secure_token()
                print("")
                print("🔑 API Key Generated!")
                print("=" * 60)
                print(f"API Key: {token}")
                print("=" * 60)
                print("📋 IMPORTANT: Save this key - you'll need it for API calls!")
                print("   Example usage:")
                print(f'   curl -H "Authorization: Bearer {token}" \\')
                print(f"        http://localhost:{DEFAULT_PORT}/v1/models")
                print("=" * 60)
                return token

            else:
                print("Please enter 'y' for yes or 'n' for no (or press Enter for no)")

        except (EOFError, KeyboardInterrupt):
            print("\n✅ Defaulting to no authentication")
            return None


# Note: claude_cli is now created inside discover_backends() and registered
# in the BackendRegistry. Access it via BackendRegistry.get("claude").


async def _log_slash_commands() -> None:
    """Fetch the slash-command allowlist from the Claude backend and log it."""
    if not BackendRegistry.is_registered("claude"):
        return
    try:
        from src.backends.claude import slash_commands

        claude_backend = BackendRegistry.get("claude")
        cwd = getattr(claude_backend, "cwd", None)
        commands = await slash_commands.get_available_commands(cwd=cwd)
    except Exception as e:
        logger.warning(f"Could not fetch slash-command list: {e}")
        return

    blocked_present = sorted(commands & slash_commands.BLOCKED_COMMANDS)
    allowed = sorted(commands - slash_commands.BLOCKED_COMMANDS)
    if allowed:
        logger.info(
            f"Slash commands available ({len(allowed)}): {', '.join('/' + n for n in allowed)}"
        )
    else:
        logger.info("Slash commands available: (none)")
    if blocked_present:
        logger.info(
            f"Slash commands blocked by server ({len(blocked_present)}): "
            f"{', '.join('/' + n for n in blocked_present)}"
        )


async def _verify_backends() -> None:
    """Verify all registered backends at startup with timeout."""
    for name, backend in BackendRegistry.all_backends().items():
        try:
            logger.info(f"Verifying {name} backend...")
            verified = await asyncio.wait_for(backend.verify(), timeout=30.0)
            if verified:
                logger.info(f"✅ {name} backend verified successfully")
            else:
                logger.warning(f"⚠️  {name} backend verification returned False")
        except asyncio.TimeoutError:
            logger.warning(f"⚠️  {name} backend verification timed out (30s)")
        except Exception as e:
            logger.error(f"⚠️  {name} backend verification failed: {e}")


async def _shutdown_backends() -> None:
    """Close backend-owned resources such as managed child processes."""
    for name, backend in BackendRegistry.all_backends().items():
        close = getattr(backend, "close", None) or getattr(backend, "shutdown", None)
        if close is None:
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Backend %s shutdown failed", name, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize backends, verify authentication, and start background tasks."""
    logger.info("Initializing backend registry...")

    # Validate admin configuration — fail fast if ADMIN_API_KEY is missing
    from src.admin_auth import validate_admin_config

    validate_admin_config()

    # Clean stale Bedrock/Vertex env vars before anything else
    auth_manager.clean_stale_env_vars()

    # Validate Claude authentication first
    auth_valid, auth_info = validate_claude_code_auth()

    if not auth_valid:
        logger.error("❌ Claude Code authentication failed!")
        for error in auth_info.get("errors", []):
            logger.error(f"  - {error}")
        logger.warning("Authentication setup guide:")
        logger.warning("  1. For Anthropic API: Set ANTHROPIC_AUTH_TOKEN")
        logger.warning("  2. For CLI auth: Run 'claude auth login'")
    else:
        logger.info(f"✅ Claude Code authentication validated: {auth_info['method']}")

    # Load custom system prompt (if configured)
    from src.system_prompt import load_default_prompt
    from src.constants import SYSTEM_PROMPT_FILE

    load_default_prompt(SYSTEM_PROMPT_FILE)

    # Discover and register backends
    discover_backends()

    # Verify all registered backends
    await _verify_backends()

    # Warm up slash-command allowlist so the first /v1/responses request
    # doesn't pay the round-trip, and surface the list in startup logs.
    await _log_slash_commands()

    # Log debug information if debug mode is enabled
    if DEBUG_MODE or VERBOSE:
        logger.debug("🔧 Debug mode enabled - Enhanced logging active")
        logger.debug("🔧 Environment variables:")
        logger.debug(f"   DEBUG_MODE: {DEBUG_MODE}")
        logger.debug(f"   VERBOSE: {VERBOSE}")
        logger.debug(f"   PORT: {DEFAULT_PORT}")
        cors_origins_val = os.getenv("CORS_ORIGINS", '["*"]')
        logger.debug(f"   CORS_ORIGINS: {cors_origins_val}")
        logger.debug(f"   MAX_TIMEOUT: {DEFAULT_TIMEOUT_MS}")
        logger.debug(f"   CLAUDE_CWD: {os.getenv('CLAUDE_CWD', 'Not set')}")
        logger.debug("🔧 Available endpoints:")
        logger.debug("   POST /v1/responses - Responses API endpoint")
        logger.debug("   GET  /v1/models - List available models")
        logger.debug("   GET  /v1/auth/status - Authentication status")
        logger.debug("   GET  /health - Health check")
        logger.debug(
            f"🔧 API Key protection: {'Enabled' if auth_manager.get_api_key() else 'Disabled'}"
        )

    # Log Responses API parameter notice
    logger.info("Responses API parameters:")
    logger.info(
        "  Supported: model, input, instructions, previous_response_id, stream, allowed_tools, metadata"
    )
    logger.info("  See README.md for details")

    # Log MCP configuration
    mcp_servers = get_mcp_servers()
    if mcp_servers:
        logger.info(f"MCP servers configured: {list(mcp_servers.keys())}")
    else:
        logger.info("No MCP servers configured (set MCP_CONFIG to enable)")

    # Start session cleanup task
    session_manager.start_cleanup_task()

    # Bring up the optional usage-log SQLAlchemy engine (no-op when the env var is
    # unset).  Kept late in startup so a flaky logging DB cannot block the
    # gateway from becoming healthy.
    from src.usage_logger import usage_logger

    await usage_logger.start()

    # Record server start time for uptime tracking
    app.state.started_at = time.time()

    yield

    # Cleanup on shutdown (async to disconnect SDK clients)
    logger.info("Shutting down session manager...")
    await session_manager.async_shutdown()
    await _shutdown_backends()
    await usage_logger.close()


# Create FastAPI app
app = FastAPI(
    title="Oh My Gateway",
    description="OpenAI-compatible gateway for coding agent backends",
    version=__version__,
    lifespan=lifespan,
)

# Configure CORS
cors_origins = json.loads(os.getenv("CORS_ORIGINS", '["*"]'))
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add rate limiting error handler
if limiter:
    app.state.limiter = limiter
    app.add_exception_handler(429, cast(Any, rate_limit_exceeded_handler))


# ==================== Middleware ====================


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to each request for audit trails."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request body size to prevent DoS attacks."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "message": f"Request body too large. Maximum size is {MAX_REQUEST_SIZE} bytes.",
                        "type": "request_too_large",
                        "code": 413,
                    }
                },
            )
        return await call_next(request)


class DebugLoggingMiddleware(BaseHTTPMiddleware):
    """ASGI-compliant middleware for logging request/response details when debug mode is enabled."""

    async def dispatch(self, request: Request, call_next):
        # Get request ID for correlation
        request_id = getattr(request.state, "request_id", "unknown")

        if not (DEBUG_MODE or VERBOSE):
            return await call_next(request)

        # Log request details
        start_time = asyncio.get_event_loop().time()

        # Log basic request info with request ID for correlation
        logger.debug(f"🔍 [{request_id}] Incoming request: {request.method} {request.url}")
        _SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
        safe_headers = {
            k: "***" if k.lower() in _SENSITIVE_HEADERS else v for k, v in request.headers.items()
        }
        logger.debug(f"🔍 [{request_id}] Headers: {safe_headers}")

        # For POST requests, try to log body (but don't break if we can't)
        body_logged = False
        if request.method == "POST" and request.url.path.startswith("/v1/"):
            try:
                # Only attempt to read body if it's reasonable size and content-type
                content_length = request.headers.get("content-length")
                if content_length and int(content_length) < 100000:  # Less than 100KB
                    body = await request.body()
                    if body:
                        try:
                            parsed_body = json.loads(body.decode())
                            # Truncate base64 image data in logged body
                            logged_body = truncate_image_data(parsed_body)
                            logger.debug(f"🔍 Request body: {json.dumps(logged_body, indent=2)}")
                            body_logged = True
                        except Exception:
                            # Do not log raw bytes: a malformed JSON body may
                            # contain Bearer tokens, API keys, or PII. Log only
                            # metadata useful for debugging.
                            logger.debug(
                                "🔍 Request body: [non-JSON, %d bytes, content-type: %s]",
                                len(body),
                                request.headers.get("content-type", "unknown"),
                            )
                            body_logged = True
            except Exception as e:
                logger.debug(f"🔍 Could not read request body: {e}")

        if not body_logged and request.method == "POST":
            logger.debug("🔍 Request body: [not logged - streaming or large payload]")

        # Process the request
        try:
            response = await call_next(request)

            # Log response details
            end_time = asyncio.get_event_loop().time()
            duration = (end_time - start_time) * 1000  # Convert to milliseconds

            logger.debug(f"🔍 Response: {response.status_code} in {duration:.2f}ms")

            return response

        except Exception as e:
            end_time = asyncio.get_event_loop().time()
            duration = (end_time - start_time) * 1000

            logger.debug(f"🔍 Request failed after {duration:.2f}ms: {e}")
            raise


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log request metadata to the in-memory request logger for admin observability.

    Excludes ``/admin/api/*`` and other non-API paths by default (configured
    via ``request_logger.should_log``).  Latency measures handler creation
    time only — streaming completion time is **not** included.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not request_logger.should_log(path):
            return await call_next(request)

        start = asyncio.get_event_loop().time()
        model: Optional[str] = None
        session_id: Optional[str] = None
        backend: Optional[str] = None

        # Extract model/session_id from request body for /v1/ POST endpoints.
        # Follow the same safety pattern as the existing debug middleware:
        # small payloads only, tolerate parse failure.
        if request.method == "POST" and path.startswith("/v1/"):
            try:
                content_length = request.headers.get("content-length")
                if content_length and int(content_length) < 100_000:
                    body = await request.body()
                    if body:
                        parsed = json.loads(body.decode())
                        model = parsed.get("model")
                        session_id = parsed.get("session_id")
                        # Resolve backend from model name
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


# Add security middleware (order matters - first added = last executed)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)

# Add the debug middleware
app.add_middleware(DebugLoggingMiddleware)

# Add request logging middleware (for admin observability)
app.add_middleware(RequestLoggingMiddleware)


# ==================== Exception Handlers ====================


# Custom exception handler for 422 validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors with sanitized field-level details."""

    sanitized_errors = _sanitize_validation_errors(exc.errors())

    # Log the validation error details
    logger.error(f"❌ Request validation failed for {request.method} {request.url}")
    logger.error("❌ Validation errors: %s", sanitized_errors)

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
async def http_exception_handler(_request: Request, exc: HTTPException):
    """Format HTTP exceptions as OpenAI-style errors."""
    detail = exc.detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        # Caller already produced an OpenAI-style {"error": {...}} payload;
        # pass it through unchanged to avoid double-nesting.
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": detail, "type": "api_error", "code": str(exc.status_code)}},
    )


# ==================== Register Routers ====================

from src.routes import (  # noqa: E402
    responses_router,
    sessions_router,
    general_router,
    admin_router,
)

app.include_router(responses_router)
app.include_router(sessions_router)
app.include_router(general_router)
app.include_router(admin_router)


# ==================== Backward-compat re-exports ====================
# Tests call these functions directly as main.X() — re-exports are required.

from src.routes.responses import (  # noqa: E402, F401
    _generate_msg_id,
    _make_response_id,
    _parse_response_id,
    _responses_streaming_preflight,
)
from src.backends.claude.constants import DEFAULT_ALLOWED_TOOLS  # noqa: E402, F401
from src.constants import PERMISSION_MODE_BYPASS  # noqa: E402, F401
from src.backend_registry import ResolvedModel  # noqa: E402, F401
from src import streaming_utils  # noqa: E402, F401


# ==================== Server Startup ====================


def find_available_port(start_port: int = 8000, max_attempts: int = 10) -> int:
    """Find an available port starting from start_port."""
    import socket

    for port in range(start_port, start_port + max_attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(("127.0.0.1", port))
            if result != 0:  # Port is available
                return port
        except Exception:
            return port
        finally:
            sock.close()

    raise RuntimeError(
        f"No available ports found in range {start_port}-{start_port + max_attempts - 1}"
    )


def run_server(port: Optional[int] = None, host: Optional[str] = None) -> None:
    """Run the server - used as script entry point."""
    import uvicorn

    # Handle interactive API key protection
    global runtime_api_key
    runtime_api_key = prompt_for_api_protection()
    auth_manager.runtime_api_key = runtime_api_key

    # Priority: CLI arg > constants (which reads env vars)
    if port is None:
        port = DEFAULT_PORT
    if host is None:
        host = DEFAULT_HOST
    preferred_port = port

    try:
        # Try the preferred port first
        # Binding to 0.0.0.0 is intentional for container/development use
        uvicorn.run(app, host=host, port=preferred_port)  # nosec B104
    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 48:
            logger.warning(f"Port {preferred_port} is already in use. Finding alternative port...")
            try:
                available_port = find_available_port(preferred_port + 1)
                logger.info(f"Starting server on alternative port {available_port}")
                print(f"\n🚀 Server starting on http://localhost:{available_port}")
                print(f"📝 Update your client base_url to: http://localhost:{available_port}/v1")
                # Binding to 0.0.0.0 is intentional for container/development use
                uvicorn.run(app, host=host, port=available_port)  # nosec B104
            except RuntimeError as port_error:
                logger.error(f"Could not find available port: {port_error}")
                print(f"\n❌ Error: {port_error}")
                print("💡 Try setting a specific port with: PORT=9000 uv run python main.py")
                raise
        else:
            raise


if __name__ == "__main__":
    import sys

    # Simple CLI argument parsing for port
    port: Optional[int] = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            print(f"Using port from command line: {port}")
        except ValueError:
            print(f"Invalid port number: {sys.argv[1]}. Using default.")

    run_server(port)
