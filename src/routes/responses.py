"""Responses API endpoint (/v1/responses)."""

import asyncio
import logging
import secrets
import uuid
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse

from src.models import Message
from src.message_adapter import MessageAdapter
from src.auth import verify_api_key, security
from src.session_manager import session_manager
from src.backends import BackendClient, ResolvedModel
from src.response_models import (
    ResponseCreateRequest,
    ResponseContentPart,
    ResponseErrorDetail,
    ResponseObject,
    OutputItem,
    ResponseUsage,
)
from src.rate_limiter import rate_limit_endpoint
from src.constants import PERMISSION_MODE_BYPASS
from src.mcp_config import get_mcp_servers
from src import streaming_utils
from src.routes.deps import (
    resolve_and_get_backend,
    validate_backend_auth_or_raise,
    validate_image_request,
    capture_provider_session_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _generate_msg_id() -> str:
    """Generate an output item ID: msg_<hex>."""
    return f"msg_{secrets.token_hex(12)}"


def _make_response_id(session_id: str, turn: int) -> str:
    """Generate a response ID encoding the session and turn: resp_{uuid}_{turn}."""
    return f"resp_{session_id}_{turn}"


def _parse_response_id(resp_id: str):
    """Parse resp_{uuid}_{turn} -> (session_id, turn) or None."""
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

        # Tag backend on first turn and snapshot base system prompt
        if is_new_session:
            session.backend = resolved.backend
            from src.system_prompt import get_system_prompt

            session.base_system_prompt = get_system_prompt()  # None = preset mode

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
            _custom_base=session.base_system_prompt,
            _metadata=body.metadata,
            permission_mode=PERMISSION_MODE_BYPASS,
            mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
            session_id=session_id if is_new_session else None,
            resume=resume_id,
        ),
    }


@router.post("/v1/responses")
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

    # Resolve model -> backend and validate auth
    resolved, backend = resolve_and_get_backend(body.model)
    logger.info(
        "Responses API: model=%s -> backend=%s (provider_model=%s)",
        body.model,
        resolved.backend,
        resolved.provider_model,
    )
    validate_backend_auth_or_raise(resolved.backend)
    validate_image_request(body, backend)

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
        # Future turn check (outside lock -- safe because turn_counter only grows)
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

                # Run SDK iteration in a dedicated task to keep anyio cancel
                # scopes task-local.  Starlette may close the response generator
                # from a different ASGI task during teardown; bridging with a
                # queue prevents cancel-scope crossing.
                _SENTINEL = object()
                sse_queue: asyncio.Queue = asyncio.Queue()

                async def _sdk_reader():
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
                            await sse_queue.put(("sse", line))
                    except Exception as exc:
                        await sse_queue.put(("error", exc))
                    finally:
                        try:
                            await chunk_source.aclose()
                        except Exception:
                            pass  # generator already running/closed or subprocess dead
                        await sse_queue.put(("done", _SENTINEL))

                reader_task = asyncio.create_task(_sdk_reader())
                try:
                    while True:
                        msg = await sse_queue.get()
                        if msg[0] == "done":
                            break
                        if msg[0] == "error":
                            raise msg[1]
                        yield msg[1]
                finally:
                    reader_task.cancel()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, RuntimeError):
                        pass

                # ALWAYS capture provider_session_id (even on failure).
                capture_provider_session_id(chunks_buffer, session)

                # SUCCESS-ONLY: commit turn counter and session messages.
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
                if chunks_buffer:
                    capture_provider_session_id(chunks_buffer, session)
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

            # Tag backend on first turn and snapshot base system prompt
            if is_new_session:
                session.backend = resolved.backend
                from src.system_prompt import get_system_prompt

                session.base_system_prompt = get_system_prompt()  # None = preset mode

            # Execute backend
            chunks = []
            try:
                async for chunk in backend.run_completion(
                    prompt=prompt,
                    model=resolved.provider_model,
                    system_prompt=system_prompt if is_new_session else None,
                    _custom_base=session.base_system_prompt,
                    _metadata=body.metadata,
                    permission_mode=PERMISSION_MODE_BYPASS,
                    mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
                    session_id=session_id if is_new_session else None,
                    resume=resume_id,
                ):
                    chunks.append(chunk)
            finally:
                # ALWAYS capture provider_session_id (even on failure/exception).
                # On failure, this is internal-only: no response_id is committed for the
                # client, so the captured thread_id is not externally recoverable.
                if chunks:
                    capture_provider_session_id(chunks, session)

            # Check for backend errors (run_completion wraps exceptions as error chunks)
            for chunk in chunks:
                if isinstance(chunk, dict) and chunk.get("is_error"):
                    error_msg = chunk.get("error_message", "Unknown backend error")
                    raise HTTPException(status_code=502, detail=f"Backend error: {error_msg}")

            # Extract assistant text
            assistant_text = backend.parse_message(chunks)
            if not assistant_text:
                raise HTTPException(status_code=502, detail="No response from backend")

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
                content=[ResponseContentPart(text=assistant_text)],
            )
        ],
        usage=ResponseUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        ),
        metadata=body.metadata or {},
    )

    return response_obj.model_dump()
