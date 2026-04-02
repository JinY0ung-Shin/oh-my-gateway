"""Chat completions endpoint (/v1/chat/completions)."""

import json
import logging
import os
from typing import Optional, AsyncGenerator, Dict, Any

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse

from src.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Message,
    Usage,
)
from src.message_adapter import MessageAdapter
from src.auth import verify_api_key, security
from src.parameter_validator import ParameterValidator, CompatibilityReporter
from src.session_manager import session_manager
from src.backends import BackendConfigError, BackendRegistry, ResolvedModel
from src.rate_limiter import rate_limit_endpoint
from src.runtime_config import get_default_max_turns
from src import streaming_utils
from src.routes.deps import (
    resolve_and_get_backend as _resolve_and_get_backend,
    validate_backend_auth_or_raise as _validate_backend_auth,
    validate_image_request as _validate_image_request,
    request_has_images as _request_has_images,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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


def _prepare_stateless_completion(
    messages: list,
    claude_options: Dict[str, Any],
    metadata: Optional[Dict[str, str]] = None,
) -> tuple:
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
        max_turns=claude_options.get("max_turns", get_default_max_turns()),
        allowed_tools=claude_options.get("allowed_tools"),
        disallowed_tools=claude_options.get("disallowed_tools"),
        permission_mode=claude_options.get("permission_mode"),
        output_format=claude_options.get("output_format"),
        mcp_servers=claude_options.get("mcp_servers"),
        _metadata=metadata,
    )
    return prompt, run_kwargs


def _prepare_session_prompt(
    request: ChatCompletionRequest,
    backend=None,
) -> tuple:
    """Prepare prompt, system_prompt, and session for session-mode requests.

    This function does NOT mutate session state and does NOT compute
    ``is_new``.  Lock acquisition, ``is_new`` checks, and message
    commits are handled by :func:`~src.session_guard.acquire_session_preflight`
    which callers invoke after this function.

    Returns:
        (prompt, system_prompt, session) tuple
    """
    assert request.session_id is not None  # caller guarantees session_id is set
    session = session_manager.get_or_create_session(request.session_id)

    last_user_msg: Any = None
    for msg in reversed(request.messages):
        if msg.role == "user":
            if isinstance(msg.content, list) and backend and hasattr(backend, "image_handler"):
                last_user_msg = MessageAdapter.extract_images_to_prompt(
                    msg.content, backend.image_handler
                )
            else:
                last_user_msg = msg.content
            break
    prompt: Any = last_user_msg or MessageAdapter.messages_to_prompt(request.messages)[0]

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
                    if hasattr(p, "type") and p.type == "text" and hasattr(p, "text") and p.text
                )
            break
    if system_prompt:
        system_prompt = MessageAdapter.filter_content(system_prompt)

    return prompt, system_prompt, session


async def _streaming_session_preflight(
    request: ChatCompletionRequest,
    resolved,
    backend,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    """Run session guards and mutation BEFORE StreamingResponse is created.

    This ensures HTTPException (400 backend mismatch) is raised while the
    endpoint can still return a proper HTTP error status, rather than inside
    the async generator where Starlette has already committed the 200 status
    line.

    Returns a dict with keys needed by the streaming generator:
        session, lock_acquired, prompt, sys_prompt, is_new, resume_id, chunk_kwargs
    """
    from src.session_guard import acquire_session_preflight

    prompt, sys_prompt, session = _prepare_session_prompt(request, backend=backend)

    assert request.session_id is not None
    pf = await acquire_session_preflight(
        session, resolved, request.session_id, messages=request.messages
    )

    return {
        "session": pf.session,
        "lock_acquired": pf.lock_acquired,
        "prompt": prompt,
        "sys_prompt": sys_prompt,
        "is_new": pf.is_new,
        "resume_id": pf.resume_id,
        "chunk_kwargs": dict(
            prompt=prompt,
            model=options.get("model"),
            system_prompt=sys_prompt if pf.is_new else None,
            _custom_base=session.base_system_prompt,
            _metadata=request.metadata,
            permission_mode=options.get("permission_mode"),
            mcp_servers=options.get("mcp_servers"),
            allowed_tools=options.get("allowed_tools"),
            disallowed_tools=options.get("disallowed_tools"),
            output_format=options.get("output_format"),
            max_turns=options.get("max_turns", get_default_max_turns()),
            session_id=request.session_id if pf.is_new else None,
            resume=pf.resume_id,
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

            from src.session_guard import acquire_session_preflight

            assert request.session_id is not None
            pf = await acquire_session_preflight(
                session, resolved, request.session_id, messages=request.messages
            )
            lock_acquired = pf.lock_acquired

            chunk_source = backend.run_completion(
                prompt=prompt,
                model=options.get("model"),
                system_prompt=sys_prompt if pf.is_new else None,
                _custom_base=session.base_system_prompt,
                _metadata=request.metadata,
                permission_mode=options.get("permission_mode"),
                mcp_servers=options.get("mcp_servers"),
                allowed_tools=options.get("allowed_tools"),
                disallowed_tools=options.get("disallowed_tools"),
                output_format=options.get("output_format"),
                max_turns=options.get("max_turns", get_default_max_turns()),
                session_id=request.session_id if pf.is_new else None,
                resume=pf.resume_id,
                stream=True,
            )
        else:
            # Stateless mode
            _validate_backend_auth(resolved.backend)
            options = _build_backend_options(request, resolved, claude_headers)
            prompt, run_kwargs = _prepare_stateless_completion(
                request.messages, options, metadata=request.metadata
            )
            chunk_source = backend.run_completion(**run_kwargs, stream=True)

        # Bridge SDK iteration through a background task to keep anyio
        # cancel scopes task-local (Starlette may close the generator
        # from a different ASGI task during teardown).
        chunks_buffer = []

        sse_source = streaming_utils.stream_chunks(
            chunk_source=chunk_source,
            request=request,
            request_id=request_id,
            chunks_buffer=chunks_buffer,
            logger=logger,
        )
        async for sse_line in streaming_utils.bridge_sse_stream(sse_source, chunk_source):
            yield sse_line

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
            completion_text = assistant_content or ""
            p_tok, c_tok = streaming_utils.resolve_token_usage(
                chunks_buffer, prompt, completion_text, request.model, backend=backend
            )
            usage_data = Usage(
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=p_tok + c_tok,
            )
            logger.debug(f"Usage: {usage_data}")

        # Extract stop_reason from SDK messages and map to OpenAI finish_reason
        sdk_stop_reason = streaming_utils.extract_stop_reason(chunks_buffer)
        finish_reason = streaming_utils.map_stop_reason(sdk_stop_reason)

        # Send final chunk with finish reason and optionally usage data
        yield streaming_utils.make_sse(
            request_id, request.model, {}, finish_reason=finish_reason, usage=usage_data
        )
        yield "data: [DONE]\n\n"

    except HTTPException:
        raise  # Let FastAPI handle these
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        error_chunk = {"error": {"message": str(e), "type": "streaming_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"
    finally:
        # Release per-session lock only if THIS coroutine acquired it
        if lock_acquired:
            assert session is not None  # lock_acquired implies session is set
            session.lock.release()


@router.post("/v1/chat/completions")
@rate_limit_endpoint("chat")
async def chat_completions(
    request_body: ChatCompletionRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """OpenAI-compatible chat completions endpoint with backend dispatch."""
    await verify_api_key(request, credentials)

    try:
        # Resolve model -> backend and validate auth
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
            # HTTPExceptions (400 backend mismatch) surface
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
                from src.session_guard import session_preflight_scope

                prompt, sys_prompt, session = _prepare_session_prompt(request_body, backend=backend)

                async with session_preflight_scope(
                    session, resolved, request_body.session_id,
                    messages=request_body.messages,
                ) as pf:
                    chunks = []
                    async for chunk in backend.run_completion(
                        prompt=prompt,
                        model=options.get("model"),
                        system_prompt=sys_prompt if pf.is_new else None,
                        _custom_base=session.base_system_prompt,
                        _metadata=request_body.metadata,
                        permission_mode=options.get("permission_mode"),
                        mcp_servers=options.get("mcp_servers"),
                        allowed_tools=options.get("allowed_tools"),
                        disallowed_tools=options.get("disallowed_tools"),
                        output_format=options.get("output_format"),
                        max_turns=options.get("max_turns", get_default_max_turns()),
                        session_id=request_body.session_id if pf.is_new else None,
                        resume=pf.resume_id,
                        stream=False,
                    ):
                        chunks.append(chunk)

                    raw_assistant_content = backend.parse_message(chunks)
                    if raw_assistant_content:
                        assistant_message = Message(role="assistant", content=raw_assistant_content)
                        session_manager.add_assistant_response(
                            request_body.session_id, assistant_message
                        )
            else:
                prompt, run_kwargs = _prepare_stateless_completion(
                    request_body.messages, options, metadata=request_body.metadata
                )
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
            prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
                chunks, prompt, assistant_content
            )

            # Extract stop_reason from SDK messages and map to OpenAI finish_reason
            sdk_stop_reason = streaming_utils.extract_stop_reason(chunks)
            finish_reason = streaming_utils.map_stop_reason(sdk_stop_reason)

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
