import os
import json
import asyncio
import logging
import secrets
import string
import uuid
from typing import Optional, AsyncGenerator, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from src.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
    SessionListResponse,
    # Anthropic API compatible models
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicTextBlock,
    AnthropicUsage,
)
from src.landing_page import build_root_page
from src.message_adapter import MessageAdapter
from src.auth import (
    verify_api_key,
    security,
    validate_claude_code_auth,
    validate_backend_auth,
    get_claude_code_auth_info,
    get_all_backends_auth_info,
    auth_manager,
)
from src.parameter_validator import ParameterValidator, CompatibilityReporter
from src.session_manager import session_manager
from src.backends import (
    BackendClient,
    BackendConfigError,
    BackendRegistry,
    resolve_model,
    ResolvedModel,
    discover_backends,
)
from src.response_models import (
    ResponseCreateRequest,
    ResponseErrorDetail,
    ResponseObject,
    OutputItem,
    ContentPart,
    ResponseUsage,
)
from src.rate_limiter import (
    limiter,
    rate_limit_exceeded_handler,
    rate_limit_endpoint,
)
from src.constants import (
    DEFAULT_TIMEOUT_MS,
    DEFAULT_PORT,
    DEFAULT_HOST,
    DEFAULT_MAX_TURNS,
    MAX_REQUEST_SIZE,
    PERMISSION_MODE_BYPASS,
    RESPONSE_SENTINEL,
    WRAP_INTERMEDIATE_THINKING,
)
from src.backends.claude.constants import DEFAULT_ALLOWED_TOOLS
from src.mcp_config import get_mcp_servers, get_mcp_tool_patterns
from src import streaming_utils

# Note: load_dotenv() is called in constants.py at import time

# Configure logging based on debug mode
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() in ("true", "1", "yes", "on")
VERBOSE = os.getenv("VERBOSE", "false").lower() in ("true", "1", "yes", "on")

# Set logging level based on debug/verbose mode
log_level = logging.DEBUG if (DEBUG_MODE or VERBOSE) else logging.INFO
logging.basicConfig(level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Global variable to store runtime-generated API key
runtime_api_key = None


def _truncate_image_data(obj):
    """Deep-copy and truncate base64 image data for safe logging."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in ("data", "url") and isinstance(v, str) and len(v) > 200:
                if "base64" in v[:50] or v.startswith("data:image"):
                    result[k] = v[:50] + "...[truncated]"
                    continue
            result[k] = _truncate_image_data(v)
        return result
    if isinstance(obj, list):
        return [_truncate_image_data(item) for item in obj]
    return obj


def _request_has_images(request) -> bool:
    """Check if any message in the request contains image content parts."""
    messages = getattr(request, "messages", None)
    if not messages:
        # Check for Responses API input
        input_data = getattr(request, "input", None)
        if isinstance(input_data, list):
            for item in input_data:
                content = getattr(item, "content", None) or (
                    item.get("content") if isinstance(item, dict) else None
                )
                if isinstance(content, list):
                    for part in content:
                        ptype = (
                            part.get("type")
                            if isinstance(part, dict)
                            else getattr(part, "type", None)
                        )
                        if ptype in ("image_url", "input_image", "image"):
                            return True
        return False
    for msg in messages:
        content = msg.content if hasattr(msg, "content") else None
        if isinstance(content, list):
            for part in content:
                ptype = (
                    part.type
                    if hasattr(part, "type")
                    else (part.get("type") if isinstance(part, dict) else None)
                )
                if ptype in ("image_url", "input_image", "image"):
                    return True
    return False


def _validate_image_request(request, backend) -> None:
    """Validate image requests: tools must be enabled, backend must support images.

    Raises HTTPException(400) on failure.
    """
    if not _request_has_images(request):
        return

    # Check tools enabled (chat completions only has enable_tools)
    enable_tools = getattr(request, "enable_tools", True)
    if not enable_tools:
        raise HTTPException(
            status_code=400,
            detail="Image input requires tools to be enabled (enable_tools=true) "
            "because Claude Code uses the Read tool to process images.",
        )

    # Check backend supports images
    if not hasattr(backend, "image_handler"):
        raise HTTPException(
            status_code=400,
            detail=f"Image input is not supported for the {backend.name} backend.",
        )


def map_stop_reason(stop_reason: Optional[str] = None) -> str:
    """Map Claude SDK stop_reason to OpenAI finish_reason."""
    return streaming_utils.map_stop_reason(stop_reason)


def extract_stop_reason(messages: list) -> Optional[str]:
    """Extract stop_reason from collected SDK messages (last result message)."""
    return streaming_utils.extract_stop_reason(messages)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize backends, verify authentication, and start background tasks."""
    logger.info("Initializing backend registry...")

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

    # Discover and register backends
    discover_backends()

    # Verify all registered backends
    await _verify_backends()

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
        logger.debug("   POST /v1/chat/completions - Main chat endpoint")
        logger.debug("   GET  /v1/models - List available models")
        logger.debug("   POST /v1/debug/request - Debug request validation")
        logger.debug("   GET  /v1/auth/status - Authentication status")
        logger.debug("   GET  /health - Health check")
        logger.debug(
            f"🔧 API Key protection: {'Enabled' if (os.getenv('API_KEY') or runtime_api_key) else 'Disabled'}"
        )

    # Log OpenAI API parameter compatibility notice
    logger.info("OpenAI API parameter compatibility:")
    logger.info(
        "  Supported: model, messages, stream, system (via role), session_id, enable_tools, response_format (json_schema)"
    )
    logger.info(
        "  Ignored: temperature, top_p, max_tokens, max_completion_tokens, presence_penalty, frequency_penalty, logit_bias, stop"
    )
    logger.info("  Rejected: n > 1 (returns validation error)")
    logger.info("  See README.md for details")

    # Log MCP configuration
    mcp_servers = get_mcp_servers()
    if mcp_servers:
        logger.info(f"MCP servers configured: {list(mcp_servers.keys())}")
    else:
        logger.info("No MCP servers configured (set MCP_CONFIG to enable)")

    # Start session cleanup task
    session_manager.start_cleanup_task()

    yield

    # Cleanup on shutdown (async to disconnect SDK clients)
    logger.info("Shutting down session manager...")
    await session_manager.async_shutdown()


# Create FastAPI app
app = FastAPI(
    title="Claude Code OpenAI API Wrapper",
    description="OpenAI-compatible API for Claude Code",
    version="1.0.0",
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
    app.add_exception_handler(429, rate_limit_exceeded_handler)

# Security configuration (MAX_REQUEST_SIZE imported from constants)

# Add middleware


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


# Add security middleware (order matters - first added = last executed)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)


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
        logger.debug(f"🔍 [{request_id}] Headers: {dict(request.headers)}")

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
                            import json as json_lib

                            parsed_body = json_lib.loads(body.decode())
                            # Truncate base64 image data in logged body
                            logged_body = _truncate_image_data(parsed_body)
                            logger.debug(
                                f"🔍 Request body: {json_lib.dumps(logged_body, indent=2)}"
                            )
                            body_logged = True
                        except Exception:
                            logger.debug(f"🔍 Request body (raw): {body.decode()[:500]}...")
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


# Add the debug middleware
app.add_middleware(DebugLoggingMiddleware)


# Custom exception handler for 422 validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors with detailed debugging information."""

    # Log the validation error details
    logger.error(f"❌ Request validation failed for {request.method} {request.url}")
    logger.error(f"❌ Validation errors: {exc.errors()}")

    # Create detailed error response
    error_details = []
    for error in exc.errors():
        location = " -> ".join(str(loc) for loc in error.get("loc", []))
        error_details.append(
            {
                "field": location,
                "message": error.get("msg", "Unknown validation error"),
                "type": error.get("type", "validation_error"),
                "input": error.get("input"),
            }
        )

    # If debug mode is enabled, include the raw request body
    debug_info = {}
    if DEBUG_MODE or VERBOSE:
        try:
            body = await request.body()
            if body:
                debug_info["raw_request_body"] = body.decode()
        except Exception:
            debug_info["raw_request_body"] = "Could not read request body"

    error_response = {
        "error": {
            "message": "Request validation failed - the request body doesn't match the expected format",
            "type": "validation_error",
            "code": "invalid_request_error",
            "details": error_details,
            "help": {
                "common_issues": [
                    "Missing required fields (model, messages)",
                    "Invalid field types (e.g. messages should be an array)",
                    "Invalid role values (must be 'system', 'user', or 'assistant')",
                    "Invalid parameter ranges (e.g. temperature must be 0-2)",
                ],
                "debug_tip": "Set DEBUG_MODE=true or VERBOSE=true environment variable for more detailed logging",
            },
        }
    }

    # Add debug info if available
    if debug_info:
        error_response["error"]["debug"] = debug_info

    return JSONResponse(status_code=422, content=error_response)


def _resolve_and_get_backend(
    model: str,
) -> tuple[ResolvedModel, "BackendClient"]:
    """Resolve model → backend and validate backend availability.

    Raises HTTPException if the backend is not registered (e.g. Codex disabled).
    """
    resolved = resolve_model(model)

    if not BackendRegistry.is_registered(resolved.backend):
        if resolved.backend == "codex":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Codex backend is not available. Install Codex CLI to use model '{model}'."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail=f"Backend '{resolved.backend}' for model '{model}' is not available.",
        )

    return resolved, BackendRegistry.get(resolved.backend)


def _validate_backend_auth(backend_name: str) -> None:
    """Validate backend authentication, raise HTTPException on failure."""
    auth_valid, auth_info = validate_backend_auth(backend_name)
    if not auth_valid:
        raise HTTPException(
            status_code=503,
            detail={
                "message": f"{backend_name} backend authentication failed",
                "errors": auth_info.get("errors", []),
                "help": "Check /v1/auth/status for detailed authentication information",
            },
        )


def _build_backend_options(
    request: ChatCompletionRequest,
    resolved: ResolvedModel,
    claude_headers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build backend-agnostic options from request, resolved model, and headers.

    Delegates to the backend's ``build_options()`` method and translates
    ``BackendConfigError`` into ``HTTPException``.
    """
    backend = BackendRegistry.get(resolved.backend)
    try:
        return backend.build_options(request, resolved, overrides=claude_headers)
    except BackendConfigError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


def _process_chunk_content(chunk: Dict[str, Any], content_sent: bool = False):
    """Extract content from a chunk message. Returns content list, result string, or None."""
    return streaming_utils.process_chunk_content(chunk, content_sent=content_sent)


def _extract_stream_event_delta(chunk: Dict[str, Any], in_thinking: bool = False) -> tuple:
    """Extract streamable text from a StreamEvent chunk."""
    return streaming_utils.extract_stream_event_delta(chunk, in_thinking=in_thinking)


def _make_sse(request_id: str, model: str, delta: dict, finish_reason=None, usage=None) -> str:
    """Build a single SSE-formatted line from a delta dict."""
    return streaming_utils.make_sse(
        request_id=request_id,
        model=model,
        delta=delta,
        finish_reason=finish_reason,
        usage=usage,
    )


def _is_assistant_content_chunk(chunk: Dict[str, Any]) -> bool:
    """Return True if chunk carries assistant content (AssistantMessage in any format)."""
    return streaming_utils.is_assistant_content_chunk(chunk)


async def _stream_chunks(
    chunk_source,
    request: ChatCompletionRequest,
    request_id: str,
    chunks_buffer: list,
) -> AsyncGenerator[str, None]:
    """Shared SSE streaming logic for both stateless and session modes."""
    async for line in streaming_utils.stream_chunks(
        chunk_source=chunk_source,
        request=request,
        request_id=request_id,
        chunks_buffer=chunks_buffer,
        logger=logger,
    ):
        yield line


def _mcp_allowed_tools() -> Optional[List[str]]:
    """Return DEFAULT_ALLOWED_TOOLS plus symbolic MCP tool patterns."""
    tools = list(DEFAULT_ALLOWED_TOOLS)
    servers = get_mcp_servers()
    if servers:
        tools.extend(get_mcp_tool_patterns(servers))
    return tools


def _prepare_stateless_completion(messages: list, claude_options: Dict[str, Any]) -> tuple:
    """Prepare prompt and run_completion kwargs for stateless mode.

    Returns:
        (prompt, run_kwargs) tuple
    """
    prompt, system_prompt = MessageAdapter.messages_to_prompt(messages)
    prompt = MessageAdapter.filter_content(prompt)
    if system_prompt:
        system_prompt = MessageAdapter.filter_content(system_prompt)

    run_kwargs = dict(
        prompt=prompt,
        system_prompt=system_prompt,
        model=claude_options.get("model"),
        max_turns=claude_options.get("max_turns", DEFAULT_MAX_TURNS),
        allowed_tools=claude_options.get("allowed_tools"),
        disallowed_tools=claude_options.get("disallowed_tools"),
        permission_mode=claude_options.get("permission_mode"),
        output_format=claude_options.get("output_format"),
        mcp_servers=claude_options.get("mcp_servers"),
    )
    return prompt, run_kwargs


def _prepare_session_prompt(
    request: ChatCompletionRequest,
    backend=None,
) -> tuple:
    """Prepare prompt, system_prompt, and session for session-mode requests.

    IMPORTANT: This function does NOT mutate session state and does NOT
    compute ``is_new``.  The ``is_new`` check must happen inside the
    per-session lock to prevent two concurrent first-turn requests from
    both seeing ``is_new=True``.  Callers must call
    ``session.add_messages()`` explicitly after acquiring the lock.

    Returns:
        (prompt, system_prompt, session) tuple
    """
    session = session_manager.get_or_create_session(request.session_id)

    last_user_msg = None
    for msg in reversed(request.messages):
        if msg.role == "user":
            if isinstance(msg.content, list) and backend and hasattr(backend, "image_handler"):
                last_user_msg = MessageAdapter.extract_images_to_prompt(
                    msg.content, backend.image_handler
                )
            else:
                last_user_msg = MessageAdapter._content_to_text(msg.content)
            break
    prompt = last_user_msg or MessageAdapter.messages_to_prompt(request.messages)[0]

    # Extract system prompt from messages
    system_prompt = None
    for msg in request.messages:
        if msg.role == "system":
            if isinstance(msg.content, str):
                system_prompt = msg.content
            elif isinstance(msg.content, list):
                system_prompt = " ".join(
                    p.text
                    for p in msg.content
                    if hasattr(p, "type") and p.type == "text" and hasattr(p, "text")
                )
            break
    if system_prompt:
        system_prompt = MessageAdapter.filter_content(system_prompt)

    return prompt, system_prompt, session


def _capture_provider_session_id(chunks_buffer: list, session) -> None:
    """Scan chunks for a ``codex_session`` meta-event and store the thread_id."""
    for chunk in chunks_buffer:
        if isinstance(chunk, dict) and chunk.get("type") == "codex_session":
            thread_id = chunk.get("session_id")
            if thread_id and session is not None:
                session.provider_session_id = thread_id
                logger.debug("Captured Codex thread_id: %s", thread_id)
            break


async def _streaming_session_preflight(
    request: ChatCompletionRequest,
    resolved,
    backend,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    """Run session guards and mutation BEFORE StreamingResponse is created.

    This ensures HTTPException (400 backend mismatch, 409 Codex resume guard)
    is raised while the endpoint can still return a proper HTTP error status,
    rather than inside the async generator where Starlette has already committed
    the 200 status line.

    Returns a dict with keys needed by the streaming generator:
        session, lock_acquired, prompt, sys_prompt, is_new, resume_id, chunk_kwargs
    """
    prompt, sys_prompt, session = _prepare_session_prompt(request, backend=backend)

    # Acquire per-session lock BEFORE checking is_new or mutating state.
    await session.lock.acquire()

    try:
        is_new = len(session.messages) == 0

        # Enforce backend invariant (inside lock to avoid TOCTOU)
        if not is_new and session.backend != resolved.backend:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Session '{request.session_id}' belongs to backend '{session.backend}', "
                    f"but model '{request.model}' resolves to '{resolved.backend}'. "
                    f"Cannot mix backends within a session."
                ),
            )

        # Compute resume token BEFORE mutating session state
        resume_id = None
        if not is_new:
            if resolved.backend == "codex" and not session.provider_session_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot resume Codex session '{request.session_id}': "
                        f"the previous turn did not return a thread_id. "
                        f"Start a new session instead."
                    ),
                )
            resume_id = session.provider_session_id or request.session_id

        # Commit messages and tag backend AFTER all checks pass
        session.add_messages(request.messages)
        if is_new:
            session.backend = resolved.backend

    except Exception:
        # On any failure, release the lock before re-raising
        session.lock.release()
        raise

    return {
        "session": session,
        "lock_acquired": True,
        "prompt": prompt,
        "sys_prompt": sys_prompt,
        "is_new": is_new,
        "resume_id": resume_id,
        "chunk_kwargs": dict(
            prompt=prompt,
            model=options.get("model"),
            system_prompt=sys_prompt if is_new else None,
            permission_mode=options.get("permission_mode"),
            mcp_servers=options.get("mcp_servers"),
            allowed_tools=options.get("allowed_tools"),
            disallowed_tools=options.get("disallowed_tools"),
            output_format=options.get("output_format"),
            max_turns=options.get("max_turns", DEFAULT_MAX_TURNS),
            session_id=request.session_id if is_new else None,
            resume=resume_id,
            stream=True,
        ),
    }


async def generate_streaming_response(
    request: ChatCompletionRequest,
    request_id: str,
    claude_headers: Optional[Dict[str, Any]] = None,
    *,
    preflight: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[str, None]:
    """Generate SSE formatted streaming response via backend dispatch.

    If ``preflight`` is provided (from ``_streaming_session_preflight``), the
    generator skips session guards and uses the pre-validated state directly.
    The caller is responsible for having run preflight before creating the
    StreamingResponse so that HTTPExceptions surface as proper HTTP errors.
    """
    session = None
    lock_acquired = False
    chunks_buffer: list = []
    try:
        resolved, backend = _resolve_and_get_backend(request.model)

        if request.session_id and preflight is not None:
            # Fast path: use pre-validated session state from preflight.
            # Adopt lock ownership FIRST so finally can release on any failure.
            session = preflight["session"]
            lock_acquired = preflight["lock_acquired"]
            prompt = preflight["prompt"]
            chunk_source = backend.run_completion(**preflight["chunk_kwargs"])
        elif request.session_id:
            # Legacy path (direct calls without preflight, e.g. tests)
            _validate_backend_auth(resolved.backend)
            options = _build_backend_options(request, resolved, claude_headers)
            prompt, sys_prompt, session = _prepare_session_prompt(request, backend=backend)

            await session.lock.acquire()
            lock_acquired = True

            is_new = len(session.messages) == 0

            if not is_new and session.backend != resolved.backend:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Session '{request.session_id}' belongs to backend "
                        f"'{session.backend}', but model '{request.model}' resolves "
                        f"to '{resolved.backend}'. Cannot mix backends within a session."
                    ),
                )

            resume_id = None
            if not is_new:
                if resolved.backend == "codex" and not session.provider_session_id:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Cannot resume Codex session '{request.session_id}': "
                            f"the previous turn did not return a thread_id. "
                            f"Start a new session instead."
                        ),
                    )
                resume_id = session.provider_session_id or request.session_id

            session.add_messages(request.messages)
            if is_new:
                session.backend = resolved.backend

            chunk_source = backend.run_completion(
                prompt=prompt,
                model=options.get("model"),
                system_prompt=sys_prompt if is_new else None,
                permission_mode=options.get("permission_mode"),
                mcp_servers=options.get("mcp_servers"),
                allowed_tools=options.get("allowed_tools"),
                disallowed_tools=options.get("disallowed_tools"),
                output_format=options.get("output_format"),
                max_turns=options.get("max_turns", DEFAULT_MAX_TURNS),
                session_id=request.session_id if is_new else None,
                resume=resume_id,
                stream=True,
            )
        else:
            # Stateless mode
            _validate_backend_auth(resolved.backend)
            options = _build_backend_options(request, resolved, claude_headers)
            prompt, run_kwargs = _prepare_stateless_completion(request.messages, options)
            chunk_source = backend.run_completion(**run_kwargs, stream=True)

        # Stream chunks using shared SSE logic
        chunks_buffer = []
        try:
            async for sse_line in _stream_chunks(chunk_source, request, request_id, chunks_buffer):
                yield sse_line
        finally:
            # Close the SDK generator in the same task that iterated it.
            # The SDK uses anyio cancel scopes internally; closing from a
            # different task (e.g. Starlette response teardown) causes
            # "Attempted to exit cancel scope in a different task".
            await chunk_source.aclose()

        # Capture provider session id (e.g. Codex thread_id) from meta-events
        if session is not None:
            _capture_provider_session_id(chunks_buffer, session)

        # Extract assistant response from all chunks
        assistant_content = None
        if chunks_buffer:
            assistant_content = backend.parse_message(chunks_buffer)

            # Store in session if applicable
            actual_session_id = request.session_id
            if actual_session_id and assistant_content:
                assistant_message = Message(role="assistant", content=assistant_content)
                session_manager.add_assistant_response(actual_session_id, assistant_message)

        # Prepare usage data if requested (prefer real SDK values)
        usage_data = None
        if request.stream_options and request.stream_options.include_usage:
            sdk_usage = streaming_utils.extract_sdk_usage(chunks_buffer)
            if sdk_usage:
                token_usage = sdk_usage
            else:
                completion_text = assistant_content or ""
                token_usage = backend.estimate_token_usage(prompt, completion_text, request.model)
            usage_data = Usage(
                prompt_tokens=token_usage["prompt_tokens"],
                completion_tokens=token_usage["completion_tokens"],
                total_tokens=token_usage["total_tokens"],
            )
            logger.debug(f"Usage: {usage_data}")

        # Extract stop_reason from SDK messages and map to OpenAI finish_reason
        sdk_stop_reason = extract_stop_reason(chunks_buffer)
        finish_reason = map_stop_reason(sdk_stop_reason)

        # Send final chunk with finish reason and optionally usage data
        yield _make_sse(
            request_id, request.model, {}, finish_reason=finish_reason, usage=usage_data
        )
        yield "data: [DONE]\n\n"

    except HTTPException:
        raise  # Let FastAPI handle these
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        # Capture provider_session_id from partial chunks on mid-stream failure
        # so the Codex thread_id is not lost for future resume attempts.
        if session is not None and chunks_buffer:
            _capture_provider_session_id(chunks_buffer, session)
        error_chunk = {"error": {"message": str(e), "type": "streaming_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"
    finally:
        # Release per-session lock only if THIS coroutine acquired it
        if lock_acquired:
            session.lock.release()


@app.post("/v1/chat/completions")
@rate_limit_endpoint("chat")
async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """OpenAI-compatible chat completions endpoint with backend dispatch."""
    await verify_api_key(request, credentials)

    try:
        # Resolve model → backend and validate auth
        resolved, backend = _resolve_and_get_backend(request_body.model)
        _validate_backend_auth(resolved.backend)
        _validate_image_request(request_body, backend)

        request_id = f"chatcmpl-{os.urandom(8).hex()}"

        # Extract Claude-specific parameters from headers
        claude_headers = ParameterValidator.extract_claude_headers(dict(request.headers))

        # Log compatibility info
        if logger.isEnabledFor(logging.DEBUG):
            compatibility_report = CompatibilityReporter.generate_compatibility_report(request_body)
            logger.debug(f"Compatibility report: {compatibility_report}")

        if request_body.stream:
            # Run session preflight BEFORE creating StreamingResponse so that
            # HTTPExceptions (400 backend mismatch, 409 Codex guard) surface
            # as proper HTTP error status codes instead of being swallowed
            # inside the async generator after the 200 status line is committed.
            preflight = None
            if request_body.session_id:
                options = _build_backend_options(request_body, resolved, claude_headers)
                preflight = await _streaming_session_preflight(
                    request_body, resolved, backend, options
                )

            return StreamingResponse(
                generate_streaming_response(
                    request_body, request_id, claude_headers, preflight=preflight
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
        else:
            # Non-streaming response
            options = _build_backend_options(request_body, resolved, claude_headers)

            session = None
            if request_body.session_id:
                prompt, sys_prompt, session = _prepare_session_prompt(request_body, backend=backend)

                # Acquire lock BEFORE is_new check to prevent concurrent first-turn race
                async with session.lock:
                    is_new = len(session.messages) == 0

                    # Enforce backend invariant (inside lock to avoid TOCTOU)
                    if not is_new and session.backend != resolved.backend:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Session '{request_body.session_id}' belongs to backend "
                                f"'{session.backend}', but model '{request_body.model}' "
                                f"resolves to '{resolved.backend}'. "
                                f"Cannot mix backends within a session."
                            ),
                        )

                    # Compute resume token BEFORE mutating session state
                    resume_id = None
                    if not is_new:
                        if resolved.backend == "codex" and not session.provider_session_id:
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"Cannot resume Codex session "
                                    f"'{request_body.session_id}': the previous turn "
                                    f"did not return a thread_id. "
                                    f"Start a new session instead."
                                ),
                            )
                        resume_id = session.provider_session_id or request_body.session_id

                    # Commit messages and tag backend AFTER all checks pass
                    session.add_messages(request_body.messages)
                    if is_new:
                        session.backend = resolved.backend

                    chunks = []
                    async for chunk in backend.run_completion(
                        prompt=prompt,
                        model=options.get("model"),
                        system_prompt=sys_prompt if is_new else None,
                        permission_mode=options.get("permission_mode"),
                        mcp_servers=options.get("mcp_servers"),
                        allowed_tools=options.get("allowed_tools"),
                        disallowed_tools=options.get("disallowed_tools"),
                        output_format=options.get("output_format"),
                        max_turns=options.get("max_turns", DEFAULT_MAX_TURNS),
                        session_id=request_body.session_id if is_new else None,
                        resume=resume_id,
                        stream=False,
                    ):
                        chunks.append(chunk)

                    # Capture provider session id + store assistant response
                    # inside lock to prevent stale reads between requests
                    _capture_provider_session_id(chunks, session)

                    raw_assistant_content = backend.parse_message(chunks)
                    if raw_assistant_content:
                        assistant_message = Message(role="assistant", content=raw_assistant_content)
                        session_manager.add_assistant_response(
                            request_body.session_id, assistant_message
                        )
            else:
                prompt, run_kwargs = _prepare_stateless_completion(request_body.messages, options)
                # Materialize images in prompt if present
                if _request_has_images(request_body) and hasattr(backend, "image_handler"):
                    for msg in request_body.messages:
                        if msg.role == "user" and isinstance(msg.content, list):
                            prompt = MessageAdapter.extract_images_to_prompt(
                                msg.content, backend.image_handler
                            )
                            run_kwargs["prompt"] = prompt
                            break

                logger.info(
                    f"Chat completion: session_id=None, total_messages={len(request_body.messages)}"
                )

                chunks = []
                async for chunk in backend.run_completion(**run_kwargs, stream=False):
                    chunks.append(chunk)

                raw_assistant_content = backend.parse_message(chunks)

            # Extract assistant message
            if not raw_assistant_content:
                raise HTTPException(
                    status_code=500,
                    detail=f"No response from {resolved.backend} backend",
                )

            assistant_content = raw_assistant_content

            # Token usage (prefer real SDK values)
            sdk_usage = streaming_utils.extract_sdk_usage(chunks)
            if sdk_usage:
                prompt_tokens = sdk_usage["prompt_tokens"]
                completion_tokens = sdk_usage["completion_tokens"]
            else:
                prompt_tokens = MessageAdapter.estimate_tokens(prompt)
                completion_tokens = MessageAdapter.estimate_tokens(assistant_content)

            # Extract stop_reason from SDK messages and map to OpenAI finish_reason
            sdk_stop_reason = extract_stop_reason(chunks)
            finish_reason = map_stop_reason(sdk_stop_reason)

            response = ChatCompletionResponse(
                id=request_id,
                model=request_body.model,
                choices=[
                    Choice(
                        index=0,
                        message=Message(role="assistant", content=assistant_content),
                        finish_reason=finish_reason,
                    )
                ],
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                ),
            )

            return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat completion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/messages")
@rate_limit_endpoint("chat")
async def anthropic_messages(
    request_body: AnthropicMessagesRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Anthropic Messages API compatible endpoint (Claude-only).

    This endpoint provides compatibility with the native Anthropic SDK,
    allowing tools like VC to use this wrapper via the VC_API_BASE setting.
    Codex models are not supported on this endpoint.
    """
    await verify_api_key(request, credentials)

    # Claude-only guard: reject Codex models on Anthropic endpoint
    resolved = resolve_model(request_body.model)
    if resolved.backend != "claude":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{request_body.model}' resolves to the {resolved.backend} backend. "
                f"The /v1/messages endpoint only supports Claude models. "
                f"Use /v1/chat/completions for {resolved.backend} models."
            ),
        )

    # Validate Claude authentication
    _validate_backend_auth("claude")

    claude_backend = BackendRegistry.get("claude")
    _validate_image_request(request_body, claude_backend)

    try:
        logger.info(f"Anthropic Messages API request: model={request_body.model}")

        # Convert Anthropic messages to internal format
        messages = request_body.to_openai_messages()

        # Build prompt from messages
        prompt_parts = []
        for msg in messages:
            if msg.role == "user":
                if isinstance(msg.content, list) and hasattr(claude_backend, "image_handler"):
                    prompt_parts.append(
                        MessageAdapter.extract_images_to_prompt(
                            msg.content, claude_backend.image_handler
                        )
                    )
                else:
                    prompt_parts.append(MessageAdapter._content_to_text(msg.content))
            elif msg.role == "assistant":
                prompt_parts.append(f"Assistant: {MessageAdapter._content_to_text(msg.content)}")

        prompt = "\n\n".join(prompt_parts)
        system_prompt = request_body.system

        # Filter content
        prompt = MessageAdapter.filter_content(prompt)
        if system_prompt:
            system_prompt = MessageAdapter.filter_content(system_prompt)

        # Run Claude Code - tools enabled by default for Anthropic SDK clients
        # (they're typically using this for agentic workflows)
        mcp_servers = get_mcp_servers() or None
        allowed_tools = list(DEFAULT_ALLOWED_TOOLS)
        if mcp_servers:
            allowed_tools.extend(get_mcp_tool_patterns(mcp_servers))
        chunks = []
        async for chunk in claude_backend.run_completion(
            prompt=prompt,
            system_prompt=system_prompt,
            model=request_body.model,
            max_turns=DEFAULT_MAX_TURNS,
            allowed_tools=allowed_tools,
            permission_mode=PERMISSION_MODE_BYPASS,
            stream=False,
            mcp_servers=mcp_servers,
        ):
            chunks.append(chunk)

        # Extract assistant message
        raw_assistant_content = claude_backend.parse_message(chunks)

        if not raw_assistant_content:
            raise HTTPException(status_code=500, detail="No response from Claude Code")

        assistant_content = raw_assistant_content

        # Token usage (prefer real SDK values)
        sdk_usage = streaming_utils.extract_sdk_usage(chunks)
        if sdk_usage:
            prompt_tokens = sdk_usage["prompt_tokens"]
            completion_tokens = sdk_usage["completion_tokens"]
        else:
            prompt_tokens = MessageAdapter.estimate_tokens(prompt)
            completion_tokens = MessageAdapter.estimate_tokens(assistant_content)

        # Extract stop_reason from SDK messages (use as-is for Anthropic format)
        sdk_stop_reason = extract_stop_reason(chunks)

        # Create Anthropic-format response
        response = AnthropicMessagesResponse(
            model=request_body.model,
            content=[AnthropicTextBlock(text=assistant_content)],
            stop_reason=sdk_stop_reason or "end_turn",
            usage=AnthropicUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            ),
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Anthropic Messages API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/models")
async def list_models(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """List available models from all registered backends."""
    await verify_api_key(request, credentials)

    return {
        "object": "list",
        "data": BackendRegistry.available_models(),
    }


@app.get("/v1/mcp/servers")
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


@app.post("/v1/compatibility")
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


@app.get("/health")
@rate_limit_endpoint("health")
async def health_check(request: Request):
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "claude-code-openai-wrapper",
        "backends": list(BackendRegistry.all_backends().keys()),
    }


@app.get("/version")
@rate_limit_endpoint("health")
async def version_info(request: Request):
    """Version information endpoint."""
    from src import __version__

    return {
        "version": __version__,
        "service": "claude-code-openai-wrapper",
        "api_version": "v1",
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    """Landing page with API documentation."""
    from src import __version__

    auth_info = get_claude_code_auth_info()
    return HTMLResponse(content=build_root_page(__version__, auth_info, DEFAULT_PORT))


@app.post("/v1/debug/request")
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


@app.get("/v1/auth/status")
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
                else ("runtime" if runtime_api_key else "none")
            ),
            "registered_backends": registered_backends,
            "codex_available": BackendRegistry.is_registered("codex"),
            "version": "1.0.0",
        },
    }


# ==================== Responses API ====================


def _generate_msg_id() -> str:
    """Generate an output item ID: msg_<hex>."""
    return f"msg_{secrets.token_hex(12)}"


def _make_response_id(session_id: str, turn: int) -> str:
    """Generate a response ID encoding the session and turn: resp_{uuid}_{turn}."""
    return f"resp_{session_id}_{turn}"


def _parse_response_id(resp_id: str):
    """Parse resp_{uuid}_{turn} → (session_id, turn) or None."""
    parts = resp_id.split("_", 2)
    if len(parts) != 3 or parts[0] != "resp":
        return None
    try:
        turn = int(parts[2])
    except ValueError:
        return None
    if turn <= 0:
        return None
    try:
        uuid.UUID(parts[1])
    except ValueError:
        return None
    return parts[1], turn


async def _responses_streaming_preflight(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    backend: "BackendClient",
    session,
    session_id: str,
    is_new_session: bool,
    prompt: str,
    system_prompt: Optional[str],
) -> Dict[str, Any]:
    """Run session guards BEFORE StreamingResponse is created for /v1/responses.

    Acquires ``session.lock`` and validates stale-ID, backend mismatch, and
    Codex resume guard inside the lock.  On validation failure the lock is
    released and an HTTPException is raised (proper HTTP status).

    Returns a dict consumed by the streaming generator.  The generator's
    ``finally`` block is responsible for releasing the lock.
    """
    await session.lock.acquire()

    try:
        # --- Validation inside lock (TOCTOU-safe) ---

        if not is_new_session:
            # _parse_response_id already extracted ``turn``; re-parse here
            parsed = _parse_response_id(body.previous_response_id)
            _, turn = parsed  # guaranteed valid at this point

            if turn != session.turn_counter:
                if turn < session.turn_counter:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Stale previous_response_id: only the latest response "
                            f"(resp_{session_id}_{session.turn_counter}) can be continued"
                        ),
                    )
                else:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"previous_response_id '{body.previous_response_id}' "
                            f"references a future turn"
                        ),
                    )

            # Backend mismatch guard
            if session.backend and session.backend != resolved.backend:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Session belongs to backend '{session.backend}', "
                        f"but model '{body.model}' resolves to '{resolved.backend}'. "
                        f"Cannot mix backends within a session."
                    ),
                )

            # Codex resume guard
            if resolved.backend == "codex" and not session.provider_session_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Cannot resume Codex session: no thread_id from previous turn. "
                        "Start a new session instead."
                    ),
                )

        # Compute resume_id and next_turn
        resume_id = session.provider_session_id or session_id if not is_new_session else None
        next_turn = session.turn_counter + 1

        # Tag backend on first turn
        if is_new_session:
            session.backend = resolved.backend

    except Exception:
        session.lock.release()
        raise

    return {
        "session": session,
        "lock_acquired": True,
        "next_turn": next_turn,
        "resume_id": resume_id,
        "chunk_kwargs": dict(
            prompt=prompt,
            model=resolved.provider_model,
            system_prompt=system_prompt if is_new_session else None,
            permission_mode=PERMISSION_MODE_BYPASS,
            mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
            allowed_tools=_mcp_allowed_tools() if resolved.backend == "claude" else None,
            session_id=session_id if is_new_session else None,
            resume=resume_id,
        ),
    }


@app.post("/v1/responses")
@rate_limit_endpoint("responses")
async def create_response(
    request: Request,
    body: ResponseCreateRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """OpenAI Responses API compatible endpoint with backend dispatch.

    Supports conversation chaining via previous_response_id.
    Routes to Claude or Codex backend based on the model field.
    """
    await verify_api_key(request, credentials)

    # Resolve model → backend and validate auth
    resolved, backend = _resolve_and_get_backend(body.model)
    logger.info(
        "Responses API: model=%s → backend=%s (provider_model=%s)",
        body.model,
        resolved.backend,
        resolved.provider_model,
    )
    _validate_backend_auth(resolved.backend)
    _validate_image_request(body, backend)

    # Validate: instructions + previous_response_id is not allowed
    if body.previous_response_id and body.instructions:
        raise HTTPException(
            status_code=400,
            detail="instructions cannot be used with previous_response_id. "
            "The system prompt is fixed to the original session.",
        )

    # Resolve session from previous_response_id or create new
    if body.previous_response_id:
        parsed = _parse_response_id(body.previous_response_id)
        if not parsed:
            raise HTTPException(
                status_code=404,
                detail=f"previous_response_id '{body.previous_response_id}' is invalid",
            )
        session_id, turn = parsed
        session = session_manager.get_session(session_id)
        if not session:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Session for previous_response_id "
                    f"'{body.previous_response_id}' not found or expired"
                ),
            )
        # Future turn check (outside lock — safe because turn_counter only grows)
        if turn > session.turn_counter:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"previous_response_id '{body.previous_response_id}' references a future turn"
                ),
            )
    else:
        session_id = str(uuid.uuid4())
        session = session_manager.get_or_create_session(session_id)

    # Extract system prompt from array input if present
    system_prompt = body.instructions
    input_for_prompt = body.input
    if isinstance(body.input, list) and not body.instructions:
        user_items = []
        for item in body.input:
            if item.role in ("system", "developer"):
                content = item.content
                if isinstance(content, str):
                    system_prompt = content
                elif isinstance(content, list):
                    system_prompt = "\n".join(
                        p["text"] for p in content if isinstance(p, dict) and p.get("text")
                    )
            else:
                user_items.append(item)
        input_for_prompt = user_items if user_items else body.input

    # Convert input to prompt
    image_handler = getattr(backend, "image_handler", None)
    prompt = MessageAdapter.response_input_to_prompt(input_for_prompt, image_handler=image_handler)
    prompt = MessageAdapter.filter_content(prompt)

    # Determine if this is a new session or a follow-up
    is_new_session = body.previous_response_id is None

    if body.stream:
        # Run preflight BEFORE StreamingResponse so HTTPExceptions produce
        # proper HTTP error status codes (not swallowed inside the generator).
        preflight = await _responses_streaming_preflight(
            body, resolved, backend, session, session_id, is_new_session, prompt, system_prompt
        )

        next_turn = preflight["next_turn"]
        resp_id = _make_response_id(session_id, next_turn)
        output_item_id = _generate_msg_id()

        async def _run_stream():
            lock_acquired = preflight["lock_acquired"]
            stream_result = {"success": False}
            try:
                chunks_buffer = []
                chunk_source = backend.run_completion(**preflight["chunk_kwargs"])
                try:
                    async for line in streaming_utils.stream_response_chunks(
                        chunk_source=chunk_source,
                        model=body.model,
                        response_id=resp_id,
                        output_item_id=output_item_id,
                        chunks_buffer=chunks_buffer,
                        logger=logger,
                        prompt_text=prompt,
                        metadata=body.metadata or {},
                        stream_result=stream_result,
                    ):
                        yield line
                finally:
                    # Close the SDK generator in the same task that iterated it.
                    # The SDK uses anyio cancel scopes internally; closing from a
                    # different task (e.g. Starlette response teardown) causes
                    # "Attempted to exit cancel scope in a different task".
                    await chunk_source.aclose()

                # ALWAYS capture provider_session_id (even on failure).
                # On failure, this is internal-only: no response_id is committed for the
                # client, so the captured thread_id is not externally recoverable.
                _capture_provider_session_id(chunks_buffer, session)

                # SUCCESS-ONLY: commit turn counter and session messages.
                # Use assistant_text assembled by stream_response_chunks() from
                # actual deltas sent to the client.  This avoids a parse_message()
                # mismatch that could emit an error *after* response.completed.
                if stream_result.get("success"):
                    assistant_text = stream_result.get("assistant_text") or ""
                    if assistant_text:
                        session.turn_counter = next_turn
                        session.add_messages([Message(role="user", content=prompt)])
                        session_manager.add_assistant_response(
                            session_id, Message(role="assistant", content=assistant_text)
                        )

            except Exception as e:
                logger.error("Responses API Stream: setup error: %s", e, exc_info=True)
                # Capture provider_session_id from partial chunks on exception.
                # Internal-only: no response_id is committed, so not client-recoverable.
                if chunks_buffer:
                    _capture_provider_session_id(chunks_buffer, session)
                failed_resp = ResponseObject(
                    id=resp_id,
                    model=body.model,
                    status="failed",
                    metadata=body.metadata or {},
                    error=ResponseErrorDetail(code="server_error", message="Internal server error"),
                )
                yield streaming_utils.make_response_sse(
                    "response.failed",
                    response_obj=failed_resp,
                    sequence_number=0,
                )
            finally:
                if lock_acquired:
                    session.lock.release()

        return StreamingResponse(_run_stream(), media_type="text/event-stream")

    # --- Non-streaming path ---
    try:
        async with session.lock:
            # --- Validation inside lock (TOCTOU-safe) ---
            if not is_new_session:
                parsed = _parse_response_id(body.previous_response_id)
                _, turn = parsed

                if turn != session.turn_counter:
                    if turn < session.turn_counter:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Stale previous_response_id: only the latest response "
                                f"(resp_{session_id}_{session.turn_counter}) can be continued"
                            ),
                        )
                    else:
                        raise HTTPException(
                            status_code=404,
                            detail=(
                                f"previous_response_id '{body.previous_response_id}' "
                                f"references a future turn"
                            ),
                        )

                # Backend mismatch guard
                if session.backend and session.backend != resolved.backend:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Session belongs to backend '{session.backend}', "
                            f"but model '{body.model}' resolves to '{resolved.backend}'. "
                            f"Cannot mix backends within a session."
                        ),
                    )

                # Codex resume guard
                if resolved.backend == "codex" and not session.provider_session_id:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Cannot resume Codex session: no thread_id from previous turn. "
                            "Start a new session instead."
                        ),
                    )

            # Compute resume_id and next_turn
            resume_id = session.provider_session_id or session_id if not is_new_session else None
            next_turn = session.turn_counter + 1

            # Tag backend on first turn
            if is_new_session:
                session.backend = resolved.backend

            # Execute backend
            chunks = []
            try:
                async for chunk in backend.run_completion(
                    prompt=prompt,
                    model=resolved.provider_model,
                    system_prompt=system_prompt if is_new_session else None,
                    permission_mode=PERMISSION_MODE_BYPASS,
                    mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
                    allowed_tools=_mcp_allowed_tools() if resolved.backend == "claude" else None,
                    session_id=session_id if is_new_session else None,
                    resume=resume_id,
                ):
                    chunks.append(chunk)
            finally:
                # ALWAYS capture provider_session_id (even on failure/exception).
                # On failure, this is internal-only: no response_id is committed for the
                # client, so the captured thread_id is not externally recoverable.
                if chunks:
                    _capture_provider_session_id(chunks, session)

            # Check for backend errors (run_completion wraps exceptions as error chunks)
            for chunk in chunks:
                if isinstance(chunk, dict) and chunk.get("is_error"):
                    error_msg = chunk.get("error_message", "Unknown backend error")
                    raise HTTPException(status_code=502, detail=f"Backend error: {error_msg}")

            # Extract assistant text
            assistant_text = backend.parse_message(chunks)
            if not assistant_text:
                raise HTTPException(status_code=502, detail="No response from backend")

            # Apply thinking wrapper for non-streaming responses
            if WRAP_INTERMEDIATE_THINKING and RESPONSE_SENTINEL in assistant_text:
                parts = assistant_text.split(RESPONSE_SENTINEL, 1)
                assistant_text = f"<think>\n{parts[0]}\n</think>\n{parts[1]}"

            # SUCCESS-ONLY: commit turn counter and session messages
            session.turn_counter = next_turn
            session.add_messages([Message(role="user", content=prompt)])
            session_manager.add_assistant_response(
                session_id, Message(role="assistant", content=assistant_text)
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Responses API: Backend error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Backend error: {e}")

    # Token usage (prefer real SDK values)
    sdk_usage = streaming_utils.extract_sdk_usage(chunks)
    if sdk_usage:
        prompt_tokens = sdk_usage["prompt_tokens"]
        completion_tokens = sdk_usage["completion_tokens"]
    else:
        token_usage = backend.estimate_token_usage(prompt, assistant_text, body.model)
        prompt_tokens = token_usage["prompt_tokens"]
        completion_tokens = token_usage["completion_tokens"]

    # Build response object
    resp_id = _make_response_id(session_id, session.turn_counter)

    response_obj = ResponseObject(
        id=resp_id,
        status="completed",
        model=body.model,
        output=[
            OutputItem(
                id=_generate_msg_id(),
                content=[ContentPart(text=assistant_text)],
            )
        ],
        usage=ResponseUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        ),
        metadata=body.metadata or {},
    )

    return response_obj.model_dump()


# ==================== Session Management ====================


@app.get("/v1/sessions/stats")
async def get_session_stats(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Get session manager statistics."""
    stats = session_manager.get_stats()
    return {
        "session_stats": stats,
        "cleanup_interval_minutes": session_manager.cleanup_interval_minutes,
        "default_ttl_minutes": session_manager.default_ttl_minutes,
    }


@app.get("/v1/sessions")
async def list_sessions(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """List all active sessions."""
    sessions = session_manager.list_sessions()
    return SessionListResponse(sessions=sessions, total=len(sessions))


@app.get("/v1/sessions/{session_id}")
async def get_session(
    session_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Get information about a specific session."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session.to_session_info()


@app.delete("/v1/sessions/{session_id}")
async def delete_session(
    session_id: str, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    """Delete a specific session."""
    deleted = session_manager.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"message": f"Session {session_id} deleted successfully"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Format HTTP exceptions as OpenAI-style errors."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"message": exc.detail, "type": "api_error", "code": str(exc.status_code)}
        },
    )


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


def run_server(port: int = None, host: str = None):
    """Run the server - used as script entry point."""
    import uvicorn

    # Handle interactive API key protection
    global runtime_api_key
    runtime_api_key = prompt_for_api_protection()

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
    port = None
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
            print(f"Using port from command line: {port}")
        except ValueError:
            print(f"Invalid port number: {sys.argv[1]}. Using default.")

    run_server(port)
