import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, Optional

from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

from src.constants import SSE_KEEPALIVE_INTERVAL
from src.message_adapter import MessageAdapter
from src.models import ChatCompletionRequest, ChatCompletionStreamResponse, StreamChoice
from src.response_models import (
    ResponseContentPart,
    OutputItem,
    ResponseErrorDetail,
    ResponseObject,
    ResponseUsage,
)


# ---------------------------------------------------------------------------
# Usage & stop-reason helpers
# ---------------------------------------------------------------------------

_STOP_REASON_MAP = {
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


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


def map_stop_reason(stop_reason: Optional[str] = None) -> str:
    """Map Claude SDK stop_reason to OpenAI finish_reason."""
    return _STOP_REASON_MAP.get(stop_reason, "stop")


def extract_stop_reason(messages: list) -> Optional[str]:
    """Extract stop_reason from collected SDK messages (last result message)."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("stop_reason") is not None:
            return msg["stop_reason"]
    return None


# ---------------------------------------------------------------------------
# Content filtering & extraction
# ---------------------------------------------------------------------------


def _extract_tool_blocks(content) -> tuple[list, list]:
    """Separate tool_use/tool_result blocks from other content.

    Returns (tool_blocks, non_tool_content).
    tool_blocks: list of tool_use and tool_result dicts/objects
    non_tool_content: remaining content blocks (text, thinking, etc.)
    """
    if not isinstance(content, list):
        return [], content if content else []
    tool_blocks = []
    non_tool = []
    for b in content:
        if isinstance(b, (ToolUseBlock, ToolResultBlock)):
            tool_blocks.append(b)
        elif isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result"):
            tool_blocks.append(b)
        elif hasattr(b, "type") and getattr(b, "type", None) in ("tool_use", "tool_result"):
            tool_blocks.append(b)
        else:
            non_tool.append(b)
    return tool_blocks, non_tool


def _filter_tool_blocks(content):
    """Filter out tool_use and tool_result blocks from a content list.

    Only filters by block type (dict or SDK object). Text blocks are never
    filtered to avoid suppressing legitimate user-visible content.
    """
    _, non_tool = _extract_tool_blocks(content)
    return non_tool or None


def strip_collab_json(text: str) -> str:
    """Remove collab_tool_call JSON blocks from text content.

    Uses a string-aware brace counter identical to the one in codex_cli so
    that braces inside JSON string values are not misinterpreted.
    """
    plain_parts: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            j = i
            in_string = False
            escape_next = False
            while j < len(text):
                ch = text[j]
                if escape_next:
                    escape_next = False
                elif ch == "\\" and in_string:
                    escape_next = True
                elif ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            block = text[i : j + 1] if j < len(text) else text[i:]
            try:
                parsed = json.loads(block)
                if isinstance(parsed, dict) and (
                    "collab_tool_call" in parsed or parsed.get("type") == "collab_tool_call"
                ):
                    # Valid collab JSON — strip it
                    i = j + 1
                    continue
            except json.JSONDecodeError:
                pass
            plain_parts.append(block)
            i = j + 1
            continue
        plain_parts.append(text[i])
        i += 1
    cleaned = "".join(plain_parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class CollabJsonStreamFilter:
    """Streaming filter that strips collab_tool_call JSON from text deltas.

    Token-level streaming delivers text character by character across many
    deltas.  When a ``{`` is encountered, this filter buffers subsequent
    characters until it can determine whether the block is a collab_tool_call
    JSON object.  Non-collab content is flushed with minimal delay.
    """

    _COLLAB_MARKERS = ('"collab_tool_call"', '"collab_tool"')
    _MAX_BUFFER = 8192

    def __init__(self):
        self._buf = ""
        self._depth = 0
        self._in_string = False
        self._escape_next = False

    @property
    def buffering(self) -> bool:
        return bool(self._buf)

    def feed(self, text: str) -> str:
        """Process a text delta, returning cleaned text (collab JSON removed)."""
        output: list[str] = []

        for ch in text:
            if self._buf:
                self._buf += ch
                if self._escape_next:
                    self._escape_next = False
                elif ch == "\\" and self._in_string:
                    self._escape_next = True
                elif ch == '"':
                    self._in_string = not self._in_string
                elif not self._in_string:
                    if ch == "{":
                        self._depth += 1
                    elif ch == "}":
                        self._depth -= 1
                        if self._depth == 0:
                            if any(m in self._buf for m in self._COLLAB_MARKERS):
                                try:
                                    parsed = json.loads(self._buf)
                                    if isinstance(parsed, dict) and (
                                        "collab_tool_call" in parsed
                                        or parsed.get("type") == "collab_tool_call"
                                    ):
                                        # Valid collab — drop it
                                        self._reset()
                                        continue
                                except json.JSONDecodeError:
                                    pass
                            output.append(self._buf)
                            self._reset()
                            continue
                # Safety limit: flush if buffer grows unreasonably large
                if len(self._buf) > self._MAX_BUFFER:
                    output.append(self._buf)
                    self._reset()
            else:
                if ch == "{":
                    self._buf = ch
                    self._depth = 1
                    self._in_string = False
                    self._escape_next = False
                else:
                    output.append(ch)

        return "".join(output)

    def flush(self) -> str:
        """Return any remaining buffered text at stream end."""
        result = self._buf
        self._reset()
        return result

    def _reset(self):
        self._buf = ""
        self._depth = 0
        self._in_string = False
        self._escape_next = False


def process_chunk_content(chunk: Dict[str, Any], content_sent: bool = False):
    """Extract content from a chunk message. Returns content list, result string, or None."""
    if chunk.get("type") == "assistant" and "message" in chunk:
        message = chunk["message"]
        if isinstance(message, dict) and "content" in message:
            return _filter_tool_blocks(message["content"])

    if "content" in chunk and isinstance(chunk["content"], list):
        return _filter_tool_blocks(chunk["content"])

    if chunk.get("subtype") == "success" and "result" in chunk and not content_sent:
        return chunk["result"]

    return None


def extract_stream_event_delta(chunk: Dict[str, Any], in_thinking: bool = False) -> tuple:
    """Extract streamable text from a StreamEvent chunk."""
    if chunk.get("type") != "stream_event":
        return None, in_thinking
    if chunk.get("parent_tool_use_id") is not None:
        return None, in_thinking

    event = chunk.get("event", {})
    event_type = event.get("type")
    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            return delta.get("text", ""), in_thinking
        if delta_type == "thinking_delta":
            return delta.get("thinking", ""), in_thinking
    if event_type == "content_block_start":
        block = event.get("content_block", {})
        if block.get("type") == "thinking":
            return "<think>", True
    if event_type == "content_block_stop" and in_thinking:
        return "</think>", False
    return None, in_thinking


# ---------------------------------------------------------------------------
# SSE builders
# ---------------------------------------------------------------------------


def make_sse(
    request_id: str,
    model: str,
    delta: dict,
    finish_reason=None,
    usage=None,
) -> str:
    """Build a single SSE-formatted line from a delta dict."""
    chunk = ChatCompletionStreamResponse(
        id=request_id,
        model=model,
        choices=[StreamChoice(index=0, delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )
    return f"data: {chunk.model_dump_json()}\n\n"


def make_response_sse(
    event_type: str,
    response_obj: Optional[Any] = None,
    *,
    sequence_number: int = 0,
    **kwargs,
) -> str:
    """Build a single SSE-formatted line for OpenAI Responses API.

    Uses proper SSE wire format: event: <type>\\ndata: <json>\\n\\n
    """
    data: Dict[str, Any] = {"type": event_type}
    if response_obj:
        if hasattr(response_obj, "model_dump"):
            data["response"] = response_obj.model_dump(mode="json", exclude_none=True)
        else:
            data["response"] = response_obj

    for key, value in kwargs.items():
        if hasattr(value, "model_dump"):
            data[key] = value.model_dump(mode="json", exclude_none=True)
        else:
            data[key] = value

    data["sequence_number"] = sequence_number

    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _build_task_event(chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a structured task event dict from a system chunk, or None."""
    subtype = chunk.get("subtype")
    if subtype == "task_started":
        return {
            "type": "task_started",
            "task_id": chunk.get("task_id", ""),
            "description": chunk.get("description", ""),
            "session_id": chunk.get("session_id", ""),
        }
    if subtype == "task_progress":
        return {
            "type": "task_progress",
            "task_id": chunk.get("task_id", ""),
            "description": chunk.get("description", ""),
            "last_tool_name": chunk.get("last_tool_name"),
            "usage": chunk.get("usage"),
        }
    if subtype == "task_notification":
        return {
            "type": "task_notification",
            "task_id": chunk.get("task_id", ""),
            "status": chunk.get("status", ""),
            "summary": chunk.get("summary", ""),
            "usage": chunk.get("usage"),
        }
    return None


def make_task_sse(request_id: str, model: str, task_event: Dict[str, Any]) -> str:
    """Build an SSE line for Chat Completions with a system_event field (empty delta)."""
    data = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "system_event": task_event,
    }
    return f"data: {json.dumps(data)}\n\n"


def make_task_response_sse(task_event: Dict[str, Any], *, sequence_number: int = 0) -> str:
    """Build an SSE line for Responses API with a custom task event type."""
    event_type = f"response.{task_event['type']}"
    data = {**task_event, "type": event_type, "sequence_number": sequence_number}
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def make_tool_use_response_sse(
    tool_block: Dict[str, Any],
    *,
    sequence_number: int = 0,
    parent_tool_use_id: Optional[str] = None,
) -> str:
    """Build an SSE line for a tool_use block as a structured event."""
    event_type = "response.tool_use"
    data = {
        "type": event_type,
        "tool_use_id": tool_block.get("id", ""),
        "name": tool_block.get("name", ""),
        "input": tool_block.get("input", {}),
        "sequence_number": sequence_number,
    }
    if parent_tool_use_id:
        data["parent_tool_use_id"] = parent_tool_use_id
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _normalize_tool_result(result_block) -> Dict[str, Any]:
    """Normalize a ToolResultBlock or dict into a plain tool_result dict."""
    if isinstance(result_block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": result_block.tool_use_id or "",
            "content": result_block.content or "",
            "is_error": bool(result_block.is_error),
        }
    if hasattr(result_block, "tool_use_id"):
        return {
            "type": "tool_result",
            "tool_use_id": getattr(result_block, "tool_use_id", "") or "",
            "content": getattr(result_block, "content", "") or "",
            "is_error": bool(getattr(result_block, "is_error", False)),
        }
    if isinstance(result_block, dict):
        return {
            "type": "tool_result",
            "tool_use_id": result_block.get("tool_use_id", ""),
            "content": result_block.get("content", ""),
            "is_error": bool(result_block.get("is_error", False)),
        }
    return {
        "type": "tool_result",
        "tool_use_id": "",
        "content": str(result_block),
        "is_error": False,
    }


def make_tool_result_response_sse(
    result_block,
    *,
    sequence_number: int = 0,
    parent_tool_use_id: Optional[str] = None,
) -> str:
    """Build an SSE line for a tool_result block as a structured event."""
    event_type = "response.tool_result"
    data = _normalize_tool_result(result_block)
    data["type"] = event_type
    data["sequence_number"] = sequence_number
    if parent_tool_use_id:
        data["parent_tool_use_id"] = parent_tool_use_id
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Chunk classification
# ---------------------------------------------------------------------------


def is_assistant_content_chunk(chunk: Dict[str, Any]) -> bool:
    """Return True for assistant chunks, including the SDK's untyped content-list shape."""
    chunk_type = chunk.get("type")
    if chunk_type == "assistant":
        return True
    if chunk_type is not None:
        return False
    return isinstance(chunk.get("content"), list)


def extract_embedded_tool_blocks(chunk: Dict[str, Any]) -> list:
    """Extract tool_use/tool_result blocks embedded in assistant content.

    Codex yields ``{"type": "assistant", "content": [...]}`` with tool_use
    and tool_result dicts inline (from collab_tool_call conversion).  Claude's
    SDK sends these as separate stream_event / user chunks.  This function
    lets the streaming loop emit them as structured SSE events regardless
    of which backend produced them.

    Returns a (possibly empty) list of tool block dicts.
    """
    if not is_assistant_content_chunk(chunk):
        return []
    content = chunk.get("content")
    if content is None:
        msg = chunk.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return []
    tool_blocks, _ = _extract_tool_blocks(content)
    # Normalize SDK objects (ToolUseBlock, ToolResultBlock) to plain dicts
    # so callers can safely use .get() on every returned block.
    normalized: list[Dict[str, Any]] = []
    for tb in tool_blocks:
        if isinstance(tb, dict):
            normalized.append(tb)
        elif isinstance(tb, ToolUseBlock):
            normalized.append(
                {
                    "type": "tool_use",
                    "id": getattr(tb, "id", ""),
                    "name": getattr(tb, "name", ""),
                    "input": getattr(tb, "input", {}),
                }
            )
        elif isinstance(tb, ToolResultBlock):
            normalized.append(_normalize_tool_result(tb))
        elif hasattr(tb, "type"):
            # Generic SDK object fallback
            d: Dict[str, Any] = {"type": getattr(tb, "type", "")}
            for attr in ("id", "name", "input", "tool_use_id", "content", "is_error"):
                if hasattr(tb, attr):
                    d[attr] = getattr(tb, attr)
            normalized.append(d)
        else:
            normalized.append(tb)
    return normalized


# ---------------------------------------------------------------------------
# Shared streaming helpers
# ---------------------------------------------------------------------------


class ToolUseAccumulator:
    """Accumulates tool_use blocks from streamed content_block events.

    Tracks partial tool_use blocks across content_block_start / content_block_delta
    (input_json_delta) / content_block_stop events and assembles them into complete
    tool_block dicts.
    """

    def __init__(self):
        self._acc: Dict[tuple, Dict[str, Any]] = {}

    def process_stream_event(self, chunk: Dict[str, Any]) -> tuple[bool, Optional[Dict[str, Any]]]:
        """Process a stream_event chunk for tool_use accumulation.

        Returns (handled, completed_tool_block):
            (True, None) — event consumed (start/delta/subagent skip), caller should continue
            (True, tool_block) — tool_use completed, caller should emit and continue
            (False, None) — not a tool_use event, caller should fall through
        """
        if chunk.get("type") != "stream_event":
            return False, None

        parent_id = chunk.get("parent_tool_use_id")
        event = chunk.get("event", {})
        event_type = event.get("type")

        if event_type == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                idx = (parent_id or "", event.get("index", 0))
                self._acc[idx] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input_parts": [],
                    "parent_tool_use_id": parent_id,
                }
                return True, None
            return False, None

        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "input_json_delta":
                idx = (parent_id or "", event.get("index", 0))
                if idx in self._acc:
                    self._acc[idx]["input_parts"].append(delta.get("partial_json", ""))
                return True, None
            # Skip sub-agent text deltas (noise)
            if parent_id is not None:
                return True, None
            return False, None

        if event_type == "content_block_stop":
            idx = (parent_id or "", event.get("index", 0))
            if idx in self._acc:
                acc = self._acc.pop(idx)
                input_str = "".join(acc["input_parts"])
                try:
                    input_parsed = json.loads(input_str) if input_str else {}
                except json.JSONDecodeError:
                    input_parsed = input_str
                tool_block: Dict[str, Any] = {
                    "type": "tool_use",
                    "id": acc["id"],
                    "name": acc["name"],
                    "input": input_parsed,
                }
                if acc["parent_tool_use_id"]:
                    tool_block["parent_tool_use_id"] = acc["parent_tool_use_id"]
                return True, tool_block
            # Skip sub-agent non-tool stream events
            if parent_id is not None:
                return True, None
            return False, None

        return False, None

    @property
    def has_incomplete(self) -> bool:
        return bool(self._acc)

    @property
    def incomplete_keys(self) -> list:
        return list(self._acc.keys())


def extract_user_tool_results(chunk: Dict[str, Any]) -> tuple[list, Optional[str]]:
    """Extract tool_result blocks and parent_tool_use_id from a user chunk.

    Returns (tool_result_blocks, parent_id).
    """
    parent_id = chunk.get("parent_tool_use_id")
    content_blocks = chunk.get("content", [])
    if not isinstance(content_blocks, list):
        msg = chunk.get("message", {})
        content_blocks = msg.get("content", []) if isinstance(msg, dict) else []
    if not content_blocks:
        return [], parent_id
    tool_result_blocks = [
        b
        for b in content_blocks
        if (b.get("type") if isinstance(b, dict) else None) == "tool_result"
        or isinstance(b, ToolResultBlock)
    ]
    return tool_result_blocks, parent_id


def format_chunk_content(chunk: Dict[str, Any], content_sent: bool) -> Optional[str]:
    """Extract content from a chunk and format as a single text string.

    Strips any embedded collab_tool_call JSON before returning.
    Returns non-empty, non-whitespace text or None.
    """
    content = process_chunk_content(chunk, content_sent=content_sent)
    if content is None:
        return None
    if isinstance(content, list):
        formatted = MessageAdapter.format_blocks(content)
        if formatted and not formatted.isspace():
            formatted = strip_collab_json(formatted)
            return formatted if formatted else None
    elif isinstance(content, str) and content and not content.isspace():
        content = strip_collab_json(content)
        return content if content else None
    return None


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
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Chat Completions streaming (/v1/chat/completions)
# ---------------------------------------------------------------------------


async def stream_chunks(
    chunk_source,
    request: ChatCompletionRequest,
    request_id: str,
    chunks_buffer: list,
    logger: logging.Logger,
) -> AsyncGenerator[str, None]:
    """Shared SSE streaming logic for both stateless and session modes."""
    role_sent = False
    content_sent = False
    token_streaming = False
    in_thinking = False
    tool_acc = ToolUseAccumulator()
    collab_filter = CollabJsonStreamFilter()

    def _emit_sse(text: str):
        nonlocal role_sent
        if not role_sent:
            role_sent = True
            return [
                make_sse(request_id, request.model, {"role": "assistant", "content": ""}),
                make_sse(request_id, request.model, {"content": text}),
            ]
        return [make_sse(request_id, request.model, {"content": text})]

    async for chunk in _keepalive_wrapper(chunk_source, SSE_KEEPALIVE_INTERVAL):
        # Keepalive SSE comments — forward directly to the client
        if chunk is _SSE_KEEPALIVE:
            yield _SSE_KEEPALIVE
            continue

        # Handle AssistantMessage.error (auth failures, rate limits, etc.)
        if chunk.get("type") == "assistant" and chunk.get("error"):
            chunks_buffer.append(chunk)
            for sse in _emit_sse(f"\n\n[Error: {chunk['error']}]\n"):
                yield sse
            content_sent = True
            continue

        # Handle SDK rate-limit events (new in SDK 0.1.49)
        if chunk.get("type") == "rate_limit":
            status = _extract_rate_limit_status(chunk)
            logger.warning("SDK rate limit event: status=%s", status)
            if status == "rejected":
                for sse in _emit_sse("\n\n[Error: rate_limit_rejected]\n"):
                    yield sse
                content_sent = True
            continue

        # Handle task system messages (subagent progress — structured JSON, not content)
        if chunk.get("type") == "system":
            task_event = _build_task_event(chunk)
            if task_event:
                yield make_task_sse(request_id, request.model, task_event)
            continue

        # Token-level streaming (text/thinking deltas)
        text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
        if text_delta is not None:
            token_streaming = True
            if text_delta:
                cleaned = collab_filter.feed(text_delta)
                if cleaned:
                    for sse in _emit_sse(cleaned):
                        yield sse
                    content_sent = True
            elif not role_sent:
                yield make_sse(request_id, request.model, {"role": "assistant", "content": ""})
                role_sent = True
            continue

        # Accumulate tool_use blocks from stream events
        handled, tool_block = tool_acc.process_stream_event(chunk)
        if handled:
            if tool_block:
                yield make_task_sse(request_id, request.model, tool_block)
            continue

        # User chunks with tool_result blocks
        if chunk.get("type") == "user":
            tool_results, parent_id = extract_user_tool_results(chunk)
            for tr_block in tool_results:
                result_data = _normalize_tool_result(tr_block)
                if parent_id:
                    result_data["parent_tool_use_id"] = parent_id
                yield make_task_sse(request_id, request.model, result_data)
            chunks_buffer.append(chunk)
            continue

        # Emit tool_use/tool_result blocks embedded in assistant content.
        # This MUST run before the token-streaming skip below so that tool
        # blocks inside assistant content chunks are not silently dropped
        # when token_streaming is True (which suppresses duplicate text but
        # must not suppress structured tool events).
        embedded_tools = extract_embedded_tool_blocks(chunk)
        for tb in embedded_tools:
            if tb.get("type") == "tool_use":
                yield make_task_sse(request_id, request.model, tb)
            elif tb.get("type") == "tool_result":
                result_data = _normalize_tool_result(tb)
                yield make_task_sse(request_id, request.model, result_data)

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
            for sse in _emit_sse(text):
                yield sse
            content_sent = True

    # Flush remaining buffered chars from collab filter
    remaining_collab = collab_filter.flush()
    if remaining_collab:
        for sse in _emit_sse(remaining_collab):
            yield sse
        content_sent = True

    if tool_acc.has_incomplete:
        logger.warning("Incomplete tool_use blocks at stream end: %s", tool_acc.incomplete_keys)

    if not role_sent:
        yield make_sse(request_id, request.model, {"role": "assistant", "content": ""})
        role_sent = True

    if role_sent and not content_sent:
        logger.warning(
            "No content received from SDK. Raw chunks: %s",
            json.dumps(chunks_buffer, default=str),
        )
        yield make_sse(
            request_id,
            request.model,
            {"content": "I'm unable to provide a response at the moment."},
        )


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
                logger.error("Responses stream: SDK error chunk: %s", error_msg)
                stream_result["success"] = False
                yield _make_failed_event("sdk_error", error_msg)
                return

            # Handle AssistantMessage.error (auth failures, rate limits, etc.)
            if chunk.get("type") == "assistant" and chunk.get("error"):
                chunks_buffer.append(chunk)
                error_type = chunk["error"]
                logger.error("Responses stream: assistant error: %s", error_type)
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

            # Handle task system messages (subagent progress — structured JSON, not content)
            if chunk.get("type") == "system":
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
                    yield make_tool_use_response_sse(
                        tool_block,
                        sequence_number=_next_seq(),
                        parent_tool_use_id=tool_block.get("parent_tool_use_id"),
                    )
                continue

            # User chunks with tool_result blocks
            if chunk.get("type") == "user":
                tool_results, parent_id = extract_user_tool_results(chunk)
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
        logger.error("Responses stream: unexpected error: %s", e, exc_info=True)
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

    # No content received → emit response.failed (not fake success)
    if not content_sent:
        logger.warning("Responses stream: no content received from SDK")
        stream_result["success"] = False
        yield _make_failed_event("empty_response", "No response generated")
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
    sdk_usage = extract_sdk_usage(chunks_buffer)
    if sdk_usage:
        prompt_tokens = sdk_usage["prompt_tokens"]
        completion_tokens = sdk_usage["completion_tokens"]
    else:
        prompt_tokens = MessageAdapter.estimate_tokens(prompt_text) if prompt_text else 0
        completion_tokens = MessageAdapter.estimate_tokens(final_text)
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
