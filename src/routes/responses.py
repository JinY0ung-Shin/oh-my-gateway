"""Responses API endpoint (/v1/responses)."""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List, cast

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse

from src.models import Message
from src.message_adapter import MessageAdapter
from src.auth import verify_api_key, security
from src.session_manager import session_manager
from src.backends import BackendClient, BackendRegistry, ResolvedModel
from src.backends.claude.client import UnsupportedContinuationPolicy
from src.backends.claude.slash_commands import (
    SlashCommandError,
    validate_prompt as validate_slash_prompt,
)
from src.response_models import (
    InputTokensDetails,
    ResponseCreateRequest,
    ResponseContentPart,
    ResponseErrorDetail,
    ResponseInputItem,
    FunctionCallOutputInput,
    FunctionCallOutputItem,
    ResponseDeletedObject,
    ResponseIncompleteDetails,
    ResponseObject,
    OutputItem,
    ReasoningContent,
    ReasoningOutputItem,
    ReasoningSummary,
    ResponseUsage,
)
from src.concurrency_middleware import take_turn_slot
from src.rate_limiter import rate_limit_endpoint
from src import session_outbox
from src.chunk_processing import classify_error_chunk
from src.constants import DEFAULT_TIMEOUT_MS
from src.mcp_config import get_mcp_servers
from src import streaming_utils
from src.usage_logger import usage_logger
from src.workspace_manager import workspace_manager
from src.image_handler import ImageHandler
from src.routes.deps import (
    resolve_and_get_backend,
    validate_backend_auth_or_raise,
    validate_image_request,
    validate_model_vision_support,
)

logger = logging.getLogger(__name__)
router = APIRouter()

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"
NON_STREAM_CONTINUATION_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_MS / 1000


def _background_response_timeout_s() -> float:
    """Wall-clock cap for one background turn (``BACKGROUND_RESPONSE_TIMEOUT_S``).

    A background turn has no HTTP connection whose disconnect would bound it,
    so a wedged backend would otherwise hold the session lock and its SDK
    subprocess forever.
    """
    try:
        return float(os.getenv("BACKGROUND_RESPONSE_TIMEOUT_S", "3600"))
    except ValueError:
        return 3600.0


# In-flight and failed background turns keyed by response id. Every mutation
# happens on the event loop with no await between read and write, so no lock
# is needed. Committed turns leave the registry (GET serves them from the
# session's turn store); failed turns stay retrievable until their session
# expires or the turn is retried — a retry reuses the same response id (the
# turn counter never advanced) and simply overwrites the entry.
_BACKGROUND_RUNS: Dict[str, Dict[str, Any]] = {}
# Strong refs: asyncio holds tasks weakly, and a GC'd task would kill a
# background turn mid-flight.
_BACKGROUND_RESPONSE_TASKS: set = set()


def _register_background_run(response_id: str, session_id: str, payload: Dict[str, Any]) -> None:
    _BACKGROUND_RUNS[response_id] = {"session_id": session_id, "payload": payload}


def _set_background_run_status(response_id: str, status: str) -> None:
    run = _BACKGROUND_RUNS.get(response_id)
    if run is not None:
        run["payload"]["status"] = status


def _set_background_run_payload(response_id: str, payload: Dict[str, Any]) -> None:
    run = _BACKGROUND_RUNS.get(response_id)
    if run is not None:
        run["payload"] = payload


def _drop_background_run(response_id: str) -> None:
    _BACKGROUND_RUNS.pop(response_id, None)


def _generate_msg_id() -> str:
    """Generate an output item ID: msg_<hex>."""
    return f"msg_{secrets.token_hex(12)}"


def _generate_rs_id() -> str:
    """Generate a reasoning output item ID: rs_<hex>."""
    return f"rs_{uuid.uuid4().hex[:24]}"


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


def _response_not_found(response_id: str) -> HTTPException:
    """OpenAI-style 404 for a missing, expired, or unstored response id."""
    return HTTPException(
        status_code=404,
        detail={
            "error": {
                "message": f"Response with id '{response_id}' not found.",
                "type": "invalid_request_error",
                "param": "response_id",
                "code": "response_not_found",
            }
        },
    )


def _previous_response_not_found(previous_response_id: Optional[str]) -> HTTPException:
    """Non-revealing 404 for an unknown/foreign previous_response_id.

    Used for cross-user access so callers cannot probe ownership: the
    response is identical to the genuinely-missing case (same message as
    the "not found or expired" path), never echoing the owning user.
    """
    return HTTPException(
        status_code=404,
        detail=(f"Session for previous_response_id '{previous_response_id}' not found or expired"),
    )


def _lookup_stored_response(response_id: str, user: Optional[str]):
    """Resolve a response_id to ``(session, session_id, turn)`` or raise 404.

    User scoping: sessions record their owner in ``session.user`` (set by
    POST /v1/responses). GET/DELETE carry no body, so the owner is matched
    against the optional ``user`` query parameter when provided; a mismatch
    is reported as 404 (unlike POST's 400) so callers cannot probe which
    user owns a response id. When ``user`` is omitted, API-key auth alone
    grants access — the same model as GET/DELETE /v1/sessions.
    """
    parsed = _parse_response_id(response_id)
    if parsed is None:
        raise _response_not_found(response_id)
    session_id, turn = parsed
    session = session_manager.get_session(session_id)
    if session is None:
        raise _response_not_found(response_id)
    if user is not None and session.user != user:
        raise _response_not_found(response_id)
    if turn > session.turn_counter:
        raise _response_not_found(response_id)
    return session, session_id, turn


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
    response_id: str,
    model: str,
    assistant_text: str,
    metadata: Optional[dict],
    *,
    input_tokens: int,
    output_tokens: int,
    usage_details: Optional[InputTokensDetails] = None,
    thinking_texts: Optional[List[str]] = None,
    structured_output: Any = None,
) -> ResponseObject:
    output_items: List[Any] = []
    for t in thinking_texts or []:
        output_items.append(
            ReasoningOutputItem(
                id=_generate_rs_id(),
                summary=[ReasoningSummary(text=t)],
                content=[ReasoningContent(text=t)],
            )
        )
    output_items.append(
        OutputItem(
            id=_generate_msg_id(),
            content=[ResponseContentPart(text=assistant_text)],
        )
    )
    return ResponseObject(
        id=response_id,
        status="completed",
        model=model,
        output=output_items,
        usage=ResponseUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=usage_details or InputTokensDetails(),
        ),
        metadata=metadata or {},
        structured_output=structured_output,
    )


def _record_turn_response(
    session, turn: int, response_obj: ResponseObject, *, store: Optional[bool]
) -> None:
    """Persist a committed turn's ResponseObject for GET /v1/responses/{id}.

    Honors OpenAI ``store`` semantics: ``store=false`` turns still chain
    normally via ``previous_response_id`` (session history is unaffected)
    but cannot be retrieved later. The payload is stored as a plain dict so
    retrieval returns exactly what POST returned (item ids, created_at,
    usage, structured_output).
    """
    if store is False:
        return
    session.record_turn_response(turn, response_obj.model_dump())


def _record_stream_turn_response(
    session,
    *,
    turn: int,
    response_id: str,
    model: str,
    stream_result: dict,
    chunks_buffer: list,
    prompt: Any,
    metadata: Optional[dict],
    store: Optional[bool],
    backend: "BackendClient",
) -> None:
    """Record a completed streaming turn for GET /v1/responses/{id}.

    Mirrors the non-stream commit: rebuilds the completed ResponseObject
    from the stream result and buffered chunks (usage, thinking texts,
    structured_output) and stores it on the session.
    """
    if store is False:
        return
    assistant_text = stream_result.get("assistant_text") or ""
    thinking_texts = stream_result.get("thinking_texts") or []
    usage_text = assistant_text or "\n".join(thinking_texts)
    input_tokens, output_tokens = streaming_utils.resolve_token_usage(
        chunks_buffer, prompt, usage_text, model, backend=backend
    )
    response_obj = _build_completed_response(
        response_id,
        model,
        assistant_text,
        metadata,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_details=streaming_utils.resolve_usage_details(chunks_buffer),
        thinking_texts=thinking_texts,
        structured_output=streaming_utils.extract_structured_output(chunks_buffer),
    )
    session.record_turn_response(turn, response_obj.model_dump())


def _split_assistant_text_and_thinking(
    chunks: list,
    fallback_text: Optional[str],
) -> tuple[str, List[str]]:
    thinking_texts = streaming_utils.extract_thinking_texts(chunks)
    visible_text = streaming_utils.extract_visible_assistant_text(chunks)
    if visible_text is None:
        visible_text = "" if thinking_texts else (fallback_text or "")
    return visible_text, thinking_texts


def _build_session_assistant_message(
    visible_text: str,
    thinking_texts: Optional[List[str]] = None,
) -> Optional[Message]:
    non_empty_thinking = [text for text in thinking_texts or [] if text]
    if not visible_text and not non_empty_thinking:
        return None
    return Message(role="assistant", content=visible_text, thinking=non_empty_thinking)


def _log_session_assistant_write(
    *,
    path: str,
    session_id: str,
    turn: int,
    visible_text: str,
    thinking_texts: Optional[List[str]] = None,
) -> None:
    non_empty_thinking = [text for text in thinking_texts or [] if text]
    logger.info(
        "Responses session assistant stored: path=%s session_id=%s turn=%d "
        "visible_chars=%d thinking_blocks=%d thinking_chars=%s",
        path,
        session_id,
        turn,
        len(visible_text),
        len(non_empty_thinking),
        [len(text) for text in non_empty_thinking],
    )


# Strong refs to detached stream-teardown tasks — the event loop holds only
# weak refs, so without this a teardown running past its cancelled generator
# could be garbage-collected mid-flight.
_teardown_tasks: set = set()


def _log_teardown_result(task: "asyncio.Task") -> None:
    _teardown_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Detached stream teardown failed", exc_info=exc)


async def _shielded_stream_teardown(coro, description: str) -> None:
    """Run stream teardown so a consumer disconnect cannot skip it.

    A StreamingResponse generator cancelled by a client disconnect unwinds
    inside an already-cancelled anyio scope, where every ``await`` in its
    ``finally`` re-raises CancelledError immediately. Cleanup that must never
    be skipped — dropping the SDK client, clearing active-response state,
    releasing the session lock — therefore runs in its own task: the
    generator's cancellation still propagates, but the teardown continues to
    completion detached. (Skipping it leaves a zombie CLI turn piling unread
    messages that the next turn's reader would steal.)
    """
    task = asyncio.get_running_loop().create_task(coro, name=description)
    _teardown_tasks.add(task)
    task.add_done_callback(_log_teardown_result)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        logger.info("Stream teardown continues detached: %s", description)
        raise


def _clear_stale_pending_tool_call(session, reason: str) -> None:
    """Release an AskUserQuestion pause whose owning client is going away.

    The pause lives inside the dropped client's ``can_use_tool`` callback; once
    that client is gone the question can never be answered. Left in place, the
    stale ``pending_tool_call`` makes EVERY later turn re-emit the same
    requires_action card at stream end (the post-stream check does not know
    which turn the pause belongs to) and keeps the idle reader gated off.
    """
    if session.pending_tool_call is None and getattr(session, "input_event", None) is None:
        return
    logger.warning(
        "Clearing stale AskUserQuestion state after %s (session=%s, call_id=%s)",
        reason,
        getattr(session, "session_id", "?"),
        (session.pending_tool_call or {}).get("call_id", "?"),
    )
    session.pending_tool_call = None
    session.input_response = None
    pending_event = getattr(session, "input_event", None)
    session.input_event = None
    if pending_event is not None:
        # If the hook coroutine is somehow still alive, unblock it — it reads
        # a null response and denies gracefully instead of waiting out the
        # full ASK_USER_TIMEOUT.
        pending_event.set()


async def _disconnect_session_client(session, reason: str, client=None) -> None:
    """Drop and disconnect a persistent SDK client after stream failure/cancel."""
    await session_outbox.pause_idle_reader(session)
    _clear_stale_pending_tool_call(session, reason)
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


async def _begin_active_response(session, response_id: str, turn: int, client: Any) -> None:
    """Publish an in-flight response for concurrent cancel requests."""
    async with session.response_control_lock:
        if session.active_response_id is not None:
            raise RuntimeError(
                f"Session {session.session_id} already has active response "
                f"{session.active_response_id}"
            )
        session.active_response_id = response_id
        session.active_response_turn = turn
        session.active_response_state = "running"
        session.active_response_client = client
        session.active_response_done.clear()


async def _finish_active_response(session, response_id: str, terminal_state: str) -> None:
    """Clear active response state without racing a concurrent cancel call."""
    async with session.response_control_lock:
        if session.active_response_id != response_id:
            return
        session.active_response_state = terminal_state
        session.active_response_id = None
        session.active_response_turn = None
        session.active_response_client = None
        # Refresh in the same critical section that unpins: only the success
        # paths touch via add_messages, so without this a *failed* long turn
        # leaves last_accessed at the turn's start — reading as idle for the
        # whole turn duration and (under SESSION_EVICTION_POLICY=lru)
        # instantly evictable under a queued follow-up.
        session.touch()
        session.active_response_done.set()
    # Turn over — hand the client's message stream back to the between-turn
    # idle reader so background task events keep flowing into the outbox.
    # (No-op for non-Claude clients, paused AskUserQuestion turns, and dropped
    # clients; a queued next turn pauses it again before reading.)
    session_outbox.resume_idle_reader(session)


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


def _validate_background_request(body: ResponseCreateRequest) -> None:
    """400 on request shapes background mode does not support."""
    if body.stream:
        raise HTTPException(
            status_code=400,
            detail=(
                "background=true does not support stream=true yet. Create the "
                "response without stream and poll GET /v1/responses/{response_id}."
            ),
        )
    if body.store is False:
        raise HTTPException(
            status_code=400,
            detail="background=true requires store=true so the result can be retrieved.",
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
    # Peek first so an ownership check happens *before* the TTL is touched —
    # otherwise probing another user's response_id would keep their session
    # alive. Only refresh the TTL once ownership is confirmed.
    session = session_manager.peek_session(session_id)
    if session is not None:
        if session.user != body.user:
            # Mirror the GET/DELETE 404 policy: never echo the owner or
            # confirm the session exists for a non-owner.
            raise _previous_response_not_found(body.previous_response_id)
        if turn > session.turn_counter:
            raise HTTPException(
                status_code=404,
                detail=f"previous_response_id '{body.previous_response_id}' references a future turn",
            )
        session.touch()
        return session_id, session

    _early_cwd: Optional[str] = None
    if body.user:
        try:
            _early_cwd = str(workspace_manager.resolve(body.user, backend=backend))
        except (ValueError, OSError):
            pass
    # max_turn: a transcript that cannot serve the requested turn must be
    # discarded before admission — at the cap, admitting it can evict a live
    # session (SESSION_EVICTION_POLICY=lru) only for this request to 404.
    session = session_manager.get_session(
        session_id, user=body.user, cwd=_early_cwd, max_turn=turn
    )
    if session is None and backend == "claude" and body.user:
        try:
            legacy_cwd = str(workspace_manager.resolve(body.user))
        except (ValueError, OSError):
            legacy_cwd = None
        if legacy_cwd and legacy_cwd != _early_cwd:
            session = session_manager.get_session(
                session_id, user=body.user, cwd=legacy_cwd, max_turn=turn
            )
    if not session:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Session for previous_response_id "
                f"'{body.previous_response_id}' not found or expired"
            ),
        )
    if session.user != body.user:
        # Same non-revealing 404 as the cache-hit path above.
        raise _previous_response_not_found(body.previous_response_id)
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
        # Non-revealing 404 consistent with GET/DELETE and
        # _resolve_response_session; never echo the owning user.
        raise _previous_response_not_found(body.previous_response_id)

    if is_new_session:
        try:
            workspace = workspace_manager.resolve(body.user, backend=backend)
        except ValueError as e:
            await session_manager.delete_session_async(session_id)
            raise HTTPException(status_code=400, detail=str(e)) from e
        session.user = body.user
        session.workspace = str(workspace)
        return workspace

    if session.workspace:
        return Path(session.workspace)

    try:
        workspace = workspace_manager.resolve(body.user, backend=backend)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    session.workspace = str(workspace)
    return workspace


def _response_model_params(body: ResponseCreateRequest) -> Optional[Dict[str, Any]]:
    """Collect OpenAI-style model overrides from the request body.

    Returns ``None`` when the caller supplied no sampling/output-control
    overrides so backends keep their existing defaults. Only fields the
    Responses API natively exposes are forwarded; backends that can't honor
    a given key are expected to ignore it.
    """
    params: Dict[str, Any] = {}
    if body.temperature is not None:
        params["temperature"] = body.temperature
    if body.max_output_tokens is not None:
        params["max_output_tokens"] = body.max_output_tokens
    return params or None


def _response_output_format(body: ResponseCreateRequest) -> Optional[Dict[str, Any]]:
    """Map the OpenAI ``text.format`` json_schema config to the SDK shape.

    Returns ``{"type": "json_schema", "schema": {...}}`` (the
    ``ClaudeAgentOptions.output_format`` payload) when the request asks for
    Structured Outputs, or ``None`` for the default ``text`` format. The
    schema is passed through as-is — the gateway does not rewrite it.
    """
    fmt = body.text.format if body.text is not None else None
    if fmt is None or fmt.type != "json_schema":
        return None
    return {"type": "json_schema", "schema": fmt.json_schema}


def _validate_output_format_backend(
    output_format: Optional[Dict[str, Any]], backend_name: str
) -> None:
    """Reject Structured Outputs requests for backends that can't honor them."""
    if output_format is not None and backend_name != "claude":
        raise HTTPException(
            status_code=400,
            detail=(
                f"text.format type 'json_schema' is not supported by the "
                f"'{backend_name}' backend; only the claude backend supports "
                f"structured outputs"
            ),
        )


def _validate_continuation_output_format(
    client: Any, output_format: Optional[Dict[str, Any]]
) -> None:
    """Fail closed when a continuation asks for a different structured-output schema.

    The Claude SDK bakes ``output_format`` into the session at create time
    and has no runtime API to change it. Re-sending the same format is a
    no-op (OpenAI clients typically resend the full request config on every
    turn); asking for a different one is rejected instead of silently
    ignored.
    """
    if output_format is None:
        return
    existing = getattr(getattr(client, "options", None), "output_format", None)
    if existing == output_format:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "text.format 'json_schema' cannot be changed on a continuation "
            "turn; structured output is fixed when the session is created. "
            "Start a new session to apply a different schema."
        ),
    )


def _response_reasoning_effort(body: ResponseCreateRequest) -> Optional[str]:
    """Extract the requested ``reasoning.effort`` value, or ``None``."""
    return body.reasoning.effort if body.reasoning is not None else None


def _validate_reasoning_backend(effort: Optional[str], backend_name: str) -> None:
    """Reject reasoning.effort requests for backends that can't honor them."""
    if effort is not None and backend_name != "claude":
        raise HTTPException(
            status_code=400,
            detail=(
                f"reasoning.effort is not supported by the '{backend_name}' "
                f"backend; only the claude backend supports reasoning control"
            ),
        )


def _validate_continuation_reasoning(client: Any, effort: Optional[str]) -> None:
    """Fail closed when a continuation asks for a different reasoning effort.

    Thinking/effort ride CLI flags baked into the SDK client at create time;
    there is no runtime API to change them. Re-sending the value the session
    already runs with is a no-op (OpenAI clients typically resend the full
    request config every turn); asking for a different one is rejected
    instead of silently ignored.
    """
    if effort is None:
        return
    opts = getattr(client, "options", None)
    if effort == "none":
        if getattr(opts, "thinking", None) == {"type": "disabled"}:
            return
    elif getattr(opts, "effort", None) == effort:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "reasoning.effort cannot be changed on a continuation turn; it "
            "is fixed when the session is created. Start a new session to "
            "apply a different effort."
        ),
    )


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


_NATIVE_IMAGE_INPUT_PART_TYPES = frozenset({"input_image"})


def _has_multimodal_input(body: ResponseCreateRequest) -> bool:
    """True when the request carries an input part a backend can carry
    natively (currently only ``input_image``; Claude and Codex).

    Narrow on purpose: an unknown type alone would otherwise route us into
    the multimodal branch and then drop out as an empty items list.
    """
    if isinstance(body.input, str):
        return False
    for item in body.input:
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            ptype = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            if ptype in _NATIVE_IMAGE_INPUT_PART_TYPES:
                return True
    return False


def _response_prompt_blocks_and_system(
    body: ResponseCreateRequest,
) -> tuple[list, Optional[str]]:
    """Build native Anthropic content blocks from a multimodal Responses request.

    Claude counterpart of ``_response_input_to_codex_items``: ``input_image``
    parts become inline ``{"type": "image", ...}`` blocks passed to the SDK as
    streaming input, so the model receives pixels directly instead of a
    ``<attached_image>`` path placeholder + Read-tool round-trip (issue #140).
    """
    system_prompt, input_for_prompt = _split_response_input(body)
    try:
        blocks = MessageAdapter.response_input_to_claude_blocks(input_for_prompt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return blocks, system_prompt


def _response_input_to_codex_items(
    body: ResponseCreateRequest,
) -> tuple[list[Dict[str, Any]], Optional[str]]:
    """Build a Codex turn-input items list from a Responses request.

    Preserves the order of ``input_text`` and ``input_image`` parts inside
    each message so multimodal turns keep the user's intended sequence. Plain
    string inputs become a single text item. Unknown part types are skipped
    (forward-compat with new Responses fields).
    """
    system_prompt, input_for_prompt = _split_response_input(body)

    if isinstance(input_for_prompt, str):
        text = input_for_prompt.strip()
        items: list[Dict[str, Any]] = []
        if text:
            items.append({"type": "text", "text": text})
        return items, system_prompt

    items = []
    for input_item in input_for_prompt:
        content = getattr(input_item, "content", None)
        if isinstance(content, str):
            if content:
                items.append({"type": "text", "text": content})
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            ptype = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            if ptype == "input_text":
                text_value = (
                    part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
                )
                if text_value:
                    items.append({"type": "text", "text": text_value})
            elif ptype == "input_image":
                image_url = (
                    part.get("image_url")
                    if isinstance(part, dict)
                    else getattr(part, "image_url", None)
                )
                if not isinstance(image_url, str) or not image_url.strip():
                    raise HTTPException(
                        status_code=400,
                        detail="Codex multimodal input contains an empty image_url",
                    )
                # Use the Codex-native shape (``type=image`` / ``url``)
                # matching the in-tree fixture
                # tests/test_codex_backend.py multi-item case.
                items.append({"type": "image", "url": image_url})
            # Other / unknown part types are ignored — they'll be added as
            # explicit cases when their Codex item shape is known.
    return items, system_prompt


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


async def _refresh_existing_client_policy(
    body: ResponseCreateRequest,
    backend: "BackendClient",
    client: Any,
) -> None:
    """Apply per-request policy overrides to an existing backend client."""
    update_policy = getattr(backend, "update_request_policy", None)
    if not callable(update_policy):
        return

    try:
        result = update_policy(
            client,
            allowed_tools=body.allowed_tools,
            disallowed_tools=body.disallowed_tools,
            permission_mode=body.permission_mode,
            model_params=_response_model_params(body),
        )
        # Backends may implement update_request_policy as either a sync call
        # (Codex) or an async one (Claude — needs SDK await). Await whichever
        # shape comes back.
        if inspect.iscoroutine(result):
            await result
    except UnsupportedContinuationPolicy as exc:
        # Backend explicitly signaled the continuation can't honor the requested
        # policy change. Fail closed at the API boundary. Other exceptions
        # propagate so they surface as 5xx instead of being misclassified as 400.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _collect_mcp_forward_headers(request: Request, body: ResponseCreateRequest) -> Dict[str, str]:
    """Build the per-request MCP context header from ``MCP_FORWARD_CONTEXT``.

    Resolves ``{{user}}`` from the request identity (``body.user`` — trustworthy
    only when set by a trusted caller, not a direct API caller) and
    ``{{header:NAME}}`` from inbound request headers (caller-owned credentials
    the downstream MCP server validates itself), then returns a single JSON
    header for injection into the MCP server configs.
    """
    from src.constants import build_mcp_context_headers

    return build_mcp_context_headers(body.user, request.headers.get)


async def _ensure_response_session_client(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    backend: "BackendClient",
    session,
    session_id: str,
    is_new_session: bool,
    system_prompt: Optional[str],
    workspace_str: str,
    forward_headers: Optional[Dict[str, str]] = None,
) -> None:
    """Create the persistent backend client after session preflight has passed."""
    output_format = _response_output_format(body)
    if session.client is not None:
        _validate_continuation_output_format(session.client, output_format)
        _validate_continuation_reasoning(session.client, _response_reasoning_effort(body))
        await _refresh_existing_client_policy(body, backend, session.client)
        return

    # About to create a FRESH client: any pending AskUserQuestion belonged to
    # the previous (dead) client and can never be answered — a teardown path
    # that skipped _disconnect_session_client may have left it behind.
    _clear_stale_pending_tool_call(session, "client replacement")

    from src.system_prompt import get_system_prompt, resolve_request_placeholders

    if session.base_system_prompt is not None:
        resolved_base = session.base_system_prompt
    else:
        resolved_base = resolve_request_placeholders(get_system_prompt(), workspace_str)
    # ``output_format`` is only passed when set: the route already rejects
    # json_schema for non-claude backends, so this keyword never reaches a
    # backend whose create_client() doesn't accept it.
    create_kwargs: Dict[str, Any] = {}
    if output_format is not None:
        create_kwargs["output_format"] = output_format
    # Like output_format, ``effort`` is claude-only and only passed when set:
    # the route preflight already rejected reasoning.effort for other
    # backends, so this keyword never reaches a create_client() that doesn't
    # accept it.
    reasoning_effort = _response_reasoning_effort(body)
    if reasoning_effort is not None:
        create_kwargs["effort"] = reasoning_effort
    # Per-request MCP header injection (persistent session path). ``user`` is
    # consumed by the claude backend only (system-prompt identity); the resolved
    # context header goes to both claude and codex. opencode's create_client
    # accepts neither, so gate to avoid an unknown-kwarg TypeError.
    if resolved.backend == "claude":
        create_kwargs["user"] = body.user
        # Session-level effort (OpenAI-shaped ``reasoning.effort``). Only the
        # claude backend accepts the kwarg, so gate it like ``user``.
        if body.reasoning and body.reasoning.effort:
            create_kwargs["effort"] = body.reasoning.effort
    if resolved.backend in {"claude", "codex"} and forward_headers:
        create_kwargs["forward_headers"] = forward_headers
    try:
        session.client = await backend.create_client(
            session=session,
            model=resolved.provider_model,
            system_prompt=system_prompt if is_new_session else None,
            permission_mode=body.permission_mode or os.getenv("PERMISSION_MODE") or None,
            allowed_tools=body.allowed_tools,
            disallowed_tools=body.disallowed_tools,
            mcp_servers=(get_mcp_servers() if resolved.backend in {"claude", "codex"} else None),
            cwd=workspace_str,
            extra_env=body.metadata,
            model_params=_response_model_params(body),
            _custom_base=resolved_base,
            **create_kwargs,
        )
    except Exception:
        logger.error("create_client failed", exc_info=True)
        await session_manager.delete_session_async(session_id)
        raise HTTPException(
            status_code=503,
            detail=f"{resolved.backend} backend unavailable; retry shortly",
        )


async def _unblock_pending_tool_call(
    session, backend: "BackendClient", fc_output: Dict[str, str]
) -> None:
    """Validate the function_call_output and reserve the continuation.

    On success the session lock is held by the caller and ``session
    .input_response`` is stashed, but the SDK has NOT been woken yet —
    ``pending_tool_call`` is still set and ``input_event`` has not been
    fired. The caller is expected to call
    :func:`_commit_pending_tool_call_locked` once any follow-up
    invariants (e.g. mid-session policy refresh) have passed. This
    split lets a rejected policy change fail closed before the SDK is
    irreversibly resumed under the old policy.
    """
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

        if session.input_event is None:
            raise HTTPException(
                status_code=400,
                detail="function_call_output received but session has no pending input event",
            )

        session.input_response = fc_output["output"]
    except Exception:
        session.lock.release()
        raise


def _commit_pending_tool_call_locked(session) -> None:
    """Wake the SDK and clear pending state for a Claude continuation.

    Caller must hold ``session.lock``. Pair with
    :func:`_unblock_pending_tool_call` once mid-session policy refresh has
    been accepted.
    """
    pending_event = session.input_event
    if pending_event is not None:
        pending_event.set()
    session.pending_tool_call = None


def _resume_backend_continuation(
    resolved: ResolvedModel,
    backend: "BackendClient",
    active_client: Any,
    session,
    fc_output: Dict[str, str],
    opencode_resume_kind: str,
):
    """Return the backend stream for a function_call_output continuation."""
    backend_with_resume = cast(Any, backend)
    if resolved.backend == "opencode":
        if opencode_resume_kind == "permission":
            return backend_with_resume.resume_permission_with_client(
                active_client,
                fc_output["call_id"],
                fc_output["output"],
                session,
            )
        return backend_with_resume.resume_question_with_client(
            active_client,
            fc_output["call_id"],
            fc_output["output"],
            session,
        )
    if resolved.backend == "codex":
        return backend_with_resume.resume_approval_with_client(
            active_client,
            fc_output["call_id"],
            fc_output["output"],
            session,
        )

    receiver = getattr(backend, "receive_response_from_client", None)
    if receiver is not None:
        return receiver(active_client, session)
    return backend.run_completion_with_client(active_client, "", session)


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
        metadata = cast(
            Dict[str, Any],
            block.get("metadata") if isinstance(block.get("metadata"), dict) else {},
        )
        input_value = cast(
            Dict[str, Any],
            block.get("input") if isinstance(block.get("input"), dict) else {},
        )
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
        metadata = cast(
            Dict[str, Any],
            block.get("metadata") if isinstance(block.get("metadata"), dict) else {},
        )
        request_id = metadata.get("codex_approval_request_id") or block.get("id")
        if str(request_id or "") == str(pending.get("call_id") or ""):
            return True
    return False


async def _capture_pending_tool_questions(chunk_source, resolved: ResolvedModel, session):
    async for chunk in chunk_source:
        # Keep the outbox's active-task registry aware of tasks started
        # mid-turn (they outlive the turn while the idle reader is off).
        session_outbox.apply_turn_task_chunk(session, chunk)
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


async def _collect_non_stream_continuation_chunks(chunk_source):
    chunks = []

    async def _consume():
        async for chunk in chunk_source:
            chunks.append(chunk)

    try:
        await asyncio.wait_for(
            _consume(),
            timeout=NON_STREAM_CONTINUATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        close = getattr(chunk_source, "aclose", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()
        raise HTTPException(
            status_code=504,
            detail=(f"Continuation timed out after {NON_STREAM_CONTINUATION_TIMEOUT_SECONDS:.3g}s"),
        ) from exc
    return chunks


async def _raise_for_error_chunks(
    chunks: List[Any],
    *,
    response_id: str,
    model: str,
    request_context: Dict[str, Any],
    started_monotonic: float,
) -> None:
    """Fail a non-streaming collection on terminal backend error chunks.

    Mirrors the streaming loop's error handling (``classify_error_chunk`` in
    ``streaming_utils.stream_response_chunks``): the same chunks that make a
    stream emit ``response.failed`` — SDK in-band errors, AssistantMessage
    errors, rejected rate limits — raise an HTTPException here, and the turn
    is recorded as failed in the usage log instead of degrading into an
    empty-response 502.
    """
    for chunk in chunks:
        error_info = classify_error_chunk(chunk)
        if error_info is None:
            continue
        logger.error(
            "Responses API non-stream: backend error chunk: %s (code=%s)",
            error_info["message"],
            error_info["code"],
        )
        try:
            await usage_logger.log_turn_from_context(
                request_context=request_context,
                response_id=response_id,
                model=model,
                chunks=chunks,
                tool_stats=None,
                started_monotonic=started_monotonic,
                status="failed",
                error_code=error_info["code"],
            )
        except Exception:
            logger.warning("usage-log emit failed (non-stream)", exc_info=True)
        # Error messages here are SDK-curated (rate-limit, auth, etc.) — not
        # raw Python exception strings — so they are safe to surface to
        # clients. Raw ``except Exception`` leaks are redacted elsewhere.
        status_code = 429 if error_info["code"] == "rate_limit" else 502
        raise HTTPException(
            status_code=status_code,
            detail=f"Backend error: {error_info['message']}",
        )


async def _create_background_response(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    backend: "BackendClient",
    session,
    session_id: str,
    is_new_session: bool,
    prompt: Any,
    system_prompt: Optional[str],
    workspace_str: str,
    forward_headers: Optional[Dict[str, str]],
    request: Optional[Request] = None,
) -> Dict[str, Any]:
    """Accept a background turn: guard, lock, spawn the runner, return queued.

    Mirrors the streaming path's manual lock protocol: preflight acquires
    ``session.lock`` here and the detached runner's ``finally`` releases it,
    so the session serializes turns exactly as it does for connected
    requests. All session-guard errors (stale/future turn, backend mismatch,
    client creation failure) surface synchronously as proper HTTP statuses —
    only backend execution happens after the queued response is returned.
    """
    preflight = await _responses_streaming_preflight(
        body,
        resolved,
        session,
        session_id,
        is_new_session,
        workspace_str=workspace_str,
    )
    try:
        await _ensure_response_session_client(
            body,
            resolved,
            backend,
            session,
            session_id,
            is_new_session,
            system_prompt,
            workspace_str,
            forward_headers=forward_headers,
        )
        next_turn = preflight["next_turn"]
        resp_id = _make_response_id(session_id, next_turn)
        await _begin_active_response(session, resp_id, next_turn, session.client)
    except Exception:
        if preflight["lock_acquired"]:
            session.lock.release()
        raise

    queued = ResponseObject(
        id=resp_id,
        status="queued",
        model=body.model,
        metadata=body.metadata or {},
        background=True,
    )
    # Two separate dumps: the registry copy is mutated by the runner
    # (queued → in_progress) and must not alias the object being returned.
    _register_background_run(resp_id, session_id, queued.model_dump())

    # Take over the admission slot only now: everything above can still raise,
    # and until the runner exists the middleware must stay responsible for
    # releasing it. From here the runner's ``finally`` owns the slot, which is
    # what keeps background turns inside MAX_CONCURRENT_TURNS instead of
    # escaping it the moment the queued response is sent.
    turn_slot = take_turn_slot(request) if request is not None else None

    try:
        task = asyncio.get_running_loop().create_task(
            _run_background_response(
                body,
                resolved,
                backend,
                session,
                session_id,
                resp_id,
                next_turn,
                prompt,
                lock_acquired=preflight["lock_acquired"],
                turn_slot=turn_slot,
            )
        )
    except BaseException:
        # The transfer above already told the middleware not to release, so
        # a failed spawn would strand the slot for the process lifetime with
        # no runner to hand it back. Reachable only on a closing event loop,
        # but a permanent capacity loss is not worth the three lines saved.
        if turn_slot is not None:
            turn_slot.release()
        raise
    _BACKGROUND_RESPONSE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_RESPONSE_TASKS.discard)
    return queued.model_dump()


async def _run_background_response(
    body: ResponseCreateRequest,
    resolved: ResolvedModel,
    backend: "BackendClient",
    session,
    session_id: str,
    resp_id: str,
    next_turn: int,
    prompt: Any,
    *,
    lock_acquired: bool,
    turn_slot: Optional[Any] = None,
) -> None:
    """Detached runner for one background turn.

    Owns the session lock acquired by the accept path and the session's
    active-response slot; both are always released in ``finally``. Terminal
    outcomes mirror the connected paths: success commits the turn exactly
    like the non-stream handler, a cancel (POST .../cancel) commits a
    continuable ``incomplete`` turn like a streamed interrupt, and failures
    leave the turn uncommitted (retry reuses the same previous_response_id)
    but keep a retrievable ``failed`` payload in the run registry.
    """
    import time as _time

    _usage_start = _time.monotonic()
    request_context = {
        "session_id": session_id,
        "user": body.user,
        "backend": resolved.backend,
        "provider_model": resolved.provider_model,
        "previous_response_id": body.previous_response_id,
        "turn": next_turn,
    }
    active_client = session.client
    chunks: List[Any] = []
    terminal_state = "failed"

    async def _log_usage(status: str, error_code: Optional[str] = None) -> None:
        try:
            await usage_logger.log_turn_from_context(
                request_context=request_context,
                response_id=resp_id,
                model=body.model,
                chunks=chunks,
                tool_stats=None,
                started_monotonic=_usage_start,
                status=status,
                error_code=error_code,
            )
        except Exception:
            logger.warning("usage-log emit failed (background)", exc_info=True)

    def _fail(code: str, message: str) -> None:
        failed = _build_failed_response(
            resp_id, body.model, body.metadata, code=code, message=message
        )
        failed.background = True
        _set_background_run_payload(resp_id, failed.model_dump())

    def _commit_user_turn(assistant_message: Optional[Message]) -> None:
        session.turn_counter = next_turn
        session.add_messages([Message(role="user", content=prompt)])
        if assistant_message is not None:
            session_manager.add_assistant_response(session_id, assistant_message)

    try:
        _set_background_run_status(resp_id, "in_progress")
        _configure_client_streaming(active_client, False)

        async def _collect() -> None:
            backend_source = backend.run_completion_with_client(active_client, prompt, session)
            async for chunk in _capture_pending_tool_questions(backend_source, resolved, session):
                chunks.append(chunk)
                # No HTTP connection refreshes this session while the turn
                # runs — keep it alive as long as the backend is producing.
                session.touch()

        try:
            # wait_for (not asyncio.timeout) — the gateway supports 3.10, and
            # its inner task keeps SDK anyio cancel scopes task-local.
            await asyncio.wait_for(_collect(), timeout=_background_response_timeout_s())
        except (TimeoutError, asyncio.TimeoutError):
            logger.error("Background response %s timed out", resp_id)
            _fail("timeout", "Background response timed out")
            await _log_usage("failed", "timeout")
            await _disconnect_session_client(session, "background timeout", client=active_client)
            return

        # Cancel endpoint interrupt → committed, continuable incomplete turn
        # (same semantics as a streamed interrupt).
        if any(isinstance(chunk, dict) and chunk.get("gateway_interrupted") for chunk in chunks):
            assistant_text = backend.parse_message(chunks) or ""
            visible_text, thinking_texts = _split_assistant_text_and_thinking(
                chunks, assistant_text
            )
            prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
                chunks,
                prompt,
                visible_text or "\n".join(thinking_texts),
                body.model,
                backend=backend,
            )
            incomplete_resp = ResponseObject(
                id=resp_id,
                model=body.model,
                status="incomplete",
                output=[
                    OutputItem(
                        id=_generate_msg_id(),
                        status="incomplete",
                        content=[ResponseContentPart(text=visible_text)],
                    )
                ],
                usage=ResponseUsage(
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    input_tokens_details=streaming_utils.resolve_usage_details(chunks),
                ),
                metadata=body.metadata or {},
                incomplete_details=ResponseIncompleteDetails(reason="user_cancelled"),
                background=True,
            )
            _commit_user_turn(
                _build_session_assistant_message(visible_text, thinking_texts)
                or Message(role="assistant", content="")
            )
            _record_turn_response(session, next_turn, incomplete_resp, store=body.store)
            _log_session_assistant_write(
                path="responses.background.interrupted",
                session_id=session_id,
                turn=next_turn,
                visible_text=visible_text,
                thinking_texts=thinking_texts,
            )
            await _log_usage("incomplete", "user_cancelled")
            _drop_background_run(resp_id)
            terminal_state = "incomplete"
            return

        # Terminal backend error chunks → failed, turn uncommitted. The
        # classifier's messages are SDK-curated (rate limit, auth, ...), safe
        # to store for retrieval — same policy as the synchronous path.
        error_info = None
        for chunk in chunks:
            error_info = classify_error_chunk(chunk)
            if error_info is not None:
                break
        if error_info is not None:
            logger.error(
                "Background response %s: backend error chunk: %s (code=%s)",
                resp_id,
                error_info["message"],
                error_info["code"],
            )
            _fail(error_info["code"], f"Backend error: {error_info['message']}")
            await _log_usage("failed", error_info["code"])
            await _disconnect_session_client(
                session, "background backend error", client=active_client
            )
            return

        # SDK paused on AskUserQuestion → requires_action, continuable with a
        # normal (non-background) function_call_output POST.
        if session.pending_tool_call is not None:
            tc = session.pending_tool_call
            requires_action_resp = _build_requires_action_response(
                resp_id, body.model, tc, body.metadata
            )
            requires_action_resp.background = True
            # Like the sync paths: a paused turn commits the user prompt but
            # no assistant message — the answer arrives on the continuation.
            _commit_user_turn(None)
            _record_turn_response(session, next_turn, requires_action_resp, store=body.store)
            _drop_background_run(resp_id)
            terminal_state = "completed"
            return

        assistant_text = backend.parse_message(chunks) or ""
        visible_text, thinking_texts = _split_assistant_text_and_thinking(chunks, assistant_text)
        assistant_message = _build_session_assistant_message(visible_text, thinking_texts)
        if assistant_message is None:
            _fail("server_error", "No response from backend")
            await _log_usage("failed", "empty_response")
            await _disconnect_session_client(
                session, "background empty response", client=active_client
            )
            return

        _commit_user_turn(assistant_message)
        _log_session_assistant_write(
            path="responses.background",
            session_id=session_id,
            turn=next_turn,
            visible_text=visible_text,
            thinking_texts=thinking_texts,
        )
        usage_text = assistant_text or visible_text or "\n".join(thinking_texts)
        prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
            chunks, prompt, usage_text, body.model, backend=backend
        )
        response_obj = _build_completed_response(
            resp_id,
            body.model,
            visible_text,
            body.metadata,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            usage_details=streaming_utils.resolve_usage_details(chunks),
            thinking_texts=thinking_texts,
            structured_output=streaming_utils.extract_structured_output(chunks),
        )
        response_obj.background = True
        _record_turn_response(session, next_turn, response_obj, store=body.store)
        await _log_usage("completed")
        _drop_background_run(resp_id)
        terminal_state = "completed"
    except asyncio.CancelledError:
        # Server shutdown or task cancellation — leave a retrievable failed
        # payload; the uncommitted turn stays continuable from the previous id.
        _fail("server_error", "Background response aborted by server shutdown")
        raise
    except Exception:
        logger.error("Background response %s failed", resp_id, exc_info=True)
        # Never store raw exception text — same redaction policy as the
        # synchronous path (backend errors can leak paths or commands).
        _fail("server_error", "Internal server error")
        await _log_usage("failed", "server_error")
        await _disconnect_session_client(session, "background failure", client=active_client)
    finally:
        try:
            session.touch()
            try:
                await _finish_active_response(session, resp_id, terminal_state)
            except BaseException:
                # Cancelled during teardown — clear the slot synchronously so
                # ``is_expired``'s active-turn pin cannot outlive the run.
                if session.active_response_id == resp_id:
                    session.active_response_state = terminal_state
                    session.active_response_id = None
                    session.active_response_turn = None
                    session.active_response_client = None
                    session.active_response_done.set()
                raise
        finally:
            try:
                if lock_acquired:
                    session.lock.release()
            finally:
                # Held on behalf of this detached run since the accept path
                # handed it over; releasing here is what makes background
                # turns count against MAX_CONCURRENT_TURNS for their real
                # duration rather than just until the queued reply is sent.
                if turn_slot is not None:
                    turn_slot.release()


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
    validate_model_vision_support(body, resolved)
    _validate_output_format_backend(_response_output_format(body), resolved.backend)
    _validate_reasoning_backend(_response_reasoning_effort(body), resolved.backend)

    # Per-request MCP context header (identity + caller-owned credentials).
    # Only the claude and codex backends consume it, so skip the env-read + JSON
    # parse for opencode.
    forward_headers = (
        _collect_mcp_forward_headers(request, body)
        if resolved.backend in {"claude", "codex"}
        else {}
    )

    is_new_session = body.previous_response_id is None
    _validate_response_continuation(body)
    if body.background:
        _validate_background_request(body)
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
        if body.background:
            raise HTTPException(
                status_code=400,
                detail=(
                    "background=true cannot continue a function_call_output "
                    "turn; send the continuation without background."
                ),
            )
        return await _handle_function_call_output(
            body, resolved, backend, session, session_id, workspace_str, fc_output
        )

    # Codex carries multimodal turns as a list of input items rather than a
    # single collapsed text prompt. Build the list directly from the request
    # body so we don't run through the disk-saving image_handler path (which
    # rejects non-``data:`` URLs and would collapse the image into a text
    # placeholder anyway).
    if resolved.backend == "codex" and _has_multimodal_input(body):
        codex_items, system_prompt = _response_input_to_codex_items(body)
        if not codex_items:
            # Defensive — _has_multimodal_input said yes but conversion
            # produced nothing (e.g. an empty image_url). Fail at the API
            # boundary instead of letting the backend reject an empty turn.
            raise HTTPException(
                status_code=400,
                detail="Codex multimodal input produced no usable items",
            )
        prompt: Any = codex_items
    elif resolved.backend == "claude" and _has_multimodal_input(body):
        # Claude carries images as native inline content blocks (issue #140) —
        # no disk round-trip, no <attached_image> placeholder, no Read-tool
        # dependency for the model to see the pixels.
        prompt, system_prompt = _response_prompt_blocks_and_system(body)
        if not prompt:
            # Defensive — same rationale as the Codex branch above.
            raise HTTPException(
                status_code=400,
                detail="Multimodal input produced no usable content blocks",
            )
    else:
        prompt, system_prompt = _response_prompt_and_system(body, workspace)

    # Reject slash-prefixed prompts that would be intercepted by the SDK as
    # unknown skills or run destructive built-ins.  Only applies to the Claude
    # backend; other backends pass through unchanged.
    if isinstance(prompt, str):
        await _validate_backend_prompt(resolved, prompt, workspace_str)
    elif resolved.backend == "claude":
        # Block-mode prompts get the same slash validation on their text.
        await _validate_backend_prompt(
            resolved,
            "\n".join(
                b.get("text", "")
                for b in prompt
                if isinstance(b, dict) and b.get("type") == "text"
            ),
            workspace_str,
        )

    if body.background:
        return await _create_background_response(
            body,
            resolved,
            backend,
            session,
            session_id,
            is_new_session,
            prompt,
            system_prompt,
            workspace_str,
            forward_headers,
            request=request,
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
        try:
            await _ensure_response_session_client(
                body,
                resolved,
                backend,
                session,
                session_id,
                is_new_session,
                system_prompt,
                workspace_str,
                forward_headers=forward_headers,
            )
        except Exception:
            if preflight["lock_acquired"]:
                session.lock.release()
            raise

        next_turn = preflight["next_turn"]
        resp_id = _make_response_id(session_id, next_turn)
        output_item_id = _generate_msg_id()
        try:
            await _begin_active_response(session, resp_id, next_turn, session.client)
        except Exception:
            if preflight["lock_acquired"]:
                session.lock.release()
            raise

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
                    # function_call_output can reference this response_id.
                    # Record the user prompt too — a paused turn still
                    # consumed the user's input, so history must reflect it.
                    session.turn_counter = next_turn
                    session.add_messages([Message(role="user", content=prompt)])
                    _record_turn_response(
                        session, next_turn, requires_action_resp, store=body.store
                    )
                    stream_result["success"] = True
                elif stream_result.get("interrupted"):
                    # A user interrupt is a committed, continuable turn.  The
                    # Claude SDK retained the partial assistant output and the
                    # synthetic interrupt marker in its conversation; mirror
                    # that boundary in the gateway's response-id chain.
                    assistant_text = stream_result.get("assistant_text") or ""
                    assistant_message = _build_session_assistant_message(
                        assistant_text,
                        stream_result.get("thinking_texts"),
                    ) or Message(role="assistant", content="")
                    session.turn_counter = next_turn
                    session.add_messages([Message(role="user", content=prompt)])
                    session_manager.add_assistant_response(session_id, assistant_message)
                    response_obj = stream_result.get("response_obj")
                    if response_obj is not None:
                        _record_turn_response(
                            session,
                            next_turn,
                            response_obj,
                            store=body.store,
                        )
                    _log_session_assistant_write(
                        path="responses.stream.interrupted",
                        session_id=session_id,
                        turn=next_turn,
                        visible_text=assistant_text,
                        thinking_texts=stream_result.get("thinking_texts"),
                    )
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
                    # The client already received response.completed carrying
                    # this resp_id, so the turn MUST be committed even when the
                    # assistant produced no visible text or thinking (e.g. a
                    # thinking block opened with no deltas). Otherwise the
                    # resp_id is uncontinuable: GET 404s and a follow-up using
                    # previous_response_id sees a "future turn".
                    assistant_text = stream_result.get("assistant_text") or ""
                    assistant_message = _build_session_assistant_message(
                        assistant_text,
                        stream_result.get("thinking_texts"),
                    ) or Message(role="assistant", content="")
                    session.turn_counter = next_turn
                    session.add_messages([Message(role="user", content=prompt)])
                    session_manager.add_assistant_response(session_id, assistant_message)
                    _record_stream_turn_response(
                        session,
                        turn=next_turn,
                        response_id=resp_id,
                        model=body.model,
                        stream_result=stream_result,
                        chunks_buffer=chunks_buffer,
                        prompt=prompt,
                        metadata=body.metadata,
                        store=body.store,
                        backend=backend,
                    )
                    _log_session_assistant_write(
                        path="responses.stream",
                        session_id=session_id,
                        turn=next_turn,
                        visible_text=assistant_text,
                        thinking_texts=stream_result.get("thinking_texts"),
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

                async def _teardown():
                    try:
                        if not stream_result.get("success") and not stream_result.get(
                            "interrupted"
                        ):
                            await _disconnect_session_client(
                                session, "responses stream failure", client=active_client
                            )
                        terminal_state = (
                            "completed"
                            if stream_result.get("success")
                            else "incomplete"
                            if stream_result.get("interrupted")
                            else "failed"
                        )
                        await _finish_active_response(session, resp_id, terminal_state)
                    finally:
                        if lock_acquired:
                            session.lock.release()

                await _shielded_stream_teardown(
                    _teardown(), f"stream-teardown:{resp_id}"
                )

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
            await _ensure_response_session_client(
                body,
                resolved,
                backend,
                session,
                session_id,
                is_new_session,
                system_prompt,
                workspace_str,
                forward_headers=forward_headers,
            )
            # Execute backend through the persistent client.
            chunks = []
            active_client = session.client
            _configure_client_streaming(active_client, False)
            backend_source = backend.run_completion_with_client(active_client, prompt, session)
            async for chunk in _capture_pending_tool_questions(backend_source, resolved, session):
                chunks.append(chunk)

            # Check for backend errors — SDK in-band error chunks plus
            # AssistantMessage errors and rejected rate limits, mirroring
            # the streaming loop's failure semantics.
            await _raise_for_error_chunks(
                chunks,
                response_id=_make_response_id(session_id, pf.next_turn),
                model=body.model,
                request_context={
                    "session_id": session_id,
                    "user": body.user,
                    "backend": resolved.backend,
                    "provider_model": resolved.provider_model,
                    "previous_response_id": body.previous_response_id,
                    "turn": pf.next_turn,
                },
                started_monotonic=_usage_start,
            )

            # Check if the SDK paused on AskUserQuestion
            if session.pending_tool_call is not None:
                tc = session.pending_tool_call
                resp_id = _make_response_id(session_id, pf.next_turn)
                # Record the user prompt too — a paused turn still consumed
                # the user's input, so history must reflect it.
                session.turn_counter = pf.next_turn
                session.add_messages([Message(role="user", content=prompt)])
                requires_action_resp = _build_requires_action_response(
                    resp_id, body.model, tc, body.metadata
                )
                _record_turn_response(session, pf.next_turn, requires_action_resp, store=body.store)
                return requires_action_resp.model_dump()

            # Extract assistant text
            assistant_text = backend.parse_message(chunks) or ""
            visible_text, thinking_texts = _split_assistant_text_and_thinking(
                chunks, assistant_text
            )
            assistant_message = _build_session_assistant_message(visible_text, thinking_texts)
            if assistant_message is None:
                raise HTTPException(status_code=502, detail="No response from backend")

            # SUCCESS-ONLY: commit turn counter and session messages
            session.turn_counter = pf.next_turn
            session.add_messages([Message(role="user", content=prompt)])
            session_manager.add_assistant_response(session_id, assistant_message)
            _log_session_assistant_write(
                path="responses.non_stream",
                session_id=session_id,
                turn=pf.next_turn,
                visible_text=visible_text,
                thinking_texts=thinking_texts,
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
    usage_text = assistant_text or visible_text or "\n".join(thinking_texts)
    prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
        chunks, prompt, usage_text, body.model, backend=backend
    )

    # Build response object
    resp_id = _make_response_id(session_id, session.turn_counter)

    response_obj = _build_completed_response(
        resp_id,
        body.model,
        visible_text,
        body.metadata,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        usage_details=streaming_utils.resolve_usage_details(chunks),
        thinking_texts=thinking_texts,
        structured_output=streaming_utils.extract_structured_output(chunks),
    )
    _record_turn_response(session, session.turn_counter, response_obj, store=body.store)

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

    session_outbox.resume_idle_reader(session)
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
    ``call_id``, reserves the backend-specific continuation, and then reads
    the continuation response from the existing backend session client.

    The validation and continuation preparation are performed atomically under the
    session lock to prevent races where concurrent requests could read
    stale ``pending_tool_call`` / backend resume state.
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
        # Claude path: validate + reserve, but DO NOT wake the SDK yet. We
        # need to run policy refresh first so a rejected change can fail
        # closed before the SDK is irreversibly resumed.
        await _unblock_pending_tool_call(session, backend, fc_output)

    # Refresh per-request tool policy on the existing session client *after*
    # validation has accepted the function_call_output, so an invalid
    # continuation can't mutate session state. The prepare/unblock helpers
    # above hand the session lock off to us on success, so any failure here
    # must release it before raising — otherwise the lock leaks for the rest
    # of the session.
    if session.client is not None:
        try:
            _validate_continuation_output_format(session.client, _response_output_format(body))
            _validate_continuation_reasoning(
                session.client, _response_reasoning_effort(body)
            )
            await _refresh_existing_client_policy(body, backend, session.client)
        except Exception:
            if session.lock.locked():
                session.lock.release()
            raise

    # All policy invariants passed — fire the irreversible Claude wake-up now
    # that we know the next turn won't run under a rejected policy. OpenCode
    # and Codex paths already committed their state inside their respective
    # prepare/unblock helpers (their continuations don't carry
    # UnsupportedContinuationPolicy semantics, so there's nothing to roll
    # back).
    if resolved.backend not in {"opencode", "codex"}:
        # The idle reader is gated off for the whole AskUserQuestion pause
        # (pending_tool_call set), so background tasks that kept running have
        # piled their output unread in the SDK stream. Sweep it into the
        # outbox BEFORE waking the SDK — the continuation reader would
        # otherwise drain that backlog into this turn's response. Safe here:
        # the SDK is still parked in the PreToolUse hook, so nothing captured
        # can belong to the continuation.
        await session_outbox.drain_backlog_to_outbox(session, session.client)
        _commit_pending_tool_call_locked(session)

    # --- Stream continuation from the client ---
    next_turn = session.turn_counter + 1
    resp_id = _make_response_id(session_id, next_turn)
    output_item_id = _generate_msg_id()
    active_client = session.client

    if body.stream:
        try:
            await _begin_active_response(session, resp_id, next_turn, active_client)
        except Exception:
            session.lock.release()
            raise

        async def _run_continuation_stream():
            stream_result = {"success": False}
            try:
                chunks_buffer = []
                _configure_client_streaming(active_client, True)
                # Resume the backend-specific pending tool request; do not
                # start a new prompt for continuation turns.
                backend_source = _resume_backend_continuation(
                    resolved,
                    backend,
                    active_client,
                    session,
                    fc_output,
                    opencode_resume_kind,
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
                    _record_turn_response(
                        session, next_turn, requires_action_resp, store=body.store
                    )
                    stream_result["success"] = True
                elif stream_result.get("interrupted"):
                    assistant_text = stream_result.get("assistant_text") or ""
                    assistant_message = _build_session_assistant_message(
                        assistant_text,
                        stream_result.get("thinking_texts"),
                    ) or Message(role="assistant", content="")
                    session.turn_counter = next_turn
                    session_manager.add_assistant_response(session_id, assistant_message)
                    response_obj = stream_result.get("response_obj")
                    if response_obj is not None:
                        _record_turn_response(
                            session,
                            next_turn,
                            response_obj,
                            store=body.store,
                        )
                    _log_session_assistant_write(
                        path="responses.continuation_stream.interrupted",
                        session_id=session_id,
                        turn=next_turn,
                        visible_text=assistant_text,
                        thinking_texts=stream_result.get("thinking_texts"),
                    )
                elif stream_result.get("success"):
                    # Commit the turn on success even when the assistant
                    # produced no visible text or thinking — the client has
                    # already seen response.completed for this resp_id, so it
                    # must remain continuable. (See the matching note in the
                    # primary streaming branch above.)
                    assistant_text = stream_result.get("assistant_text") or ""
                    assistant_message = _build_session_assistant_message(
                        assistant_text,
                        stream_result.get("thinking_texts"),
                    ) or Message(role="assistant", content="")
                    session.turn_counter = next_turn
                    session_manager.add_assistant_response(session_id, assistant_message)
                    _record_stream_turn_response(
                        session,
                        turn=next_turn,
                        response_id=resp_id,
                        model=body.model,
                        stream_result=stream_result,
                        chunks_buffer=chunks_buffer,
                        prompt="",
                        metadata=body.metadata,
                        store=body.store,
                        backend=backend,
                    )
                    _log_session_assistant_write(
                        path="responses.continuation_stream",
                        session_id=session_id,
                        turn=next_turn,
                        visible_text=assistant_text,
                        thinking_texts=stream_result.get("thinking_texts"),
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

                async def _teardown():
                    try:
                        if not stream_result.get("success") and not stream_result.get(
                            "interrupted"
                        ):
                            await _disconnect_session_client(
                                session,
                                "responses continuation stream failure",
                                client=active_client,
                            )
                        terminal_state = (
                            "completed"
                            if stream_result.get("success")
                            else "incomplete"
                            if stream_result.get("interrupted")
                            else "failed"
                        )
                        await _finish_active_response(session, resp_id, terminal_state)
                    finally:
                        session.lock.release()

                await _shielded_stream_teardown(
                    _teardown(), f"continuation-teardown:{resp_id}"
                )

        return StreamingResponse(_run_continuation_stream(), media_type="text/event-stream")

    # --- Non-streaming continuation ---
    continuation_success = False
    try:
        # Resume the backend-specific pending tool request; do not start a
        # new prompt for continuation turns.
        _configure_client_streaming(active_client, False)
        backend_source = _resume_backend_continuation(
            resolved,
            backend,
            active_client,
            session,
            fc_output,
            opencode_resume_kind,
        )
        chunks = await _collect_non_stream_continuation_chunks(
            _capture_pending_tool_questions(backend_source, resolved, session)
        )

        # Check for another pending_tool_call
        if session.pending_tool_call is not None:
            tc = session.pending_tool_call
            session.turn_counter = next_turn
            continuation_success = True
            requires_action_resp = _build_requires_action_response(
                resp_id, body.model, tc, body.metadata
            )
            _record_turn_response(session, next_turn, requires_action_resp, store=body.store)
            return requires_action_resp.model_dump()

        # Check for backend errors — SDK in-band error chunks plus
        # AssistantMessage errors and rejected rate limits, mirroring the
        # streaming continuation's failure semantics.
        await _raise_for_error_chunks(
            chunks,
            response_id=resp_id,
            model=body.model,
            request_context={
                "session_id": session_id,
                "user": body.user,
                "backend": resolved.backend,
                "provider_model": resolved.provider_model,
                "previous_response_id": body.previous_response_id,
                "turn": next_turn,
                "continuation": True,
                "function_call_output_call_id": fc_output["call_id"],
            },
            started_monotonic=_usage_start,
        )

        assistant_text = backend.parse_message(chunks) or ""
        visible_text, thinking_texts = _split_assistant_text_and_thinking(chunks, assistant_text)
        assistant_message = _build_session_assistant_message(visible_text, thinking_texts)
        if assistant_message is None:
            raise HTTPException(status_code=502, detail="No response from backend")

        session.turn_counter = next_turn
        session_manager.add_assistant_response(session_id, assistant_message)
        _log_session_assistant_write(
            path="responses.continuation_non_stream",
            session_id=session_id,
            turn=next_turn,
            visible_text=visible_text,
            thinking_texts=thinking_texts,
        )
        continuation_success = True
    finally:
        if not continuation_success:
            await _disconnect_session_client(
                session, "responses continuation non-stream failure", client=active_client
            )
        session.lock.release()
        session_outbox.resume_idle_reader(session)

    usage_text = assistant_text or visible_text or "\n".join(thinking_texts)
    prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
        chunks, "", usage_text, body.model, backend=backend
    )

    response_obj = _build_completed_response(
        resp_id,
        body.model,
        visible_text,
        body.metadata,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        usage_details=streaming_utils.resolve_usage_details(chunks),
        thinking_texts=thinking_texts,
        structured_output=streaming_utils.extract_structured_output(chunks),
    )
    _record_turn_response(session, next_turn, response_obj, store=body.store)

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


@router.post("/v1/responses/{response_id}/cancel")
@rate_limit_endpoint("responses")
async def cancel_response(
    request: Request,
    response_id: str,
    user: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Interrupt an active streamed or background response, preserving its session.

    The active turn has not yet advanced ``session.turn_counter``, so this
    endpoint intentionally does not use ``_lookup_stored_response``.  It
    resolves the session encoded in the response id, verifies ownership, then
    transitions the separate active-response state machine before sending the
    backend control request. Background turns commit the partial output as a
    continuable ``incomplete`` turn, exactly like a streamed interrupt.
    """
    await verify_api_key(request, credentials)
    parsed = _parse_response_id(response_id)
    if parsed is None:
        raise _response_not_found(response_id)
    session_id, turn = parsed
    session = session_manager.get_session(session_id)
    if session is None or (user is not None and session.user != user):
        raise _response_not_found(response_id)

    async with session.response_control_lock:
        if session.active_response_id != response_id:
            if turn <= session.turn_counter:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": {
                            "message": f"Response '{response_id}' is no longer in progress.",
                            "type": "invalid_request_error",
                            "param": "response_id",
                            "code": "response_not_cancellable",
                        }
                    },
                )
            raise _response_not_found(response_id)

        if session.active_response_state == "cancelling":
            return {
                "id": response_id,
                "object": "response",
                "status": "cancelling",
            }
        if session.active_response_state != "running":
            raise HTTPException(
                status_code=409,
                detail=f"Response '{response_id}' is not running",
            )

        try:
            backend = BackendRegistry.get(session.backend)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="Backend unavailable") from exc
        interrupt_client = getattr(backend, "interrupt_client", None)
        if not callable(interrupt_client):
            raise HTTPException(
                status_code=400,
                detail=f"Backend '{session.backend}' does not support response cancellation",
            )
        active_client = session.active_response_client
        if active_client is None:
            raise HTTPException(status_code=409, detail="Active response has no client")
        session.active_response_state = "cancelling"

    try:
        await asyncio.wait_for(interrupt_client(active_client), timeout=2.0)
    except Exception as exc:
        async with session.response_control_lock:
            if (
                session.active_response_id == response_id
                and session.active_response_state == "cancelling"
            ):
                session.active_response_state = "running"
        logger.error("Response interrupt failed for %s", response_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to interrupt response") from exc

    return {"id": response_id, "object": "response", "status": "cancelling"}


@router.get("/v1/responses/{response_id}")
@rate_limit_endpoint("responses")
async def get_response(
    request: Request,
    response_id: str,
    user: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """OpenAI-compatible retrieve: return the stored response for a past turn.

    Background turns (``background=true``) are served from the in-flight run
    registry while they are ``queued``/``in_progress`` and after a failure;
    once committed they are read from the session's turn store like any other
    turn. Polling a background response refreshes the session TTL, so an
    actively watched run cannot expire out from under its poller.

    Responses are recorded per turn on the in-memory session at commit time,
    so retrieval is subject to the session TTL. Turns created with
    ``store=false`` and sessions rehydrated from the on-disk jsonl transcript
    (which only preserves the turn counter) are not retrievable and return
    404 with an OpenAI-style error body.

    The optional ``user`` query parameter scopes the lookup to the session
    owner: when provided it must match ``session.user`` (mismatches are
    indistinguishable from a missing response); when omitted, API-key auth
    alone grants access, matching GET /v1/sessions semantics.
    """
    await verify_api_key(request, credentials)
    run = _BACKGROUND_RUNS.get(response_id)
    if run is not None:
        run_session = session_manager.get_session(run["session_id"])
        if run_session is None:
            _drop_background_run(response_id)
            raise _response_not_found(response_id)
        if user is not None and run_session.user != user:
            raise _response_not_found(response_id)
        return dict(run["payload"])
    session, _session_id, turn = _lookup_stored_response(response_id, user)
    payload = session.get_turn_response(turn)
    if payload is None:
        raise _response_not_found(response_id)
    return payload


@router.delete("/v1/responses/{response_id}")
@rate_limit_endpoint("responses")
async def delete_response(
    request: Request,
    response_id: str,
    user: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """OpenAI-compatible delete, mapped onto the gateway session model.

    Gateway responses are turns of a chained session, so deleting a single
    mid-chain turn would be destructive and ambiguous (the session history
    and SDK transcript cannot drop one turn). Chosen semantics:

    * Latest turn (``resp_<session>_<turn_counter>``, the same id the 409
      stale-turn recovery message advertises): deletes the entire session —
      SDK client and temp workspace included — consistent with
      ``DELETE /v1/sessions/{session_id}``.
    * Earlier turn: 409 with an error explaining that turn-level deletion is
      not supported and which id is deletable.
    * Unknown/expired session or turn, or ``user`` mismatch: 404.

    Returns the OpenAI deletion acknowledgment
    ``{"id": ..., "object": "response", "deleted": true}``.
    """
    await verify_api_key(request, credentials)
    session, session_id, turn = _lookup_stored_response(response_id, user)
    if turn != session.turn_counter:
        latest_id = _make_response_id(session_id, session.turn_counter)
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "message": (
                        f"Response '{response_id}' is not the latest turn of its "
                        f"session; turn-level deletion is not supported. Delete "
                        f"'{latest_id}' to remove the session and its history."
                    ),
                    "type": "invalid_request_error",
                    "param": "response_id",
                    "code": "response_delete_not_latest",
                }
            },
        )
    await session_manager.delete_session_async(session_id)
    return ResponseDeletedObject(id=response_id).model_dump()
