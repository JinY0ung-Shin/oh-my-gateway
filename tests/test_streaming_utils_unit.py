#!/usr/bin/env python3
"""
Unit tests for src/streaming_utils.py.
"""

import json
import logging

import pytest

from src.response_models import OutputItem, ResponseObject
from src.streaming_utils import (
    CollabJsonStreamFilter,
    ToolUseAccumulator,
    bridge_sse_stream,
    extract_embedded_tool_blocks,
    extract_sdk_usage,
    extract_user_tool_results,
    format_chunk_content,
    make_response_sse,
    resolve_token_usage,
    stream_response_chunks,
    strip_collab_json,
)


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
    assert event_types.index("response.output_text.done") < event_types.index(
        "response.content_part.done"
    )
    assert event_types.index("response.content_part.done") < event_types.index(
        "response.output_item.done"
    )
    assert event_types.index("response.output_item.done") < event_types.index("response.completed")

    deltas = [
        payload["delta"]
        for event_type, payload in parsed
        if event_type == "response.output_text.delta"
    ]
    assert deltas[0] == "Hello"
    assert all("hidden" not in delta for delta in deltas)
    assert all("<think>" not in delta for delta in deltas)
    assert all("duplicate assistant payload" not in delta for delta in deltas)

    # tool_use and tool_result are now separate structured SSE events
    assert "response.tool_use" in event_types
    tool_use_events = [payload for et, payload in parsed if et == "response.tool_use"]
    assert tool_use_events[0]["name"] == "Read"
    assert tool_use_events[0]["input"] == {"path": "/tmp/demo.txt"}

    assert "response.tool_result" in event_types
    tool_result_events = [payload for et, payload in parsed if et == "response.tool_result"]
    assert tool_result_events[0]["tool_use_id"] == "tool-1"
    assert tool_result_events[0]["content"] == "done"

    completed_payload = parsed[-1][1]
    assert completed_payload["response"]["status"] == "completed"
    assert completed_payload["response"]["metadata"] == {"trace_id": "abc"}
    assert completed_payload["response"]["usage"]["input_tokens"] == 2
    assert completed_payload["response"]["usage"]["output_tokens"] > 0
    assert stream_result["success"] is True
    assert len(chunks_buffer) == 1
    assert chunks_buffer[0]["type"] == "user"


@pytest.mark.asyncio
async def test_stream_response_chunks_suppresses_ask_user_question_response_result():
    async def ask_user_question_continuation_source():
        yield {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_ask_1",
                    "content": "User responded: 그냥 테스트",
                    "is_error": True,
                }
            ],
        }
        yield {
            "type": "assistant",
            "content": [{"type": "text", "text": "잘 작동하네요."}],
        }
        yield {
            "type": "result",
            "subtype": "success",
            "result": "잘 작동하네요.",
        }

    chunks_buffer = []
    stream_result = {}

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=ask_user_question_continuation_source(),
            model="claude-test",
            response_id="resp-stream-ask",
            output_item_id="msg-stream-ask",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-stream-ask-user-question"),
            prompt_text="",
            metadata={},
            stream_result=stream_result,
            request_context={
                "continuation": True,
                "function_call_output_call_id": "toolu_ask_1",
            },
        )
    ]

    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [event_type for event_type, _payload in parsed]

    assert "response.tool_result" not in event_types
    assert [
        payload["delta"]
        for event_type, payload in parsed
        if event_type == "response.output_text.delta"
    ] == ["잘 작동하네요."]
    assert parsed[-1][0] == "response.completed"
    assert stream_result["success"] is True


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

    delta_payloads = [
        payload for event_type, payload in parsed if event_type == "response.output_text.delta"
    ]
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
async def test_stream_response_chunks_signals_empty_without_yielding_failed():
    """When no content is produced, signal via stream_result["empty"] rather
    than yielding response.failed directly. The route decides whether to emit
    a failed event or a function_call (AskUserQuestion hook path)."""
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

    # The final setup events (created/in_progress/output_item_added/content_part_added)
    # may still be yielded, but no response.failed should be emitted here.
    assert not any(event == "response.failed" for event, _ in parsed)
    assert stream_result["success"] is False
    assert stream_result["empty"] is True
    assert chunks_buffer == [{"type": "metadata"}]


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

    def test_falls_back_to_assistant_usage(self):
        """When no ResultMessage, sum per-turn AssistantMessage.usage (SDK 0.1.49+)."""
        chunks = [
            {"type": "assistant", "usage": {"input_tokens": 100, "output_tokens": 40}},
            {"type": "assistant", "usage": {"input_tokens": 120, "output_tokens": 60}},
        ]
        result = extract_sdk_usage(chunks)
        assert result == {"prompt_tokens": 220, "completion_tokens": 100, "total_tokens": 320}

    def test_result_usage_preferred_over_assistant(self):
        """ResultMessage.usage takes priority even when AssistantMessage.usage exists."""
        chunks = [
            {"type": "assistant", "usage": {"input_tokens": 50, "output_tokens": 20}},
            {"type": "result", "usage": {"input_tokens": 200, "output_tokens": 80}},
        ]
        result = extract_sdk_usage(chunks)
        assert result["prompt_tokens"] == 200
        assert result["completion_tokens"] == 80

    def test_assistant_usage_ignored_when_absent(self):
        """AssistantMessage without usage field does not contribute to fallback."""
        chunks = [
            {"type": "assistant", "content": []},
            {"type": "assistant", "usage": {"input_tokens": 50, "output_tokens": 30}},
        ]
        result = extract_sdk_usage(chunks)
        assert result == {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}

    def test_assistant_usage_includes_cache_tokens(self):
        """Assistant fallback includes cache_creation and cache_read tokens."""
        chunks = [
            {
                "type": "assistant",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 30,
                },
            },
        ]
        result = extract_sdk_usage(chunks)
        assert result["prompt_tokens"] == 180  # 100 + 50 + 30
        assert result["completion_tokens"] == 40


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
        yield {
            "type": "system",
            "subtype": "task_started",
            "task_id": "t1",
            "description": "Working",
            "session_id": "s1",
        }
        yield {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "t1",
            "status": "completed",
            "summary": "Done",
        }

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

    # Task-only stream has no assistant text → signals empty via stream_result.
    # The route converts that into a response.failed event (see
    # src/routes/responses.py), so this inner helper no longer yields it.
    assert not any(et == "response.failed" for et, _ in parsed)
    assert stream_result["success"] is False
    assert stream_result["empty"] is True


# ==================== Tests for refactored helpers ====================


class TestToolUseAccumulator:
    def test_non_stream_event_returns_not_handled(self):
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event({"type": "assistant"})
        assert handled is False
        assert result is None

    def test_accumulates_and_completes_tool_use(self):
        acc = ToolUseAccumulator()

        # content_block_start
        handled, result = acc.process_stream_event(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
                },
            }
        )
        assert handled is True
        assert result is None

        # content_block_delta (input_json_delta)
        handled, result = acc.process_stream_event(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":"/tmp/a.txt"}'},
                },
            }
        )
        assert handled is True
        assert result is None

        # content_block_stop
        handled, result = acc.process_stream_event(
            {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 0},
            }
        )
        assert handled is True
        assert result is not None
        assert result["name"] == "Read"
        assert result["input"] == {"path": "/tmp/a.txt"}
        assert "parent_tool_use_id" not in result

    def test_tracks_incomplete_blocks(self):
        acc = ToolUseAccumulator()
        acc.process_stream_event(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tool-1", "name": "Write"},
                },
            }
        )
        assert acc.has_incomplete is True
        assert len(acc.incomplete_keys) == 1

    def test_subagent_text_delta_is_skipped(self):
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event(
            {
                "type": "stream_event",
                "parent_tool_use_id": "parent-1",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "sub-agent output"},
                },
            }
        )
        assert handled is True
        assert result is None

    def test_includes_parent_tool_use_id_when_present(self):
        acc = ToolUseAccumulator()
        acc.process_stream_event(
            {
                "type": "stream_event",
                "parent_tool_use_id": "parent-1",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tool-1", "name": "Read"},
                },
            }
        )
        _, result = acc.process_stream_event(
            {
                "type": "stream_event",
                "parent_tool_use_id": "parent-1",
                "event": {"type": "content_block_stop", "index": 0},
            }
        )
        assert result["parent_tool_use_id"] == "parent-1"


class TestExtractUserToolResults:
    def test_extracts_tool_results_from_content(self):
        chunk = {
            "type": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "done"},
                {"type": "text", "text": "ignored"},
            ],
        }
        results, parent_id = extract_user_tool_results(chunk)
        assert len(results) == 1
        assert results[0]["tool_use_id"] == "tool-1"
        assert parent_id is None

    def test_extracts_from_message_content_fallback(self):
        chunk = {
            "type": "user",
            "content": "ignored-string",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool-2", "content": "ok"}]
            },
        }
        results, parent_id = extract_user_tool_results(chunk)
        assert len(results) == 1
        assert results[0]["tool_use_id"] == "tool-2"

    def test_returns_empty_when_no_tool_results(self):
        chunk = {"type": "user", "content": [{"type": "text", "text": "hi"}]}
        results, _ = extract_user_tool_results(chunk)
        assert results == []

    def test_returns_parent_id(self):
        chunk = {
            "type": "user",
            "parent_tool_use_id": "parent-1",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}],
        }
        results, parent_id = extract_user_tool_results(chunk)
        assert parent_id == "parent-1"


class TestFormatChunkContent:
    def test_formats_text_blocks(self):
        chunk = {"content": [{"type": "text", "text": "Hello world"}]}
        assert format_chunk_content(chunk, content_sent=False) == "Hello world"

    def test_returns_result_string(self):
        chunk = {"subtype": "success", "result": "Done"}
        assert format_chunk_content(chunk, content_sent=False) == "Done"

    def test_returns_none_for_result_when_content_already_sent(self):
        chunk = {"subtype": "success", "result": "Done"}
        assert format_chunk_content(chunk, content_sent=True) is None

    def test_returns_none_for_whitespace_only(self):
        chunk = {"content": [{"type": "text", "text": "   "}]}
        assert format_chunk_content(chunk, content_sent=False) is None

    def test_returns_none_for_empty_chunk(self):
        chunk = {"type": "metadata"}
        assert format_chunk_content(chunk, content_sent=False) is None


# ==================== Embedded tool blocks (collab_tool_call) ====================


class TestExtractEmbeddedToolBlocks:
    def test_extracts_tool_use_and_tool_result_from_assistant_content(self):
        chunk = {
            "type": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"prompt": "hi"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "done", "is_error": False},
                {"type": "text", "text": "Final answer"},
            ],
        }
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "tool_use"
        assert blocks[1]["type"] == "tool_result"

    def test_returns_empty_for_text_only_content(self):
        chunk = {
            "type": "assistant",
            "content": [{"type": "text", "text": "No tools here"}],
        }
        assert extract_embedded_tool_blocks(chunk) == []

    def test_returns_empty_for_non_assistant_chunk(self):
        chunk = {"type": "system", "subtype": "task_started"}
        assert extract_embedded_tool_blocks(chunk) == []

    def test_handles_message_wrapper(self):
        chunk = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        }
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 1
        assert blocks[0]["name"] == "Bash"


@pytest.mark.asyncio
async def test_stream_response_chunks_emits_embedded_tool_blocks_as_structured_sse():
    """Embedded tool blocks emit as response.tool_use/response.tool_result SSE."""

    async def embedded_tool_source():
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "agent_xyz",
                    "name": "Agent",
                    "input": {"prompt": "analyze"},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "agent_xyz",
                    "content": "Analysis complete",
                    "is_error": False,
                },
                {"type": "text", "text": "The analysis is done."},
            ],
        }

    chunks_buffer = []
    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=embedded_tool_source(),
            model="claude-test",
            response_id="resp-embedded-tool",
            output_item_id="msg-embedded-tool",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-embedded-resp-tool"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]
    event_types = [et for et, _ in parsed]

    # Should have structured tool events
    assert "response.tool_use" in event_types
    assert "response.tool_result" in event_types

    tool_use_ev = next(p for et, p in parsed if et == "response.tool_use")
    assert tool_use_ev["name"] == "Agent"
    assert tool_use_ev["tool_use_id"] == "agent_xyz"
    assert tool_use_ev["input"] == {"prompt": "analyze"}

    tool_result_ev = next(p for et, p in parsed if et == "response.tool_result")
    assert tool_result_ev["tool_use_id"] == "agent_xyz"
    assert tool_result_ev["content"] == "Analysis complete"

    # Text content should also be emitted as delta
    deltas = [p["delta"] for et, p in parsed if et == "response.output_text.delta"]
    assert "The analysis is done." in deltas

    assert stream_result["success"] is True
    assert parsed[-1][0] == "response.completed"


# ---------------------------------------------------------------------------
# strip_collab_json tests
# ---------------------------------------------------------------------------


class TestStripCollabJson:
    """Tests for the strip_collab_json utility."""

    def test_no_collab_returns_unchanged(self):
        text = "Hello world. Normal text here."
        assert strip_collab_json(text) == text

    def test_strips_single_collab_block(self):
        collab = json.dumps({"collab_tool_call": {"type": "spawn_agent", "prompt": "hi"}})
        text = f"Before{collab}After"
        assert strip_collab_json(text) == "BeforeAfter"

    def test_strips_multiple_collab_blocks(self):
        c1 = json.dumps({"collab_tool_call": {"type": "spawn_agent", "prompt": "a"}})
        c2 = json.dumps({"collab_tool_call": {"type": "wait", "agents_states": {}}})
        text = f"Start\n{c1}\nMiddle\n{c2}\nEnd"
        result = strip_collab_json(text)
        assert "collab_tool_call" not in result
        assert "Start" in result
        assert "Middle" in result
        assert "End" in result

    def test_preserves_non_collab_json(self):
        text = 'Use {"key": "value"} in your config.'
        assert strip_collab_json(text) == text

    def test_handles_braces_in_json_strings(self):
        collab = json.dumps(
            {
                "collab_tool_call": {
                    "type": "wait",
                    "agents_states": {"t1": {"message": "Found {3} files"}},
                }
            }
        )
        text = f"Result: {collab} done."
        result = strip_collab_json(text)
        assert "collab_tool_call" not in result
        assert "Result:" in result
        assert "done." in result

    def test_empty_string(self):
        assert strip_collab_json("") == ""


# ---------------------------------------------------------------------------
# CollabJsonStreamFilter tests
# ---------------------------------------------------------------------------


class TestCollabJsonStreamFilter:
    """Tests for the streaming collab JSON filter."""

    def test_plain_text_passes_through(self):
        f = CollabJsonStreamFilter()
        assert f.feed("Hello world") == "Hello world"
        assert f.flush() == ""

    def test_filters_collab_json_in_single_delta(self):
        collab = json.dumps({"collab_tool_call": {"type": "spawn_agent"}})
        f = CollabJsonStreamFilter()
        result = f.feed(f"Before{collab}After")
        assert "collab_tool_call" not in result
        assert "Before" in result
        assert "After" in result

    def test_filters_collab_json_split_across_deltas(self):
        collab = json.dumps({"collab_tool_call": {"type": "spawn_agent", "prompt": "test"}})
        f = CollabJsonStreamFilter()
        output_parts = []
        # Split the collab JSON across character-by-character deltas
        full_text = f"Hello {collab} World"
        for ch in full_text:
            out = f.feed(ch)
            if out:
                output_parts.append(out)
        remaining = f.flush()
        if remaining:
            output_parts.append(remaining)
        result = "".join(output_parts)
        assert "collab_tool_call" not in result
        assert "Hello" in result
        assert "World" in result

    def test_non_collab_json_passes_through(self):
        f = CollabJsonStreamFilter()
        result = f.feed('Use {"key": "val"} here')
        result += f.flush()
        assert '{"key": "val"}' in result

    def test_buffering_property(self):
        f = CollabJsonStreamFilter()
        assert not f.buffering
        f.feed("{")
        assert f.buffering
        f.flush()
        assert not f.buffering

    def test_flush_returns_buffered_text(self):
        f = CollabJsonStreamFilter()
        f.feed("{incomplete")
        remaining = f.flush()
        assert remaining == "{incomplete"


# ---------------------------------------------------------------------------
# stream_response_chunks collab filtering test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_response_chunks_strips_collab_from_token_deltas():
    """Token-level text deltas containing collab_tool_call JSON should be stripped."""
    collab = json.dumps({"collab_tool_call": {"type": "spawn_agent", "prompt": "test"}})
    full_text = f"Hello {collab} World"

    async def source():
        # Simulate token-level streaming with collab JSON in text deltas
        for ch in full_text:
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": ch},
                },
            }

    chunks_buffer = []
    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="test-model",
            response_id="resp-collab-strip",
            output_item_id="msg-collab-strip",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-collab-strip"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]
    deltas = [p.get("delta", "") for et, p in parsed if et == "response.output_text.delta"]
    combined = "".join(d for d in deltas if isinstance(d, str))
    assert "collab_tool_call" not in combined
    assert "Hello" in combined
    assert "World" in combined
    assert stream_result["success"] is True


# ===========================================================================
# resolve_token_usage tests
# ===========================================================================


class TestResolveTokenUsage:
    """Tests for resolve_token_usage() fallback paths."""

    def test_returns_sdk_usage_when_available(self):
        """Path 1: SDK usage present → use it directly."""
        chunks = [{"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}}]
        p, c = resolve_token_usage(chunks, "ignored prompt", "ignored completion")
        assert p == 100
        assert c == 50

    def test_falls_back_to_backend_estimation(self):
        """Path 2: No SDK usage, backend provided → use backend.estimate_token_usage."""
        from unittest.mock import MagicMock

        backend = MagicMock()
        backend.estimate_token_usage.return_value = {
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        }
        p, c = resolve_token_usage([], "prompt", "completion", "model-x", backend=backend)
        assert p == 200
        assert c == 80
        backend.estimate_token_usage.assert_called_once_with("prompt", "completion", "model-x")

    def test_falls_back_to_character_estimation(self):
        """Path 3: No SDK usage, no backend → MessageAdapter.estimate_tokens."""
        p, c = resolve_token_usage([], "abcd", "efghijkl")
        # MessageAdapter.estimate_tokens = len(text) // 4
        assert p == 1  # len("abcd") // 4
        assert c == 2  # len("efghijkl") // 4

    def test_sdk_usage_takes_priority_over_backend(self):
        """SDK usage wins even when backend is provided."""
        from unittest.mock import MagicMock

        backend = MagicMock()
        chunks = [{"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}]
        p, c = resolve_token_usage(chunks, "prompt", "text", "model", backend=backend)
        assert p == 10
        assert c == 5
        backend.estimate_token_usage.assert_not_called()

    def test_includes_cache_tokens_in_sdk_usage(self):
        """SDK usage includes cache_creation + cache_read tokens."""
        chunks = [
            {
                "type": "result",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 30,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 5,
                },
            }
        ]
        p, c = resolve_token_usage(chunks, "", "")
        assert p == 65  # 50 + 10 + 5
        assert c == 30


# ===========================================================================
# bridge_sse_stream tests
# ===========================================================================


class TestBridgeSSEStream:
    """Tests for bridge_sse_stream() async generator bridge."""

    async def test_forwards_lines_from_source(self):
        """Lines from the SSE source are yielded in order."""

        async def source():
            yield "data: line1\n\n"
            yield "data: line2\n\n"

        class FakeChunkSource:
            async def aclose(self):
                pass

        lines = [line async for line in bridge_sse_stream(source(), FakeChunkSource())]
        assert lines == ["data: line1\n\n", "data: line2\n\n"]

    async def test_propagates_source_error(self):
        """Errors raised inside the source propagate to the consumer."""

        async def failing_source():
            yield "data: ok\n\n"
            raise ValueError("boom")

        class FakeChunkSource:
            async def aclose(self):
                pass

        with pytest.raises(ValueError, match="boom"):
            lines = []
            async for line in bridge_sse_stream(failing_source(), FakeChunkSource()):
                lines.append(line)

        assert lines == ["data: ok\n\n"]

    async def test_closes_chunk_source_on_completion(self):
        """chunk_source.aclose() is called when the stream completes normally."""

        async def source():
            yield "data: done\n\n"

        class TrackingChunkSource:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        cs = TrackingChunkSource()
        _ = [line async for line in bridge_sse_stream(source(), cs)]
        assert cs.closed is True

    async def test_closes_chunk_source_on_error(self):
        """chunk_source.aclose() is called even when the source raises."""

        async def failing_source():
            raise RuntimeError("fail")
            yield  # make it an async generator  # pragma: no cover

        class TrackingChunkSource:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        cs = TrackingChunkSource()
        with pytest.raises(RuntimeError, match="fail"):
            async for _ in bridge_sse_stream(failing_source(), cs):
                pass

        assert cs.closed is True

    async def test_handles_empty_source(self):
        """An empty source yields nothing and still cleans up."""

        async def empty_source():
            return
            yield  # make it an async generator  # pragma: no cover

        class TrackingChunkSource:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        cs = TrackingChunkSource()
        lines = [line async for line in bridge_sse_stream(empty_source(), cs)]
        assert lines == []
        assert cs.closed is True
