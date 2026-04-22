"""Responses API endpoint (/v1/responses)."""

import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
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
from src.backends.claude.slash_commands import (
    SlashCommandError,
    validate_prompt as validate_slash_prompt,
)
from src.response_models import (
    ResponseCreateRequest,
    ResponseContentPart,
    ResponseErrorDetail,
    FunctionCallOutputItem,
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


def _detect_function_call_output(input_data) -> Optional[Dict[str, str]]:
    """Extract function_call_output from input if present.

    Scans the input array for a ``function_call_output`` item and returns
    its ``call_id`` and ``output`` values.  Returns ``None`` when no such
    item is found (e.g. when the input is a plain string or only contains
    regular message items).
    """
    if isinstance(input_data, str):
        return None
    for item in input_data:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            return {"call_id": item["call_id"], "output": item["output"]}
        if hasattr(item, "type") and getattr(item, "type", None) == "function_call_output":
            return {"call_id": item.call_id, "output": item.output}
    return None


def _build_requires_action_response(
    resp_id: str,
    model: str,
    tc: dict,
    metadata: Optional[dict],
) -> ResponseObject:
    """Construct the `requires_action` ResponseObject for an AskUserQuestion pause."""
    return ResponseObject(
        id=resp_id,
        model=model,
        status="requires_action",
        output=[
            FunctionCallOutputItem(
                id=f"fc_{tc['call_id']}",
                call_id=tc["call_id"],
                name=tc["name"],
                arguments=json.dumps(tc.get("arguments", {})),
            )
        ],
        metadata=metadata or {},
    )


def _build_failed_response(
    resp_id: str,
    model: str,
    metadata: Optional[dict],
    *,
    code: str = "server_error",
    message: str = "Internal server error",
) -> ResponseObject:
    """Construct a failed ResponseObject for stream error fallback."""
    return ResponseObject(
        id=resp_id,
        model=model,
        status="failed",
        metadata=metadata or {},
        error=ResponseErrorDetail(code=code, message=message),
    )


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
        workspace=workspace_str,
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
            _user=body.user,
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

    # ------------------------------------------------------------------
    # Detect function_call_output BEFORE converting input to prompt.
    # If present, this is a tool-continuation turn: the client is sending
    # the user's response to an AskUserQuestion function_call.
    # ------------------------------------------------------------------
    fc_output = _detect_function_call_output(body.input)
    if fc_output is not None:
        return await _handle_function_call_output(
            body, resolved, backend, session, session_id, workspace_str, fc_output
        )

    # Extract system prompt from array input if present
    system_prompt = body.instructions
    input_for_prompt = body.input
    if isinstance(body.input, list) and not body.instructions:
        user_items = []
        for item in body.input:
            # FunctionCallOutputInput items don't have role; skip them here
            if not hasattr(item, "role"):
                continue
            if item.role in ("system", "developer"):
                content = item.content
                if isinstance(content, str):
                    system_prompt = content
                elif isinstance(content, list):
                    system_prompt = "\n".join(
                        p.get("text") if isinstance(p, dict) else getattr(p, "text", "")
                        for p in content
                        if (p.get("text") if isinstance(p, dict) else getattr(p, "text", ""))
                    )
            else:
                user_items.append(item)
        input_for_prompt = user_items if user_items else body.input

    # Convert input to prompt
    # Per-request ImageHandler pointing to user workspace
    image_handler = ImageHandler(workspace)
    try:
        prompt = MessageAdapter.response_input_to_prompt(
            input_for_prompt, image_handler=image_handler
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    prompt = MessageAdapter.filter_content(prompt)

    # Reject slash-prefixed prompts that would be intercepted by the SDK as
    # unknown skills or run destructive built-ins.  Only applies to the Claude
    # backend; other backends pass through unchanged.
    if resolved.backend == "claude":
        try:
            await validate_slash_prompt(
                prompt,
                cwd=Path(workspace_str) if workspace_str else None,
            )
        except SlashCommandError as e:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request_error",
                        "code": e.code,
                        "message": e.message,
                    }
                },
            ) from e

    # ------------------------------------------------------------------
    # Determine whether to use ClaudeSDKClient (persistent session) or
    # the single-use query() path (run_completion).
    # ------------------------------------------------------------------
    # Create a persistent ClaudeSDKClient if the backend supports it.
    # This enables PreToolUse hook registration for AskUserQuestion
    # interception on any turn, including the first.
    # bypassPermissions avoids workspace-level settings.local.json checks
    # (temp workspaces lack shell command allow-lists).  PreToolUse hooks
    # still fire independently of the permission mode.
    if session.client is None and hasattr(backend, "create_client"):
        try:
            session.client = await backend.create_client(
                session=session,
                model=resolved.provider_model,
                system_prompt=system_prompt if len(session.messages) == 0 else None,
                permission_mode=PERMISSION_MODE_BYPASS,
                allowed_tools=body.allowed_tools,
                mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
                cwd=workspace_str,
                extra_env=body.metadata,
            )
        except Exception:
            logger.warning(
                "Failed to create persistent client, falling back to query()", exc_info=True
            )
            session.client = None

    use_sdk_client = session.client is not None and hasattr(backend, "run_completion_with_client")

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
            stream_result: dict = {"success": False}
            # When using SDK client, the PreToolUse hook may break the
            # stream with no text content.  Suppress the empty_response
            # failed event so we can emit function_call + requires_action.
            if use_sdk_client:
                stream_result["allow_empty"] = True
            try:
                chunks_buffer = []

                # Choose chunk source: persistent client or single-use query
                if use_sdk_client:
                    chunk_source = backend.run_completion_with_client(
                        session.client, prompt, session
                    )
                else:
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
                    request_context={
                        "session_id": session_id,
                        "user": body.user,
                        "workdir": workspace_str,
                        "backend": resolved.backend,
                        "provider_model": resolved.provider_model,
                        "previous_response_id": body.previous_response_id,
                        "turn": next_turn,
                        "use_sdk_client": use_sdk_client,
                    },
                )
                async for line in streaming_utils.bridge_sse_stream(sse_source, chunk_source):
                    yield line

                # Check if the SDK paused on AskUserQuestion (pending_tool_call).
                # If so, emit function_call SSE and complete with requires_action.
                if session.pending_tool_call is not None:
                    tc = session.pending_tool_call
                    yield streaming_utils.make_function_call_response_sse(
                        response_id=resp_id,
                        call_id=tc["call_id"],
                        name=tc["name"],
                        arguments=json.dumps(tc.get("arguments", {})),
                    )
                    # Emit response.completed with requires_action status
                    requires_action_resp = _build_requires_action_response(
                        resp_id, body.model, tc, body.metadata
                    )
                    yield streaming_utils.make_response_sse(
                        "response.completed",
                        response_obj=requires_action_resp,
                        sequence_number=0,
                    )
                    # Commit turn even for requires_action so the next
                    # function_call_output can reference this response_id
                    session.turn_counter = next_turn
                    stream_result["success"] = True
                elif stream_result.get("success"):
                    # SUCCESS-ONLY: commit turn counter and session messages.
                    assistant_text = stream_result.get("assistant_text") or ""
                    if assistant_text:
                        session.turn_counter = next_turn
                        session.add_messages([Message(role="user", content=prompt)])
                        session_manager.add_assistant_response(
                            session_id, Message(role="assistant", content=assistant_text)
                        )

            except Exception as e:
                logger.error("Responses API Stream: setup error: %s", e, exc_info=True)
                failed_resp = _build_failed_response(resp_id, body.model, body.metadata)
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
            workspace=workspace_str,
        ) as pf:
            # Execute backend — persistent client or single-use query
            chunks = []
            if use_sdk_client:
                async for chunk in backend.run_completion_with_client(
                    session.client, prompt, session
                ):
                    chunks.append(chunk)
            else:
                async for chunk in backend.run_completion(
                    prompt=prompt,
                    model=resolved.provider_model,
                    system_prompt=system_prompt if pf.is_new else None,
                    _custom_base=session.base_system_prompt,
                    _metadata=body.metadata,
                    _user=body.user,
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
                    # ``error_message`` here is SDK-curated (rate-limit, auth,
                    # etc.) — not a raw Python exception string — so it is
                    # safe to surface to clients. Raw ``except Exception``
                    # leaks are redacted at the catch-all below.
                    error_msg = chunk.get("error_message", "Unknown backend error")
                    raise HTTPException(status_code=502, detail=f"Backend error: {error_msg}")

            # Check if the SDK paused on AskUserQuestion
            if session.pending_tool_call is not None:
                tc = session.pending_tool_call
                resp_id = _make_response_id(session_id, pf.next_turn)
                session.turn_counter = pf.next_turn
                return _build_requires_action_response(
                    resp_id, body.model, tc, body.metadata
                ).model_dump()

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
        # Do not echo raw exception strings to clients — they can contain
        # file paths, subprocess commands, or other backend internals.
        # Full details go to logs for operators; response stays generic.
        logger.error("Responses API: Backend error: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail="Backend error") from e

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


async def _handle_function_call_output(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    backend: "BackendClient",
    session,
    session_id: str,
    workspace_str: str,
    fc_output: Dict[str, str],
):
    """Handle a function_call_output continuation request.

    Validates that the session has a pending tool call with a matching
    ``call_id``, unblocks the SDK's PreToolUse hook, and then streams
    the continuation response from the existing :class:`ClaudeSDKClient`.

    The validation and event-unblock are performed atomically under the
    session lock to prevent races where concurrent requests could read
    stale ``pending_tool_call`` / ``input_event`` state.
    """
    # --- Validate + unblock under session lock ---
    await session.lock.acquire()
    try:
        if session.pending_tool_call is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but no pending tool call in session",
            )

        if session.pending_tool_call["call_id"] != fc_output["call_id"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"call_id mismatch: pending tool call has "
                    f"'{session.pending_tool_call['call_id']}', "
                    f"but received '{fc_output['call_id']}'"
                ),
            )

        if not hasattr(backend, "run_completion_with_client"):
            raise HTTPException(
                status_code=400,
                detail="function_call_output requires a backend that supports persistent clients",
            )

        if session.client is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but session has no active SDK client",
            )

        # --- Unblock the PreToolUse hook ---
        session.input_response = fc_output["output"]
        pending_event = session.input_event
        if pending_event is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but session has no pending input event",
            )
        pending_event.set()  # Unblocks the callback; SDK continues processing
        session.pending_tool_call = None  # Clear the pending state
    finally:
        session.lock.release()

    # --- Stream continuation from the client ---
    next_turn = session.turn_counter + 1
    resp_id = _make_response_id(session_id, next_turn)
    output_item_id = _generate_msg_id()

    if body.stream:
        # Acquire session lock for the streaming duration
        await session.lock.acquire()

        async def _run_continuation_stream():
            lock_acquired = True
            stream_result = {"success": False}
            try:
                chunks_buffer = []
                # After the hook returns deny+reason, the SDK continues
                # processing from where it left off.  Use
                # receive_response_from_client (no new query needed).
                if hasattr(backend, "receive_response_from_client"):
                    chunk_source = backend.receive_response_from_client(session.client, session)
                else:
                    chunk_source = backend.run_completion_with_client(session.client, "", session)

                sse_source = streaming_utils.stream_response_chunks(
                    chunk_source=chunk_source,
                    model=body.model,
                    response_id=resp_id,
                    output_item_id=output_item_id,
                    chunks_buffer=chunks_buffer,
                    logger=logger,
                    prompt_text="",
                    metadata=body.metadata or {},
                    stream_result=stream_result,
                    request_context={
                        "session_id": session_id,
                        "user": body.user,
                        "workdir": workspace_str,
                        "backend": resolved.backend,
                        "provider_model": resolved.provider_model,
                        "previous_response_id": body.previous_response_id,
                        "turn": next_turn,
                        "continuation": True,
                    },
                )
                async for line in streaming_utils.bridge_sse_stream(sse_source, chunk_source):
                    yield line

                # Check for another pending_tool_call (chained AskUserQuestion)
                if session.pending_tool_call is not None:
                    tc = session.pending_tool_call
                    yield streaming_utils.make_function_call_response_sse(
                        response_id=resp_id,
                        call_id=tc["call_id"],
                        name=tc["name"],
                        arguments=json.dumps(tc.get("arguments", {})),
                    )
                    requires_action_resp = _build_requires_action_response(
                        resp_id, body.model, tc, body.metadata
                    )
                    yield streaming_utils.make_response_sse(
                        "response.completed",
                        response_obj=requires_action_resp,
                        sequence_number=0,
                    )
                    session.turn_counter = next_turn
                    stream_result["success"] = True
                elif stream_result.get("success"):
                    assistant_text = stream_result.get("assistant_text") or ""
                    if assistant_text:
                        session.turn_counter = next_turn
                        session_manager.add_assistant_response(
                            session_id,
                            Message(role="assistant", content=assistant_text),
                        )

            except Exception as e:
                logger.error("Responses API Stream: continuation error: %s", e, exc_info=True)
                failed_resp = _build_failed_response(resp_id, body.model, body.metadata)
                yield streaming_utils.make_response_sse(
                    "response.failed",
                    response_obj=failed_resp,
                    sequence_number=0,
                )
            finally:
                if lock_acquired:
                    session.lock.release()

        return StreamingResponse(_run_continuation_stream(), media_type="text/event-stream")

    # --- Non-streaming continuation ---
    async with _nonstreaming_lock(session):
        chunks = []
        # After the hook returns deny+reason, the SDK continues
        # processing from where it left off — no new query needed.
        if hasattr(backend, "receive_response_from_client"):
            chunk_source = backend.receive_response_from_client(session.client, session)
        else:
            chunk_source = backend.run_completion_with_client(session.client, "", session)
        async for chunk in chunk_source:
            chunks.append(chunk)

        # Check for another pending_tool_call
        if session.pending_tool_call is not None:
            tc = session.pending_tool_call
            session.turn_counter = next_turn
            return _build_requires_action_response(
                resp_id, body.model, tc, body.metadata
            ).model_dump()

        for chunk in chunks:
            if isinstance(chunk, dict) and chunk.get("is_error"):
                # ``error_message`` here is SDK-curated (rate-limit, auth,
                # etc.) — not a raw Python exception string — so it is safe
                # to surface to clients.
                error_msg = chunk.get("error_message", "Unknown backend error")
                raise HTTPException(status_code=502, detail=f"Backend error: {error_msg}")

        assistant_text = backend.parse_message(chunks)
        if not assistant_text:
            raise HTTPException(status_code=502, detail="No response from backend")

        session.turn_counter = next_turn
        session_manager.add_assistant_response(
            session_id, Message(role="assistant", content=assistant_text)
        )

    prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
        chunks, "", assistant_text, body.model, backend=backend
    )

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


@asynccontextmanager
async def _nonstreaming_lock(session):
    """Acquire and release session lock for non-streaming continuation."""
    await session.lock.acquire()
    try:
        yield
    finally:
        session.lock.release()
