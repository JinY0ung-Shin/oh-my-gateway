"""Responses API endpoint (/v1/responses)."""

import asyncio
import json
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
from src.backends.claude.slash_commands import (
    SlashCommandError,
    validate_prompt as validate_slash_prompt,
)
from src.response_models import (
    ResponseCreateRequest,
    ResponseContentPart,
    ResponseErrorDetail,
    ResponseInputItem,
    FunctionCallOutputInput,
    FunctionCallOutputItem,
    ResponseObject,
    OutputItem,
    ResponseUsage,
)
from src.rate_limiter import rate_limit_endpoint
from src.constants import PERMISSION_MODE_BYPASS
from src.mcp_config import get_mcp_servers
from src import streaming_utils
from src.usage_logger import usage_logger
from src.workspace_manager import workspace_manager
from src.image_handler import ImageHandler
from src.routes.deps import (
    resolve_and_get_backend,
    validate_backend_auth_or_raise,
    validate_image_request,
)

logger = logging.getLogger(__name__)
router = APIRouter()

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"


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


def _content_part_text(part: object) -> str:
    if isinstance(part, dict):
        value = part.get("text")
    else:
        value = getattr(part, "text", "")
    return value if isinstance(value, str) else ""


def _input_content_to_text(content: str | list[Any]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(text for part in content if (text := _content_part_text(part)))


def _split_response_input(
    body: ResponseCreateRequest,
) -> tuple[Optional[str], str | list[ResponseInputItem | FunctionCallOutputInput]]:
    """Return ``(system_prompt, input_for_prompt)`` for prompt conversion.

    Explicit ``instructions`` wins: when present, array input is passed through
    unchanged so the adapter sees exactly what the client sent.  Without
    ``instructions``, array-form ``system`` / ``developer`` items become the
    per-request system prompt and are removed from user prompt input.  If the
    array only contains system/developer or function_call_output items, the
    original input is preserved so downstream validation/continuation handling
    still sees the complete request shape.
    """
    if not isinstance(body.input, list) or body.instructions:
        return body.instructions, body.input

    system_prompt: Optional[str] = body.instructions
    user_items: list[ResponseInputItem | FunctionCallOutputInput] = []
    for item in body.input:
        if not isinstance(item, ResponseInputItem):
            continue
        if item.role in ("system", "developer"):
            system_prompt = _input_content_to_text(item.content)
        else:
            user_items.append(item)

    return system_prompt, user_items if user_items else body.input


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


def _build_completed_response(
    resp_id: str,
    model: str,
    assistant_text: str,
    metadata: Optional[dict],
    *,
    input_tokens: int,
    output_tokens: int,
) -> ResponseObject:
    return ResponseObject(
        id=resp_id,
        status="completed",
        model=model,
        output=[
            OutputItem(
                id=_generate_msg_id(),
                content=[ResponseContentPart(text=assistant_text)],
            )
        ],
        usage=ResponseUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
        metadata=metadata or {},
    )


async def _disconnect_session_client(session, reason: str, client=None) -> None:
    """Drop and disconnect a persistent SDK client after stream failure/cancel."""
    if client is None:
        client = getattr(session, "client", None)
    if client is None:
        return
    if getattr(session, "client", None) is client:
        session.client = None
    disconnect = getattr(client, "disconnect", None)
    if disconnect is None:
        return
    try:
        await asyncio.wait_for(disconnect(), timeout=2.0)
    except Exception:
        logger.debug("SDK client disconnect failed after %s", reason, exc_info=True)


def _configure_client_streaming(client: Any, enabled: bool) -> None:
    """Enable backend-specific event streaming when a client supports it."""
    if client is not None and hasattr(client, "stream_events"):
        setattr(client, "stream_events", enabled)


async def _responses_streaming_preflight(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    session,
    session_id: str,
    is_new_session: bool,
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
        if body.previous_response_id is None:
            raise HTTPException(
                status_code=400,
                detail="previous_response_id is required for an existing session",
            )
        parsed = _parse_response_id(body.previous_response_id)
        if parsed is None:
            raise HTTPException(
                status_code=404,
                detail=f"previous_response_id '{body.previous_response_id}' is invalid",
            )
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
    }


def _validate_response_continuation(body: ResponseCreateRequest) -> None:
    if body.previous_response_id and body.instructions:
        raise HTTPException(
            status_code=400,
            detail="instructions cannot be used with previous_response_id. "
            "The system prompt is fixed to the original session.",
        )

    if body.previous_response_id and isinstance(body.input, list):
        for item in body.input:
            role = getattr(item, "role", None) or (
                item.get("role") if isinstance(item, dict) else None
            )
            if role in ("system", "developer"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "system/developer input items cannot be used with "
                        "previous_response_id. The system prompt is fixed "
                        "to the original session."
                    ),
                )


def _resolve_response_session(body: ResponseCreateRequest, backend: str) -> tuple[str, Any]:
    if not body.previous_response_id:
        session_id = str(uuid.uuid4())
        return session_id, session_manager.get_or_create_session(session_id)

    parsed = _parse_response_id(body.previous_response_id)
    if not parsed:
        raise HTTPException(
            status_code=404,
            detail=f"previous_response_id '{body.previous_response_id}' is invalid",
        )
    session_id, turn = parsed
    _early_cwd: Optional[str] = None
    if body.user:
        try:
            _early_cwd = str(
                workspace_manager.resolve(body.user, sync_template=False, backend=backend)
            )
        except (ValueError, OSError):
            pass
    session = session_manager.get_session(session_id, user=body.user, cwd=_early_cwd)
    if session is None and backend == "claude" and body.user:
        try:
            legacy_cwd = str(workspace_manager.resolve(body.user, sync_template=False))
        except (ValueError, OSError):
            legacy_cwd = None
        if legacy_cwd and legacy_cwd != _early_cwd:
            session = session_manager.get_session(session_id, user=body.user, cwd=legacy_cwd)
    if not session:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Session for previous_response_id "
                f"'{body.previous_response_id}' not found or expired"
            ),
        )
    if turn > session.turn_counter:
        raise HTTPException(
            status_code=404,
            detail=f"previous_response_id '{body.previous_response_id}' references a future turn",
        )
    return session_id, session


async def _resolve_response_workspace(
    body: ResponseCreateRequest,
    session,
    session_id: str,
    is_new_session: bool,
    backend: str,
) -> Path:
    if not is_new_session and session.user != body.user:
        raise HTTPException(
            status_code=400,
            detail=f"User mismatch: session belongs to {session.user!r}, "
            f"but request specifies {body.user!r}",
        )

    if is_new_session:
        try:
            workspace = workspace_manager.resolve(
                body.user,
                sync_template=True,
                backend=backend,
            )
        except ValueError as e:
            await session_manager.delete_session_async(session_id)
            raise HTTPException(status_code=400, detail=str(e)) from e
        session.user = body.user
        session.workspace = str(workspace)
        return workspace

    if session.workspace:
        return Path(session.workspace)

    try:
        workspace = workspace_manager.resolve(
            body.user,
            sync_template=False,
            backend=backend,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    session.workspace = str(workspace)
    return workspace


def _response_prompt_and_system(
    body: ResponseCreateRequest,
    workspace: Path,
) -> tuple[str, Optional[str]]:
    system_prompt, input_for_prompt = _split_response_input(body)
    image_handler = ImageHandler(workspace)
    try:
        prompt = MessageAdapter.response_input_to_prompt(
            input_for_prompt, image_handler=image_handler
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return MessageAdapter.filter_content(prompt), system_prompt


async def _validate_backend_prompt(
    resolved: ResolvedModel,
    prompt: str,
    workspace_str: str,
) -> None:
    if resolved.backend != "claude":
        return
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


async def _unblock_pending_tool_call(
    session, backend: "BackendClient", fc_output: Dict[str, str]
) -> None:
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

        session.input_response = fc_output["output"]
        pending_event = session.input_event
        if pending_event is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but session has no pending input event",
            )
        pending_event.set()
        session.pending_tool_call = None
    except Exception:
        session.lock.release()
        raise


async def _prepare_opencode_tool_continuation(
    session, backend: "BackendClient", fc_output: Dict[str, str]
) -> str:
    """Validate and reserve an OpenCode pending tool continuation."""
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

        pending = session.pending_tool_call
        resume_kind = pending.get("opencode_resume")
        if resume_kind is None:
            resume_kind = "question"
        if resume_kind not in ("question", "permission"):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported OpenCode resume kind: {resume_kind}",
            )
        if resume_kind == "permission":
            resume = getattr(backend, "resume_permission_with_client", None)
        else:
            resume = getattr(backend, "resume_question_with_client", None)
        if not callable(resume):
            raise HTTPException(
                status_code=400,
                detail=f"OpenCode {resume_kind} continuation is not supported by this backend",
            )

        if session.client is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but session has no active SDK client",
            )

        session.pending_tool_call = None
        return resume_kind
    except Exception:
        session.lock.release()
        raise


async def _prepare_codex_approval_continuation(
    session, backend: "BackendClient", fc_output: Dict[str, str]
) -> None:
    """Validate and reserve a Codex app-server approval continuation."""
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

        if session.pending_tool_call.get("codex_resume") != "approval":
            raise HTTPException(
                status_code=400,
                detail="Unsupported Codex continuation type",
            )

        resume = getattr(backend, "resume_approval_with_client", None)
        if not callable(resume):
            raise HTTPException(
                status_code=400,
                detail="Codex approval continuation is not supported by this backend",
            )

        if session.client is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but session has no active SDK client",
            )

        session.pending_tool_call = None
    except Exception:
        session.lock.release()
        raise


def _normalize_opencode_question_arguments(input_value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return OpenCode question input in the public AskUserQuestion argument shape."""
    question = input_value.get("question")
    if isinstance(question, str) and question:
        return input_value

    questions = input_value.get("questions")
    if not isinstance(questions, list):
        return None
    for item in questions:
        if isinstance(item, dict) and isinstance(item.get("question"), str) and item["question"]:
            return input_value
    return None


def _normalize_opencode_permission_arguments(
    input_value: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return OpenCode permission input in the public AskUserQuestion shape."""
    permission = input_value.get("permission")
    if not isinstance(permission, str) or not permission:
        return None

    raw_patterns = input_value.get("patterns")
    patterns = (
        [item for item in raw_patterns if isinstance(item, str)]
        if isinstance(raw_patterns, list)
        else []
    )
    raw_always = input_value.get("always")
    always = (
        [item for item in raw_always if isinstance(item, str)]
        if isinstance(raw_always, list)
        else []
    )
    metadata = input_value.get("metadata") if isinstance(input_value.get("metadata"), dict) else {}
    target = ", ".join(patterns) if patterns else "(no patterns specified)"
    allowed_context = f"; already allowed: {', '.join(always)}" if always else ""
    return {
        "question": f"OpenCode requests permission '{permission}' for: {target}{allowed_context}",
        "options": [
            {"label": "once", "description": "Allow this request once."},
            {"label": "always", "description": "Always allow matching requests."},
            {"label": "reject", "description": "Reject this request."},
        ],
        "permission": permission,
        "patterns": patterns,
        "always": always,
        "metadata": metadata,
    }


def _store_opencode_pending_tool_call(
    resolved: ResolvedModel,
    session,
    chunk: Dict[str, Any],
) -> bool:
    """Capture the first resumable OpenCode tool_use from a backend chunk."""
    if resolved.backend != "opencode" or not isinstance(chunk, dict):
        return False
    content = chunk.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        tool_name = block.get("name")
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
        if tool_name == "question":
            request_id = metadata.get("opencode_question_request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            arguments = _normalize_opencode_question_arguments(input_value)
            if arguments is None:
                continue
            session.pending_tool_call = {
                "call_id": request_id,
                "name": ASK_USER_QUESTION_TOOL_NAME,
                "arguments": arguments,
                "backend": "opencode",
                "opencode_resume": "question",
            }
            return True
        if tool_name == "permission":
            request_id = metadata.get("opencode_permission_request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            arguments = _normalize_opencode_permission_arguments(input_value)
            if arguments is None:
                continue
            session.pending_tool_call = {
                "call_id": request_id,
                "name": ASK_USER_QUESTION_TOOL_NAME,
                "arguments": arguments,
                "backend": "opencode",
                "opencode_resume": "permission",
            }
            return True
    return False


def _is_codex_pending_approval_chunk(
    resolved: ResolvedModel,
    session,
    chunk: Dict[str, Any],
) -> bool:
    """Return True when a Codex approval chunk already populated pending_tool_call."""
    if resolved.backend != "codex" or not isinstance(chunk, dict):
        return False
    pending = getattr(session, "pending_tool_call", None)
    if not isinstance(pending, dict) or pending.get("codex_resume") != "approval":
        return False
    content = chunk.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or block.get("name") != "codex_approval":
            continue
        metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
        request_id = metadata.get("codex_approval_request_id") or block.get("id")
        if str(request_id or "") == str(pending.get("call_id") or ""):
            return True
    return False


async def _capture_pending_tool_questions(chunk_source, resolved: ResolvedModel, session):
    async for chunk in chunk_source:
        if _is_codex_pending_approval_chunk(resolved, session, chunk):
            close = getattr(chunk_source, "aclose", None)
            if callable(close):
                await close()
            return
        if resolved.backend == "opencode" and isinstance(chunk, dict):
            content = chunk.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in ("question", "permission")
                for block in content
            ):
                if _store_opencode_pending_tool_call(resolved, session, chunk):
                    close = getattr(chunk_source, "aclose", None)
                    if callable(close):
                        await close()
                    return
                continue
        yield chunk


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
    _validate_response_continuation(body)
    session_id, session = _resolve_response_session(body, resolved.backend)
    workspace = await _resolve_response_workspace(
        body, session, session_id, is_new_session, resolved.backend
    )
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

    prompt, system_prompt = _response_prompt_and_system(body, workspace)

    # Reject slash-prefixed prompts that would be intercepted by the SDK as
    # unknown skills or run destructive built-ins.  Only applies to the Claude
    # backend; other backends pass through unchanged.
    await _validate_backend_prompt(resolved, prompt, workspace_str)

    # ------------------------------------------------------------------
    # Create the persistent ClaudeSDKClient.  Every turn flows through it
    # so PreToolUse hooks (AskUserQuestion) fire reliably and the on-disk
    # transcript stays in lockstep with session.session_id.  If creation
    # fails the request returns 503 — there is no longer a degraded
    # query() fallback.
    # bypassPermissions avoids workspace-level settings.local.json checks
    # (temp workspaces lack shell command allow-lists).
    # ------------------------------------------------------------------
    if session.client is None:
        # Pre-resolve {{WORKING_DIRECTORY}} so the persistent client's
        # frozen system_prompt matches the cwd the SDK will actually use.
        #
        # Reconnect case: when an existing session lost its persistent client
        # (e.g. SDK error/disconnect), prefer the prompt frozen at session
        # start so admin-side prompt changes mid-conversation don't leak
        # into in-flight sessions.
        from src.system_prompt import get_system_prompt, resolve_request_placeholders

        resolved_base: Optional[str]
        if session.base_system_prompt is not None:
            resolved_base = session.base_system_prompt
        else:
            resolved_base = resolve_request_placeholders(get_system_prompt(), workspace_str)
        try:
            session.client = await backend.create_client(
                session=session,
                model=resolved.provider_model,
                system_prompt=system_prompt if is_new_session else None,
                permission_mode=PERMISSION_MODE_BYPASS,
                allowed_tools=body.allowed_tools,
                mcp_servers=get_mcp_servers() if resolved.backend == "claude" else None,
                cwd=workspace_str,
                extra_env=body.metadata,
                _custom_base=resolved_base,
            )
        except Exception:
            logger.error("create_client failed", exc_info=True)
            await session_manager.delete_session_async(session_id)
            raise HTTPException(
                status_code=503,
                detail=f"{resolved.backend} backend unavailable; retry shortly",
            )

    if body.stream:
        # Run preflight BEFORE StreamingResponse so HTTPExceptions produce
        # proper HTTP error status codes (not swallowed inside the generator).
        preflight = await _responses_streaming_preflight(
            body,
            resolved,
            session,
            session_id,
            is_new_session,
            workspace_str=workspace_str,
        )

        next_turn = preflight["next_turn"]
        resp_id = _make_response_id(session_id, next_turn)
        output_item_id = _generate_msg_id()

        async def _run_stream():
            lock_acquired = preflight["lock_acquired"]
            stream_result: dict = {"success": False}
            active_client = session.client
            try:
                chunks_buffer = []

                _configure_client_streaming(session.client, True)
                backend_source = backend.run_completion_with_client(session.client, prompt, session)
                chunk_source = _capture_pending_tool_questions(backend_source, resolved, session)

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
                        "use_sdk_client": True,
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
                elif stream_result.get("empty"):
                    # Stream ended with no text and no pending tool call —
                    # same empty-response condition the non-stream path
                    # surfaces as HTTP 502.  Emit response.failed so the
                    # client doesn't hang on a silent success.
                    logger.warning(
                        "Responses stream: no content and no pending tool call; emitting failed"
                    )
                    failed_resp = _build_failed_response(
                        resp_id,
                        body.model,
                        body.metadata,
                        code="empty_response",
                        message="No response generated",
                    )
                    yield streaming_utils.make_response_sse(
                        "response.failed",
                        response_obj=failed_resp,
                        sequence_number=0,
                    )
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
                if not stream_result.get("success"):
                    await _disconnect_session_client(
                        session, "responses stream failure", client=active_client
                    )
                if lock_acquired:
                    session.lock.release()

        return StreamingResponse(_run_stream(), media_type="text/event-stream")

    # --- Non-streaming path ---
    import time as _time

    _usage_start = _time.monotonic()
    active_client = None
    try:
        from src.session_guard import session_preflight_scope

        # Pre-parse turn for validation inside the lock
        _turn: Optional[int] = None
        if not is_new_session:
            if body.previous_response_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="previous_response_id is required for an existing session",
                )
            _parsed = _parse_response_id(body.previous_response_id)
            if _parsed is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"previous_response_id '{body.previous_response_id}' is invalid",
                )
            _, _turn = _parsed

        async with session_preflight_scope(
            session,
            resolved,
            session_id,
            is_new=is_new_session,
            turn=_turn,
            workspace=workspace_str,
        ) as pf:
            # Execute backend through the persistent client.
            chunks = []
            active_client = session.client
            _configure_client_streaming(active_client, False)
            backend_source = backend.run_completion_with_client(active_client, prompt, session)
            async for chunk in _capture_pending_tool_questions(backend_source, resolved, session):
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
        if active_client is not None:
            await _disconnect_session_client(
                session, "responses non-stream failure", client=active_client
            )
        raise
    except Exception as e:
        if active_client is not None:
            await _disconnect_session_client(
                session, "responses non-stream failure", client=active_client
            )
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

    response_obj = _build_completed_response(
        resp_id,
        body.model,
        assistant_text,
        body.metadata,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
    )

    try:
        await usage_logger.log_turn_from_context(
            request_context={
                "session_id": session_id,
                "user": body.user,
                "backend": resolved.backend,
                "provider_model": resolved.provider_model,
                "previous_response_id": body.previous_response_id,
                "turn": session.turn_counter,
            },
            response_id=resp_id,
            model=body.model,
            chunks=chunks,
            tool_stats=None,
            started_monotonic=_usage_start,
            status="completed",
        )
    except Exception:
        logger.warning("usage-log emit failed (non-stream)", exc_info=True)

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
    import time as _time

    _usage_start = _time.monotonic()
    # --- Validate + unblock under session lock, then keep the lock through
    # the continuation read so no concurrent request can mutate session state
    # between the tool output and SDK resume.
    opencode_resume_kind = "question"
    if resolved.backend == "opencode":
        opencode_resume_kind = await _prepare_opencode_tool_continuation(
            session, backend, fc_output
        )
    elif resolved.backend == "codex":
        await _prepare_codex_approval_continuation(session, backend, fc_output)
    else:
        await _unblock_pending_tool_call(session, backend, fc_output)

    # --- Stream continuation from the client ---
    next_turn = session.turn_counter + 1
    resp_id = _make_response_id(session_id, next_turn)
    output_item_id = _generate_msg_id()
    active_client = session.client

    if body.stream:

        async def _run_continuation_stream():
            stream_result = {"success": False}
            try:
                chunks_buffer = []
                _configure_client_streaming(active_client, True)
                # After the hook returns deny+reason, the SDK continues
                # processing from where it left off.  Use receive_response_from_client
                # when available; do not start a new prompt.
                if resolved.backend == "opencode":
                    if opencode_resume_kind == "permission":
                        backend_source = backend.resume_permission_with_client(
                            active_client,
                            fc_output["call_id"],
                            fc_output["output"],
                            session,
                        )
                    else:
                        backend_source = backend.resume_question_with_client(
                            active_client,
                            fc_output["call_id"],
                            fc_output["output"],
                            session,
                        )
                elif resolved.backend == "codex":
                    backend_source = backend.resume_approval_with_client(
                        active_client,
                        fc_output["call_id"],
                        fc_output["output"],
                        session,
                    )
                else:
                    receiver = getattr(backend, "receive_response_from_client", None)
                    if receiver is not None:
                        backend_source = receiver(active_client, session)
                    else:
                        backend_source = backend.run_completion_with_client(
                            active_client, "", session
                        )
                chunk_source = _capture_pending_tool_questions(backend_source, resolved, session)

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
                        "function_call_output_call_id": fc_output["call_id"],
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
                if not stream_result.get("success"):
                    await _disconnect_session_client(
                        session,
                        "responses continuation stream failure",
                        client=active_client,
                    )
                session.lock.release()

        return StreamingResponse(_run_continuation_stream(), media_type="text/event-stream")

    # --- Non-streaming continuation ---
    continuation_success = False
    try:
        chunks = []
        # After the hook returns deny+reason, the SDK continues
        # processing from where it left off — no new query needed.
        _configure_client_streaming(active_client, False)
        if resolved.backend == "opencode":
            if opencode_resume_kind == "permission":
                backend_source = backend.resume_permission_with_client(
                    active_client,
                    fc_output["call_id"],
                    fc_output["output"],
                    session,
                )
            else:
                backend_source = backend.resume_question_with_client(
                    active_client,
                    fc_output["call_id"],
                    fc_output["output"],
                    session,
                )
        elif resolved.backend == "codex":
            backend_source = backend.resume_approval_with_client(
                active_client,
                fc_output["call_id"],
                fc_output["output"],
                session,
            )
        else:
            receiver = getattr(backend, "receive_response_from_client", None)
            if receiver is not None:
                backend_source = receiver(active_client, session)
            else:
                backend_source = backend.run_completion_with_client(active_client, "", session)
        async for chunk in _capture_pending_tool_questions(backend_source, resolved, session):
            chunks.append(chunk)

        # Check for another pending_tool_call
        if session.pending_tool_call is not None:
            tc = session.pending_tool_call
            session.turn_counter = next_turn
            continuation_success = True
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
        continuation_success = True
    finally:
        if not continuation_success:
            await _disconnect_session_client(
                session, "responses continuation non-stream failure", client=active_client
            )
        session.lock.release()

    prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
        chunks, "", assistant_text, body.model, backend=backend
    )

    response_obj = _build_completed_response(
        resp_id,
        body.model,
        assistant_text,
        body.metadata,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
    )

    try:
        await usage_logger.log_turn_from_context(
            request_context={
                "session_id": session_id,
                "user": body.user,
                "backend": resolved.backend,
                "provider_model": resolved.provider_model,
                "previous_response_id": body.previous_response_id,
                "turn": session.turn_counter,
            },
            response_id=resp_id,
            model=body.model,
            chunks=chunks,
            tool_stats=None,
            started_monotonic=_usage_start,
            status="completed",
        )
    except Exception:
        logger.warning("usage-log emit failed (non-stream continuation)", exc_info=True)

    return response_obj.model_dump()
