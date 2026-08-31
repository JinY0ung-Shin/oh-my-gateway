#!/usr/bin/env python3
"""
Unit tests for src/streaming_utils.py.
"""

import json
import logging

import pytest

from claude_agent_sdk.types import ServerToolResultBlock, ServerToolUseBlock
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
    # Message output_item.done (skip any reasoning output_item.done that precedes it).
    msg_item_done_idx = next(
        i for i, (et, p) in enumerate(parsed)
        if et == "response.output_item.done" and p["item"]["type"] == "message"
    )
    assert event_types.index("response.content_part.done") < msg_item_done_idx
    assert msg_item_done_idx < event_types.index("response.completed")

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
        # Registry patch — the only terminal signal for killed/background tasks
        yield {
            "type": "system",
            "subtype": "task_updated",
            "task_id": "t2",
            "patch": {"status": "killed"},
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

    # task_updated is forwarded even without a tool_use_id (registry-level
    # event) — status derived from the patch, raw patch passed through.
    assert "response.task_updated" in event_types
    task_updated = next(p for et, p in parsed if et == "response.task_updated")
    assert task_updated["type"] == "response.task_updated"
    assert task_updated["task_id"] == "t2"
    assert task_updated["status"] == "killed"
    assert task_updated["patch"] == {"status": "killed"}

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


@pytest.mark.asyncio
async def test_stream_response_chunks_preserves_server_tool_block_types():
    """SDK server/advisor blocks emit as distinct structured SSE events."""

    async def embedded_server_tool_source():
        yield {
            "type": "assistant",
            "content": [
                ServerToolUseBlock(id="srv_xyz", name="web_search", input={"query": "docs"}),
                ServerToolResultBlock(tool_use_id="srv_xyz", content="Found docs"),
                {"type": "text", "text": "Done."},
            ],
        }

    chunks_buffer = []
    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=embedded_server_tool_source(),
            model="claude-test",
            response_id="resp-server-tool",
            output_item_id="msg-server-tool",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-server-tool-blocks"),
            stream_result=stream_result,
        )
    ]
    parsed = [_parse_response_sse(line) for line in lines]

    server_event = next(p for et, p in parsed if et == "response.server_tool_use")
    assert server_event["block"] == {
        "type": "server_tool_use",
        "id": "srv_xyz",
        "name": "web_search",
        "input": {"query": "docs"},
    }

    result_event = next(p for et, p in parsed if et == "response.advisor_tool_result")
    assert result_event["block"] == {
        "type": "advisor_tool_result",
        "tool_use_id": "srv_xyz",
        "content": "Found docs",
    }
    assert not any(et == "response.tool_use" for et, _ in parsed)
    assert stream_result["success"] is True


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
        # A '{' followed by a quote stays buffered as a potential collab block
        # until the stream ends, so flush() returns the partial text.
        assert f.feed('{"incomplete') == ""
        remaining = f.flush()
        assert remaining == '{"incomplete'


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


class TestExtractThinkingTexts:
    def test_returns_thinking_block_text_in_order(self):
        from src.streaming_utils import extract_thinking_texts

        chunks = [
            {"type": "assistant", "content": [
                {"type": "thinking", "thinking": "first"},
                {"type": "text", "text": "hi"},
                {"type": "thinking", "thinking": "second"},
            ]},
        ]
        assert extract_thinking_texts(chunks) == ["first", "second"]

    def test_returns_empty_when_no_thinking(self):
        from src.streaming_utils import extract_thinking_texts

        chunks = [{"type": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        assert extract_thinking_texts(chunks) == []

    def test_handles_object_blocks_with_attributes(self):
        from src.streaming_utils import extract_thinking_texts

        class _ThinkingBlock:
            def __init__(self, t):
                self.thinking = t

        chunks = [{"content": [_ThinkingBlock("hidden")]}]
        assert extract_thinking_texts(chunks) == ["hidden"]


async def test_stream_emits_message_item_after_first_text_when_no_thinking():
    """No thinking → message output_item.added still fires, just deferred until first text."""
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"subtype": "success", "result": "hi"}

    events: list[tuple[str, dict]] = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    # response.output_item.added must come BEFORE response.output_text.delta
    first_delta_idx = types.index("response.output_text.delta")
    added_idx = types.index("response.output_item.added")
    assert added_idx < first_delta_idx
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"


async def test_stream_opens_reasoning_on_first_thinking_delta():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    # output_item.added for reasoning
    assert "response.output_item.added" in types
    added_idx = types.index("response.output_item.added")
    _, added_payload = events[added_idx]
    assert added_payload["item"]["type"] == "reasoning"
    assert added_payload["output_index"] == 0
    # reasoning_summary_part.added present
    assert "response.reasoning_summary_part.added" in types


async def test_stream_emits_summary_and_reasoning_text_deltas_with_same_text():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        for piece in ("abc", "def"):
            yield {"type": "stream_event", "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": piece},
            }}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    summary_deltas = [p for t, p in events if t == "response.reasoning_summary_text.delta"]
    reasoning_deltas = [p for t, p in events if t == "response.reasoning_text.delta"]
    assert [d["delta"] for d in summary_deltas] == ["abc", "def"]
    assert [d["delta"] for d in reasoning_deltas] == ["abc", "def"]
    assert all(d["output_index"] == 0 for d in summary_deltas + reasoning_deltas)
    assert all(d["summary_index"] == 0 for d in summary_deltas)
    assert all(d["content_index"] == 0 for d in reasoning_deltas)


async def test_stream_closes_reasoning_with_accumulated_text():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hello"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "world"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}
        yield {"subtype": "success", "result": "world"}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    done_summary = next(p for t, p in events if t == "response.reasoning_summary_text.done")
    assert done_summary["text"] == "hello"
    done_reasoning = next(p for t, p in events if t == "response.reasoning_text.done")
    assert done_reasoning["text"] == "hello"
    # reasoning output_item.done must come BEFORE message output_item.added
    r_done_idx = next(i for i, (t, p) in enumerate(events)
                      if t == "response.output_item.done" and p["item"]["type"] == "reasoning")
    msg_added_idx = next(i for i, (t, p) in enumerate(events)
                         if t == "response.output_item.added" and p["item"]["type"] == "message")
    assert r_done_idx < msg_added_idx
    # Message lands at output_index=1 (reasoning was at 0)
    _, msg_added = events[msg_added_idx]
    assert msg_added["output_index"] == 1


async def test_stream_interleaved_think_text_think_text_preserves_second_block():
    """Fully-interleaved think → text → think → text must NOT drop the 2nd block.

    The OpenAI Responses ``output`` array is an ordered sequence, so a new
    thinking block after text closes the current message item and opens its
    own reasoning item, then a fresh message item for the trailing text. All
    four blocks (r1, m1, r2, m2) survive and stream live.
    """
    import logging
    from src.streaming_utils import stream_response_chunks

    def _think_start(idx):
        return {"type": "stream_event", "event": {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "thinking", "thinking": ""},
        }}

    def _think_delta(idx, text):
        return {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "thinking_delta", "thinking": text},
        }}

    def _text_start(idx):
        return {"type": "stream_event", "event": {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "text", "text": ""},
        }}

    def _text_delta(idx, text):
        return {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "text_delta", "text": text},
        }}

    def _stop(idx):
        return {"type": "stream_event", "event": {"type": "content_block_stop", "index": idx}}

    async def chunk_source():
        yield _think_start(0)
        yield _think_delta(0, "ponder-A")
        yield _stop(0)
        yield _text_start(1)
        yield _text_delta(1, "answer-A")
        yield _stop(1)
        yield _think_start(2)
        yield _think_delta(2, "ponder-B")
        yield _stop(2)
        yield _text_start(3)
        yield _text_delta(3, "answer-B")
        yield _stop(3)

    events = []
    stream_result: dict = {}
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
        stream_result=stream_result,
    ):
        events.append(_parse_response_sse(line))

    # Both thinking blocks must be streamed as reasoning text (the 2nd is the regression).
    reasoning_deltas = [p["delta"] for t, p in events if t == "response.reasoning_text.delta"]
    assert "ponder-A" in reasoning_deltas
    assert "ponder-B" in reasoning_deltas, "2nd thinking block was dropped"

    # Both text runs must be streamed live as message deltas.
    text_deltas = [p["delta"] for t, p in events if t == "response.output_text.delta"]
    assert text_deltas == ["answer-A", "answer-B"]

    # Two reasoning items and two message items closed, interleaved in order.
    done_types = [
        p["item"]["type"]
        for t, p in events
        if t == "response.output_item.done"
    ]
    assert done_types == ["reasoning", "message", "reasoning", "message"]

    # output_index increases monotonically across the four items: 0,1,2,3.
    done_indices = [p["output_index"] for t, p in events if t == "response.output_item.done"]
    assert done_indices == [0, 1, 2, 3]

    # The final response.completed carries all four items in order.
    completed = next(p for t, p in events if t == "response.completed")
    out_types = [item["type"] for item in completed["response"]["output"]]
    assert out_types == ["reasoning", "message", "reasoning", "message"]

    # The full visible text handed back to the route is the concatenation of
    # both message segments (not just the last one).
    assert stream_result["assistant_text"] == "answer-Aanswer-B"
    assert stream_result["thinking_texts"] == ["ponder-A", "ponder-B"]


# Regression: the common case (single thinking block then text) must still
# stream text deltas live, not buffer them until end-of-stream.


async def test_stream_text_after_thinking_emits_deltas_live_not_buffered():
    """After reasoning closes, text deltas must be emitted as they arrive,
    not buffered until end-of-stream."""
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "T"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "first"},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "second"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}
        yield {"subtype": "success", "result": "firstsecond"}

    saw_first_text_delta = False
    saw_second_text_delta = False
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        ev_type, payload = _parse_response_sse(line)
        if ev_type == "response.output_text.delta":
            if payload.get("delta") == "first":
                saw_first_text_delta = True
            elif payload.get("delta") == "second":
                saw_second_text_delta = True

    assert saw_first_text_delta and saw_second_text_delta, \
        "Text deltas were not emitted (or were buffered and lost)"


async def test_stream_thinking_only_emits_empty_message_item():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "thoughts"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"subtype": "success", "result": ""}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    # Reasoning item should be closed (output_item.done with item.type=reasoning).
    assert any(t == "response.output_item.done" and p["item"]["type"] == "reasoning"
               for t, p in events)
    # Message item should still be emitted, with output_index=1 (after the reasoning at 0).
    msg_added = next(p for t, p in events
                     if t == "response.output_item.added" and p["item"]["type"] == "message")
    assert msg_added["output_index"] == 1
    msg_done = next(p for t, p in events
                    if t == "response.output_item.done" and p["item"]["type"] == "message")
    assert msg_done is not None
    assert types[-1] == "response.completed"


async def test_full_reasoning_lifecycle_streaming():
    """End-to-end: think -> text stream produces a complete OpenAI Responses sequence.

    Reasoning item open -> deltas -> done -> message item open -> text delta -> message close -> completed.
    """
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "I should "},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "say hello"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "Hi!"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}
        yield {"subtype": "success", "result": "Hi!"}

    types = []
    payloads = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        t, p = _parse_response_sse(line)
        types.append(t)
        payloads.append(p)

    # Required event types in any order:
    required = {
        "response.created",
        "response.in_progress",
        "response.output_item.added",      # reasoning + message (2 occurrences)
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",       # reasoning + message (2 occurrences)
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.completed",
    }
    missing = required - set(types)
    assert not missing, f"missing event types: {missing}"

    # Ordering: reasoning output_item.added must be BEFORE message output_item.added
    reasoning_added_idx = next(i for i, (t, p) in enumerate(zip(types, payloads))
                               if t == "response.output_item.added" and p["item"]["type"] == "reasoning")
    message_added_idx = next(i for i, (t, p) in enumerate(zip(types, payloads))
                             if t == "response.output_item.added" and p["item"]["type"] == "message")
    assert reasoning_added_idx < message_added_idx

    # Reasoning done text equals concatenated thinking
    summary_done = next(p for t, p in zip(types, payloads) if t == "response.reasoning_summary_text.done")
    assert summary_done["text"] == "I should say hello"

    # Text delta carried "Hi!"
    text_delta = next(p for t, p in zip(types, payloads) if t == "response.output_text.delta")
    assert text_delta["delta"] == "Hi!"

    # Sequence_number monotonic across events
    seqs = [p["sequence_number"] for p in payloads if "sequence_number" in p]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    # Completed last
    assert types[-1] == "response.completed"


@pytest.mark.asyncio
async def test_response_completed_payload_includes_reasoning_items():
    """response.completed.response.output must include reasoning items emitted earlier in the stream."""
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reasoning..."},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "answer"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}
        yield {"subtype": "success", "result": "answer"}

    completed_event = None
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        t, p = _parse_response_sse(line)
        if t == "response.completed":
            completed_event = p

    assert completed_event is not None
    output = completed_event["response"]["output"]
    item_types = [item["type"] for item in output]
    assert item_types == ["reasoning", "message"]
    assert output[0]["summary"][0]["text"] == "reasoning..."
    assert output[0]["content"][0]["text"] == "reasoning..."
    assert output[1]["content"][0]["text"] == "answer"


@pytest.mark.asyncio
async def test_stream_result_captures_thinking_texts_for_session_history(caplog):
    """The route uses stream_result to persist thinking in admin session history."""
    import logging
    from src.streaming_utils import stream_response_chunks

    caplog.set_level("INFO")

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reasoning..."},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "answer"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}

    stream_result = {}
    async for _ in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
        stream_result=stream_result,
    ):
        pass

    assert stream_result["success"] is True
    assert stream_result["assistant_text"] == "answer"
    assert stream_result["thinking_texts"] == ["reasoning..."]
    assert "Responses stream captured thinking block" in caplog.text
    assert "thinking_blocks=1" in caplog.text


@pytest.mark.asyncio
async def test_stream_warns_when_thinking_delta_arrives_outside_thinking_block(caplog):
    import logging
    from src.streaming_utils import stream_response_chunks

    caplog.set_level("WARNING")

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "misframed"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}

    async for _ in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        pass

    assert "thinking_delta outside a thinking block" in caplog.text


async def test_stream_second_thinking_after_text_is_silently_dropped_not_leaked():
    """text → thinking → text: the second thinking must not leak into output_text.delta
    or into response.completed.response.output."""
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        # First a tiny text block so message_item gets opened.
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "first text"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        # Then a thinking block (post-message). Must be dropped from output_text.
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "thinking_delta", "thinking": "SECRET_REASONING"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}
        # More visible text.
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 2,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 2,
            "delta": {"type": "text_delta", "text": "second text"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 2}}
        yield {"subtype": "success", "result": "first textsecond text"}

    text_deltas = []
    completed_event = None
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        t, p = _parse_response_sse(line)
        if t == "response.output_text.delta":
            text_deltas.append(p.get("delta", ""))
        if t == "response.completed":
            completed_event = p

    joined = "".join(text_deltas)
    assert "SECRET_REASONING" not in joined, \
        f"Thinking content leaked into output_text.delta: {joined!r}"

    # Also assert against response.completed.response.output
    assert completed_event is not None
    for item in completed_event["response"]["output"]:
        if item["type"] == "message":
            for part in item.get("content", []):
                assert "SECRET_REASONING" not in part.get("text", ""), \
                    f"Thinking leaked into completed message: {part!r}"


class TestExtractVisibleAssistantText:
    def test_prefers_result_message_when_present(self):
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden reasoning"},
                {"type": "text", "text": "the answer"},
            ]},
            {"subtype": "success", "result": "the answer"},
        ]
        assert extract_visible_assistant_text(chunks) == "the answer"

    def test_falls_back_to_text_blocks_excluding_thinking(self):
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden reasoning"},
                {"type": "text", "text": "visible part 1"},
                {"type": "thinking", "thinking": "more hidden"},
                {"type": "text", "text": "visible part 2"},
            ]},
        ]
        out = extract_visible_assistant_text(chunks)
        assert "hidden reasoning" not in out
        assert "more hidden" not in out
        assert "visible part 1" in out
        assert "visible part 2" in out

    def test_returns_none_when_no_text_content(self):
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [
                {"type": "thinking", "thinking": "only thinking"},
            ]},
        ]
        assert extract_visible_assistant_text(chunks) is None

    def test_preserves_literal_think_tags_in_text_blocks(self):
        """A text block whose .text contains literal '<think>...</think>' must be preserved."""
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [
                {"type": "text", "text": "use the <think>foo</think> tag for X"},
            ]},
        ]
        assert extract_visible_assistant_text(chunks) == "use the <think>foo</think> tag for X"

    def test_handles_object_blocks_with_attributes(self):
        from src.streaming_utils import extract_visible_assistant_text

        class _ThinkingBlock:
            def __init__(self, t):
                self.thinking = t

        class _TextBlock:
            def __init__(self, t):
                self.text = t

        chunks = [{"content": [_ThinkingBlock("hidden"), _TextBlock("visible")]}]
        out = extract_visible_assistant_text(chunks)
        assert "hidden" not in out
        assert "visible" in out

    def test_consecutive_text_blocks_in_same_message_join_without_separator(self):
        """Mirrors MessageAdapter.format_blocks join: same-message text blocks
        are concatenated with no separator. Crossing chunk boundaries still
        uses '\\n' (matches parse_message)."""
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [
                {"type": "text", "text": "Hello"},
                {"type": "text", "text": " world"},
            ]},
        ]
        assert extract_visible_assistant_text(chunks) == "Hello world"

    def test_thinking_between_text_blocks_in_same_message_does_not_add_separator(self):
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [
                {"type": "text", "text": "ab"},
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "cd"},
            ]},
        ]
        # The two text blocks are concatenated as if the thinking weren't there.
        assert extract_visible_assistant_text(chunks) == "abcd"

    def test_separate_messages_joined_with_newline(self):
        from src.streaming_utils import extract_visible_assistant_text

        chunks = [
            {"type": "assistant", "content": [{"type": "text", "text": "first"}]},
            {"type": "assistant", "content": [{"type": "text", "text": "second"}]},
        ]
        assert extract_visible_assistant_text(chunks) == "first\nsecond"


# ---------------------------------------------------------------------------
# Liveness / "still working" progress signals (tool_use_started, hook events,
# compaction). These let a UI show activity in the gaps between text.
# ---------------------------------------------------------------------------


async def _collect_response_events(source):
    import logging
    from src.streaming_utils import stream_response_chunks

    events = []
    stream_result: dict = {}
    async for line in stream_response_chunks(
        source,
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
        stream_result=stream_result,
    ):
        events.append(_parse_response_sse(line))
    return events, stream_result


async def test_stream_emits_tool_use_started_before_full_tool_use():
    """`response.tool_use_started` fires at content_block_start, before the
    accumulated `response.tool_use` with the full arguments."""

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command":'},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"ls"}'},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    types = [t for t, _ in events]

    assert "response.tool_use_started" in types, "no tool_use_started signal emitted"
    started_idx = types.index("response.tool_use_started")
    full_idx = types.index("response.tool_use")
    assert started_idx < full_idx, "started must precede the completed tool_use"

    started = events[started_idx][1]
    assert started["tool_use_id"] == "tu_1"
    assert started["name"] == "Bash"
    # No arguments yet on the started signal.
    assert "input" not in started

    # The completed tool_use carries the assembled arguments.
    full = events[full_idx][1]
    assert full["tool_use_id"] == "tu_1"
    assert full["input"] == {"command": "ls"}


async def test_stream_tool_use_started_disabled_by_flag(monkeypatch):
    monkeypatch.setattr("src.streaming_utils.STREAM_TOOL_PROGRESS", False)

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    types = [t for t, _ in events]
    assert "response.tool_use_started" not in types
    # The completed tool_use still flows.
    assert "response.tool_use" in types


async def test_stream_forwards_hook_lifecycle_events():
    """hook_started/hook_response system messages become response.hook_event."""

    async def chunk_source():
        yield {
            "type": "system", "subtype": "hook_started", "hook_event_name": "PreToolUse",
            "data": {"hook_event": "PreToolUse", "tool_name": "Bash", "tool_use_id": "tu_9"},
            "session_id": "s1",
        }
        yield {
            "type": "system", "subtype": "hook_response", "hook_event_name": "PostToolUse",
            "data": {
                "hook_event": "PostToolUse", "tool_name": "Bash",
                "tool_use_id": "tu_9", "outcome": "success",
            },
            "session_id": "s1",
        }
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }}

    events, _ = await _collect_response_events(chunk_source())
    hook_events = [p for t, p in events if t == "response.hook_event"]
    assert len(hook_events) == 2

    assert hook_events[0]["phase"] == "hook_started"
    assert hook_events[0]["hook_event_name"] == "PreToolUse"
    assert hook_events[0]["tool_name"] == "Bash"
    assert hook_events[0]["tool_use_id"] == "tu_9"
    assert "parent_tool_use_id" not in hook_events[0]

    assert hook_events[1]["phase"] == "hook_response"
    assert hook_events[1]["hook_event_name"] == "PostToolUse"
    assert hook_events[1]["outcome"] == "success"
    assert "parent_tool_use_id" not in hook_events[1]

    # Normal content still flows after the hook events.
    deltas = [p.get("delta") for t, p in events if t == "response.output_text.delta"]
    assert "ok" in deltas


async def test_stream_hook_events_disabled_by_flag(monkeypatch):
    monkeypatch.setattr("src.sse_builders.STREAM_HOOK_EVENTS", False)

    async def chunk_source():
        yield {
            "type": "system", "subtype": "hook_started", "hook_event_name": "PreToolUse",
            "data": {"hook_event": "PreToolUse", "tool_name": "Bash"},
        }
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }}

    events, _ = await _collect_response_events(chunk_source())
    assert not any(t == "response.hook_event" for t, _ in events)


async def test_stream_hook_event_preserves_explicit_parent_tool_use_id():
    """Only an explicit parent_tool_use_id marks hook_event nesting."""

    async def chunk_source():
        yield {
            "type": "system", "subtype": "hook_started", "hook_event_name": "PreToolUse",
            "parent_tool_use_id": "parent_1",
            "data": {"hook_event": "PreToolUse", "tool_name": "Bash", "tool_use_id": "tu_child"},
        }
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }}

    events, _ = await _collect_response_events(chunk_source())
    hook_event = next(p for t, p in events if t == "response.hook_event")
    assert hook_event["tool_use_id"] == "tu_child"
    assert hook_event["parent_tool_use_id"] == "parent_1"


async def test_stream_top_level_hook_event_not_gated_as_subagent(monkeypatch):
    """A hook's tool_use_id is its target tool, not a subagent parent marker."""
    monkeypatch.setattr("src.streaming_utils.SUBAGENT_STREAM_PROGRESS", False)

    async def chunk_source():
        yield {
            "type": "system", "subtype": "hook_started", "hook_event_name": "PreToolUse",
            "data": {"hook_event": "PreToolUse", "tool_name": "Bash", "tool_use_id": "tu_top"},
        }
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }}

    events, _ = await _collect_response_events(chunk_source())
    hook_event = next(p for t, p in events if t == "response.hook_event")
    assert hook_event["tool_use_id"] == "tu_top"
    assert "parent_tool_use_id" not in hook_event


async def test_stream_forwards_compaction_event():
    """compact_boundary system messages become response.compaction."""

    async def chunk_source():
        yield {
            "type": "system", "subtype": "compact_boundary",
            "data": {"trigger": "auto"}, "session_id": "s1",
        }
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }}

    events, _ = await _collect_response_events(chunk_source())
    compaction = [p for t, p in events if t == "response.compaction"]
    assert len(compaction) == 1
    assert compaction[0]["subtype"] == "compact_boundary"
    assert compaction[0]["trigger"] == "auto"


# ---------------------------------------------------------------------------
# Agent-team teammate messages. A teammate's SendMessage reaches the leader as
# an injected plain `user` message, whose text the tool_result path drops.
# ---------------------------------------------------------------------------


# Shaped like the CLI's injected body: the opener, the sender's from= address,
# and the trailing guidance whose literal backticked `from=` must not be
# mistaken for an address.
TEAMMATE_TEXT = (
    "Another Claude session sent a message while you were working:\n\n"
    "from=reviewer-2\nThe auth middleware test is flaky on retry.\n\n"
    "This came from another Claude session — reply by sending a message with "
    "SendMessage to the `from=` address."
)


def _teammate_user_chunk(text=TEAMMATE_TEXT, **overrides):
    chunk = {
        "type": "user",
        "content": [{"type": "text", "text": text}],
        "session_id": "s1",
    }
    chunk.update(overrides)
    return chunk


async def test_stream_emits_teammate_message_event():
    """Injected teammate text becomes response.teammate_message with a from."""

    async def chunk_source():
        yield _teammate_user_chunk()
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    teammate = [p for t, p in events if t == "response.teammate_message"]
    assert len(teammate) == 1
    # Raw text passes through untransformed.
    assert teammate[0]["text"] == TEAMMATE_TEXT
    # The real address wins over the backticked `from=` in the guidance.
    assert teammate[0]["from"] == "reviewer-2"
    assert teammate[0]["session_id"] == "s1"
    assert isinstance(teammate[0]["sequence_number"], int)


async def test_stream_teammate_message_requires_both_markers():
    """Only the opener, or only the explanation, is not a teammate message."""

    async def chunk_source():
        yield _teammate_user_chunk("Another Claude session sent a message: hello")
        yield _teammate_user_chunk(
            "This came from another Claude session — quoting it in my answer."
        )
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    assert not any(t == "response.teammate_message" for t, _ in events)


async def test_stream_teammate_message_skipped_for_subagent_chunk():
    """A nested chunk is a subagent's own transcript, not a leader message."""

    async def chunk_source():
        yield _teammate_user_chunk(parent_tool_use_id="toolu_parent")
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    assert not any(t == "response.teammate_message" for t, _ in events)


async def test_stream_tool_result_user_chunk_emits_no_teammate_message():
    """A plain tool_result user chunk keeps its existing behavior."""

    async def chunk_source():
        yield {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "file contents",
                }
            ],
        }
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    types = [t for t, _ in events]
    assert "response.teammate_message" not in types
    tool_results = [p for t, p in events if t == "response.tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "tu_1"


async def test_stream_teammate_message_without_from_address():
    """A body with no from= still surfaces, with from null."""
    text = (
        "Another Claude session sent a message:\n\nping\n\n"
        "This came from another Claude session — reply with SendMessage."
    )

    async def chunk_source():
        yield _teammate_user_chunk(text)
        yield {"subtype": "success", "result": "done"}

    events, _ = await _collect_response_events(chunk_source())
    teammate = [p for t, p in events if t == "response.teammate_message"]
    assert len(teammate) == 1
    assert teammate[0]["text"] == text
    assert teammate[0]["from"] is None


@pytest.mark.asyncio
async def test_response_completed_carries_the_context_window_snapshot():
    """The context indicator's only data source is this field.

    ChatDRAGON's composer chip reads ``input_tokens_details.context_tokens`` and
    refuses to estimate from anything else, so while the gateway omitted the key
    the chip read "—" on every conversation past its first turn. This walks a
    realistic agentic turn — two main-agent rounds with a subagent in between —
    and pins that the published snapshot is the LAST main-agent prompt, not the
    cumulative ``input_tokens`` the same payload reports for billing.
    """
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {
            "type": "assistant",
            "usage": {
                "input_tokens": 500,
                "output_tokens": 40,
                "cache_read_input_tokens": 11_000,
                "cache_creation_input_tokens": 0,
            },
        }
        # A subagent's own (much smaller) context must not be mistaken for ours.
        yield {
            "type": "assistant",
            "parent_tool_use_id": "toolu_sub",
            "usage": {"input_tokens": 300, "output_tokens": 12},
        }
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "done"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        # Final main-agent round: the transcript grew, so the window is fuller.
        yield {
            "type": "assistant",
            "usage": {
                "input_tokens": 900,
                "output_tokens": 60,
                "cache_read_input_tokens": 13_500,
                "cache_creation_input_tokens": 100,
            },
        }
        yield {"subtype": "success", "result": "done"}

    completed = None
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_ctx",
        output_item_id="msg_ctx",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        t, p = _parse_response_sse(line)
        if t == "response.completed":
            completed = p

    assert completed is not None
    usage = completed["response"]["usage"]
    # 900 + 13500 + 100 — the final main-agent prompt.
    assert usage["input_tokens_details"]["context_tokens"] == 14_500
    # Cumulative billing total is a different, larger number and stays intact.
    assert usage["input_tokens"] > usage["input_tokens_details"]["context_tokens"]
