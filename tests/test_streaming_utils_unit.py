#!/usr/bin/env python3
"""
Unit tests for src/streaming_utils.py.
"""

import json
import logging

import pytest

from src.models import ChatCompletionRequest, Message
from src.response_models import OutputItem, ResponseObject
from src.streaming_utils import extract_sdk_usage, make_response_sse, stream_chunks, stream_response_chunks


def _parse_chat_sse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


class TestMakeResponseSSE:
    def test_serializes_models_and_sequence_numbers(self):
        response_obj = ResponseObject(id="resp-1", model="claude-test")
        item = OutputItem(id="msg-1")

        line = make_response_sse(
            "response.created",
            response_obj=response_obj,
            item=item,
            sequence_number=7,
        )

        event_type, payload = _parse_response_sse(line)
        assert event_type == "response.created"
        assert payload["type"] == "response.created"
        assert payload["response"]["id"] == "resp-1"
        assert payload["item"]["id"] == "msg-1"
        assert payload["sequence_number"] == 7


@pytest.mark.asyncio
async def test_stream_chunks_formats_tool_results_from_legacy_user_messages():
    async def tool_result_source():
        yield {
            "type": "user",
            "content": "ignored",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "done",
                    }
                ]
            },
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    chunks_buffer = []
    logger = logging.getLogger("test-stream-chunks-tool-result")

    lines = [
        line
        async for line in stream_chunks(tool_result_source(), request, "req-tool-result", chunks_buffer, logger)
    ]

    assert len(lines) == 2
    assert _parse_chat_sse(lines[0])["choices"][0]["delta"]["role"] == "assistant"
    assert "tool_result" in lines[1]
    assert chunks_buffer[0]["type"] == "user"


@pytest.mark.asyncio
async def test_stream_chunks_emits_role_for_empty_text_delta_then_fallback():
    async def empty_delta_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": ""},
            },
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    lines = [
        line
        async for line in stream_chunks(
            empty_delta_source(),
            request,
            "req-empty-delta",
            [],
            logging.getLogger("test-stream-chunks-empty-delta"),
        )
    ]

    assert len(lines) == 2
    assert _parse_chat_sse(lines[0])["choices"][0]["delta"]["role"] == "assistant"
    assert "unable to provide a response" in lines[1]


@pytest.mark.asyncio
async def test_stream_chunks_reassembles_tool_use_with_invalid_json_as_raw_text():
    async def invalid_tool_use_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hi"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "{bad json"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 1},
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    lines = [
        line
        async for line in stream_chunks(
            invalid_tool_use_source(),
            request,
            "req-invalid-tool-json",
            [],
            logging.getLogger("test-stream-chunks-invalid-tool-json"),
        )
    ]

    assert len(lines) == 3
    assert "Hi" in lines[1]
    payload = _parse_chat_sse(lines[2])
    content = payload["choices"][0]["delta"]["content"]
    assert '"type": "tool_use"' in content
    assert '"input": "{bad json"' in content


@pytest.mark.asyncio
async def test_stream_chunks_warns_when_tool_use_is_incomplete(caplog):
    async def incomplete_tool_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 3,
                "content_block": {"type": "tool_use", "id": "tool-3", "name": "Write"},
            },
        }

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    logger = logging.getLogger("test-stream-chunks-incomplete-tool")

    with caplog.at_level(logging.WARNING):
        lines = [
            line
            async for line in stream_chunks(
                incomplete_tool_source(),
                request,
                "req-incomplete-tool",
                [],
                logger,
            )
        ]

    assert len(lines) == 2
    assert "Hello" in lines[1]
    assert "Incomplete tool_use blocks" in caplog.text


@pytest.mark.asyncio
async def test_stream_response_chunks_success_suppresses_thinking_and_formats_tool_blocks():
    async def success_source():
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_start", "content_block": {"type": "thinking"}},
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "thinking_delta", "thinking": "hidden"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop"},
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {"content": [{"type": "text", "text": "duplicate assistant payload"}]}
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"/tmp/demo.txt"}',
                },
            },
        }
        yield {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 1},
        }
        yield {
            "type": "user",
            "content": "ignored",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "done",
                    }
                ]
            },
        }

    chunks_buffer = []
    stream_result = {}
    logger = logging.getLogger("test-stream-response-success")

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=success_source(),
            model="claude-test",
            response_id="resp-stream-1",
            output_item_id="msg-stream-1",
            chunks_buffer=chunks_buffer,
            logger=logger,
            prompt_text="Prompt text",
            metadata={"trace_id": "abc"},
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [event_type for event_type, _payload in parsed]

    assert event_types[0] == "response.created"
    assert event_types[1] == "response.in_progress"
    assert "response.output_item.added" in event_types
    assert "response.content_part.added" in event_types
    assert event_types[-1] == "response.completed"
    assert event_types.index("response.output_text.done") < event_types.index("response.content_part.done")
    assert event_types.index("response.content_part.done") < event_types.index("response.output_item.done")
    assert event_types.index("response.output_item.done") < event_types.index("response.completed")

    deltas = [payload["delta"] for event_type, payload in parsed if event_type == "response.output_text.delta"]
    assert deltas[0] == "Hello"
    assert any("tool_use" in delta for delta in deltas[1:])
    assert any("tool_result" in delta for delta in deltas[1:])
    assert all("hidden" not in delta for delta in deltas)
    assert all("<think>" not in delta for delta in deltas)
    assert all("duplicate assistant payload" not in delta for delta in deltas)

    completed_payload = parsed[-1][1]
    assert completed_payload["response"]["status"] == "completed"
    assert completed_payload["response"]["metadata"] == {"trace_id": "abc"}
    assert completed_payload["response"]["usage"]["input_tokens"] == 2
    assert completed_payload["response"]["usage"]["output_tokens"] > 0
    assert stream_result["success"] is True
    assert len(chunks_buffer) == 1
    assert chunks_buffer[0]["type"] == "user"


@pytest.mark.asyncio
async def test_stream_response_chunks_formats_legacy_assistant_messages():
    async def legacy_assistant_source():
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Legacy answer"}]},
        }

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=legacy_assistant_source(),
            model="claude-test",
            response_id="resp-stream-legacy",
            output_item_id="msg-stream-legacy",
            chunks_buffer=[],
            logger=logging.getLogger("test-stream-response-legacy"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    delta_payloads = [payload for event_type, payload in parsed if event_type == "response.output_text.delta"]
    assert delta_payloads[0]["delta"] == "Legacy answer"
    assert parsed[-1][1]["response"]["output"][0]["content"][0]["text"] == "Legacy answer"
    assert stream_result["success"] is True


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_failed_event_for_sdk_error_chunk():
    async def sdk_error_source():
        yield {"is_error": True, "error_message": "sdk exploded"}

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=sdk_error_source(),
            model="claude-test",
            response_id="resp-stream-sdk-error",
            output_item_id="msg-stream-sdk-error",
            chunks_buffer=[],
            logger=logging.getLogger("test-stream-response-sdk-error"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "sdk_error"
    assert parsed[-1][1]["response"]["error"]["message"] == "sdk exploded"
    assert stream_result["success"] is False


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_failed_event_for_empty_response():
    async def empty_source():
        yield {"type": "metadata"}

    chunks_buffer = []
    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=empty_source(),
            model="claude-test",
            response_id="resp-stream-empty",
            output_item_id="msg-stream-empty",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-stream-response-empty"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "empty_response"
    assert chunks_buffer == [{"type": "metadata"}]
    assert stream_result["success"] is False


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_failed_event_for_unexpected_exception():
    async def exploding_source():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=exploding_source(),
            model="claude-test",
            response_id="resp-stream-exception",
            output_item_id="msg-stream-exception",
            chunks_buffer=[],
            logger=logging.getLogger("test-stream-response-exception"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "server_error"
    assert parsed[-1][1]["response"]["error"]["message"] == "Internal server error"
    assert stream_result["success"] is False


@pytest.mark.asyncio
async def test_stream_response_chunks_warns_on_incomplete_tool_use_and_still_completes(caplog):
    async def incomplete_tool_source():
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }
        yield {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 9,
                "content_block": {"type": "tool_use", "id": "tool-9", "name": "Read"},
            },
        }

    stream_result = {}
    logger = logging.getLogger("test-stream-response-incomplete-tool")

    with caplog.at_level(logging.WARNING):
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=incomplete_tool_source(),
                model="claude-test",
                response_id="resp-stream-incomplete",
                output_item_id="msg-stream-incomplete",
                chunks_buffer=[],
                logger=logger,
                stream_result=stream_result,
            )
        ]

    parsed = [_parse_response_sse(line) for line in lines]
    assert parsed[-1][0] == "response.completed"
    assert stream_result["success"] is True
    assert "Incomplete tool_use blocks" in caplog.text


# ==================== New tests for error/task/usage handling ====================


class TestExtractSdkUsage:
    def test_returns_none_when_no_result(self):
        assert extract_sdk_usage([{"type": "assistant"}]) is None

    def test_returns_none_when_usage_missing(self):
        assert extract_sdk_usage([{"type": "result", "subtype": "success"}]) is None

    def test_extracts_basic_usage(self):
        chunks = [{"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}}]
        result = extract_sdk_usage(chunks)
        assert result == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    def test_includes_cache_tokens_in_prompt(self):
        chunks = [
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 300,
                },
            }
        ]
        result = extract_sdk_usage(chunks)
        assert result["prompt_tokens"] == 600  # 100 + 200 + 300
        assert result["completion_tokens"] == 50
        assert result["total_tokens"] == 650

    def test_picks_last_result_message(self):
        chunks = [
            {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"type": "assistant"},
            {"type": "result", "usage": {"input_tokens": 99, "output_tokens": 88}},
        ]
        result = extract_sdk_usage(chunks)
        assert result["prompt_tokens"] == 99
        assert result["completion_tokens"] == 88


@pytest.mark.asyncio
async def test_stream_chunks_emits_assistant_error():
    """AssistantMessage with error field emits error text and buffers the chunk."""

    async def error_source():
        yield {"type": "assistant", "error": "rate_limit", "content": []}

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    chunks_buffer = []
    lines = [
        line
        async for line in stream_chunks(
            error_source(), request, "req-error", chunks_buffer, logging.getLogger("test-error")
        )
    ]

    # Should have role + error text + fallback finish
    all_content = "".join(lines)
    assert "[Error: rate_limit]" in all_content
    # Error chunk should be buffered
    assert any(c.get("error") == "rate_limit" for c in chunks_buffer)


@pytest.mark.asyncio
async def test_stream_chunks_task_messages_as_structured_json():
    """Task system messages are emitted as structured JSON system_event, not content."""

    async def task_only_source():
        yield {"type": "system", "subtype": "task_started", "task_id": "t1", "description": "Analyzing code", "session_id": "s1"}
        yield {"type": "system", "subtype": "task_progress", "task_id": "t1", "description": "Reading files", "last_tool_name": "Read", "usage": {"tool_uses": 3}}
        yield {"type": "system", "subtype": "task_notification", "task_id": "t1", "status": "completed", "summary": "Done", "usage": {"tool_uses": 5}}

    request = ChatCompletionRequest(
        model="claude-test",
        messages=[Message(role="user", content="Hi")],
        stream=True,
    )
    lines = [
        line
        async for line in stream_chunks(
            task_only_source(), request, "req-task", [], logging.getLogger("test-task")
        )
    ]

    # Parse task event SSE lines (system_event field, empty delta)
    task_events = []
    for line in lines:
        if line.startswith("data: ") and "system_event" in line:
            parsed = json.loads(line[len("data: "):])
            task_events.append(parsed["system_event"])

    assert len(task_events) == 3
    assert task_events[0]["type"] == "task_started"
    assert task_events[0]["description"] == "Analyzing code"
    assert task_events[0]["task_id"] == "t1"
    assert task_events[1]["type"] == "task_progress"
    assert task_events[1]["last_tool_name"] == "Read"
    assert task_events[2]["type"] == "task_notification"
    assert task_events[2]["status"] == "completed"
    assert task_events[2]["summary"] == "Done"

    # Since no real content, fallback "unable to provide" should appear
    all_content = "".join(lines)
    assert "unable to provide a response" in all_content


@pytest.mark.asyncio
async def test_stream_response_chunks_assistant_error_emits_failed():
    """AssistantMessage.error triggers response.failed in Responses API."""

    async def error_source():
        yield {"type": "assistant", "error": "authentication_failed", "content": []}

    chunks_buffer = []
    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=error_source(),
            model="claude-test",
            response_id="resp-err",
            output_item_id="msg-err",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-resp-error"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]
    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "authentication_failed"
    assert stream_result["success"] is False
    # Error chunk should be in buffer
    assert any(c.get("error") == "authentication_failed" for c in chunks_buffer)


@pytest.mark.asyncio
async def test_stream_response_chunks_task_events_as_custom_sse():
    """Task events are emitted as custom SSE event types, not content."""

    async def task_only_source():
        yield {"type": "system", "subtype": "task_started", "task_id": "t1", "description": "Working", "session_id": "s1"}
        yield {"type": "system", "subtype": "task_notification", "task_id": "t1", "status": "completed", "summary": "Done"}

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=task_only_source(),
            model="claude-test",
            response_id="resp-task-only",
            output_item_id="msg-task-only",
            chunks_buffer=[],
            logger=logging.getLogger("test-resp-task-only"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [et for et, _ in parsed]

    # Task events should be custom SSE event types
    assert "response.task_started" in event_types
    assert "response.task_notification" in event_types

    # Verify task event payload — both SSE event name AND JSON type field must match
    task_started = next(p for et, p in parsed if et == "response.task_started")
    assert task_started["type"] == "response.task_started"
    assert task_started["task_id"] == "t1"
    assert task_started["description"] == "Working"

    task_done = next(p for et, p in parsed if et == "response.task_notification")
    assert task_done["type"] == "response.task_notification"
    assert task_done["status"] == "completed"

    # Task-only stream should still fail (no real content)
    assert parsed[-1][0] == "response.failed"
    assert parsed[-1][1]["response"]["error"]["code"] == "empty_response"
    assert stream_result["success"] is False
