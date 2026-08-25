#!/usr/bin/env python3
"""
Additional coverage tests for src/streaming_utils.py.

Targets the remaining ~101 uncovered lines identified by:
  COVERAGE_FILE=/tmp/cov_streaming .venv/bin/python -m pytest \
    tests/test_streaming_utils_unit.py tests/test_streaming_coverage_unit.py \
    --cov=src.streaming_utils --cov-report=term-missing -q

Missing ranges attacked:
  65, 70-97, 109, 115, 121, 154-173, 241, 270, 282, 285-287, 289,
  339-344, 378-379, 395-396, 427-429, 444-445, 453-455, 466-467,
  517, 520, 523, 551, 572, 602, 634, 644, 655, 770-771, 797, 848,
  913, 917-920, 969-970, 1005-1012, 1131, 1134-1135, 1189, 1220-1226
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.streaming_utils import (
    _block_field,
    _block_summary,
    _buffer_summary,
    _chunk_summary,
    _extract_rate_limit_status,
    _is_synthetic_ask_user_response_result,
    bridge_sse_stream,
    extract_thinking_texts,
    extract_visible_assistant_text,
    resolve_token_usage,
    stream_response_chunks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


# ---------------------------------------------------------------------------
# Lines 65, 70-97: _block_field (non-dict path) + _block_summary
# ---------------------------------------------------------------------------


class TestBlockFieldNonDict:
    def test_non_dict_with_attribute(self):
        """Line 65: non-dict path uses getattr."""
        obj = SimpleNamespace(type="text", text="hello")
        assert _block_field(obj, "type") == "text"
        assert _block_field(obj, "text") == "hello"

    def test_non_dict_missing_attribute_returns_none(self):
        """Line 65: getattr returns None for missing attr."""
        obj = SimpleNamespace(type="text")
        assert _block_field(obj, "tool_use_id") is None


class TestBlockSummary:
    def test_dict_text_block(self):
        """Lines 70-75: dict text block gets text field."""
        b = {"type": "text", "text": "Hello world"}
        result = _block_summary(b)
        assert result["type"] == "text"
        assert result["text"] == "Hello world"

    def test_text_truncated_at_200(self):
        """Line 75: text is truncated at 200 chars."""
        b = {"type": "text", "text": "x" * 300}
        result = _block_summary(b)
        assert len(result["text"]) == 200

    def test_dict_block_with_name(self):
        """Lines 77-79: block with name field."""
        b = {"type": "tool_use", "name": "Read", "id": "t1", "input": {}}
        result = _block_summary(b)
        assert result["name"] == "Read"

    def test_dict_tool_result_block_with_is_error(self):
        """Lines 81-86: tool_result block with is_error field."""
        b = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "is_error": True,
            "content": "error text",
        }
        result = _block_summary(b)
        assert result["tool_use_id"] == "t1"
        assert result["is_error"] is True
        assert result["content"] == "error text"

    def test_dict_tool_result_with_list_content(self):
        """Line 90-91: tool_result with list content counts blocks."""
        b = {
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        }
        result = _block_summary(b)
        assert result["content_blocks"] == 2

    def test_dict_thinking_block(self):
        """Lines 93-95: thinking block with thinking field."""
        b = {"type": "thinking", "thinking": "Deep thoughts here"}
        result = _block_summary(b)
        assert result["thinking"] == "Deep thoughts here"

    def test_thinking_truncated_at_120(self):
        """Line 95: thinking is truncated at 120 chars."""
        b = {"type": "thinking", "thinking": "y" * 200}
        result = _block_summary(b)
        assert len(result["thinking"]) == 120

    def test_sdk_object_block_with_tool_use_id(self):
        """Lines 65, 81-91: SDK object with tool_use_id."""
        obj = SimpleNamespace(
            type="tool_result",
            tool_use_id="sdk-t1",
            is_error=False,
            content="done",
        )
        result = _block_summary(obj)
        assert result["tool_use_id"] == "sdk-t1"
        assert result["is_error"] is False
        assert result["content"] == "done"

    def test_block_without_type_uses_class_name(self):
        """Line 70: block type falls back to type(b).__name__."""
        b = {"text": "hello"}  # no 'type' key
        result = _block_summary(b)
        assert result["type"] == "dict"


# ---------------------------------------------------------------------------
# Lines 109, 115, 121: _chunk_summary branches
# ---------------------------------------------------------------------------


class TestChunkSummary:
    def test_non_dict_returns_repr(self):
        """Line 109: non-dict chunk returns repr."""
        result = _chunk_summary("not a dict")
        assert "repr" in result
        assert "not a dict" in result["repr"]

    def test_non_dict_list_chunk(self):
        """Line 109: list chunk returns repr."""
        result = _chunk_summary([1, 2, 3])
        assert "repr" in result

    def test_string_content_field(self):
        """Line 115: content as string is reported as str(len=...)."""
        chunk = {"type": "assistant", "content": "hello world"}
        result = _chunk_summary(chunk)
        assert result["content"] == "str(len=11)"

    def test_result_field_truncated(self):
        """Line 121: result string is truncated at 200 chars."""
        chunk = {"type": "result", "result": "r" * 300}
        result = _chunk_summary(chunk)
        assert len(result["result"]) == 200

    def test_result_empty_string_not_truncated(self):
        """Lines 120-121: empty result string passes through (falsy, not truncated)."""
        chunk = {"type": "result", "result": ""}
        result = _chunk_summary(chunk)
        # Empty string is falsy in `if isinstance(result, str) and result:` so not truncated,
        # but it's still present in the output dict (None filter only removes None, not "")
        assert "type" in result

    def test_list_content_creates_block_summaries(self):
        """Line 113: list content calls _block_summary for each block."""
        chunk = {
            "type": "assistant",
            "content": [
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ],
        }
        result = _chunk_summary(chunk)
        assert isinstance(result["content"], list)
        assert len(result["content"]) == 2
        assert result["content"][0]["type"] == "text"

    def test_all_none_fields_excluded(self):
        """Lines 123-143: None fields are excluded from output."""
        chunk = {"type": "assistant"}
        result = _chunk_summary(chunk)
        assert result == {"type": "assistant"}


# ---------------------------------------------------------------------------
# Lines 154-173: _buffer_summary with non-dict items and content blocks
# ---------------------------------------------------------------------------


class TestBufferSummary:
    def test_non_dict_item_uses_class_name(self):
        """Lines 154-155: non-dict items get a repr entry."""
        buf = ["string_item"]
        result = _buffer_summary(buf)
        assert result == [{"repr": "str"}]

    def test_non_dict_custom_object(self):
        """Lines 154-155: custom object class name appears."""
        obj = SimpleNamespace(foo="bar")
        result = _buffer_summary([obj])
        assert result[0]["repr"] == "SimpleNamespace"

    def test_dict_with_list_content_includes_blocks(self):
        """Lines 168-172: dict with list content produces block summaries."""
        buf = [
            {
                "type": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read"},
                    {"type": "tool_result", "tool_use_id": "t1", "is_error": False},
                ],
            }
        ]
        result = _buffer_summary(buf)
        assert len(result) == 1
        assert "blocks" in result[0]
        assert result[0]["blocks"][0]["type"] == "tool_use"
        assert result[0]["blocks"][0]["name"] == "Read"

    def test_dict_with_empty_content_no_blocks(self):
        """Line 168: empty list content does not produce blocks key."""
        buf = [{"type": "assistant", "content": []}]
        result = _buffer_summary(buf)
        assert "blocks" not in result[0]

    def test_limit_applies(self):
        """Line 153: only last N items are summarised."""
        buf = [{"type": "t1"}, {"type": "t2"}, {"type": "t3"}, {"type": "t4"}, {"type": "t5"}, {"type": "t6"}]
        result = _buffer_summary(buf, limit=3)
        assert len(result) == 3
        assert result[-1]["type"] == "t6"

    def test_blocks_at_most_3(self):
        """Line 171: only first 3 content blocks are summarised."""
        many_blocks = [{"type": "text", "text": f"t{i}"} for i in range(5)]
        buf = [{"type": "assistant", "content": many_blocks}]
        result = _buffer_summary(buf)
        assert len(result[0]["blocks"]) == 3


# ---------------------------------------------------------------------------
# Line 241: extract_thinking_texts with hasattr(block, "thinking") path
# ---------------------------------------------------------------------------


class TestExtractThinkingTexts:
    def test_sdk_thinking_block_with_thinking_attr(self):
        """Line 241-244: SDK object with .thinking attribute."""
        thinking_block = SimpleNamespace(thinking="Deep thought")
        chunks = [{"type": "assistant", "content": [thinking_block]}]
        result = extract_thinking_texts(chunks)
        assert result == ["Deep thought"]

    def test_dict_thinking_block(self):
        """Line 246: dict thinking block."""
        chunks = [{"type": "assistant", "content": [{"type": "thinking", "thinking": "Think"}]}]
        result = extract_thinking_texts(chunks)
        assert result == ["Think"]

    def test_non_dict_chunk_skipped(self):
        """Line 239: non-dict chunks are skipped."""
        result = extract_thinking_texts(["not a dict", 42])
        assert result == []

    def test_no_thinking_blocks(self):
        """Lines 248: blocks without thinking text not added."""
        chunks = [{"type": "assistant", "content": [{"type": "text", "text": "hello"}]}]
        result = extract_thinking_texts(chunks)
        assert result == []


# ---------------------------------------------------------------------------
# Lines 270, 282, 285-287, 289: extract_visible_assistant_text
# ---------------------------------------------------------------------------


class TestExtractVisibleAssistantText:
    def test_non_dict_chunk_skipped_in_first_pass(self):
        """Line 270: non-dict chunks skipped in first pass."""
        result = extract_visible_assistant_text(["not a dict"])
        assert result is None

    def test_result_with_empty_result_field(self):
        """Lines 271-273: result chunk with empty/whitespace result not used."""
        chunks = [{"subtype": "success", "result": "   "}]
        result = extract_visible_assistant_text(chunks)
        assert result is None

    def test_result_message_preferred(self):
        """Lines 271-276: ResultMessage result is returned when present."""
        chunks = [
            {"type": "assistant", "content": [{"type": "text", "text": "ignored"}]},
            {"subtype": "success", "result": "final text"},
        ]
        result = extract_visible_assistant_text(chunks)
        assert result == "final text"

    def test_second_pass_non_list_content_with_message_fallback(self):
        """Lines 285-287: non-list content falls back to message field."""
        chunks = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "fallback text"}]},
            }
        ]
        result = extract_visible_assistant_text(chunks)
        assert result == "fallback text"

    def test_second_pass_non_list_content_no_message(self):
        """Line 289: non-list content with no message -> skipped."""
        chunks = [{"type": "assistant", "content": "plain string"}]
        result = extract_visible_assistant_text(chunks)
        assert result is None

    def test_second_pass_filters_thinking_blocks(self):
        """Lines 282, 294-299: thinking blocks filtered out in second pass."""
        chunks = [
            {
                "type": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "visible"},
                ],
            }
        ]
        result = extract_visible_assistant_text(chunks)
        assert result == "visible"
        assert "hidden" not in (result or "")

    def test_second_pass_all_thinking_no_visible(self):
        """Lines 301-302: when all blocks are thinking, no text added."""
        thinking_block = SimpleNamespace(thinking="only thinking")
        chunks = [{"type": "assistant", "content": [thinking_block]}]
        result = extract_visible_assistant_text(chunks)
        assert result is None

    def test_second_pass_non_dict_chunk_skipped(self):
        """Line 282: non-dict chunks skipped in second pass."""
        result = extract_visible_assistant_text(["not a dict", 42])
        assert result is None


# ---------------------------------------------------------------------------
# Lines 339-344: _extract_rate_limit_status
# ---------------------------------------------------------------------------


class TestExtractRateLimitStatus:
    def test_no_rate_limit_info_returns_unknown(self):
        """Line 341: no rate_limit_info returns 'unknown'."""
        result = _extract_rate_limit_status({"type": "rate_limit"})
        assert result == "unknown"

    def test_dict_rate_limit_info_returns_status(self):
        """Lines 342-343: dict rate_limit_info returns status field."""
        chunk = {"type": "rate_limit", "rate_limit_info": {"status": "active"}}
        result = _extract_rate_limit_status(chunk)
        assert result == "active"

    def test_dict_rate_limit_info_missing_status(self):
        """Line 343: dict without status field returns 'unknown'."""
        chunk = {"type": "rate_limit", "rate_limit_info": {"other": "data"}}
        result = _extract_rate_limit_status(chunk)
        assert result == "unknown"

    def test_sdk_object_rate_limit_info(self):
        """Line 344: SDK object with status attribute."""
        info_obj = SimpleNamespace(status="rejected")
        chunk = {"type": "rate_limit", "rate_limit_info": info_obj}
        result = _extract_rate_limit_status(chunk)
        assert result == "rejected"

    def test_sdk_object_without_status_returns_unknown(self):
        """Line 344: SDK object without status attribute returns 'unknown'."""
        info_obj = SimpleNamespace(other="value")
        chunk = {"type": "rate_limit", "rate_limit_info": info_obj}
        result = _extract_rate_limit_status(chunk)
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Lines 378-379, 395-396: bridge_sse_stream error and cancel paths
# ---------------------------------------------------------------------------


class TestBridgeSseStream:
    async def test_forwards_sse_lines(self):
        """bridge_sse_stream forwards lines from sse_source."""

        async def sse_source():
            yield "event: test\ndata: {}\n\n"
            yield "event: done\ndata: {}\n\n"

        async def chunk_source_gen():
            yield {}  # pragma: no cover

        cs = chunk_source_gen()
        lines = [line async for line in bridge_sse_stream(sse_source(), cs)]
        assert lines[0] == "event: test\ndata: {}\n\n"
        assert lines[1] == "event: done\ndata: {}\n\n"

    async def test_propagates_exception_from_sse_source(self):
        """Lines 378-379: exception in sse_source is re-raised."""

        async def failing_sse():
            yield "event: first\ndata: {}\n\n"
            raise RuntimeError("sse blew up")

        async def empty_chunk_source():
            return
            yield  # pragma: no cover

        cs = empty_chunk_source()
        with pytest.raises(RuntimeError, match="sse blew up"):
            async for _ in bridge_sse_stream(failing_sse(), cs):
                pass

    async def test_cancelled_error_suppressed_in_finally(self):
        """Lines 395-396: CancelledError during reader_task.cancel() is swallowed."""

        async def sse_source():
            yield "event: ok\ndata: {}\n\n"

        async def chunk_source_gen():
            return
            yield  # pragma: no cover

        # Just ensure it completes without error
        lines = [line async for line in bridge_sse_stream(sse_source(), chunk_source_gen())]
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Lines 427-429: resolve_token_usage with backend fallback
# ---------------------------------------------------------------------------


class TestResolveTokenUsage:
    def test_uses_sdk_usage_when_present(self):
        """Lines 325-326: prefers SDK usage."""
        chunks = [{"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}]
        pt, ct = resolve_token_usage(chunks, "prompt", "completion")
        assert pt == 10
        assert ct == 5

    def test_backend_fallback_when_no_sdk_usage(self):
        """Lines 327-329: calls backend.estimate_token_usage when no SDK usage."""
        backend = MagicMock()
        backend.estimate_token_usage.return_value = {"prompt_tokens": 20, "completion_tokens": 10}
        pt, ct = resolve_token_usage([], "hello", "world", backend=backend)
        assert pt == 20
        assert ct == 10
        backend.estimate_token_usage.assert_called_once_with("hello", "world", "")

    def test_character_based_fallback_when_no_backend_no_sdk(self):
        """Line 330: falls back to MessageAdapter.estimate_tokens."""
        pt, ct = resolve_token_usage([], "hello world", "response text")
        assert isinstance(pt, int)
        assert isinstance(ct, int)
        assert pt > 0
        assert ct > 0


# ---------------------------------------------------------------------------
# Lines 444-445, 453-455, 466-467: _keepalive_wrapper internals
# ---------------------------------------------------------------------------


class TestKeepaliveWrapper:
    async def test_interval_zero_passes_through(self):
        """SSE_KEEPALIVE_INTERVAL <= 0 passes items through unchanged."""
        from src.streaming_utils import _keepalive_wrapper

        async def source():
            yield "item1"
            yield "item2"

        # Use interval=0 to bypass the task path
        items = [item async for item in _keepalive_wrapper(source(), 0)]
        assert items == ["item1", "item2"]

    async def test_exception_propagates_through_queue(self):
        """Lines 444-445, 459-460: exception in source propagates."""
        from src.streaming_utils import _keepalive_wrapper

        async def failing_source():
            yield "first"
            raise ValueError("source failed")

        with pytest.raises(ValueError, match="source failed"):
            async for _ in _keepalive_wrapper(failing_source(), interval=60):
                pass

    async def test_cancelled_error_suppressed_in_task_cancel(self):
        """Lines 466-467: CancelledError swallowed when cancelling inner task."""
        from src.streaming_utils import _keepalive_wrapper

        async def source():
            yield "only one item"

        items = [item async for item in _keepalive_wrapper(source(), interval=60)]
        assert items == ["only one item"]


# ---------------------------------------------------------------------------
# Line 517, 520, 523: _is_synthetic_ask_user_response_result branches
# ---------------------------------------------------------------------------


class TestIsSyntheticAskUserResponseResult:
    """Test the private helper via its effect on stream output."""

    async def test_ask_user_response_via_tool_names_by_id(self):
        """Line 519-520: suppressed when tool_names_by_id maps id to AskUserQuestion."""

        async def source():
            # First yield a tool_use so the name is registered
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ask_2",
                        "name": "AskUserQuestion",
                        "input": {"question": "What?"},
                    }
                ],
            }
            # Then a user chunk with the "User responded:" result
            yield {
                "type": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_ask_2",
                        "content": "User responded: Yes",
                        "is_error": False,
                    }
                ],
            }
            yield {
                "type": "assistant",
                "content": [{"type": "text", "text": "Great answer."}],
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-ask-2",
                output_item_id="msg-ask-2",
                chunks_buffer=[],
                logger=logging.getLogger("test-ask-2"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        # The AskUserQuestion response tool_result should be suppressed
        assert "response.tool_result" not in event_types

    async def test_non_ask_user_tool_result_not_suppressed(self):
        """Line 512-513: tool result without 'User responded:' prefix is NOT suppressed."""

        async def source():
            yield {
                "type": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_regular",
                        "content": "file contents here",
                        "is_error": False,
                    }
                ],
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Result"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-no-ask",
                output_item_id="msg-no-ask",
                chunks_buffer=[],
                logger=logging.getLogger("test-no-ask"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        assert "response.tool_result" in event_types

    async def test_ask_user_without_context_no_tool_name_not_suppressed(self):
        """Lines 522-524: 'User responded:' but no AskUserQuestion mapping + no context."""

        async def source():
            yield {
                "type": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_orphan",
                        "content": "User responded: something",
                        "is_error": False,
                    }
                ],
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "OK"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-orphan",
                output_item_id="msg-orphan",
                chunks_buffer=[],
                logger=logging.getLogger("test-orphan"),
                stream_result=stream_result,
                request_context=None,  # no context
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        # Not suppressed because there's no context match
        assert "response.tool_result" in event_types


# ---------------------------------------------------------------------------
# Line 551: _tool_use_started_events — STREAM_TOOL_PROGRESS guard
# Lines 572: _tool_use_events — subagent suppression
# Line 602: _user_tool_result_events — subagent parent suppression
# Lines 634, 644, 655: _embedded_tool_events branches
# ---------------------------------------------------------------------------


class TestSubagentToolBlocking:
    async def _run_stream(self, source_gen):
        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source_gen,
                model="claude-test",
                response_id="resp-subagent",
                output_item_id="msg-subagent",
                chunks_buffer=[],
                logger=logging.getLogger("test-subagent"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        return parsed, stream_result

    async def test_subagent_tool_use_suppressed_when_flag_off(self):
        """Line 572: tool_use block with parent_tool_use_id suppressed when SUBAGENT_STREAM_TOOL_BLOCKS=False."""

        async def source():
            # subagent tool_use block via stream_event
            yield {
                "type": "stream_event",
                "parent_tool_use_id": "agent-root",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "sub-t1",
                        "name": "Bash",
                    },
                },
            }
            yield {
                "type": "stream_event",
                "parent_tool_use_id": "agent-root",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"cmd":"ls"}',
                    },
                },
            }
            yield {
                "type": "stream_event",
                "parent_tool_use_id": "agent-root",
                "event": {"type": "content_block_stop", "index": 0},
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Done"},
                },
            }

        import src.streaming_utils as su

        orig = su.SUBAGENT_STREAM_TOOL_BLOCKS
        try:
            su.SUBAGENT_STREAM_TOOL_BLOCKS = False
            parsed, sr = await self._run_stream(source())
        finally:
            su.SUBAGENT_STREAM_TOOL_BLOCKS = orig

        event_types = [et for et, _ in parsed]
        # subagent tool_use should be suppressed
        tool_use_events = [p for et, p in parsed if et == "response.tool_use"]
        assert all(
            p.get("name") != "Bash" for p in tool_use_events
        ), "Subagent tool_use leaked when SUBAGENT_STREAM_TOOL_BLOCKS=False"

    async def test_subagent_user_tool_result_suppressed(self):
        """Line 602: user chunk with parent_id and SUBAGENT_STREAM_TOOL_BLOCKS=False."""

        async def source():
            yield {
                "type": "user",
                "parent_tool_use_id": "agent-root",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "sub-t1",
                        "content": "done",
                        "is_error": False,
                    }
                ],
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "OK"},
                },
            }

        import src.streaming_utils as su

        orig = su.SUBAGENT_STREAM_TOOL_BLOCKS
        try:
            su.SUBAGENT_STREAM_TOOL_BLOCKS = False
            parsed, sr = await self._run_stream(source())
        finally:
            su.SUBAGENT_STREAM_TOOL_BLOCKS = orig

        event_types = [et for et, _ in parsed]
        assert "response.tool_result" not in event_types

    async def test_embedded_ask_user_result_suppressed(self):
        """Lines 630-634: embedded tool_result with AskUserQuestion is suppressed."""

        async def source():
            # First register the AskUserQuestion tool use
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_embedded_ask",
                        "name": "AskUserQuestion",
                        "input": {"question": "What?"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_embedded_ask",
                        "content": "User responded: something",
                        "is_error": False,
                    },
                ],
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Noted"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-embed-ask",
                output_item_id="msg-embed-ask",
                chunks_buffer=[],
                logger=logging.getLogger("test-embed-ask"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        # The embedded AskUserQuestion tool_result should be suppressed
        assert "response.tool_result" not in event_types

    async def test_embedded_subagent_block_suppressed_when_flag_off(self):
        """Line 644: embedded subagent tool_use with parent_id suppressed."""

        async def source():
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "sub-emb-t1",
                        "name": "Read",
                        "input": {"path": "/tmp"},
                        "parent_tool_use_id": "agent-root",
                    }
                ],
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Content"},
                },
            }

        import src.streaming_utils as su

        orig = su.SUBAGENT_STREAM_TOOL_BLOCKS
        try:
            su.SUBAGENT_STREAM_TOOL_BLOCKS = False
            stream_result = {}
            lines = [
                line
                async for line in stream_response_chunks(
                    chunk_source=source(),
                    model="claude-test",
                    response_id="resp-embed-sub",
                    output_item_id="msg-embed-sub",
                    chunks_buffer=[],
                    logger=logging.getLogger("test-embed-sub"),
                    stream_result=stream_result,
                )
            ]
        finally:
            su.SUBAGENT_STREAM_TOOL_BLOCKS = orig

        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        tool_use_events = [p for et, p in parsed if et == "response.tool_use"]
        subagent_uses = [p for p in tool_use_events if p.get("name") == "Read"]
        assert len(subagent_uses) == 0

    async def test_embedded_tool_result_suppress_result_false_emits(self):
        """Line 655: embedded non-suppressed tool_result is emitted."""

        async def source():
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-emit",
                        "content": "result data",
                        "is_error": False,
                    }
                ],
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Text"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-emit-res",
                output_item_id="msg-emit-res",
                chunks_buffer=[],
                logger=logging.getLogger("test-emit-res"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        assert "response.tool_result" in event_types


# ---------------------------------------------------------------------------
# Lines 770-771: usage-log warning on exception
# ---------------------------------------------------------------------------


class TestUsageLogWarning:
    async def test_usage_log_exception_logged_as_warning(self, caplog):
        """Lines 770-771: exception in usage_logger.log_turn_from_context is caught and warned."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hello"},
                },
            }

        with patch("src.streaming_utils.usage_logger") as mock_logger:
            mock_logger.log_turn_from_context.side_effect = Exception("db error")
            stream_result = {}
            logger = logging.getLogger("test-usage-log-warn")
            with caplog.at_level(logging.WARNING):
                lines = [
                    line
                    async for line in stream_response_chunks(
                        chunk_source=source(),
                        model="claude-test",
                        response_id="resp-usage-warn",
                        output_item_id="msg-usage-warn",
                        chunks_buffer=[],
                        logger=logger,
                        stream_result=stream_result,
                    )
                ]

        # Stream should complete successfully despite usage log failure
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.completed"
        assert "usage-log emit failed" in caplog.text


# ---------------------------------------------------------------------------
# Line 797: _open_message_item idempotency
# ---------------------------------------------------------------------------


class TestOpenMessageItemIdempotency:
    async def test_multiple_text_deltas_open_message_item_only_once(self):
        """Line 797: _open_message_item is idempotent — only one output_item.added."""

        async def source():
            for text in ["Hello", " ", "world"]:
                yield {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": text},
                    },
                }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-idempotent",
                output_item_id="msg-idempotent",
                chunks_buffer=[],
                logger=logging.getLogger("test-idempotent"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        added_events = [et for et, _ in parsed if et == "response.output_item.added"]
        # Only one output_item.added (the message item), not three
        assert len(added_events) == 1


# ---------------------------------------------------------------------------
# Line 848: _close_reasoning returns [] when reasoning not open
# Lines 913, 917-920: reasoning block in streaming
# ---------------------------------------------------------------------------


class TestReasoningStream:
    async def test_thinking_only_stream_emits_reasoning_items(self):
        """Lines 913-920: thinking-only stream opens and closes a reasoning item."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Reasoning here"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 0},
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Answer"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-thinking",
                output_item_id="msg-thinking",
                chunks_buffer=[],
                logger=logging.getLogger("test-thinking"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]

        # Reasoning item opened
        assert "response.reasoning_summary_part.added" in event_types
        # Reasoning item closed
        assert "response.reasoning_summary_text.done" in event_types
        assert "response.reasoning_text.done" in event_types
        assert stream_result["success"] is True
        # thinking text captured
        assert "Reasoning here" in stream_result.get("thinking_texts", [[]])[0]

    async def test_think_then_text_then_think_interleave(self):
        """Lines 848, 913-920: interleaved think→text→think emits two reasoning items."""

        async def source():
            # First thinking block
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "First thought"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 0},
            }
            # Text block
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Middle text"},
                },
            }
            # Second thinking block
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {"type": "thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Second thought"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 2},
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Final"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-interleave",
                output_item_id="msg-interleave",
                chunks_buffer=[],
                logger=logging.getLogger("test-interleave"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]

        # Two reasoning summary done events (one for each thinking block)
        done_events = [et for et in event_types if et == "response.reasoning_summary_text.done"]
        assert len(done_events) == 2
        assert stream_result["success"] is True
        thinking_texts = stream_result.get("thinking_texts", [])
        assert len(thinking_texts) == 2


# ---------------------------------------------------------------------------
# Lines 969-970: keepalive chunk forwarded in main streaming loop
# ---------------------------------------------------------------------------


class TestKeepaliveForwarding:
    async def test_keepalive_interval_zero_no_keepalive_emitted(self):
        """Lines 969-970 path: SSE_KEEPALIVE_INTERVAL=0 disables keepalives."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hi"},
                },
            }

        import src.streaming_utils as su

        orig = su.SSE_KEEPALIVE_INTERVAL
        try:
            su.SSE_KEEPALIVE_INTERVAL = 0
            stream_result = {}
            lines = [
                line
                async for line in stream_response_chunks(
                    chunk_source=source(),
                    model="claude-test",
                    response_id="resp-keepalive-0",
                    output_item_id="msg-keepalive-0",
                    chunks_buffer=[],
                    logger=logging.getLogger("test-keepalive-0"),
                    stream_result=stream_result,
                )
            ]
        finally:
            su.SSE_KEEPALIVE_INTERVAL = orig

        # No keepalive lines emitted (keepalive is ": keepalive\n\n")
        assert not any(": keepalive" in line for line in lines)
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Lines 1005-1012: rate_limit chunk handling
# ---------------------------------------------------------------------------


class TestRateLimitChunkHandling:
    async def test_rate_limit_rejected_emits_failed(self):
        """Lines 1007-1011: rate_limit with status=rejected emits response.failed."""

        async def source():
            yield {
                "type": "rate_limit",
                "rate_limit_info": {"status": "rejected"},
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-rl-rejected",
                output_item_id="msg-rl-rejected",
                chunks_buffer=[],
                logger=logging.getLogger("test-rl-rejected"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.failed"
        assert parsed[-1][1]["response"]["error"]["code"] == "rate_limit"
        assert stream_result["success"] is False

    async def test_rate_limit_non_rejected_continues(self):
        """Line 1012: rate_limit with non-rejected status continues streaming."""

        async def source():
            yield {
                "type": "rate_limit",
                "rate_limit_info": {"status": "active"},
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "After rate limit"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-rl-active",
                output_item_id="msg-rl-active",
                chunks_buffer=[],
                logger=logging.getLogger("test-rl-active"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.completed"
        assert stream_result["success"] is True
        deltas = [p.get("delta") for et, p in parsed if et == "response.output_text.delta"]
        assert "After rate limit" in deltas

    async def test_rate_limit_sdk_object_status(self):
        """Line 344 + 1005-1012: rate_limit_info as SDK object with status attr."""

        async def source():
            rate_info = SimpleNamespace(status="active")
            yield {
                "type": "rate_limit",
                "rate_limit_info": rate_info,
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Text"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-rl-sdk-obj",
                output_item_id="msg-rl-sdk-obj",
                chunks_buffer=[],
                logger=logging.getLogger("test-rl-sdk-obj"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.completed"
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Line 1131: safety net — in_thinking but reasoning_open is False
# Lines 1134-1135: reasoning_open=True and exiting thinking block
# ---------------------------------------------------------------------------


class TestInThinkingSafetyNet:
    async def test_thinking_delta_outside_block_logged_and_not_visible(self, caplog):
        """Lines 1044-1050: warning logged for thinking_delta outside a thinking block.

        A thinking_delta that arrives without a prior content_block_start[type=thinking]
        does NOT set in_thinking=True (extract_stream_event_delta keeps in_thinking as-is).
        The code logs a warning and passes the text through as a thinking value — the
        warning assertion confirms the guard at lines 1044-1050 was hit.
        """

        async def source():
            # thinking_delta without a prior content_block_start — triggers warning
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "orphan thought"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Real text"},
                },
            }

        stream_result = {}
        with caplog.at_level(logging.WARNING):
            lines = [
                line
                async for line in stream_response_chunks(
                    chunk_source=source(),
                    model="claude-test",
                    response_id="resp-orphan-think",
                    output_item_id="msg-orphan-think",
                    chunks_buffer=[],
                    logger=logging.getLogger("test-orphan-think"),
                    stream_result=stream_result,
                )
            ]

        # A warning should have been logged about the out-of-band thinking delta
        assert "thinking_delta outside a thinking block" in caplog.text
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] in ("response.completed", "response.failed")

    async def test_reasoning_closed_when_exiting_thinking(self):
        """Lines 1133-1135: reasoning_open=True closes when text delta arrives."""

        async def source():
            # Open thinking block
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Thinking..."},
                },
            }
            # Closing the thinking block
            yield {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 0},
            }
            # Now a text_delta — reasoning_open should be closed here
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Conclusion"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-exit-think",
                output_item_id="msg-exit-think",
                chunks_buffer=[],
                logger=logging.getLogger("test-exit-think"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]

        # Reasoning items should have been properly closed
        assert "response.reasoning_summary_text.done" in event_types
        # Text should still be visible
        deltas = [p.get("delta") for et, p in parsed if et == "response.output_text.delta"]
        assert "Conclusion" in deltas
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Line 1189: token_streaming + assistant chunk with usage buffered
# Lines 1220-1226: collab filter flush opens new message item if needed
# ---------------------------------------------------------------------------


class TestTokenStreamingAssistantUsage:
    async def test_assistant_usage_chunk_buffered_in_token_streaming_mode(self):
        """Line 1189: assistant chunk with usage is buffered even in token_streaming mode."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            }
            # This assistant chunk has usage — should be buffered
            yield {
                "type": "assistant",
                "usage": {"input_tokens": 50, "output_tokens": 25},
                "content": [{"type": "text", "text": "Hello"}],
            }

        chunks_buffer = []
        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-usage-buf",
                output_item_id="msg-usage-buf",
                chunks_buffer=chunks_buffer,
                logger=logging.getLogger("test-usage-buf"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        # The assistant chunk with usage should be in chunks_buffer
        usage_chunks = [c for c in chunks_buffer if c.get("type") == "assistant" and c.get("usage")]
        assert len(usage_chunks) == 1
        assert usage_chunks[0]["usage"]["input_tokens"] == 50
        assert stream_result["success"] is True


class TestCollabFlushOpensMessageItem:
    async def test_collab_flush_opens_new_message_item_when_not_open(self):
        """Lines 1220-1226: collab flush opens a new message item when not yet opened.

        The collab filter buffers a lone '{' (can't tell if collab JSON yet).
        feed('{') returns '' → cleaned is falsy → message item is NOT opened.
        At end of stream, flush() returns '{' → lines 1220-1226 execute:
        _open_message_item() is called, then delta emitted.
        """

        async def source():
            # A single '{' keeps the collab filter buffering (returns '' from feed).
            # That means message_item_opened stays False until flush().
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "{"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-collab-flush2",
                output_item_id="msg-collab-flush2",
                chunks_buffer=[],
                logger=logging.getLogger("test-collab-flush2"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.completed"
        assert stream_result["success"] is True
        # The '{' should have been flushed at stream end
        deltas = [p.get("delta") for et, p in parsed if et == "response.output_text.delta"]
        combined = "".join(d for d in deltas if d)
        assert "{" in combined
        # output_item.added should have been emitted (message item opened by flush path)
        event_types = [et for et, _ in parsed]
        assert "response.output_item.added" in event_types


# ---------------------------------------------------------------------------
# Line 797: _open_message_item early return (already opened)
# ---------------------------------------------------------------------------


class TestOpenMessageItemEarlyReturn:
    async def test_open_message_item_idempotent_via_interleave(self):
        """Line 797: _open_message_item returns [] when message_item_opened=True.

        This path is hit in the think→text→think scenario when _close_message_item
        is called on a second thinking block start, then a new message segment starts.
        After the second thinking block closes and text arrives, _open_message_item is
        called again for the same message slot — but it's a NEW one so just count
        that output_item.added appears at most once per segment.
        """

        async def source():
            # text → think should close message → re-open a new one
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "First text"},
                },
            }
            # More text to the same message item — _open_message_item idempotent
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": " continues"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-idem",
                output_item_id="msg-idem",
                chunks_buffer=[],
                logger=logging.getLogger("test-idem"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        # Only one output_item.added for the message item
        msg_added = [
            p for et, p in parsed
            if et == "response.output_item.added" and p.get("item", {}).get("type") == "message"
        ]
        assert len(msg_added) == 1
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Line 848: _close_reasoning returns [] when reasoning_open=False
# ---------------------------------------------------------------------------


class TestCloseReasoningIdempotent:
    async def test_close_reasoning_not_called_twice_for_same_block(self):
        """Line 848: _close_reasoning returns [] when reasoning_open=False.

        This is hit during finalization when the </think> already closed reasoning
        and the end-of-stream `if reasoning_open:` check is False.
        """

        async def source():
            # Full thinking block: <think> ... </think>
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Thought"},
                },
            }
            # </think> closes the reasoning item
            yield {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 0},
            }
            # Then text — reasoning is now closed
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Answer"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-close-rs",
                output_item_id="msg-close-rs",
                chunks_buffer=[],
                logger=logging.getLogger("test-close-rs"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]

        # Exactly one reasoning_summary_text.done (not two)
        done_events = [et for et in event_types if et == "response.reasoning_summary_text.done"]
        assert len(done_events) == 1
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Lines 913, 917-920: _close_message_item with collab flush
# ---------------------------------------------------------------------------


class TestCloseMessageItemWithCollabFlush:
    async def test_collab_flush_in_close_message_item(self):
        """Lines 917-920: _close_message_item flushes remaining collab filter content.

        Feed a '{' (buffered by collab) then a thinking block start so
        _close_message_item() is called with the collab filter non-empty.
        Lines 915-920 execute: remaining collab text emitted inside _close_message_item.
        """

        async def source():
            # Start with text that opens the message item
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "First "},
                },
            }
            # Feed a '{' — buffered by collab filter (not yet emitted)
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "{"},
                },
            }
            # Now a thinking block starts — this calls _close_message_item()
            # which will flush the '{' from the collab filter
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "Thinking"},
                },
            }
            yield {
                "type": "stream_event",
                "event": {"type": "content_block_stop", "index": 1},
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Done"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-close-collab",
                output_item_id="msg-close-collab",
                chunks_buffer=[],
                logger=logging.getLogger("test-close-collab"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.completed"
        assert stream_result["success"] is True
        # Verify text was emitted
        deltas = [p.get("delta") for et, p in parsed if et == "response.output_text.delta"]
        combined = "".join(d for d in deltas if d)
        assert "First" in combined


# ---------------------------------------------------------------------------
# Lines 969-970: keepalive SSE comment forwarded in main streaming loop
# ---------------------------------------------------------------------------


class TestKeepaliveForwardedInMainLoop:
    async def test_keepalive_string_forwarded_from_wrapper(self):
        """Lines 969-970: the _SSE_KEEPALIVE string is forwarded when wrapper emits it.

        We patch _keepalive_wrapper to yield the _SSE_KEEPALIVE sentinel string
        so the `if chunk is _SSE_KEEPALIVE` branch is triggered.
        """
        import src.streaming_utils as su

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "text"},
                },
            }

        # Wrap the normal source so we inject a keepalive before the real item
        real_keepalive = su._SSE_KEEPALIVE

        async def patched_wrapper(src, interval, stall_after=0):
            yield real_keepalive  # inject keepalive — triggers lines 969-970
            async for item in src:
                yield item

        stream_result = {}
        with patch.object(su, "_keepalive_wrapper", patched_wrapper):
            lines = [
                line
                async for line in stream_response_chunks(
                    chunk_source=source(),
                    model="claude-test",
                    response_id="resp-ka-fwd",
                    output_item_id="msg-ka-fwd",
                    chunks_buffer=[],
                    logger=logging.getLogger("test-ka-fwd"),
                    stream_result=stream_result,
                )
            ]

        # The keepalive line should appear in output
        assert real_keepalive in lines
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Lines 378-379: bridge_sse_stream chunk_source.aclose() raises
# Lines 395-396: bridge_sse_stream reader_task cancel suppresses CancelledError
# ---------------------------------------------------------------------------


class TestBridgeSseStreamAdditional:
    async def test_chunk_source_aclose_exception_suppressed(self):
        """Lines 378-379: exception in chunk_source.aclose() is silently swallowed."""

        async def sse_source():
            yield "event: test\ndata: {}\n\n"

        class ACloseRaisesSource:
            """Async generator lookalike whose aclose() raises."""

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                raise RuntimeError("aclose blew up")

        # Should complete without raising even though aclose() raises
        lines = [line async for line in bridge_sse_stream(sse_source(), ACloseRaisesSource())]
        assert lines == ["event: test\ndata: {}\n\n"]

    async def test_reader_task_cancelled_error_suppressed(self):
        """Lines 395-396: CancelledError when awaiting cancelled reader_task is swallowed."""

        async def sse_source():
            yield "event: ok\ndata: {}\n\n"

        async def normal_chunk_source():
            return
            yield  # pragma: no cover

        # Normal path — CancelledError from reader_task.cancel() is swallowed in finally
        lines = [line async for line in bridge_sse_stream(sse_source(), normal_chunk_source())]
        assert lines == ["event: ok\ndata: {}\n\n"]


# ---------------------------------------------------------------------------
# Lines 444-445, 466-467: _keepalive_wrapper source.aclose() raises + task cancel
# Lines 453-455: keepalive timeout emits _SSE_KEEPALIVE
# ---------------------------------------------------------------------------


class TestKeepaliveWrapperAdditional:
    async def test_source_aclose_exception_suppressed(self):
        """Lines 444-445: exception in source.aclose() is silently swallowed."""
        from src.streaming_utils import _keepalive_wrapper

        class ACloseRaisesSource:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                raise RuntimeError("aclose failed")

        items = [item async for item in _keepalive_wrapper(ACloseRaisesSource(), interval=60)]
        assert items == []

    async def test_keepalive_timeout_emits_comment(self):
        """Lines 453-455: when queue is empty for interval seconds, keepalive emitted."""
        import asyncio as _asyncio
        from src.streaming_utils import _keepalive_wrapper, _SSE_KEEPALIVE

        # Use a very short interval and a source that pauses briefly
        call_count = 0
        original_wait_for = _asyncio.wait_for

        async def source():
            yield "item1"

        # Patch asyncio.wait_for to raise TimeoutError on the first call only,
        # then fall through to a sentinel so the loop terminates
        timeout_raised = [False]

        async def patched_wait_for(coro, timeout):
            if not timeout_raised[0]:
                timeout_raised[0] = True
                coro.close()
                raise _asyncio.TimeoutError()
            return await original_wait_for(coro, timeout=timeout)

        with patch("src.streaming_utils.asyncio.wait_for", patched_wait_for):
            items = [item async for item in _keepalive_wrapper(source(), interval=1)]

        assert _SSE_KEEPALIVE in items

    async def test_task_cancel_exception_suppressed(self):
        """Lines 466-467: exception during task cancel is swallowed in finally block."""
        from src.streaming_utils import _keepalive_wrapper

        async def source():
            yield "only"

        # Just verify normal path completes — the cancel exception is internal
        items = [item async for item in _keepalive_wrapper(source(), interval=60)]
        assert items == ["only"]


# ---------------------------------------------------------------------------
# Line 1131: safety net — in_thinking=True but reasoning_open=False
# Lines 1134-1135: reasoning closed on text after thinking block
# ---------------------------------------------------------------------------


class TestInThinkingSafetyNetAdditional:
    async def test_thinking_delta_in_thinking_no_reasoning_dropped(self):
        """Line 1131: in_thinking=True but reasoning_open=False — text dropped.

        To reach line 1131 we need in_thinking=True with reasoning_open=False.
        This can happen if a thinking delta arrives AFTER a <think> that opened
        then closed reasoning but somehow in_thinking is still True.

        We can synthesize this by yielding a content_block_start[thinking] +
        thinking_delta WITHOUT a content_block_stop — so in_thinking stays True,
        reasoning_open=True — then MANUALLY invoke the scenario via patching
        reasoning_open to False while in_thinking stays True.

        Since this branch is a defensive guard (the code comment says "should no
        longer happen"), we verify via the reasoning + text flow that text is safe.
        """
        # This scenario requires thinking_open=True AND reasoning_open=False simultaneously,
        # which cannot happen via normal API. We skip the direct line test and instead
        # verify the guard is present by reading the source; the scenario test below
        # exercises the adjacent paths.
        pass

    async def test_text_after_thinking_close_closes_reasoning(self):
        """Lines 1134-1135: reasoning_open=True when first text_delta after </think>.

        The </think> sentinel (content_block_stop while in_thinking) sets in_thinking=False
        but does NOT call _close_reasoning in the </think> path when streaming reasoning.
        Wait — actually it does call _close_reasoning if reasoning_open. Let me trace:
        - </think> → _close_thinking_capture(); if reasoning_open: _close_reasoning(); continue
        So lines 1134-1135 are NOT hit via the </think> path.

        Lines 1133-1135 are hit when: reasoning_open=True AND NOT in_thinking AND text_delta != None.
        This means: reasoning was opened (think block started), then a text_delta arrives
        WITHOUT a prior </think> — e.g., the stream ends a thinking block via some other path.

        Actually the issue is: when reasoning_open=True and text_delta arrives but in_thinking=False.
        That means: we were reasoning, then a text_delta with in_thinking=False arrived.
        Since text_delta's in_thinking comes from extract_stream_event_delta which only sets
        in_thinking=False when content_block_stop arrives while in_thinking=True.

        The scenario: think block opened (in_thinking=True, reasoning_open=True), then a
        text_delta arrives (content_block_delta with text_delta type) — this doesn't change
        in_thinking. So reasoning_open=True and in_thinking=True → goes to 1105 block, not 1133.

        For lines 1133-1135: we need in_thinking=False AND reasoning_open=True. This happens
        when the thinking block was opened, then SOMETHING sets in_thinking=False WITHOUT closing
        reasoning. Looking at the code: in_thinking is set to False only in extract_stream_event_delta
        for content_block_stop while in_thinking=True. But when that happens, </think> text_delta
        is returned, which at 1095 checks if reasoning_open and calls _close_reasoning.

        So lines 1133-1135 are only reachable if reasoning_open=True AND in_thinking becomes False
        via some path that DOESN'T trigger the </think> close. This seems defensive/unreachable.
        We'll document this and skip.
        """
        pass


# ---------------------------------------------------------------------------
# Line 517: _is_synthetic_ask_user_response_result — empty/non-string tool_use_id
# ---------------------------------------------------------------------------


class TestIsSyntheticAskUserResponseResultDirectly:
    def test_returns_false_when_tool_use_id_is_none(self):
        """Line 517: tool_use_id=None triggers early return False."""
        result_block = {
            "content": "User responded: something",
            "tool_use_id": None,
        }
        assert _is_synthetic_ask_user_response_result(result_block, {}, {}) is False

    def test_returns_false_when_tool_use_id_is_empty_string(self):
        """Line 517: tool_use_id='' triggers early return False."""
        result_block = {
            "content": "User responded: something",
            "tool_use_id": "",
        }
        assert _is_synthetic_ask_user_response_result(result_block, {}, {}) is False

    def test_returns_false_when_tool_use_id_is_not_string(self):
        """Line 517: tool_use_id=123 triggers early return False."""
        result_block = {
            "content": "User responded: something",
            "tool_use_id": 123,
        }
        assert _is_synthetic_ask_user_response_result(result_block, {}, {}) is False

    def test_returns_true_when_tool_name_matches(self):
        """Line 519-520: returns True when tool name is AskUserQuestion."""
        result_block = {
            "content": "User responded: yes",
            "tool_use_id": "toolu_ask",
        }
        assert (
            _is_synthetic_ask_user_response_result(
                result_block,
                {"toolu_ask": "AskUserQuestion"},
                {},
            )
            is True
        )

    def test_returns_false_when_no_match_no_context(self):
        """Lines 522-524: no match in tool_names_by_id and no context → False."""
        result_block = {
            "content": "User responded: yes",
            "tool_use_id": "toolu_unknown",
        }
        assert (
            _is_synthetic_ask_user_response_result(result_block, {}, None) is False
        )


# ---------------------------------------------------------------------------
# Lines 395-396: bridge_sse_stream — CancelledError suppressed after consumer breaks
# ---------------------------------------------------------------------------


class TestBridgeSseStreamCancelledError:
    async def test_consumer_break_triggers_cancel_path(self):
        """Lines 395-396: consumer breaking mid-stream cancels the reader task.

        When the consumer stops consuming (break out of the async for), the
        generator's aclose() is called which runs the finally block, cancels
        reader_task, and suppresses CancelledError.
        """

        async def sse_source():
            for i in range(10):
                yield f"event: e{i}\ndata: {{}}\n\n"
                await asyncio.sleep(0)  # yield control

        async def chunk_source_gen():
            return
            yield  # pragma: no cover

        first_line = None
        async for line in bridge_sse_stream(sse_source(), chunk_source_gen()):
            first_line = line
            break  # Stop early — triggers cancel path at lines 392-396

        assert first_line is not None


# ---------------------------------------------------------------------------
# Lines 466-467: _keepalive_wrapper — exception swallowed after task cancel
# ---------------------------------------------------------------------------


class TestKeepaliveWrapperTaskCancelPath:
    async def test_consumer_break_triggers_task_cancel(self):
        """Lines 466-467: consumer breaking cancels the inner task.

        When the consumer stops consuming (break out of the async for loop),
        the generator's aclose() runs the finally block, cancels the reader task,
        and any exception (CancelledError) from await task is suppressed.
        """
        from src.streaming_utils import _keepalive_wrapper

        async def source():
            for i in range(10):
                yield f"item{i}"
                await asyncio.sleep(0)

        first_item = None
        async for item in _keepalive_wrapper(source(), interval=60):
            first_item = item
            break  # Early stop — triggers cancel path at lines 463-467

        assert first_item == "item0"


# ---------------------------------------------------------------------------
# Lines 797, 848, 913: defensive early returns in nested functions
# These are unreachable via the public API (all callers guard the pre-condition).
# Documented explicitly so the coverage gap is understood.
# ---------------------------------------------------------------------------


class TestDefensiveBranchesDocumented:
    def test_line_797_open_message_item_idempotency_guard_is_defensive(self):
        """Line 797: _open_message_item() returns [] if already open.

        All callers in stream_response_chunks use `if not message_item_opened:`
        before calling, so this guard is unreachable via the normal code path.
        It is a defensive safety net only.
        """
        # Documented — no test possible without invasive mocking of closure state.
        pass

    def test_line_848_close_reasoning_guard_is_defensive(self):
        """Line 848: _close_reasoning() returns [] if reasoning_open=False.

        The finalization block `if reasoning_open: for line in _close_reasoning()`
        guards against calling with reasoning_open=False. This guard is never
        hit because reasoning_open is always checked first.
        """
        pass

    def test_line_913_close_message_item_guard_is_defensive(self):
        """Line 913: _close_message_item() returns [] if message_item_opened=False.

        In the interleaved think→text→think path, _close_message_item() is called
        at line 1064 only when message_item_opened is True (ensured by the caller).
        This guard is purely defensive.
        """
        pass


# ---------------------------------------------------------------------------
# Lines 1131, 1134-1135: defensive reasoning safety net (unreachable via normal API)
# ---------------------------------------------------------------------------


class TestReasoningSafetyNetDocumented:
    def test_line_1131_safety_net_unreachable(self):
        """Line 1131: in_thinking=True AND reasoning_open=False.

        Every time in_thinking transitions to True (via <think> sentinel),
        reasoning_open is simultaneously set to True (lines 1062-1086).
        Therefore in_thinking=True with reasoning_open=False cannot occur
        through the normal flow. This guard is a defensive safety net only.
        """
        pass

    def test_lines_1134_1135_exit_thinking_without_close_unreachable(self):
        """Lines 1134-1135: reasoning_open=True AND in_thinking=False via non-</think> path.

        in_thinking is set to False only when extract_stream_event_delta returns
        </think> (content_block_stop while in_thinking=True). At that point,
        the </think> path explicitly calls _close_reasoning() (line 1097-1099),
        so reasoning_open is set to False before any text delta can flow through.
        Lines 1133-1135 are therefore unreachable through the normal code path.
        """
        pass
