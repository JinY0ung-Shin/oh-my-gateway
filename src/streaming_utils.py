import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Callable, Dict, Optional

from src import metrics
from src.constants import (
    SSE_KEEPALIVE_INTERVAL,
    STREAM_STALL_TIMEOUT_SECONDS,
    STREAM_TOOL_PROGRESS,
    SUBAGENT_STREAM_PROGRESS,
    SUBAGENT_STREAM_TEXT,
    SUBAGENT_STREAM_TOOL_BLOCKS,
)
from src.message_adapter import MessageAdapter
from src.response_models import (
    InputTokensDetails,
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
from src.usage_logger import extract_sdk_usage_detail, usage_logger

# Backward-compat re-exports from split modules.
# External callers continue to use `from src.streaming_utils import X`.
from src.collab_filter import CollabJsonStreamFilter, strip_collab_json  # noqa: F401
from src.sse_builders import (  # noqa: F401
    _build_progress_event,
    _build_task_event,
    _normalize_tool_result,
    is_teammate_message_text,
    make_function_call_response_sse,
    make_response_sse,
    make_task_response_sse,
    make_teammate_message_response_sse,
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
    """Compact, debug-friendly snapshot of one SDK chunk for error logs."""
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
    """Compact summary of the last N chunks seen before a failure."""
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


def _prompt_tokens_from_usage(usage: Any) -> int:
    """Prompt size for one model request, including cache read/write tokens."""
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("input_tokens") or 0) + int(
        usage.get("cache_creation_input_tokens") or 0
    ) + int(usage.get("cache_read_input_tokens") or 0)


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
            input_tokens = _prompt_tokens_from_usage(usage)
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
            total_input += _prompt_tokens_from_usage(usage)
            total_output += usage.get("output_tokens", 0)
    if found_any:
        return {
            "prompt_tokens": total_input,
            "completion_tokens": total_output,
            "total_tokens": total_input + total_output,
        }

    return None


def extract_context_tokens(chunks: list) -> Optional[int]:
    """Return the latest main-agent prompt snapshot, not cumulative billing input.

    An agentic turn may make many model requests. ``ResultMessage.usage`` sums
    them, so its input total can exceed the context window many times and must
    never drive a context gauge. Since SDK 0.1.49 each ``AssistantMessage``
    carries the usage for *that one request*. The latest top-level assistant
    message therefore gives the same occupancy numerator used by Claude Code's
    context display: input + cache creation + cache reads for the final
    main-agent request.

    Subagent messages are excluded because they own separate context windows.
    After auto/manual compaction, the next main-agent request is built from the
    compacted transcript, so this snapshot naturally drops to the post-compact
    size. ``None`` is returned when no honest per-request snapshot exists; callers
    must not substitute cumulative usage.
    """
    for msg in reversed(chunks):
        if not isinstance(msg, dict) or msg.get("type") != "assistant":
            continue
        if msg.get("parent_tool_use_id"):
            continue
        tokens = _prompt_tokens_from_usage(msg.get("usage"))
        if tokens > 0:
            return tokens
    return None


def extract_structured_output(chunks: list) -> Any:
    """Return ``ResultMessage.structured_output`` from SDK chunks, if present."""
    for msg in reversed(chunks):
        if isinstance(msg, dict) and msg.get("type") == "result":
            value = msg.get("structured_output")
            if value is not None:
                return value
    return None


def extract_thinking_texts(chunks: list) -> list[str]:
    """Return thinking-block texts in the order they appear in the chunk list."""
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
    """Return the visible assistant text from SDK chunks, excluding thinking blocks."""
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

    all_parts = []
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
    """Return (prompt_tokens, completion_tokens) from SDK usage or estimation."""
    sdk_usage = extract_sdk_usage(chunks)
    if sdk_usage:
        return sdk_usage["prompt_tokens"], sdk_usage["completion_tokens"]
    if backend is not None:
        est = backend.estimate_token_usage(prompt, completion_text, model)
        return est["prompt_tokens"], est["completion_tokens"]
    return MessageAdapter.estimate_tokens(prompt), MessageAdapter.estimate_tokens(completion_text)


def resolve_usage_details(chunks: list) -> InputTokensDetails:
    """Return cache accounting plus an honest live-context snapshot.

    Cache counters come from the turn-cumulative SDK result because they are
    billing/accounting fields. ``context_tokens`` comes from the latest
    main-agent AssistantMessage instead because it is a *single-request*
    occupancy snapshot. If that snapshot is unavailable it stays ``None``;
    cumulative input is never relabelled as context.
    """
    detail = extract_sdk_usage_detail(chunks)
    return InputTokensDetails(
        cached_tokens=detail["cache_read_tokens"],
        cache_creation_tokens=detail["cache_creation_tokens"],
        context_tokens=extract_context_tokens(chunks),
    )


# ---------------------------------------------------------------------------
# SSE bridge helper
# ---------------------------------------------------------------------------


async def bridge_sse_stream(
    sse_source: AsyncGenerator[str, None],
    chunk_source,
) -> AsyncGenerator[str, None]:
    """Bridge an SSE async generator through a background asyncio task."""
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
                pass
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

_SSE_KEEPALIVE = ": keepalive\n\n"


class StreamStallError(Exception):
    """The SDK produced no chunk for STREAM_STALL_TIMEOUT_SECONDS."""


logger = logging.getLogger(__name__)
_BACKGROUND_READERS: set = set()


def _reap_background_reader(task) -> None:
    _BACKGROUND_READERS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.info("SSE reader task ended with error", exc_info=exc)


async def _keepalive_wrapper(
    source: AsyncGenerator,
    interval: int,
    stall_after: float = 0,
) -> AsyncGenerator:
    """Wrap *source* to yield keepalives and bound a completely silent stream."""
    if interval <= 0:
        async for item in source:
            yield item
        return

    _SENTINEL = object()
    queue: asyncio.Queue = asyncio.Queue()

    async def _reader():
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:
            await queue.put(exc)
        finally:
            try:
                await source.aclose()
            except Exception:
                pass
            await queue.put(_SENTINEL)

    task = asyncio.create_task(_reader())
    last_item_at = time.monotonic()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                if stall_after > 0 and time.monotonic() - last_item_at > stall_after:
                    raise StreamStallError(
                        f"no SDK output for {stall_after:.0f}s — turn is wedged, failing it so the worker is reclaimed"
                    )
                yield _SSE_KEEPALIVE
                continue

            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                raise item
            last_item_at = time.monotonic()
            yield item
    finally:
        task.cancel()
        _BACKGROUND_READERS.add(task)
        task.add_done_callback(_reap_background_reader)
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


def _user_chunk_texts(chunk: Dict[str, Any]) -> list[str]:
    content = chunk.get("content")
    if not isinstance(content, (list, str)):
        msg = chunk.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    texts = []
    for block in content:
        block_type = _block_field(block, "type")
        if block_type is not None and block_type != "text":
            continue
        text = _block_field(block, "text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _teammate_message_events(
    chunk: Dict[str, Any],
    next_seq: Callable[[], int],
) -> list[str]:
    if chunk.get("parent_tool_use_id") is not None:
        return []
    return [
        make_teammate_message_response_sse(
            text,
            session_id=chunk.get("session_id"),
            sequence_number=next_seq(),
        )
        for text in _user_chunk_texts(chunk)
        if is_teammate_message_text(text)
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
    """SSE streaming logic for /v1/responses (OpenAI Responses API)."""
    content_sent = False
    token_streaming = False
    in_thinking = False
    tool_acc = ToolUseAccumulator()
    collab_filter = CollabJsonStreamFilter()
    full_text = []
    all_visible_text: list[str] = []
    seq = 0
    annotation_index = 0
    message_item_opened = False
    any_message_item = False
    output_index = 0
    reasoning_open = False
    reasoning_item_id: Optional[str] = None
    reasoning_text_buf: list[str] = []
    thinking_seen = False
    thinking_texts: list[str] = []
    thinking_capture_buf: list[str] = []
    completed_output_items: list = []
    _metadata = metadata or {}
    if stream_result is None:
        stream_result = {}

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

    resp_in_progress = ResponseObject(
        id=response_id, model=model, status="in_progress", metadata=_metadata
    )
    yield make_response_sse(
        "response.created", response_obj=resp_in_progress, sequence_number=_next_seq()
    )
    yield make_response_sse(
        "response.in_progress", response_obj=resp_in_progress, sequence_number=_next_seq()
    )

    def _open_message_item() -> list[str]:
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
        captured = "".join(thinking_capture_buf)
        if captured:
            thinking_texts.append(captured)
            logger.info(
                "Responses stream captured thinking block: response_id=%s block_index=%d chars=%d",
                response_id,
                len(thinking_texts) - 1,
                len(captured),
            )
        thinking_capture_buf = []

    def _close_reasoning(status: str = "completed") -> list[str]:
        nonlocal reasoning_open, reasoning_item_id, reasoning_text_buf, output_index
        if not reasoning_open:
            return []
        assert reasoning_item_id is not None
        captured = "".join(reasoning_text_buf)
        item = ReasoningOutputItem(
            id=reasoning_item_id,
            status=status,
            summary=[ReasoningSummary(text=captured)],
            content=[ReasoningContent(text=captured)],
        )
        completed_output_items.append(item)
        lines = [
            make_response_sse(
                "response.reasoning_summary_text.done",
                item_id=reasoning_item_id,
                output_index=output_index,
                summary_index=0,
                text=captured,
                sequence_number=_next_seq(),
            ),
            make_response_sse(
                "response.reasoning_text.done",
                item_id=reasoning_item_id,
                output_index=output_index,
                content_index=0,
                text=captured,
                sequence_number=_next_seq(),
            ),
            make_response_sse(
                "response.reasoning_summary_part.done",
                item_id=reasoning_item_id,
                output_index=output_index,
                summary_index=0,
                part={"type": "summary_text", "text": captured},
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

    try:
        async for chunk in _keepalive_wrapper(
            chunk_source,
            SSE_KEEPALIVE_INTERVAL,
            stall_after=STREAM_STALL_TIMEOUT_SECONDS,
        ):
            if chunk is _SSE_KEEPALIVE:
                yield _SSE_KEEPALIVE
                continue

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
                        input_tokens_details=resolve_usage_details(chunks_buffer),
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

            if chunk.get("type") == "result" and str(chunk.get("subtype", "")).startswith(
                "error_max_turns"
            ):
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
                truncated_resp = ResponseObject(
                    id=response_id,
                    model=model,
                    status="incomplete",
                    output=list(completed_output_items),
                    usage=ResponseUsage(
                        input_tokens=prompt_tokens,
                        output_tokens=completion_tokens,
                        input_tokens_details=resolve_usage_details(chunks_buffer),
                    ),
                    metadata=_metadata,
                    incomplete_details=ResponseIncompleteDetails(reason="max_turns"),
                )
                logger.warning(
                    "Responses stream hit the agentic turn limit: response_id=%s assistant_chars=%d",
                    response_id,
                    len(partial_text),
                )
                stream_result["success"] = False
                stream_result["assistant_text"] = partial_text
                stream_result["thinking_texts"] = thinking_texts
                stream_result["response_obj"] = truncated_resp
                yield make_response_sse(
                    "response.incomplete",
                    response_obj=truncated_resp,
                    sequence_number=_next_seq(),
                )
                await _log_usage("incomplete", "max_turns")
                return

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

            if chunk.get("type") == "rate_limit":
                logger.warning("SDK rate limit event: status=%s", _extract_rate_limit_status(chunk))
                continue

            if chunk.get("type") == "system":
                task_event = _build_task_event(chunk)
                if task_event:
                    is_subagent_task = (
                        chunk.get("parent_tool_use_id") is not None
                        or chunk.get("tool_use_id") is not None
                    )
                    if not is_subagent_task or SUBAGENT_STREAM_PROGRESS:
                        yield make_task_response_sse(task_event, sequence_number=_next_seq())
                else:
                    is_subagent_progress = chunk.get("parent_tool_use_id") is not None
                    if not is_subagent_progress or SUBAGENT_STREAM_PROGRESS:
                        progress_event = _build_progress_event(chunk)
                        if progress_event:
                            yield make_task_response_sse(
                                progress_event, sequence_number=_next_seq()
                            )
                continue

            was_thinking = in_thinking
            event = chunk.get("event", {}) if chunk.get("type") == "stream_event" else {}
            delta = event.get("delta", {}) if isinstance(event, dict) else {}

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
                    "Responses stream received thinking_delta outside a thinking block: response_id=%s event_type=%s",
                    response_id,
                    event.get("type"),
                )
            text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
            if (
                text_delta is not None
                and chunk.get("parent_tool_use_id") is not None
                and not SUBAGENT_STREAM_TEXT
            ):
                continue
            if text_delta is not None:
                token_streaming = True
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

                if text_delta == "<think>":
                    _close_thinking_capture()
                    continue
                if text_delta == "</think>":
                    _close_thinking_capture()
                    if reasoning_open:
                        for line in _close_reasoning():
                            yield line
                    continue

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
                if in_thinking:
                    continue
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

            for started_event in _tool_use_started_events(chunk, _next_seq):
                yield started_event

            handled, tool_block = tool_acc.process_stream_event(chunk)
            if handled:
                if tool_block:
                    for tool_event in _tool_use_events(
                        tool_block, tool_stats, _next_seq, tool_names_by_id
                    ):
                        yield tool_event
                continue

            if chunk.get("type") == "user":
                for user_event in _user_tool_result_events(
                    chunk, tool_stats, _next_seq, tool_names_by_id, request_context
                ):
                    yield user_event
                for teammate_event in _teammate_message_events(chunk, _next_seq):
                    yield teammate_event
                chunks_buffer.append(chunk)
                continue

            for embedded_event in _embedded_tool_events(
                chunk, tool_stats, _next_seq, tool_names_by_id, request_context
            ):
                yield embedded_event

            if token_streaming:
                if chunk.get("type") == "stream_event":
                    continue
                if chunk.get("type") != "user" and is_assistant_content_chunk(chunk):
                    if chunk.get("type") == "assistant" and chunk.get("usage"):
                        chunks_buffer.append(chunk)
                    continue

            chunks_buffer.append(chunk)
            if (
                chunk.get("parent_tool_use_id") is not None
                and not SUBAGENT_STREAM_TEXT
                and chunk.get("type") == "assistant"
            ):
                continue
            rendered = format_chunk_content(chunk, content_sent)
            if rendered:
                if not message_item_opened:
                    for line in _open_message_item():
                        yield line
                yield _emit_delta(rendered)
                full_text.append(rendered)
                all_visible_text.append(rendered)
                content_sent = True

    except StreamStallError as e:
        logger.error(
            "Responses stream: stalled: %s | prior_chunks=%r | %s",
            e,
            _buffer_summary(chunks_buffer),
            _error_context(),
        )
        metrics.record_stream_stall()
        stream_result["success"] = False
        yield _make_failed_event("server_error", f"Turn stalled: {e}")
        await _log_usage("failed", "stream_stall")
        return
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

    _paused_session = (
        (request_context or {}).get("session") if isinstance(request_context, dict) else None
    )
    if getattr(_paused_session, "pending_tool_call", None) is not None:
        logger.info("Responses stream paused on a pending tool call — deferring completion")
        _close_thinking_capture()
        if reasoning_open:
            for line in _close_reasoning():
                yield line
        if message_item_opened:
            for line in _close_message_item():
                yield line
        stream_result["success"] = False
        stream_result["paused"] = True
        stream_result["assistant_text"] = "".join(all_visible_text)
        stream_result["thinking_texts"] = thinking_texts
        return

    if not content_sent and not thinking_seen:
        logger.info("Responses stream: no text content yielded")
        stream_result["success"] = False
        stream_result["empty"] = True
        return

    _close_thinking_capture()
    if reasoning_open:
        for line in _close_reasoning():
            yield line

    if message_item_opened:
        for line in _close_message_item():
            yield line
    elif not any_message_item:
        for line in _open_message_item():
            yield line
        for line in _close_message_item():
            yield line

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
            input_tokens_details=resolve_usage_details(chunks_buffer),
        ),
        metadata=_metadata,
        structured_output=extract_structured_output(chunks_buffer),
    )
    stream_result["success"] = True
    stream_result["assistant_text"] = complete_text
    stream_result["thinking_texts"] = thinking_texts
    logger.info(
        "Responses stream completed: response_id=%s assistant_chars=%d thinking_blocks=%d thinking_chars=%s",
        response_id,
        len(complete_text),
        len(thinking_texts),
        [len(text) for text in thinking_texts],
    )
    yield make_response_sse(
        "response.completed", response_obj=final_resp, sequence_number=_next_seq()
    )
    await _log_usage("completed")
