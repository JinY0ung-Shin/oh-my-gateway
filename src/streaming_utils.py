import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Callable, Dict, Optional

from src.constants import (
    SSE_KEEPALIVE_INTERVAL,
    SUBAGENT_STREAM_PROGRESS,
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
    ResponseObject,
    ResponseUsage,
)
from src.tool_stats import ToolStatsCollector
from src.usage_logger import usage_logger

# Backward-compat re-exports from split modules.
# External callers continue to use `from src.streaming_utils import X`.
from src.collab_filter import CollabJsonStreamFilter, strip_collab_json  # noqa: F401
from src.sse_builders import (  # noqa: F401
    _build_task_event,
    _normalize_tool_result,
    make_function_call_response_sse,
    make_response_sse,
    make_task_response_sse,
    make_tool_result_response_sse,
    make_tool_use_response_sse,
)
from src.chunk_processing import (  # noqa: F401
    _extract_tool_blocks,
    _filter_tool_blocks,
    ToolUseAccumulator,
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


def _extract_rate_limit_status(chunk: Dict[str, Any]) -> str:
    """Extract the status string from a rate_limit chunk.

    ``rate_limit_info`` may be a plain dict (in tests) or an SDK
    ``RateLimitInfo`` dataclass (at runtime).  Handle both.
    """
    info = chunk.get("rate_limit_info")
    if info is None:
        return "unknown"
    if isinstance(info, dict):
        return info.get("status", "unknown")
    return getattr(info, "status", "unknown")


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

_SENTINEL = object()


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
    seq = 0
    message_item_opened = False
    output_index = 0
    reasoning_open = False
    reasoning_item_id: Optional[str] = None
    reasoning_text_buf: list[str] = []
    thinking_seen = False
    thinking_texts: list[str] = []
    thinking_capture_buf: list[str] = []
    completed_reasoning_items: list[ReasoningOutputItem] = []
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
        nonlocal message_item_opened
        if message_item_opened:
            return []
        message_item_opened = True
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
        thinking_capture_buf = []

    def _close_reasoning() -> list[str]:
        """Emit the four close events for the open reasoning item, bump output_index."""
        nonlocal reasoning_open, reasoning_item_id, reasoning_text_buf, output_index
        if not reasoning_open:
            return []
        full_text = "".join(reasoning_text_buf)
        item = ReasoningOutputItem(
            id=reasoning_item_id,
            status="completed",
            summary=[ReasoningSummary(text=full_text)],
            content=[ReasoningContent(text=full_text)],
        )
        completed_reasoning_items.append(item)
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

    # --- Main streaming loop ---

    try:
        async for chunk in _keepalive_wrapper(chunk_source, SSE_KEEPALIVE_INTERVAL):
            # Keepalive SSE comments — forward directly to the client
            if chunk is _SSE_KEEPALIVE:
                yield _SSE_KEEPALIVE
                continue

            # Detect SDK in-band error chunks
            if isinstance(chunk, dict) and chunk.get("is_error"):
                error_msg = chunk.get("error_message", "Unknown SDK error")
                logger.error(
                    "Responses stream: SDK error chunk: %s | chunk=%r | prior_chunks=%r | %s",
                    error_msg,
                    _chunk_summary(chunk),
                    _buffer_summary(chunks_buffer),
                    _error_context(),
                )
                stream_result["success"] = False
                yield _make_failed_event("sdk_error", error_msg)
                await _log_usage("failed", "sdk_error")
                return

            # Handle AssistantMessage.error (auth failures, rate limits, etc.)
            if chunk.get("type") == "assistant" and chunk.get("error"):
                error_type = chunk["error"]
                logger.error(
                    "Responses stream: assistant error: %s | chunk=%r | prior_chunks=%r | %s",
                    error_type,
                    _chunk_summary(chunk),
                    _buffer_summary(chunks_buffer),
                    _error_context(),
                )
                chunks_buffer.append(chunk)
                stream_result["success"] = False
                yield _make_failed_event(error_type, f"Claude error: {error_type}")
                await _log_usage("failed", error_type)
                return

            # Handle SDK rate-limit events (new in SDK 0.1.49)
            if chunk.get("type") == "rate_limit":
                status = _extract_rate_limit_status(chunk)
                logger.warning("SDK rate limit event: status=%s", status)
                if status == "rejected":
                    stream_result["success"] = False
                    yield _make_failed_event("rate_limit", "Rate limit rejected")
                    await _log_usage("failed", "rate_limit")
                    return
                continue

            # Handle task system messages (structured JSON, not content)
            if chunk.get("type") == "system":
                is_subagent_task = chunk.get("parent_tool_use_id") is not None
                if not is_subagent_task or SUBAGENT_STREAM_PROGRESS:
                    task_event = _build_task_event(chunk)
                    if task_event:
                        yield make_task_response_sse(task_event, sequence_number=_next_seq())
                continue

            # Token-level streaming (text/thinking deltas)
            was_thinking = in_thinking
            text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
            if text_delta is not None:
                token_streaming = True
                # Open reasoning output_item on first thinking delta of a block,
                # but only if the message item is not already open. The OpenAI
                # Responses API contract does not support interleaving reasoning
                # back in after a message item has started emitting text; if a
                # second thinking block arrives after text in the rare
                # think→text→think case, those thinking deltas are dropped.
                if in_thinking and not was_thinking and not message_item_opened:
                    reasoning_item_id = _generate_rs_id()
                    reasoning_open = True
                    reasoning_text_buf = []
                    thinking_seen = True
                    reasoning_item = ReasoningOutputItem(
                        id=reasoning_item_id, status="in_progress"
                    )
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
                # We're inside a thinking block but couldn't open a reasoning item
                # (message item already opened — OpenAI Responses contract doesn't support
                # reopening reasoning). Drop these deltas so they don't leak as message text.
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
                        content_sent = True
                continue

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

    # Flush remaining buffered chars from collab filter
    remaining_collab = collab_filter.flush()
    if remaining_collab:
        if not message_item_opened:
            for line in _open_message_item():
                yield line
        yield _emit_delta(remaining_collab)
        full_text.append(remaining_collab)
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

    # Close reasoning if it's still open (stream ended without exiting thinking).
    _close_thinking_capture()
    if reasoning_open:
        for line in _close_reasoning():
            yield line

    # Emit closing events for successful stream
    final_text = "".join(full_text)

    # Ensure the message item has been announced (consumer always sees one).
    if not message_item_opened:
        for line in _open_message_item():
            yield line

    # response.output_text.done
    yield make_response_sse(
        "response.output_text.done",
        item_id=output_item_id,
        output_index=output_index,
        content_index=0,
        text=final_text,
        logprobs=[],
        sequence_number=_next_seq(),
    )

    # response.content_part.done
    yield make_response_sse(
        "response.content_part.done",
        item_id=output_item_id,
        output_index=output_index,
        content_index=0,
        part=ResponseContentPart(text=final_text),
        sequence_number=_next_seq(),
    )

    # response.output_item.done
    yield make_response_sse(
        "response.output_item.done",
        output_index=output_index,
        item=OutputItem(
            id=output_item_id,
            status="completed",
            content=[ResponseContentPart(text=final_text)],
        ),
        sequence_number=_next_seq(),
    )

    # response.completed (with usage — prefer real SDK values)
    prompt_tokens, completion_tokens = resolve_token_usage(
        chunks_buffer, prompt_text or "", final_text
    )
    final_resp = ResponseObject(
        id=response_id,
        model=model,
        status="completed",
        output=[
            *completed_reasoning_items,
            OutputItem(
                id=output_item_id,
                status="completed",
                content=[ResponseContentPart(text=final_text)],
            ),
        ],
        usage=ResponseUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        ),
        metadata=_metadata,
    )
    stream_result["success"] = True
    stream_result["assistant_text"] = final_text
    stream_result["thinking_texts"] = thinking_texts
    yield make_response_sse(
        "response.completed", response_obj=final_resp, sequence_number=_next_seq()
    )
    await _log_usage("completed")
