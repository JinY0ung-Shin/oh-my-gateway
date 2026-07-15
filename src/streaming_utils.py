import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Callable, Dict, Optional

from src.constants import (
    SSE_KEEPALIVE_INTERVAL,
    STREAM_TOOL_PROGRESS,
    SUBAGENT_STREAM_PROGRESS,
    SUBAGENT_STREAM_TEXT,
    SUBAGENT_STREAM_TOOL_BLOCKS,
)
from src.message_adapter import MessageAdapter
from src.response_models import (
    ResponseContentPart,
    OutputItem,
    ReasoningContent,
    ReasoningOutputItem,
    ReasoningSummary,
    ResponseErrorDetail,
    ResponseIncompleteDetails,
    ResponseObject,
    ResponseUsage,
)
from src.tool_stats import ToolStatsCollector
from src.usage_logger import usage_logger

# Backward-compat re-exports from split modules.
# External callers continue to use `from src.streaming_utils import X`.
from src.collab_filter import CollabJsonStreamFilter, strip_collab_json  # noqa: F401
from src.sse_builders import (  # noqa: F401
    _build_progress_event,
    _build_task_event,
    _normalize_tool_result,
    make_function_call_response_sse,
    make_response_sse,
    make_task_response_sse,
    make_tool_result_response_sse,
    make_tool_use_response_sse,
    make_tool_use_started_response_sse,
)
from src.chunk_processing import (  # noqa: F401
    _extract_rate_limit_status,
    _extract_tool_blocks,
    _filter_tool_blocks,
    ToolUseAccumulator,
    classify_error_chunk,
    extract_embedded_tool_blocks,
    extract_stream_event_delta,
    extract_user_tool_results,
    format_chunk_content,
    is_assistant_content_chunk,
    process_chunk_content,
)


_ASK_USER_RESPONSE_PREFIX = "User responded:"


# ---------------------------------------------------------------------------
# Error-logging helpers
# ---------------------------------------------------------------------------


def _block_field(b: Any, key: str) -> Any:
    """Read a field from either a dict block or an SDK dataclass block."""
    if isinstance(b, dict):
        return b.get(key)
    return getattr(b, key, None)


def _block_summary(b: Any) -> Dict[str, Any]:
    """Compact summary of a single content block (text/tool_use/tool_result/thinking)."""
    btype = _block_field(b, "type") or type(b).__name__
    out: Dict[str, Any] = {"type": btype}

    text = _block_field(b, "text")
    if text is not None:
        out["text"] = str(text)[:200]

    name = _block_field(b, "name")
    if name:
        out["name"] = name

    tool_use_id = _block_field(b, "tool_use_id")
    if tool_use_id:
        out["tool_use_id"] = tool_use_id
        is_err = _block_field(b, "is_error")
        if is_err is not None:
            out["is_error"] = is_err
        content = _block_field(b, "content")
        if isinstance(content, str):
            out["content"] = content[:200]
        elif isinstance(content, list):
            out["content_blocks"] = len(content)

    thinking = _block_field(b, "thinking")
    if thinking:
        out["thinking"] = str(thinking)[:120]

    return out


def _chunk_summary(chunk: Any) -> Dict[str, Any]:
    """Compact, debug-friendly snapshot of an SDK chunk for error logs.

    Captures fields that help diagnose `error="unknown"` and similar opaque
    failures: model, stop_reason, usage, session/message ids, content blocks
    (including text/tool_use/tool_result details), and any embedded error
    or result text. Values are truncated so a single log line stays readable.
    """
    if not isinstance(chunk, dict):
        return {"repr": repr(chunk)[:200]}

    content = chunk.get("content")
    if isinstance(content, list):
        content_summary: Any = [_block_summary(b) for b in content]
    elif isinstance(content, str):
        content_summary = f"str(len={len(content)})"
    else:
        content_summary = None

    result = chunk.get("result")
    if isinstance(result, str) and result:
        result = result[:200]

    return {
        k: v
        for k, v in {
            "type": chunk.get("type"),
            "subtype": chunk.get("subtype"),
            "error": chunk.get("error"),
            "is_error": chunk.get("is_error"),
            "error_message": chunk.get("error_message"),
            "errors": chunk.get("errors"),
            "model": chunk.get("model"),
            "message_id": chunk.get("message_id"),
            "stop_reason": chunk.get("stop_reason"),
            "usage": chunk.get("usage"),
            "session_id": chunk.get("session_id"),
            "uuid": chunk.get("uuid"),
            "parent_tool_use_id": chunk.get("parent_tool_use_id"),
            "content": content_summary,
            "result": result,
        }.items()
        if v is not None
    }


def _buffer_summary(buf: list, limit: int = 5) -> list:
    """Compact summary of the last N chunks seen before a failure.

    Includes each chunk's content-block shape (up to 3 blocks with
    type/name/is_error) so tool-use loops are legible at a glance.
    """
    summary = []
    for c in buf[-limit:]:
        if not isinstance(c, dict):
            summary.append({"repr": type(c).__name__})
            continue
        entry = {
            k: v
            for k, v in {
                "type": c.get("type"),
                "subtype": c.get("subtype"),
                "error": c.get("error"),
                "is_error": c.get("is_error"),
            }.items()
            if v is not None
        }
        content = c.get("content")
        if isinstance(content, list) and content:
            entry["blocks"] = [
                {k: v for k, v in _block_summary(b).items() if k in ("type", "name", "is_error")}
                for b in content[:3]
            ]
        summary.append(entry)
    return summary


# ---------------------------------------------------------------------------
# Usage & stop-reason helpers
# ---------------------------------------------------------------------------


def extract_sdk_usage(chunks: list) -> Optional[Dict[str, int]]:
    """Extract real token usage from SDK messages if available.

    Prefers ResultMessage.usage (final totals).  Falls back to summing
    per-turn AssistantMessage.usage (available since SDK 0.1.49).

    Returns dict with prompt_tokens, completion_tokens, total_tokens or None.
    """
    # Primary: ResultMessage usage (cumulative totals)
    for msg in reversed(chunks):
        if isinstance(msg, dict) and msg.get("type") == "result" and msg.get("usage"):
            usage = msg["usage"]
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            output_tokens = usage.get("output_tokens", 0)
            return {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }

    # Fallback: sum per-turn usage from AssistantMessage (SDK 0.1.49+)
    total_input = 0
    total_output = 0
    found_any = False
    for msg in chunks:
        if isinstance(msg, dict) and msg.get("type") == "assistant" and msg.get("usage"):
            found_any = True
            usage = msg["usage"]
            total_input += (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            total_output += usage.get("output_tokens", 0)
    if found_any:
        return {
            "prompt_tokens": total_input,
            "completion_tokens": total_output,
            "total_tokens": total_input + total_output,
        }

    return None


def extract_context_tokens(chunks: list) -> Optional[int]:
    """Context-window occupancy of the turn's *last* model call, if known.

    ``extract_sdk_usage`` reports run-cumulative totals — an agentic turn
    with many tool calls sums every call's input (cache reads repeatedly),
    which wildly overstates how full the context window is. For a gauge we
    want the last AssistantMessage's usage: its input side (uncached +
    cache-created + cache-read) is exactly the prompt that call carried,
    plus its output joins the context for the next call.
    """
    for msg in reversed(chunks):
        if isinstance(msg, dict) and msg.get("type") == "assistant" and msg.get("usage"):
            usage = msg["usage"]
            return (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("output_tokens", 0)
            )
    return None


def extract_structured_output(chunks: list) -> Any:
    """Return ``ResultMessage.structured_output`` from SDK chunks, if present.

    Mirrors ``extract_sdk_usage``: when the session was created with
    ``output_format={"type": "json_schema", ...}``, the Claude SDK attaches
    the parsed structured-output payload to the final ResultMessage.
    Returns ``None`` when no result chunk carries it.
    """
    for msg in reversed(chunks):
        if isinstance(msg, dict) and msg.get("type") == "result":
            value = msg.get("structured_output")
            if value is not None:
                return value
    return None


def extract_thinking_texts(chunks: list) -> list[str]:
    """Return thinking-block texts in the order they appear in the chunk list.

    Walks ``assistant`` chunks and pulls out every ``ThinkingBlock``'s text.
    Tolerates both SDK dataclass objects (``block.thinking``) and dict form
    (``{"type": "thinking", "thinking": "..."}``).
    """
    out: list[str] = []
    for chunk in chunks:
        content = chunk.get("content") if isinstance(chunk, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            text = None
            if hasattr(block, "thinking"):
                text = getattr(block, "thinking", None)
            elif isinstance(block, dict) and block.get("type") == "thinking":
                text = block.get("thinking")
            if text:
                out.append(text)
    return out


def extract_visible_assistant_text(chunks: list) -> Optional[str]:
    """Return the visible assistant text from SDK chunks, excluding thinking blocks.

    Matches ``backend.parse_message()``'s join structure exactly:
    - prefer ``ResultMessage.result`` when present
    - otherwise, for each assistant chunk, filter out ``ThinkingBlock`` entries
      from its content list and pass the rest to ``MessageAdapter.format_blocks``
      (which concatenates with no separator)
    - join the per-chunk strings with ``"\\n"``

    The only behavioral difference from ``parse_message`` is that ThinkingBlock
    contents do not appear as ``<think>...</think>`` text in the result.
    """
    # First pass: prefer ResultMessage.result, as SDK collapses content to it.
    result_text: Optional[str] = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("subtype") == "success" and "result" in chunk:
            r = chunk["result"]
            if r and r.strip():
                result_text = r
    if result_text is not None:
        return result_text

    # Second pass: per-message, filter out ThinkingBlocks then format_blocks.
    all_parts: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        content = chunk.get("content")
        if not isinstance(content, list):
            inner = chunk.get("message")
            if isinstance(inner, dict):
                content = inner.get("content")
        if not isinstance(content, list):
            continue

        filtered = []
        for block in content:
            is_thinking = False
            if hasattr(block, "thinking") and not hasattr(block, "text"):
                is_thinking = True
            elif isinstance(block, dict) and block.get("type") == "thinking":
                is_thinking = True
            if not is_thinking:
                filtered.append(block)

        if not filtered:
            continue
        formatted = MessageAdapter.format_blocks(filtered)
        if formatted:
            all_parts.append(formatted)

    return "\n".join(all_parts) if all_parts else None


def resolve_token_usage(
    chunks: list,
    prompt: str,
    completion_text: str,
    model: str = "",
    *,
    backend=None,
) -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) from SDK usage or estimation.

    If *backend* is provided, uses ``backend.estimate_token_usage`` as
    fallback.  Otherwise falls back to character-based estimation via
    ``MessageAdapter.estimate_tokens``.
    """
    sdk_usage = extract_sdk_usage(chunks)
    if sdk_usage:
        return sdk_usage["prompt_tokens"], sdk_usage["completion_tokens"]
    if backend is not None:
        est = backend.estimate_token_usage(prompt, completion_text, model)
        return est["prompt_tokens"], est["completion_tokens"]
    return MessageAdapter.estimate_tokens(prompt), MessageAdapter.estimate_tokens(completion_text)


# ---------------------------------------------------------------------------
# SSE bridge helper
# ---------------------------------------------------------------------------


async def bridge_sse_stream(
    sse_source: AsyncGenerator[str, None],
    chunk_source,
) -> AsyncGenerator[str, None]:
    """Bridge an SSE async generator through a background asyncio task.

    Runs *sse_source* in a dedicated task, forwarding lines through a
    queue.  This keeps anyio cancel scopes task-local when Starlette
    closes the response generator from a different ASGI task during
    teardown.

    *chunk_source* is closed in the ``finally`` block of the reader task
    so that the SDK subprocess is cleaned up regardless of cancellation.
    """
    _SENTINEL = object()
    sse_queue: asyncio.Queue = asyncio.Queue()

    async def _reader():
        try:
            async for line in sse_source:
                await sse_queue.put(("sse", line))
        except Exception as exc:
            await sse_queue.put(("error", exc))
        finally:
            try:
                await chunk_source.aclose()
            except Exception:
                pass  # generator already running/closed or subprocess dead
            await sse_queue.put(("done", _SENTINEL))

    reader_task = asyncio.create_task(_reader())
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


# ---------------------------------------------------------------------------
# SSE keepalive
# ---------------------------------------------------------------------------

# SSE comment line — compliant clients silently ignore these.
_SSE_KEEPALIVE = ": keepalive\n\n"


async def _keepalive_wrapper(
    source: AsyncGenerator,
    interval: int,
) -> AsyncGenerator:
    """Wrap *source* to yield ``_SSE_KEEPALIVE`` during idle periods.

    When the underlying async generator produces no item for *interval*
    seconds, a keepalive SSE comment is yielded instead.  This prevents
    HTTP intermediaries and client-side read timeouts from killing the
    connection while the SDK is busy (tool execution, context compaction).

    If *interval* is ``<= 0`` keepalives are disabled and the source is
    yielded through unchanged.

    The source generator is iterated inside a **single dedicated task** so
    that anyio cancel scopes within the SDK never cross task boundaries.
    Items are bridged to this generator via an ``asyncio.Queue``; when the
    queue is empty for ``interval`` seconds a keepalive is emitted instead.
    """
    if interval <= 0:
        async for item in source:
            yield item
        return

    _SENTINEL = object()
    queue: asyncio.Queue = asyncio.Queue()

    async def _reader():
        """Iterate *source* entirely within one task (cancel-scope safe)."""
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:
            await queue.put(exc)
        finally:
            try:
                await source.aclose()
            except Exception:
                pass  # generator already closed or subprocess dead
            await queue.put(_SENTINEL)

    task = asyncio.create_task(_reader())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield _SSE_KEEPALIVE
                continue

            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Responses API streaming (/v1/responses)
# ---------------------------------------------------------------------------


def _record_tool_use(tool_stats: ToolStatsCollector, tool_block: Dict[str, Any]) -> None:
    tool_stats.record_use(
        _block_field(tool_block, "id") or _block_field(tool_block, "tool_use_id"),
        _block_field(tool_block, "name") or "",
    )


def _record_tool_result(tool_stats: ToolStatsCollector, tool_block: Dict[str, Any]) -> None:
    tool_stats.record_result(
        _block_field(tool_block, "tool_use_id"),
        bool(_block_field(tool_block, "is_error") or False),
    )


def _remember_tool_use(tool_names_by_id: Dict[str, str], tool_block: Dict[str, Any]) -> None:
    tool_use_id = _block_field(tool_block, "id") or _block_field(tool_block, "tool_use_id")
    name = _block_field(tool_block, "name")
    if isinstance(tool_use_id, str) and tool_use_id and isinstance(name, str) and name:
        tool_names_by_id[tool_use_id] = name


def _generate_rs_id() -> str:
    import uuid

    return f"rs_{uuid.uuid4().hex[:24]}"


def _generate_msg_id() -> str:
    import uuid

    return f"msg_{uuid.uuid4().hex[:24]}"


def _is_synthetic_ask_user_response_result(
    result_block: Dict[str, Any],
    tool_names_by_id: Dict[str, str],
    request_context: Optional[Dict[str, Any]],
) -> bool:
    content = result_block.get("content")
    if not isinstance(content, str) or not content.startswith(_ASK_USER_RESPONSE_PREFIX):
        return False

    tool_use_id = result_block.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return False

    if tool_names_by_id.get(tool_use_id) == "AskUserQuestion":
        return True

    if not isinstance(request_context, dict):
        return False
    return request_context.get("function_call_output_call_id") == tool_use_id


def _tool_use_started_events(
    chunk: Dict[str, Any],
    next_seq: Callable[[], int],
) -> list[str]:
    """Emit a ``response.tool_use_started`` signal at the tool_use block start.

    Fires once per tool call at ``content_block_start`` — before its JSON
    arguments finish streaming — so a UI can show "preparing <tool>…" during
    long argument generation instead of a silent gap. The matching
    ``response.tool_use`` (same ``tool_use_id``) arrives once the input is
    complete. Honours STREAM_TOOL_PROGRESS and the subagent tool-block gate.
    """
    if not STREAM_TOOL_PROGRESS:
        return []
    if chunk.get("type") != "stream_event":
        return []
    event = chunk.get("event")
    if not isinstance(event, dict) or event.get("type") != "content_block_start":
        return []
    block = event.get("content_block")
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return []
    parent_id = chunk.get("parent_tool_use_id")
    if parent_id is not None and not SUBAGENT_STREAM_TOOL_BLOCKS:
        return []
    return [
        make_tool_use_started_response_sse(
            block.get("id", ""),
            block.get("name", ""),
            sequence_number=next_seq(),
            parent_tool_use_id=parent_id,
        )
    ]


def _tool_use_events(
    tool_block: Dict[str, Any],
    tool_stats: ToolStatsCollector,
    next_seq: Callable[[], int],
    tool_names_by_id: Dict[str, str],
) -> list[str]:
    _remember_tool_use(tool_names_by_id, tool_block)
    _record_tool_use(tool_stats, tool_block)
    is_subagent_tool = tool_block.get("parent_tool_use_id") is not None
    if is_subagent_tool and not SUBAGENT_STREAM_TOOL_BLOCKS:
        return []
    return [
        make_tool_use_response_sse(
            tool_block,
            sequence_number=next_seq(),
            parent_tool_use_id=tool_block.get("parent_tool_use_id"),
        )
    ]


def _user_tool_result_events(
    chunk: Dict[str, Any],
    tool_stats: ToolStatsCollector,
    next_seq: Callable[[], int],
    tool_names_by_id: Dict[str, str],
    request_context: Optional[Dict[str, Any]],
) -> list[str]:
    tool_results, parent_id = extract_user_tool_results(chunk)
    normalized_results = [_normalize_tool_result(tr_block) for tr_block in tool_results]
    visible_results = []
    for tr_block in normalized_results:
        if _is_synthetic_ask_user_response_result(tr_block, tool_names_by_id, request_context):
            tr_block["is_error"] = False
            _record_tool_result(tool_stats, tr_block)
            continue
        _record_tool_result(tool_stats, tr_block)
        visible_results.append(tr_block)

    is_subagent_result = parent_id is not None
    if is_subagent_result and not SUBAGENT_STREAM_TOOL_BLOCKS:
        return []
    return [
        make_tool_result_response_sse(
            tr_block,
            sequence_number=next_seq(),
            parent_tool_use_id=parent_id,
        )
        for tr_block in visible_results
    ]


def _embedded_tool_events(
    chunk: Dict[str, Any],
    tool_stats: ToolStatsCollector,
    next_seq: Callable[[], int],
    tool_names_by_id: Dict[str, str],
    request_context: Optional[Dict[str, Any]],
) -> list[str]:
    events = []
    for tool_block in extract_embedded_tool_blocks(chunk):
        block_type = _block_field(tool_block, "type")
        result_block = None
        suppress_result = False
        if block_type == "tool_use":
            _remember_tool_use(tool_names_by_id, tool_block)
            _record_tool_use(tool_stats, tool_block)
        elif block_type == "tool_result":
            result_block = _normalize_tool_result(tool_block)
            suppress_result = _is_synthetic_ask_user_response_result(
                result_block, tool_names_by_id, request_context
            )
            if suppress_result:
                result_block["is_error"] = False
            _record_tool_result(tool_stats, result_block)
        elif block_type == "server_tool_use":
            _remember_tool_use(tool_names_by_id, tool_block)
            _record_tool_use(tool_stats, tool_block)
        elif block_type == "advisor_tool_result":
            _record_tool_result(tool_stats, tool_block)

        is_subagent_block = tool_block.get("parent_tool_use_id") is not None
        if is_subagent_block and not SUBAGENT_STREAM_TOOL_BLOCKS:
            continue
        if block_type == "tool_use":
            events.append(
                make_tool_use_response_sse(
                    tool_block,
                    sequence_number=next_seq(),
                    parent_tool_use_id=tool_block.get("parent_tool_use_id"),
                )
            )
        elif block_type == "tool_result":
            if suppress_result:
                continue
            events.append(
                make_tool_result_response_sse(
                    result_block if result_block is not None else tool_block,
                    sequence_number=next_seq(),
                    parent_tool_use_id=tool_block.get("parent_tool_use_id"),
                )
            )
        elif block_type in {"server_tool_use", "advisor_tool_result"}:
            events.append(
                make_response_sse(
                    f"response.{block_type}",
                    block=tool_block,
                    sequence_number=next_seq(),
                )
            )
    return events


async def stream_response_chunks(
    chunk_source,
    model: str,
    response_id: str,
    output_item_id: str,
    chunks_buffer: list,
    logger: logging.Logger,
    prompt_text: str = "",
    metadata: Optional[Dict[str, str]] = None,
    stream_result: Optional[Dict[str, Any]] = None,
    request_context: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[str, None]:
    """SSE streaming logic for /v1/responses (OpenAI Responses API).

    Emits proper SSE events per OpenAI Responses API spec:
    response.created → response.in_progress → response.output_item.added →
    response.content_part.added → response.output_text.delta (repeated) →
    response.output_text.done → response.content_part.done →
    response.output_item.done → response.completed

    On SDK error or failure: emits response.failed instead of response.completed.
    Sets stream_result["success"] to indicate outcome to caller.
    """
    content_sent = False
    token_streaming = False
    in_thinking = False
    tool_acc = ToolUseAccumulator()
    collab_filter = CollabJsonStreamFilter()
    full_text = []
    # Accumulates *all* visible text across every message segment.  ``full_text``
    # is reset at each segment boundary (think→text→think), so this is the
    # source of truth for the complete assistant text handed back to the route.
    all_visible_text: list[str] = []
    seq = 0
    # Index of the next text annotation (citation) within the open message
    # item's content part.  Reset whenever a message item closes.
    annotation_index = 0
    message_item_opened = False
    # True once *any* message output item has been opened (never reset), so
    # finalization can tell "thinking-only response" (needs a trailing empty
    # message item) apart from "stream ended after a real message segment".
    any_message_item = False
    output_index = 0
    reasoning_open = False
    reasoning_item_id: Optional[str] = None
    reasoning_text_buf: list[str] = []
    thinking_seen = False
    thinking_texts: list[str] = []
    thinking_capture_buf: list[str] = []
    # Every completed output item (reasoning and message) in emission order.
    # Interleaving is allowed, so this is no longer reasoning-only.
    completed_output_items: list = []
    _metadata = metadata or {}
    if stream_result is None:
        stream_result = {}

    # Usage-log state.  ``usage_start`` measures wall duration; ``tool_stats``
    # aggregates tool-call name/count/errors/latency for the usage_tool table.
    usage_start = time.monotonic()
    tool_stats = ToolStatsCollector()
    tool_names_by_id: Dict[str, str] = {}

    def _next_seq() -> int:
        nonlocal seq
        current = seq
        seq += 1
        return current

    def _error_context() -> str:
        ctx = dict(request_context or {})
        ctx.setdefault("response_id", response_id)
        ctx["prompt_preview"] = (prompt_text or "")[:200]
        return " ".join(f"{k}={v!r}" for k, v in ctx.items() if v is not None)

    def _make_failed_event(error_code: str, error_msg: str) -> str:
        failed_resp = ResponseObject(
            id=response_id,
            model=model,
            status="failed",
            metadata=_metadata,
            error=ResponseErrorDetail(code=error_code, message=error_msg),
        )
        return make_response_sse(
            "response.failed", response_obj=failed_resp, sequence_number=_next_seq()
        )

    async def _log_usage(status: str, error_code: Optional[str] = None) -> None:
        """Best-effort usage-log write.  Never raises."""
        try:
            await usage_logger.log_turn_from_context(
                request_context=request_context,
                response_id=response_id,
                model=model,
                chunks=chunks_buffer,
                tool_stats=tool_stats.snapshot(),
                started_monotonic=usage_start,
                status=status,
                error_code=error_code,
            )
        except Exception:
            logger.warning("usage-log emit failed", exc_info=True)

    # --- Preamble: emit opening events ---

    # 1. response.created
    resp_in_progress = ResponseObject(
        id=response_id, model=model, status="in_progress", metadata=_metadata
    )
    yield make_response_sse(
        "response.created", response_obj=resp_in_progress, sequence_number=_next_seq()
    )

    # 2. response.in_progress
    yield make_response_sse(
        "response.in_progress", response_obj=resp_in_progress, sequence_number=_next_seq()
    )

    # NOTE: response.output_item.added + response.content_part.added for the
    # message item are DEFERRED until the first text delta arrives (or the
    # stream closes without text). This leaves room for reasoning output
    # items to be emitted before the message item in future tasks.

    def _open_message_item() -> list[str]:
        """Emit message output_item.added + content_part.added. Idempotent."""
        nonlocal message_item_opened, any_message_item
        if message_item_opened:
            return []
        message_item_opened = True
        any_message_item = True
        out_item = OutputItem(id=output_item_id, status="in_progress")
        content_part = ResponseContentPart(type="output_text", text="")
        return [
            make_response_sse(
                "response.output_item.added",
                output_index=output_index,
                item=out_item,
                sequence_number=_next_seq(),
            ),
            make_response_sse(
                "response.content_part.added",
                item_id=output_item_id,
                output_index=output_index,
                content_index=0,
                part=content_part,
                sequence_number=_next_seq(),
            ),
        ]

    def _emit_delta(text: str) -> str:
        return make_response_sse(
            "response.output_text.delta",
            item_id=output_item_id,
            output_index=output_index,
            content_index=0,
            delta=text,
            logprobs=[],
            sequence_number=_next_seq(),
        )

    def _close_thinking_capture() -> None:
        nonlocal thinking_capture_buf
        full_text = "".join(thinking_capture_buf)
        if full_text:
            thinking_texts.append(full_text)
            logger.info(
                "Responses stream captured thinking block: response_id=%s block_index=%d chars=%d",
                response_id,
                len(thinking_texts) - 1,
                len(full_text),
            )
        thinking_capture_buf = []

    def _close_reasoning(status: str = "completed") -> list[str]:
        """Emit the four close events for the open reasoning item, bump output_index."""
        nonlocal reasoning_open, reasoning_item_id, reasoning_text_buf, output_index
        if not reasoning_open:
            return []
        assert reasoning_item_id is not None
        full_text = "".join(reasoning_text_buf)
        item = ReasoningOutputItem(
            id=reasoning_item_id,
            status=status,
            summary=[ReasoningSummary(text=full_text)],
            content=[ReasoningContent(text=full_text)],
        )
        completed_output_items.append(item)
        lines = [
            make_response_sse(
                "response.reasoning_summary_text.done",
                item_id=reasoning_item_id,
                output_index=output_index,
                summary_index=0,
                text=full_text,
                sequence_number=_next_seq(),
            ),
            make_response_sse(
                "response.reasoning_text.done",
                item_id=reasoning_item_id,
                output_index=output_index,
                content_index=0,
                text=full_text,
                sequence_number=_next_seq(),
            ),
            make_response_sse(
                "response.reasoning_summary_part.done",
                item_id=reasoning_item_id,
                output_index=output_index,
                summary_index=0,
                part={"type": "summary_text", "text": full_text},
                sequence_number=_next_seq(),
            ),
            make_response_sse(
                "response.output_item.done",
                output_index=output_index,
                item=item,
                sequence_number=_next_seq(),
            ),
        ]
        reasoning_open = False
        reasoning_item_id = None
        reasoning_text_buf = []
        output_index += 1
        return lines

    def _close_message_item(status: str = "completed") -> list[str]:
        """Close the currently-open message item and record it.

        Flushes the collab filter into the segment, emits
        output_text.done / content_part.done / output_item.done, appends the
        completed item to ``completed_output_items`` (preserving emission
        order), then resets the per-segment state, advances ``output_index``,
        and mints a fresh ``output_item_id`` so a following reasoning or
        message item gets its own slot.  No-op when no message item is open.

        This is what makes interleaved think→text→think→text turns work: each
        text run becomes its own message item rather than the second thinking
        block being dropped because reasoning can't reopen after text started.
        """
        nonlocal message_item_opened, full_text, output_index, output_item_id
        nonlocal content_sent, annotation_index
        if not message_item_opened:
            return []
        lines: list[str] = []
        remaining = collab_filter.flush()
        if remaining:
            lines.append(_emit_delta(remaining))
            full_text.append(remaining)
            all_visible_text.append(remaining)
            content_sent = True
        seg_text = "".join(full_text)
        item = OutputItem(
            id=output_item_id,
            status=status,
            content=[ResponseContentPart(text=seg_text)],
        )
        lines.append(
            make_response_sse(
                "response.output_text.done",
                item_id=output_item_id,
                output_index=output_index,
                content_index=0,
                text=seg_text,
                logprobs=[],
                sequence_number=_next_seq(),
            )
        )
        lines.append(
            make_response_sse(
                "response.content_part.done",
                item_id=output_item_id,
                output_index=output_index,
                content_index=0,
                part=ResponseContentPart(text=seg_text),
                sequence_number=_next_seq(),
            )
        )
        lines.append(
            make_response_sse(
                "response.output_item.done",
                output_index=output_index,
                item=item,
                sequence_number=_next_seq(),
            )
        )
        completed_output_items.append(item)
        message_item_opened = False
        full_text = []
        annotation_index = 0
        output_index += 1
        output_item_id = _generate_msg_id()
        return lines

    # --- Main streaming loop ---

    try:
        async for chunk in _keepalive_wrapper(chunk_source, SSE_KEEPALIVE_INTERVAL):
            # Keepalive SSE comments — forward directly to the client
            if chunk is _SSE_KEEPALIVE:
                yield _SSE_KEEPALIVE
                continue

            # ClaudeSDKClient.interrupt() ends the active SDK turn with an
            # is_error ResultMessage.  The backend marks that result only when
            # it corresponds to an explicit gateway cancel request, allowing
            # us to preserve partial output as an incomplete turn instead of
            # misclassifying it as a backend failure.
            if isinstance(chunk, dict) and chunk.get("gateway_interrupted"):
                chunks_buffer.append(chunk)
                _close_thinking_capture()
                if reasoning_open:
                    for line in _close_reasoning(status="incomplete"):
                        yield line
                if message_item_opened:
                    for line in _close_message_item(status="incomplete"):
                        yield line

                partial_text = "".join(all_visible_text)
                prompt_tokens, completion_tokens = resolve_token_usage(
                    chunks_buffer, prompt_text or "", partial_text
                )
                incomplete_resp = ResponseObject(
                    id=response_id,
                    model=model,
                    status="incomplete",
                    output=list(completed_output_items),
                    usage=ResponseUsage(
                        input_tokens=prompt_tokens,
                        output_tokens=completion_tokens,
                    ),
                    metadata=_metadata,
                    incomplete_details=ResponseIncompleteDetails(reason="user_cancelled"),
                )
                stream_result["success"] = False
                stream_result["interrupted"] = True
                stream_result["assistant_text"] = partial_text
                stream_result["thinking_texts"] = thinking_texts
                stream_result["response_obj"] = incomplete_resp
                yield make_response_sse(
                    "response.incomplete",
                    response_obj=incomplete_resp,
                    sequence_number=_next_seq(),
                )
                await _log_usage("incomplete", "user_cancelled")
                return

            # Detect terminal error chunks: SDK in-band errors (is_error),
            # AssistantMessage.error (auth failures, rate limits, etc.) and
            # rejected SDK rate-limit events.  Classification is shared with
            # the non-streaming collection paths (classify_error_chunk) so
            # both report identical failure semantics.
            error_info = classify_error_chunk(chunk)
            if error_info is not None:
                if chunk.get("type") == "assistant":
                    chunks_buffer.append(chunk)
                logger.error(
                    "Responses stream: %s (code=%s) | chunk=%r | prior_chunks=%r | %s",
                    error_info["message"],
                    error_info["code"],
                    _chunk_summary(chunk),
                    _buffer_summary(chunks_buffer),
                    _error_context(),
                )
                stream_result["success"] = False
                yield _make_failed_event(error_info["code"], error_info["message"])
                await _log_usage("failed", error_info["code"])
                return

            # Non-rejected SDK rate-limit events (new in SDK 0.1.49) are
            # informational only — rejected ones fail above.
            if chunk.get("type") == "rate_limit":
                logger.warning("SDK rate limit event: status=%s", _extract_rate_limit_status(chunk))
                continue

            # Handle task system messages (structured JSON, not content)
            if chunk.get("type") == "system":
                # Subagent task messages identify their spawning Task tool via
                # ``tool_use_id``. Hook events also carry ``tool_use_id``, but
                # that is the hook's target tool, not a parent/nesting marker.
                task_event = _build_task_event(chunk)
                if task_event:
                    is_subagent_task = (
                        chunk.get("parent_tool_use_id") is not None
                        or chunk.get("tool_use_id") is not None
                    )
                    if not is_subagent_task or SUBAGENT_STREAM_PROGRESS:
                        yield make_task_response_sse(task_event, sequence_number=_next_seq())
                else:
                    # Other liveness signals: hook lifecycle + compaction.
                    # For these, only an explicit parent_tool_use_id marks
                    # subagent origin. A plain tool_use_id is the target tool.
                    is_subagent_progress = chunk.get("parent_tool_use_id") is not None
                    if not is_subagent_progress or SUBAGENT_STREAM_PROGRESS:
                        progress_event = _build_progress_event(chunk)
                        if progress_event:
                            yield make_task_response_sse(
                                progress_event, sequence_number=_next_seq()
                            )
                continue

            # Token-level streaming (text/thinking deltas)
            was_thinking = in_thinking
            event = chunk.get("event", {}) if chunk.get("type") == "stream_event" else {}
            delta = event.get("delta", {}) if isinstance(event, dict) else {}

            # Citations attached to streamed text (SDK ``citations_delta``)
            # map to OpenAI Responses annotation events.  Pass-through: the
            # raw citation dict becomes the annotation, untransformed.
            if delta.get("type") == "citations_delta":
                if chunk.get("parent_tool_use_id") is None or SUBAGENT_STREAM_TEXT:
                    if not message_item_opened:
                        for line in _open_message_item():
                            yield line
                    yield make_response_sse(
                        "response.output_text.annotation.added",
                        item_id=output_item_id,
                        output_index=output_index,
                        content_index=0,
                        annotation_index=annotation_index,
                        annotation=delta.get("citation") or {},
                        sequence_number=_next_seq(),
                    )
                    annotation_index += 1
                continue

            if delta.get("type") == "thinking_delta" and not in_thinking:
                logger.warning(
                    "Responses stream received thinking_delta outside a thinking block: "
                    "response_id=%s event_type=%s. Check sanitizer routing/upstream SSE shape.",
                    response_id,
                    event.get("type"),
                )
            text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
            if text_delta is not None:
                token_streaming = True
                # Open a reasoning output_item on the first thinking delta of a
                # block. If a message item is already streaming text (the
                # think→text→think case), close it first so this thinking block
                # gets its OWN reasoning item at a fresh output_index. The
                # OpenAI Responses output array is an ordered sequence, so
                # interleaving reasoning and message items is valid — we open a
                # new reasoning item, we never reopen the closed one — and the
                # later thinking blocks are preserved instead of dropped.
                if in_thinking and not was_thinking:
                    if message_item_opened:
                        for line in _close_message_item():
                            yield line
                    reasoning_item_id = _generate_rs_id()
                    reasoning_open = True
                    reasoning_text_buf = []
                    thinking_seen = True
                    reasoning_item = ReasoningOutputItem(id=reasoning_item_id, status="in_progress")
                    yield make_response_sse(
                        "response.output_item.added",
                        output_index=output_index,
                        item=reasoning_item,
                        sequence_number=_next_seq(),
                    )
                    yield make_response_sse(
                        "response.reasoning_summary_part.added",
                        item_id=reasoning_item_id,
                        output_index=output_index,
                        summary_index=0,
                        part={"type": "summary_text", "text": ""},
                        sequence_number=_next_seq(),
                    )

                # Drop synthetic markers, which are state-only.  When </think>
                # arrives (content_block_stop while in_thinking), close the
                # reasoning item immediately so any new thinking block that
                # follows gets a fresh output_index.
                if text_delta == "<think>":
                    _close_thinking_capture()
                    continue
                if text_delta == "</think>":
                    _close_thinking_capture()
                    if reasoning_open:
                        for line in _close_reasoning():
                            yield line
                    continue

                # Inside a reasoning block: emit summary_text + reasoning_text deltas.
                if in_thinking:
                    thinking_capture_buf.append(text_delta)
                if reasoning_open and in_thinking:
                    reasoning_text_buf.append(text_delta)
                    if text_delta:
                        yield make_response_sse(
                            "response.reasoning_summary_text.delta",
                            item_id=reasoning_item_id,
                            output_index=output_index,
                            summary_index=0,
                            delta=text_delta,
                            sequence_number=_next_seq(),
                        )
                        yield make_response_sse(
                            "response.reasoning_text.delta",
                            item_id=reasoning_item_id,
                            output_index=output_index,
                            content_index=0,
                            delta=text_delta,
                            sequence_number=_next_seq(),
                        )
                    continue
                # Safety net: inside a thinking block but no reasoning item is
                # open. This should no longer happen — a new thinking block now
                # closes any open message item and opens its own reasoning item
                # above — but keep the guard so thinking text can never leak
                # into the visible message stream.
                if in_thinking:
                    continue
                # If reasoning was open and we just exited it, close it now.
                if reasoning_open and not in_thinking:
                    for line in _close_reasoning():
                        yield line
                if text_delta:
                    cleaned = collab_filter.feed(text_delta)
                    if cleaned:
                        if not message_item_opened:
                            for line in _open_message_item():
                                yield line
                        yield _emit_delta(cleaned)
                        full_text.append(cleaned)
                        all_visible_text.append(cleaned)
                        content_sent = True
                continue

            # Announce a tool call as it starts (content_block_start), before
            # its arguments finish streaming, so the client isn't left silent
            # during long tool-input generation.
            for event in _tool_use_started_events(chunk, _next_seq):
                yield event

            # Accumulate tool_use blocks from stream events
            handled, tool_block = tool_acc.process_stream_event(chunk)
            if handled:
                if tool_block:
                    for event in _tool_use_events(
                        tool_block, tool_stats, _next_seq, tool_names_by_id
                    ):
                        yield event
                continue

            # User chunks with tool_result blocks
            if chunk.get("type") == "user":
                for event in _user_tool_result_events(
                    chunk, tool_stats, _next_seq, tool_names_by_id, request_context
                ):
                    yield event
                chunks_buffer.append(chunk)
                continue

            # Emit tool_use/tool_result blocks embedded in assistant content.
            # This MUST run before the token-streaming skip below so that tool
            # blocks inside assistant content chunks are not silently dropped
            # when token_streaming is True.
            for event in _embedded_tool_events(
                chunk, tool_stats, _next_seq, tool_names_by_id, request_context
            ):
                yield event

            # Skip duplicate assistant text in token-streaming mode.
            # Tool blocks were already extracted above, so only text is suppressed.
            if token_streaming:
                if chunk.get("type") == "stream_event":
                    continue
                if chunk.get("type") != "user" and is_assistant_content_chunk(chunk):
                    if chunk.get("type") == "assistant" and chunk.get("usage"):
                        chunks_buffer.append(chunk)
                    continue

            # Content chunks (assistant messages, results)
            chunks_buffer.append(chunk)
            text = format_chunk_content(chunk, content_sent)
            if text:
                if not message_item_opened:
                    for line in _open_message_item():
                        yield line
                yield _emit_delta(text)
                full_text.append(text)
                all_visible_text.append(text)
                content_sent = True

    except Exception as e:
        logger.error(
            "Responses stream: unexpected error: %s | prior_chunks=%r | %s",
            e,
            _buffer_summary(chunks_buffer),
            _error_context(),
            exc_info=True,
        )
        stream_result["success"] = False
        yield _make_failed_event("server_error", "Internal server error")
        await _log_usage("failed", "server_error")
        return

    # Flush remaining buffered chars from collab filter into the open segment.
    remaining_collab = collab_filter.flush()
    if remaining_collab:
        if not message_item_opened:
            for line in _open_message_item():
                yield line
        yield _emit_delta(remaining_collab)
        full_text.append(remaining_collab)
        all_visible_text.append(remaining_collab)
        content_sent = True

    if tool_acc.has_incomplete:
        logger.warning("Incomplete tool_use blocks at stream end: %s", tool_acc.incomplete_keys)

    # --- Finalization ---

    # No content received AND no reasoning emitted.  Don't yield a failed event
    # here: the caller may still need to emit function_call + requires_action
    # (AskUserQuestion hook path).  Signal "empty" via stream_result and let
    # the route decide.  When reasoning was emitted (thinking-only response),
    # we still need to close the stream cleanly with an empty message item.
    if not content_sent and not thinking_seen:
        logger.info("Responses stream: no text content yielded")
        stream_result["success"] = False
        stream_result["empty"] = True
        return

    # Close a trailing reasoning item if the stream ended without exiting thinking.
    _close_thinking_capture()
    if reasoning_open:
        for line in _close_reasoning():
            yield line

    # Close the final (or only) message segment.  If no message item was ever
    # opened — a thinking-only response, or a stream that ended right after a
    # think→text→think segment boundary — ensure the consumer still sees one
    # trailing (possibly empty) message item.
    if message_item_opened:
        for line in _close_message_item():
            yield line
    elif not any_message_item:
        for line in _open_message_item():
            yield line
        for line in _close_message_item():
            yield line

    # response.completed (with usage — prefer real SDK values).  ``output``
    # carries every reasoning and message item in emission order, so an
    # interleaved think→text→think→text turn round-trips intact rather than
    # dropping the later thinking blocks.
    complete_text = "".join(all_visible_text)
    prompt_tokens, completion_tokens = resolve_token_usage(
        chunks_buffer, prompt_text or "", complete_text
    )
    final_resp = ResponseObject(
        id=response_id,
        model=model,
        status="completed",
        output=list(completed_output_items),
        usage=ResponseUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            context_tokens=extract_context_tokens(chunks_buffer),
        ),
        metadata=_metadata,
        structured_output=extract_structured_output(chunks_buffer),
    )
    stream_result["success"] = True
    stream_result["assistant_text"] = complete_text
    stream_result["thinking_texts"] = thinking_texts
    logger.info(
        "Responses stream completed: response_id=%s assistant_chars=%d "
        "thinking_blocks=%d thinking_chars=%s",
        response_id,
        len(complete_text),
        len(thinking_texts),
        [len(text) for text in thinking_texts],
    )
    yield make_response_sse(
        "response.completed", response_obj=final_resp, sequence_number=_next_seq()
    )
    await _log_usage("completed")
