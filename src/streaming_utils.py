import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from claude_agent_sdk.types import ToolResultBlock

from src.message_adapter import MessageAdapter
from src.models import ChatCompletionRequest, ChatCompletionStreamResponse, StreamChoice
from src.response_models import (
    ContentPart as ResponseContentPart,
    OutputItem,
    ResponseErrorDetail,
    ResponseObject,
    ResponseUsage,
)


def extract_sdk_usage(chunks: list) -> Optional[Dict[str, int]]:
    """Extract real token usage from SDK ResultMessage if available.

    Returns dict with prompt_tokens, completion_tokens, total_tokens or None.
    """
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
    return None


def map_stop_reason(stop_reason: Optional[str] = None) -> str:
    """Map Claude SDK stop_reason to OpenAI finish_reason."""
    if stop_reason == "max_tokens":
        return "length"
    return "stop"


def extract_stop_reason(messages: list) -> Optional[str]:
    """Extract stop_reason from collected SDK messages (last result message)."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("stop_reason") is not None:
            return msg["stop_reason"]
    return None


def process_chunk_content(chunk: Dict[str, Any], content_sent: bool = False):
    """Extract content from a chunk message. Returns content list, result string, or None."""
    if chunk.get("type") == "assistant" and "message" in chunk:
        message = chunk["message"]
        if isinstance(message, dict) and "content" in message:
            return message["content"]

    if "content" in chunk and isinstance(chunk["content"], list):
        return chunk["content"]

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
    import time

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


def is_assistant_content_chunk(chunk: Dict[str, Any]) -> bool:
    """Return True for assistant chunks, including the SDK's untyped content-list shape."""
    chunk_type = chunk.get("type")
    if chunk_type == "assistant":
        return True
    if chunk_type is not None:
        return False
    return isinstance(chunk.get("content"), list)


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
    tool_use_acc = {}

    def _emit_sse(text: str):
        nonlocal role_sent, content_sent
        if not role_sent:
            role_sent = True
            return [
                make_sse(request_id, request.model, {"role": "assistant", "content": ""}),
                make_sse(request_id, request.model, {"content": text}),
            ]
        return [make_sse(request_id, request.model, {"content": text})]

    async for chunk in chunk_source:
        # Handle AssistantMessage.error (auth failures, rate limits, etc.)
        if chunk.get("type") == "assistant" and chunk.get("error"):
            chunks_buffer.append(chunk)
            error_type = chunk["error"]
            error_text = f"\n\n[Error: {error_type}]\n"
            for sse in _emit_sse(error_text):
                yield sse
            content_sent = True
            continue

        # Handle task system messages (subagent progress — structured JSON, not content)
        if chunk.get("type") == "system":
            task_event = _build_task_event(chunk)
            if task_event:
                yield make_task_sse(request_id, request.model, task_event)
            continue

        text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
        if text_delta is not None:
            token_streaming = True
            if text_delta:
                for sse in _emit_sse(text_delta):
                    yield sse
                content_sent = True
            elif not role_sent:
                yield make_sse(request_id, request.model, {"role": "assistant", "content": ""})
                role_sent = True
            continue

        if token_streaming:
            if chunk.get("type") == "stream_event":
                if chunk.get("parent_tool_use_id") is not None:
                    continue
                event = chunk.get("event", {})
                event_type = event.get("type")
                if event_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        idx = event.get("index", 0)
                        tool_use_acc[idx] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "input_parts": [],
                        }
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "input_json_delta":
                        idx = event.get("index", 0)
                        if idx in tool_use_acc:
                            tool_use_acc[idx]["input_parts"].append(delta.get("partial_json", ""))
                elif event_type == "content_block_stop":
                    idx = event.get("index", 0)
                    if idx in tool_use_acc:
                        acc = tool_use_acc.pop(idx)
                        input_str = "".join(acc["input_parts"])
                        try:
                            input_parsed = json.loads(input_str) if input_str else {}
                        except json.JSONDecodeError:
                            input_parsed = input_str
                        tool_block = {
                            "type": "tool_use",
                            "id": acc["id"],
                            "name": acc["name"],
                            "input": input_parsed,
                        }
                        formatted = MessageAdapter.format_block(tool_block)
                        if formatted:
                            for sse in _emit_sse(formatted):
                                yield sse
                            content_sent = True
                continue
            if chunk.get("type") != "user" and is_assistant_content_chunk(chunk):
                continue

        if chunk.get("type") == "user" and chunk.get("parent_tool_use_id") is None:
            content_blocks = chunk.get("content", [])
            if not isinstance(content_blocks, list):
                msg = chunk.get("message", {})
                content_blocks = msg.get("content", []) if isinstance(msg, dict) else []
            if content_blocks:
                tool_result_blocks = [
                    b
                    for b in content_blocks
                    if (b.get("type") if isinstance(b, dict) else None) == "tool_result"
                    or isinstance(b, ToolResultBlock)
                ]
                if tool_result_blocks:
                    formatted = MessageAdapter.format_blocks(tool_result_blocks)
                    if formatted and not formatted.isspace():
                        for sse in _emit_sse(formatted):
                            yield sse
                        content_sent = True
            chunks_buffer.append(chunk)
            continue

        chunks_buffer.append(chunk)

        content = process_chunk_content(chunk, content_sent=content_sent)
        if content is not None:
            if isinstance(content, list):
                formatted = MessageAdapter.format_blocks(content)
                if formatted and not formatted.isspace():
                    for sse in _emit_sse(formatted):
                        yield sse
                    content_sent = True
            elif isinstance(content, str) and content and not content.isspace():
                for sse in _emit_sse(content):
                    yield sse
                content_sent = True

    if tool_use_acc:
        logger.warning("Incomplete tool_use blocks at stream end: %s", list(tool_use_acc.keys()))

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
    tool_use_acc = {}
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

    try:
        async for chunk in chunk_source:
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

            # Handle task system messages (subagent progress — structured JSON, not content)
            if chunk.get("type") == "system":
                task_event = _build_task_event(chunk)
                if task_event:
                    yield make_task_response_sse(task_event, sequence_number=_next_seq())
                continue

            was_thinking = in_thinking
            text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
            if text_delta is not None:
                token_streaming = True
                # Suppress thinking content in Responses API
                if was_thinking or in_thinking or text_delta in ("<think>", "</think>"):
                    continue
                if text_delta:
                    yield _emit_delta(text_delta)
                    full_text.append(text_delta)
                    content_sent = True
                continue

            if token_streaming:
                if chunk.get("type") == "stream_event":
                    if chunk.get("parent_tool_use_id") is not None:
                        continue
                    event = chunk.get("event", {})
                    event_type = event.get("type")
                    if event_type == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            idx = event.get("index", 0)
                            tool_use_acc[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input_parts": [],
                            }
                    elif event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "input_json_delta":
                            idx = event.get("index", 0)
                            if idx in tool_use_acc:
                                tool_use_acc[idx]["input_parts"].append(
                                    delta.get("partial_json", "")
                                )
                    elif event_type == "content_block_stop":
                        idx = event.get("index", 0)
                        if idx in tool_use_acc:
                            acc = tool_use_acc.pop(idx)
                            input_str = "".join(acc["input_parts"])
                            try:
                                input_parsed = json.loads(input_str) if input_str else {}
                            except json.JSONDecodeError:
                                input_parsed = input_str
                            tool_block = {
                                "type": "tool_use",
                                "id": acc["id"],
                                "name": acc["name"],
                                "input": input_parsed,
                            }
                            formatted = MessageAdapter.format_block(tool_block)
                            if formatted:
                                yield _emit_delta(formatted)
                                full_text.append(formatted)
                                content_sent = True
                    continue
                if chunk.get("type") != "user" and is_assistant_content_chunk(chunk):
                    continue

            if chunk.get("type") == "user" and chunk.get("parent_tool_use_id") is None:
                content_blocks = chunk.get("content", [])
                if not isinstance(content_blocks, list):
                    msg = chunk.get("message", {})
                    content_blocks = msg.get("content", []) if isinstance(msg, dict) else []
                if content_blocks:
                    tool_result_blocks = [
                        b
                        for b in content_blocks
                        if (b.get("type") if isinstance(b, dict) else None) == "tool_result"
                        or isinstance(b, ToolResultBlock)
                    ]
                    if tool_result_blocks:
                        formatted = MessageAdapter.format_blocks(tool_result_blocks)
                        if formatted and not formatted.isspace():
                            yield _emit_delta(formatted)
                            full_text.append(formatted)
                            content_sent = True
                chunks_buffer.append(chunk)
                continue

            chunks_buffer.append(chunk)

            content = process_chunk_content(chunk, content_sent=content_sent)
            if content is not None:
                if isinstance(content, list):
                    formatted = MessageAdapter.format_blocks(content)
                    if formatted and not formatted.isspace():
                        yield _emit_delta(formatted)
                        full_text.append(formatted)
                        content_sent = True
                elif isinstance(content, str) and content and not content.isspace():
                    yield _emit_delta(content)
                    full_text.append(content)
                    content_sent = True

    except Exception as e:
        logger.error("Responses stream: unexpected error: %s", e, exc_info=True)
        stream_result["success"] = False
        yield _make_failed_event("server_error", "Internal server error")
        return

    if tool_use_acc:
        logger.warning("Incomplete tool_use blocks at stream end: %s", list(tool_use_acc.keys()))

    # No content received → emit response.failed (not fake success)
    if not content_sent:
        logger.warning("Responses stream: no content received from SDK")
        stream_result["success"] = False
        yield _make_failed_event("empty_response", "No response generated")
        return

    # Finalize: emit closing events for successful stream
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
    yield make_response_sse(
        "response.completed", response_obj=final_resp, sequence_number=_next_seq()
    )
