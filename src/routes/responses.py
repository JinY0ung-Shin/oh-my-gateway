"""Responses API endpoint (/v1/responses)."""

import logging
import secrets
import uuid
from pathlib import Path
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
from src.workspace_manager import workspace_manager
from src.image_handler import ImageHandler
from src.routes.deps import (
    resolve_and_get_backend,
    validate_backend_auth_or_raise,
    validate_image_request,
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
    workspace_str: str = "",
) -> Dict[str, Any]:
    """Run session guards BEFORE StreamingResponse is created for /v1/responses.

    Acquires ``session.lock`` and validates stale-ID and backend mismatch
    inside the lock.  On validation failure the lock is released and an
    HTTPException is raised (proper HTTP status).

    Returns a dict consumed by the streaming generator.  The generator's
    ``finally`` block is responsible for releasing the lock.
    """
    from src.session_guard import acquire_session_preflight

    # Pre-parse turn for validation inside the lock
    turn: Optional[int] = None
    if not is_new_session:
        assert body.previous_response_id is not None
        parsed = _parse_response_id(body.previous_response_id)
        _, turn = parsed  # guaranteed valid at this point

    pf = await acquire_session_preflight(
        session,
        resolved,
        session_id,
        is_new=is_new_session,
        turn=turn,
    )

    return {
        "session": pf.session,
        "lock_acquired": pf.lock_acquired,
        "next_turn": pf.next_turn,
        "resume_id": pf.resume_id,
        "chunk_kwargs": dict(
            prompt=prompt,
            model=resolved.provider_model,
            system_prompt=system_prompt if pf.is_new else None,
            _custom_base=session.base_system_prompt,
            _metadata=body.metadata,
            permission_mode=PERMISSION_MODE_BYPASS,
            mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
            session_id=session_id if pf.is_new else None,
            resume=pf.resume_id,
            cwd=workspace_str,
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
    Routes to the appropriate backend based on the model field.
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

    # Moved earlier — needed for workspace sync_template decision
    is_new_session = body.previous_response_id is None

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

    # --- Per-user workspace isolation ---
    if not is_new_session and session.user != body.user:
        raise HTTPException(
            status_code=400,
            detail=f"User mismatch: session belongs to {session.user!r}, "
            f"but request specifies {body.user!r}",
        )

    if is_new_session:
        try:
            workspace = workspace_manager.resolve(body.user, sync_template=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        session.user = body.user
        session.workspace = str(workspace)
    else:
        # Follow-up: reuse stored workspace, no template sync
        if session.workspace:
            workspace = Path(session.workspace)
        else:
            try:
                workspace = workspace_manager.resolve(body.user, sync_template=False)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            session.workspace = str(workspace)
    workspace_str = str(workspace)

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
    # Per-request ImageHandler pointing to user workspace
    image_handler = ImageHandler(workspace)
    prompt = MessageAdapter.response_input_to_prompt(input_for_prompt, image_handler=image_handler)
    prompt = MessageAdapter.filter_content(prompt)

    if body.stream:
        # Run preflight BEFORE StreamingResponse so HTTPExceptions produce
        # proper HTTP error status codes (not swallowed inside the generator).
        preflight = await _responses_streaming_preflight(
            body,
            resolved,
            backend,
            session,
            session_id,
            is_new_session,
            prompt,
            system_prompt,
            workspace_str=workspace_str,
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

                # Bridge SDK iteration through a background task to keep
                # anyio cancel scopes task-local.
                sse_source = streaming_utils.stream_response_chunks(
                    chunk_source=chunk_source,
                    model=body.model,
                    response_id=resp_id,
                    output_item_id=output_item_id,
                    chunks_buffer=chunks_buffer,
                    logger=logger,
                    prompt_text=prompt,
                    metadata=body.metadata or {},
                    stream_result=stream_result,
                )
                async for line in streaming_utils.bridge_sse_stream(sse_source, chunk_source):
                    yield line

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
        from src.session_guard import session_preflight_scope

        # Pre-parse turn for validation inside the lock
        _turn: Optional[int] = None
        if not is_new_session:
            assert body.previous_response_id is not None
            _parsed = _parse_response_id(body.previous_response_id)
            _, _turn = _parsed

        async with session_preflight_scope(
            session,
            resolved,
            session_id,
            is_new=is_new_session,
            turn=_turn,
        ) as pf:
            # Execute backend
            chunks = []
            async for chunk in backend.run_completion(
                prompt=prompt,
                model=resolved.provider_model,
                system_prompt=system_prompt if pf.is_new else None,
                _custom_base=session.base_system_prompt,
                _metadata=body.metadata,
                permission_mode=PERMISSION_MODE_BYPASS,
                mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
                session_id=session_id if pf.is_new else None,
                resume=pf.resume_id,
                cwd=workspace_str,
            ):
                chunks.append(chunk)

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
            session.turn_counter = pf.next_turn
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
    prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
        chunks, prompt, assistant_text, body.model, backend=backend
    )

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
