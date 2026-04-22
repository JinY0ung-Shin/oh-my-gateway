import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from src.constants import (
    SSE_KEEPALIVE_INTERVAL,
    SUBAGENT_STREAM_PROGRESS,
    SUBAGENT_STREAM_TOOL_BLOCKS,
)
from src.message_adapter import MessageAdapter
from src.response_models import (
    ResponseContentPart,
    OutputItem,
    ResponseErrorDetail,
    ResponseObject,
    ResponseUsage,
)

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
                {
                    k: v
                    for k, v in _block_summary(b).items()
                    if k in ("type", "name", "is_error")
                }
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
    _metadata = metadata or {}
    if stream_result is None:
        stream_result = {}

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

    # 3. response.output_item.added
    output_item = OutputItem(id=output_item_id, status="in_progress")
    yield make_response_sse(
        "response.output_item.added",
        output_index=0,
        item=output_item,
        sequence_number=_next_seq(),
    )

    # 4. response.content_part.added
    content_part = ResponseContentPart(type="output_text", text="")
    yield make_response_sse(
        "response.content_part.added",
        item_id=output_item_id,
        output_index=0,
        content_index=0,
        part=content_part,
        sequence_number=_next_seq(),
    )

    def _emit_delta(text: str) -> str:
        return make_response_sse(
            "response.output_text.delta",
            item_id=output_item_id,
            output_index=0,
            content_index=0,
            delta=text,
            logprobs=[],
            sequence_number=_next_seq(),
        )

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
                return

            # Handle SDK rate-limit events (new in SDK 0.1.49)
            if chunk.get("type") == "rate_limit":
                status = _extract_rate_limit_status(chunk)
                logger.warning("SDK rate limit event: status=%s", status)
                if status == "rejected":
                    stream_result["success"] = False
                    yield _make_failed_event("rate_limit", "Rate limit rejected")
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
                # Suppress thinking content in Responses API
                if was_thinking or in_thinking or text_delta in ("<think>", "</think>"):
                    continue
                if text_delta:
                    cleaned = collab_filter.feed(text_delta)
                    if cleaned:
                        yield _emit_delta(cleaned)
                        full_text.append(cleaned)
                        content_sent = True
                continue

            # Accumulate tool_use blocks from stream events
            handled, tool_block = tool_acc.process_stream_event(chunk)
            if handled:
                if tool_block:
                    is_subagent_tool = tool_block.get("parent_tool_use_id") is not None
                    if not is_subagent_tool or SUBAGENT_STREAM_TOOL_BLOCKS:
                        yield make_tool_use_response_sse(
                            tool_block,
                            sequence_number=_next_seq(),
                            parent_tool_use_id=tool_block.get("parent_tool_use_id"),
                        )
                continue

            # User chunks with tool_result blocks
            if chunk.get("type") == "user":
                tool_results, parent_id = extract_user_tool_results(chunk)
                is_subagent_result = parent_id is not None
                if not is_subagent_result or SUBAGENT_STREAM_TOOL_BLOCKS:
                    for tr_block in tool_results:
                        yield make_tool_result_response_sse(
                            tr_block,
                            sequence_number=_next_seq(),
                            parent_tool_use_id=parent_id,
                        )
                chunks_buffer.append(chunk)
                continue

            # Emit tool_use/tool_result blocks embedded in assistant content.
            # This MUST run before the token-streaming skip below so that tool
            # blocks inside assistant content chunks are not silently dropped
            # when token_streaming is True.
            embedded_tools = extract_embedded_tool_blocks(chunk)
            for tb in embedded_tools:
                is_subagent_tb = tb.get("parent_tool_use_id") is not None
                if not is_subagent_tb or SUBAGENT_STREAM_TOOL_BLOCKS:
                    if tb.get("type") == "tool_use":
                        yield make_tool_use_response_sse(
                            tb,
                            sequence_number=_next_seq(),
                            parent_tool_use_id=tb.get("parent_tool_use_id"),
                        )
                    elif tb.get("type") == "tool_result":
                        yield make_tool_result_response_sse(
                            tb,
                            sequence_number=_next_seq(),
                            parent_tool_use_id=tb.get("parent_tool_use_id"),
                        )

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
        return

    # Flush remaining buffered chars from collab filter
    remaining_collab = collab_filter.flush()
    if remaining_collab:
        yield _emit_delta(remaining_collab)
        full_text.append(remaining_collab)
        content_sent = True

    if tool_acc.has_incomplete:
        logger.warning("Incomplete tool_use blocks at stream end: %s", tool_acc.incomplete_keys)

    # --- Finalization ---

    # No content received.  Don't yield a failed event here: the caller may
    # still need to emit function_call + requires_action (AskUserQuestion hook
    # path).  Signal "empty" via stream_result and let the route decide.
    if not content_sent:
        logger.info("Responses stream: no text content yielded")
        stream_result["success"] = False
        stream_result["empty"] = True
        return

    # Emit closing events for successful stream
    final_text = "".join(full_text)

    # response.output_text.done
    yield make_response_sse(
        "response.output_text.done",
        item_id=output_item_id,
        output_index=0,
        content_index=0,
        text=final_text,
        logprobs=[],
        sequence_number=_next_seq(),
    )

    # response.content_part.done
    yield make_response_sse(
        "response.content_part.done",
        item_id=output_item_id,
        output_index=0,
        content_index=0,
        part=ResponseContentPart(text=final_text),
        sequence_number=_next_seq(),
    )

    # response.output_item.done
    yield make_response_sse(
        "response.output_item.done",
        output_index=0,
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
            OutputItem(
                id=output_item_id,
                status="completed",
                content=[ResponseContentPart(text=final_text)],
            )
        ],
        usage=ResponseUsage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
        ),
        metadata=_metadata,
    )
    stream_result["success"] = True
    stream_result["assistant_text"] = final_text
    yield make_response_sse(
        "response.completed", response_obj=final_resp, sequence_number=_next_seq()
    )
