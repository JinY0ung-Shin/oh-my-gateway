#!/usr/bin/env python3
"""
Coverage-focused tests for src/streaming_utils.py.

Targets the ~49 uncovered lines identified by coverage analysis.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock
from src.models import ChatCompletionRequest, Message
from src.streaming_utils import (
    CollabJsonStreamFilter,
    ToolUseAccumulator,
    _build_task_event,
    _extract_tool_blocks,
    _normalize_tool_result,
    extract_embedded_tool_blocks,
    extract_stop_reason,
    extract_stream_event_delta,
    extract_user_tool_results,
    make_response_sse,
    make_tool_result_response_sse,
    make_tool_use_response_sse,
    stream_chunks,
    stream_response_chunks,
    strip_collab_json,
)


def _parse_chat_sse(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: "):])


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: "):], json.loads(data_line[len("data: "):])


# ---------------------------------------------------------------------------
# Line 78: Non-list content in _extract_tool_blocks()
# ---------------------------------------------------------------------------


class TestExtractToolBlocksNonList:
    def test_string_content_returns_empty_tools_and_content(self):
        """Line 78: non-list content returns ([], content)."""
        tool_blocks, non_tool = _extract_tool_blocks("some text")
        assert tool_blocks == []
        assert non_tool == "some text"

    def test_none_content_returns_empty_lists(self):
        """Line 78: None content returns ([], [])."""
        tool_blocks, non_tool = _extract_tool_blocks(None)
        assert tool_blocks == []
        assert non_tool == []

    def test_empty_string_returns_empty_lists(self):
        """Line 78: empty string is falsy, returns ([], [])."""
        tool_blocks, non_tool = _extract_tool_blocks("")
        assert tool_blocks == []
        assert non_tool == []


# ---------------------------------------------------------------------------
# Lines 82-87: Tool block type checking with hasattr fallback for SDK objects
# ---------------------------------------------------------------------------


class TestExtractToolBlocksSDKObjects:
    def test_sdk_tool_use_block(self):
        """Lines 82-83: ToolUseBlock instance is classified as tool block."""
        tb = ToolUseBlock(id="t1", name="Read", input={"path": "/tmp"})
        tool_blocks, non_tool = _extract_tool_blocks([tb])
        assert len(tool_blocks) == 1
        assert tool_blocks[0] is tb
        assert non_tool == []

    def test_sdk_tool_result_block(self):
        """Lines 82-83: ToolResultBlock instance is classified as tool block."""
        tr = ToolResultBlock(tool_use_id="t1", content="done")
        tool_blocks, non_tool = _extract_tool_blocks([tr])
        assert len(tool_blocks) == 1
        assert tool_blocks[0] is tr

    def test_hasattr_fallback_tool_use(self):
        """Lines 86-87: generic object with type='tool_use' via hasattr fallback."""
        obj = SimpleNamespace(type="tool_use", id="t1", name="Read", input={})
        tool_blocks, non_tool = _extract_tool_blocks([obj])
        assert len(tool_blocks) == 1
        assert tool_blocks[0] is obj

    def test_hasattr_fallback_tool_result(self):
        """Lines 86-87: generic object with type='tool_result' via hasattr fallback."""
        obj = SimpleNamespace(type="tool_result", tool_use_id="t1", content="ok")
        tool_blocks, non_tool = _extract_tool_blocks([obj])
        assert len(tool_blocks) == 1
        assert tool_blocks[0] is obj

    def test_non_tool_object_classified_as_non_tool(self):
        """Non-tool objects go to non_tool list."""
        obj = SimpleNamespace(type="text", text="hello")
        tool_blocks, non_tool = _extract_tool_blocks([obj])
        assert tool_blocks == []
        assert len(non_tool) == 1


# ---------------------------------------------------------------------------
# Lines 119-122: Escape sequence handling in collab JSON parser
# ---------------------------------------------------------------------------


class TestStripCollabJsonEscapeSequences:
    def test_escape_in_json_string_values(self):
        """Lines 120, 122: backslash escapes inside JSON string values."""
        collab = json.dumps({
            "collab_tool_call": {
                "type": "spawn_agent",
                "prompt": 'path is C:\\Users\\test and "quoted"',
            }
        })
        text = f"Before {collab} After"
        result = strip_collab_json(text)
        assert "collab_tool_call" not in result
        assert "Before" in result
        assert "After" in result

    def test_escaped_quotes_in_json(self):
        """Lines 120, 122: escaped double quotes don't break brace matching."""
        collab = json.dumps({
            "collab_tool_call": {
                "type": "wait",
                "message": 'He said \\"hello\\" to {everyone}',
            }
        })
        text = f"Start{collab}End"
        result = strip_collab_json(text)
        assert "collab_tool_call" not in result
        assert "Start" in result
        assert "End" in result


# ---------------------------------------------------------------------------
# Lines 142-143: JSON decode error in collab JSON strip
# ---------------------------------------------------------------------------


class TestStripCollabJsonDecodeError:
    def test_invalid_json_block_preserved(self):
        """Lines 142-143: malformed JSON block is preserved in output."""
        text = "Before {not valid json: !! } After"
        result = strip_collab_json(text)
        assert "{not valid json: !! }" in result
        assert "Before" in result
        assert "After" in result

    def test_incomplete_json_preserved(self):
        """Lines 142-143: incomplete JSON block (no closing brace) is preserved."""
        text = "Start {incomplete"
        result = strip_collab_json(text)
        assert "{incomplete" in result
        assert "Start" in result


# ---------------------------------------------------------------------------
# Lines 184, 186: Escape sequence handling in streaming collab filter
# ---------------------------------------------------------------------------


class TestCollabStreamFilterEscapeSequences:
    def test_escape_sequences_in_streaming(self):
        """Lines 183-186: backslash escapes in buffered JSON strings."""
        collab = json.dumps({
            "collab_tool_call": {
                "type": "spawn_agent",
                "prompt": 'path\\with\\backslashes and "quotes"',
            }
        })
        f = CollabJsonStreamFilter()
        output_parts = []
        for ch in f"Hello {collab} World":
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

    def test_escaped_quote_does_not_break_buffer(self):
        """Lines 183-186: escaped quote inside string keeps buffer tracking correct."""
        # Build a JSON string with escaped quotes manually
        raw_json = '{"collab_tool_call": {"type": "wait", "msg": "say \\"hi\\""}}'
        f = CollabJsonStreamFilter()
        output_parts = []
        for ch in f"A{raw_json}B":
            out = f.feed(ch)
            if out:
                output_parts.append(out)
        remaining = f.flush()
        if remaining:
            output_parts.append(remaining)
        result = "".join(output_parts)
        assert "collab_tool_call" not in result
        assert "A" in result
        assert "B" in result


# ---------------------------------------------------------------------------
# Lines 205-206: JSON decode error in streaming collab JSON validation
# ---------------------------------------------------------------------------


class TestCollabStreamFilterDecodeError:
    def test_malformed_json_with_collab_marker_flushed(self):
        """Lines 205-206: buffer contains collab marker but is not valid JSON."""
        f = CollabJsonStreamFilter()
        # Feed a block that contains the collab marker but is invalid JSON
        bad_block = '{"collab_tool_call": broken}'
        result = f.feed(bad_block)
        result += f.flush()
        # Invalid JSON should be flushed as-is
        assert "collab_tool_call" in result


# ---------------------------------------------------------------------------
# Lines 211-213: Buffer overflow protection (>MAX_BUFFER flush)
# ---------------------------------------------------------------------------


class TestCollabStreamFilterBufferOverflow:
    def test_buffer_overflow_flushes(self):
        """Lines 211-213: buffer exceeding MAX_BUFFER is flushed."""
        f = CollabJsonStreamFilter()
        # Start a JSON block that never closes
        out = f.feed("{")
        assert out == ""
        # Feed enough characters to exceed MAX_BUFFER (8192)
        big_chunk = "x" * 8200
        out = f.feed(big_chunk)
        # Buffer should have been flushed because it exceeded MAX_BUFFER
        assert len(out) > 8000
        assert out.startswith("{")


# ---------------------------------------------------------------------------
# Line 317: Pydantic model_dump fallback for response objects
# ---------------------------------------------------------------------------


class TestMakeResponseSSEPlainDictFallback:
    def test_plain_dict_response_object(self):
        """Line 317: response_obj without model_dump (plain dict) is used directly."""
        plain_dict = {"id": "resp-1", "model": "test", "status": "completed"}
        line = make_response_sse(
            "response.completed",
            response_obj=plain_dict,
            sequence_number=5,
        )
        _, payload = _parse_response_sse(line)
        assert payload["response"] == plain_dict
        assert payload["sequence_number"] == 5


# ---------------------------------------------------------------------------
# Line 356: None return for unmatched system chunk subtype
# ---------------------------------------------------------------------------


class TestBuildTaskEventUnmatched:
    def test_unmatched_subtype_returns_none(self):
        """Line 356: unrecognized system subtype returns None."""
        result = _build_task_event({"subtype": "unknown_subtype"})
        assert result is None

    def test_missing_subtype_returns_none(self):
        """Line 356: missing subtype returns None."""
        result = _build_task_event({})
        assert result is None


# ---------------------------------------------------------------------------
# Lines 395, 399-427: Tool block normalization fallbacks
# ---------------------------------------------------------------------------


class TestNormalizeToolResult:
    def test_sdk_tool_result_block(self):
        """Lines 401-407: ToolResultBlock instance is normalized."""
        tr = ToolResultBlock(tool_use_id="t1", content="hello", is_error=None)
        result = _normalize_tool_result(tr)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "t1"
        assert result["content"] == "hello"
        assert result["is_error"] is False

    def test_generic_sdk_object_with_tool_use_id(self):
        """Lines 408-414: generic object with tool_use_id attribute."""
        obj = SimpleNamespace(tool_use_id="t2", content="ok", is_error=True)
        result = _normalize_tool_result(obj)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "t2"
        assert result["content"] == "ok"
        assert result["is_error"] is True

    def test_dict_tool_result(self):
        """Lines 415-421: dict is normalized."""
        d = {"tool_use_id": "t3", "content": "done", "is_error": False}
        result = _normalize_tool_result(d)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "t3"

    def test_unknown_type_fallback(self):
        """Lines 422-427: completely unknown type uses str() fallback."""
        result = _normalize_tool_result(42)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == ""
        assert result["content"] == "42"
        assert result["is_error"] is False

    def test_string_fallback(self):
        """Lines 422-427: string input uses str() fallback."""
        result = _normalize_tool_result("raw text result")
        assert result["content"] == "raw text result"


# ---------------------------------------------------------------------------
# Line 479: Non-list content in extract_embedded_tool_blocks
# ---------------------------------------------------------------------------


class TestExtractEmbeddedToolBlocksNonList:
    def test_string_content_returns_empty(self):
        """Line 479: non-list content returns []."""
        chunk = {"type": "assistant", "content": "just a string"}
        assert extract_embedded_tool_blocks(chunk) == []

    def test_none_content_falls_back_to_message(self):
        """Lines 474-478: None content checks message fallback."""
        chunk = {
            "type": "assistant",
            "content": None,
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
                ]
            },
        }
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"

    def test_none_content_no_message_returns_empty(self):
        """Line 479: None content and no message content returns []."""
        chunk = {"type": "assistant", "content": None}
        assert extract_embedded_tool_blocks(chunk) == []


# ---------------------------------------------------------------------------
# Lines 487-506: Tool block normalization for SDK ToolUseBlock, ToolResultBlock
# ---------------------------------------------------------------------------


class TestExtractEmbeddedToolBlocksSDKNormalization:
    def test_sdk_tool_use_block_normalized_to_dict(self):
        """Lines 487-495: ToolUseBlock is normalized to a plain dict."""
        tb = ToolUseBlock(id="sdk-t1", name="Read", input={"path": "/tmp"})
        chunk = {"type": "assistant", "content": [tb]}
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 1
        assert isinstance(blocks[0], dict)
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["id"] == "sdk-t1"
        assert blocks[0]["name"] == "Read"
        assert blocks[0]["input"] == {"path": "/tmp"}

    def test_sdk_tool_result_block_normalized_to_dict(self):
        """Lines 496-497: ToolResultBlock is normalized via _normalize_tool_result."""
        tr = ToolResultBlock(tool_use_id="sdk-t1", content="file data")
        chunk = {"type": "assistant", "content": [tr]}
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 1
        assert isinstance(blocks[0], dict)
        assert blocks[0]["type"] == "tool_result"
        assert blocks[0]["tool_use_id"] == "sdk-t1"
        assert blocks[0]["content"] == "file data"

    def test_generic_sdk_object_with_type_attr_fallback(self):
        """Lines 498-504: generic SDK object with type attribute uses fallback."""
        obj = SimpleNamespace(
            type="tool_use",
            id="gen-t1",
            name="Bash",
            input={"cmd": "ls"},
        )
        chunk = {"type": "assistant", "content": [obj]}
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["id"] == "gen-t1"
        assert blocks[0]["name"] == "Bash"
        assert blocks[0]["input"] == {"cmd": "ls"}

    def test_generic_sdk_object_partial_attrs(self):
        """Lines 498-504: generic SDK object with only some attributes."""
        obj = SimpleNamespace(type="tool_result", tool_use_id="gen-t2", content="ok")
        chunk = {"type": "assistant", "content": [obj]}
        blocks = extract_embedded_tool_blocks(chunk)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_result"
        assert blocks[0]["tool_use_id"] == "gen-t2"
        assert "name" not in blocks[0]

    def test_object_without_type_attr_passthrough(self):
        """Lines 505-506: object without type attribute passes through as-is."""
        obj = MagicMock(spec=[])  # no attributes at all
        chunk = {"type": "assistant", "content": [obj]}
        blocks = extract_embedded_tool_blocks(chunk)
        # Object without type attr is not a tool block, goes to non_tool
        assert blocks == []


# ---------------------------------------------------------------------------
# Line 552: Tool use accumulator non-tool content_block_start returns not handled
# ---------------------------------------------------------------------------


class TestToolUseAccumulatorContentBlockStart:
    def test_non_tool_use_content_block_start_not_handled(self):
        """Line 552: content_block_start with non-tool_use type returns (False, None)."""
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text"},
            },
        })
        assert handled is False
        assert result is None


# ---------------------------------------------------------------------------
# Line 564: Sub-agent text delta skipping
# ---------------------------------------------------------------------------


class TestToolUseAccumulatorSubAgentTextDelta:
    def test_sub_agent_non_input_json_delta_with_parent_id(self):
        """Line 564: content_block_delta with parent_id but not input_json_delta."""
        acc = ToolUseAccumulator()
        # text_delta from sub-agent (has parent_tool_use_id, not input_json_delta)
        handled, result = acc.process_stream_event({
            "type": "stream_event",
            "parent_tool_use_id": "parent-1",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "sub-agent noise"},
            },
        })
        assert handled is True
        assert result is None

    def test_non_input_json_delta_without_parent_id(self):
        """Line 564: content_block_delta without parent_id and not input_json_delta."""
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "normal text"},
            },
        })
        assert handled is False
        assert result is None


# ---------------------------------------------------------------------------
# Lines 585-589: Sub-agent non-tool stream event skipping
# ---------------------------------------------------------------------------


class TestToolUseAccumulatorSubAgentContentBlockStop:
    def test_content_block_stop_with_parent_id_no_accumulator(self):
        """Lines 585-586: content_block_stop with parent_id but no accumulated block."""
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event({
            "type": "stream_event",
            "parent_tool_use_id": "parent-1",
            "event": {"type": "content_block_stop", "index": 0},
        })
        assert handled is True
        assert result is None

    def test_content_block_stop_no_parent_no_accumulator(self):
        """Line 587: content_block_stop with no parent_id and no accumulated block."""
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event({
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 99},
        })
        assert handled is False
        assert result is None

    def test_unrecognized_event_type_returns_not_handled(self):
        """Line 589: unrecognized event type returns (False, None)."""
        acc = ToolUseAccumulator()
        handled, result = acc.process_stream_event({
            "type": "stream_event",
            "event": {"type": "message_start"},
        })
        assert handled is False
        assert result is None


# ---------------------------------------------------------------------------
# Line 611: Empty content in extract_user_tool_results
# ---------------------------------------------------------------------------


class TestExtractUserToolResultsEmptyContent:
    def test_empty_list_content(self):
        """Line 611: empty content list returns ([], parent_id)."""
        results, parent_id = extract_user_tool_results({
            "type": "user",
            "content": [],
            "parent_tool_use_id": "p1",
        })
        assert results == []
        assert parent_id == "p1"

    def test_none_content_no_message(self):
        """Line 611: non-list content and no message fallback."""
        results, parent_id = extract_user_tool_results({
            "type": "user",
            "content": 42,
        })
        assert results == []
        assert parent_id is None

    def test_non_list_content_message_fallback_empty(self):
        """Line 611: non-list content with message that has empty content."""
        results, parent_id = extract_user_tool_results({
            "type": "user",
            "content": "string",
            "message": {"content": []},
        })
        assert results == []


# ---------------------------------------------------------------------------
# Lines 712, 714: Token streaming mode duplicate assistant content filtering
# ---------------------------------------------------------------------------


class TestStreamChunksTokenStreamingDuplicateFiltering:
    @pytest.mark.asyncio
    async def test_stream_event_skipped_after_token_streaming(self):
        """Line 712: stream_event chunks are skipped in token streaming mode."""

        async def source():
            # First: a text delta to enable token_streaming
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            }
            # Then: a stray stream_event that should be skipped
            yield {
                "type": "stream_event",
                "event": {"type": "message_stop"},
            }
            # Then: an assistant content chunk that should be skipped
            yield {"content": [{"type": "text", "text": "duplicate"}]}

        request = ChatCompletionRequest(
            model="claude-test",
            messages=[Message(role="user", content="Hi")],
            stream=True,
        )
        lines = [
            line
            async for line in stream_chunks(
                source(), request, "req-dup", [], logging.getLogger("test-dup")
            )
        ]
        all_content = "".join(lines)
        assert "Hello" in all_content
        # "duplicate" should NOT appear because it's filtered in token streaming mode
        assert "duplicate" not in all_content


# ---------------------------------------------------------------------------
# Line 722: Parent tool use ID assignment
# ---------------------------------------------------------------------------


class TestStreamChunksParentToolUseId:
    @pytest.mark.asyncio
    async def test_user_chunk_with_parent_tool_use_id(self):
        """Line 722: parent_tool_use_id is attached to tool_result system_event."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hi"},
                },
            }
            yield {
                "type": "user",
                "parent_tool_use_id": "parent-99",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "result"},
                ],
            }

        request = ChatCompletionRequest(
            model="claude-test",
            messages=[Message(role="user", content="Hi")],
            stream=True,
        )
        lines = [
            line
            async for line in stream_chunks(
                source(), request, "req-parent", [], logging.getLogger("test-parent")
            )
        ]
        system_events = []
        for line in lines:
            if line.startswith("data: ") and "system_event" in line:
                parsed = json.loads(line[len("data: "):])
                system_events.append(parsed["system_event"])

        assert len(system_events) == 1
        assert system_events[0]["parent_tool_use_id"] == "parent-99"
        assert system_events[0]["tool_use_id"] == "t1"


# ---------------------------------------------------------------------------
# Lines 748-750: Collab filter flush at stream end
# ---------------------------------------------------------------------------


class TestStreamChunksCollabFilterFlush:
    @pytest.mark.asyncio
    async def test_collab_filter_flushes_remaining_at_end(self):
        """Lines 748-750: remaining buffered text is flushed at stream end."""

        async def source():
            # Start a JSON-like block that never closes
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello {incomplete"},
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
                source(), request, "req-flush", [], logging.getLogger("test-flush")
            )
        ]
        all_content = "".join(lines)
        assert "Hello" in all_content
        assert "{incomplete" in all_content


# ---------------------------------------------------------------------------
# Line 807: Task event extraction for system chunks in Responses API
# (Line 897 in stream_response_chunks — system chunk with valid task_event)
# ---------------------------------------------------------------------------


class TestStreamResponseChunksSystemTaskEvent:
    @pytest.mark.asyncio
    async def test_system_chunk_task_event_in_responses_api(self):
        """Line 897: system chunks with task events are emitted as custom SSE."""

        async def source():
            yield {
                "type": "system",
                "subtype": "task_progress",
                "task_id": "t1",
                "description": "Processing",
                "last_tool_name": "Grep",
                "usage": {"tool_uses": 2},
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
                response_id="resp-sys",
                output_item_id="msg-sys",
                chunks_buffer=[],
                logger=logging.getLogger("test-resp-sys"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        assert "response.task_progress" in event_types
        task_ev = next(p for et, p in parsed if et == "response.task_progress")
        assert task_ev["task_id"] == "t1"
        assert task_ev["last_tool_name"] == "Grep"


# ---------------------------------------------------------------------------
# Line 931: Logging warning for incomplete tool use blocks (Responses API)
# ---------------------------------------------------------------------------


class TestStreamResponseChunksIncompleteToolWarning:
    @pytest.mark.asyncio
    async def test_incomplete_tool_blocks_warning(self, caplog):
        """Line 931/986: incomplete tool_use blocks trigger warning."""

        async def source():
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
                    "index": 5,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-incomplete",
                        "name": "Write",
                    },
                },
            }
            # No content_block_stop — tool_use remains incomplete

        stream_result = {}
        logger = logging.getLogger("test-incomplete-tool-resp")

        with caplog.at_level(logging.WARNING):
            lines = [
                line
                async for line in stream_response_chunks(
                    chunk_source=source(),
                    model="claude-test",
                    response_id="resp-incomplete",
                    output_item_id="msg-incomplete",
                    chunks_buffer=[],
                    logger=logger,
                    stream_result=stream_result,
                )
            ]

        assert "Incomplete tool_use blocks" in caplog.text
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Lines 981-983: Responses API stream exception handling
# ---------------------------------------------------------------------------


class TestStreamResponseChunksExceptionHandling:
    @pytest.mark.asyncio
    async def test_exception_during_streaming(self):
        """Lines 972-976: unexpected exception emits response.failed."""

        async def exploding_source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "partial"},
                },
            }
            raise ValueError("unexpected boom")
            yield  # pragma: no cover

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=exploding_source(),
                model="claude-test",
                response_id="resp-exception",
                output_item_id="msg-exception",
                chunks_buffer=[],
                logger=logging.getLogger("test-resp-exception"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.failed"
        assert parsed[-1][1]["response"]["error"]["code"] == "server_error"
        assert stream_result["success"] is False


# ---------------------------------------------------------------------------
# Lines 978-983: Responses API collab filter flush at stream end
# ---------------------------------------------------------------------------


class TestStreamResponseChunksCollabFlush:
    @pytest.mark.asyncio
    async def test_collab_filter_flush_at_end(self):
        """Lines 978-983: remaining buffered text flushed at end of response stream."""

        async def source():
            # Emit text that starts a JSON-like block
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello {leftover"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-flush",
                output_item_id="msg-flush",
                chunks_buffer=[],
                logger=logging.getLogger("test-resp-flush"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        deltas = [p.get("delta", "") for et, p in parsed if et == "response.output_text.delta"]
        combined = "".join(d for d in deltas if isinstance(d, str))
        assert "Hello" in combined
        assert "{leftover" in combined
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Responses API: duplicate assistant content filtering in token streaming
# ---------------------------------------------------------------------------


class TestStreamResponseChunksDuplicateFiltering:
    @pytest.mark.asyncio
    async def test_duplicate_assistant_content_skipped(self):
        """Lines 930-933: duplicate assistant content skipped in token streaming mode."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Real"},
                },
            }
            # Stray stream_event in token streaming mode
            yield {
                "type": "stream_event",
                "event": {"type": "message_stop"},
            }
            # Duplicate assistant content in token streaming mode
            yield {"content": [{"type": "text", "text": "Duplicate"}]}

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-dup",
                output_item_id="msg-dup",
                chunks_buffer=[],
                logger=logging.getLogger("test-resp-dup"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        deltas = [p.get("delta", "") for et, p in parsed if et == "response.output_text.delta"]
        combined = "".join(d for d in deltas if isinstance(d, str))
        assert "Real" in combined
        assert "Duplicate" not in combined
        assert stream_result["success"] is True


# ---------------------------------------------------------------------------
# Responses API: system chunk with unmatched subtype returns None
# ---------------------------------------------------------------------------


class TestStreamResponseChunksSystemUnmatchedSubtype:
    @pytest.mark.asyncio
    async def test_system_chunk_unmatched_subtype_skipped(self):
        """Line 356/897: system chunk with unrecognized subtype is skipped."""

        async def source():
            yield {
                "type": "system",
                "subtype": "weird_unknown_subtype",
            }
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Content"},
                },
            }

        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-unknown-sys",
                output_item_id="msg-unknown-sys",
                chunks_buffer=[],
                logger=logging.getLogger("test-unknown-sys"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        event_types = [et for et, _ in parsed]
        # No task event should have been emitted for unknown subtype
        assert "response.weird_unknown_subtype" not in event_types
        # Content should still flow through
        deltas = [p.get("delta", "") for et, p in parsed if et == "response.output_text.delta"]
        assert "Content" in "".join(d for d in deltas if isinstance(d, str))


# ---------------------------------------------------------------------------
# Chat Completions: system chunk with unmatched subtype (None return)
# ---------------------------------------------------------------------------


class TestStreamChunksSystemUnmatched:
    @pytest.mark.asyncio
    async def test_system_unmatched_subtype_no_event(self):
        """Lines 356, 682-685: system chunk with None task_event skips emission."""

        async def source():
            yield {"type": "system", "subtype": "not_a_real_subtype"}
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "OK"},
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
                source(), request, "req-sys-skip", [], logging.getLogger("test-sys-skip")
            )
        ]
        all_content = "".join(lines)
        assert "OK" in all_content
        # No system_event for unmatched subtype
        for line in lines:
            if line.startswith("data: "):
                parsed = json.loads(line[len("data: "):])
                if "system_event" in parsed:
                    assert parsed["system_event"]["type"] != "not_a_real_subtype"


# ---------------------------------------------------------------------------
# Lines 59-62: extract_stop_reason
# ---------------------------------------------------------------------------


class TestExtractStopReason:
    def test_returns_stop_reason_from_last_result(self):
        """Lines 59-61: extracts stop_reason from the last result message."""
        messages = [
            {"type": "assistant", "stop_reason": "end_turn"},
            {"type": "metadata"},
        ]
        assert extract_stop_reason(messages) == "end_turn"

    def test_returns_none_when_no_stop_reason(self):
        """Line 62: returns None when no message has stop_reason."""
        messages = [{"type": "assistant"}, {"type": "metadata"}]
        assert extract_stop_reason(messages) is None

    def test_picks_last_message_with_stop_reason(self):
        messages = [
            {"stop_reason": "max_tokens"},
            {"stop_reason": "end_turn"},
        ]
        assert extract_stop_reason(messages) == "end_turn"


# ---------------------------------------------------------------------------
# Line 259: extract_stream_event_delta with parent_tool_use_id
# ---------------------------------------------------------------------------


class TestExtractStreamEventDeltaParentId:
    def test_stream_event_with_parent_id_returns_none(self):
        """Line 259: stream_event with parent_tool_use_id returns (None, in_thinking)."""
        result, in_thinking = extract_stream_event_delta(
            {
                "type": "stream_event",
                "parent_tool_use_id": "parent-1",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "sub-agent text"},
                },
            },
            in_thinking=False,
        )
        assert result is None
        assert in_thinking is False


# ---------------------------------------------------------------------------
# Line 395: make_tool_use_response_sse with parent_tool_use_id
# ---------------------------------------------------------------------------


class TestMakeToolUseResponseSSEParentId:
    def test_includes_parent_tool_use_id(self):
        """Line 395: parent_tool_use_id is included when provided."""
        tool_block = {"id": "t1", "name": "Read", "input": {"path": "/tmp"}}
        line = make_tool_use_response_sse(
            tool_block, sequence_number=0, parent_tool_use_id="parent-1"
        )
        _, payload = _parse_response_sse(line)
        assert payload["parent_tool_use_id"] == "parent-1"

    def test_omits_parent_tool_use_id_when_none(self):
        """Line 394: parent_tool_use_id is not included when None."""
        tool_block = {"id": "t1", "name": "Read", "input": {}}
        line = make_tool_use_response_sse(tool_block, sequence_number=0)
        _, payload = _parse_response_sse(line)
        assert "parent_tool_use_id" not in payload


# ---------------------------------------------------------------------------
# Line 442: make_tool_result_response_sse with parent_tool_use_id
# ---------------------------------------------------------------------------


class TestMakeToolResultResponseSSEParentId:
    def test_includes_parent_tool_use_id(self):
        """Line 442: parent_tool_use_id is included when provided."""
        result_block = {"tool_use_id": "t1", "content": "ok", "is_error": False}
        line = make_tool_result_response_sse(
            result_block, sequence_number=0, parent_tool_use_id="parent-2"
        )
        _, payload = _parse_response_sse(line)
        assert payload["parent_tool_use_id"] == "parent-2"


# ---------------------------------------------------------------------------
# Line 807: stream_result is None default
# ---------------------------------------------------------------------------


class TestStreamResponseChunksStreamResultNone:
    @pytest.mark.asyncio
    async def test_stream_result_none_defaults_to_empty_dict(self):
        """Line 807: stream_result=None is handled by defaulting to {}."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Test"},
                },
            }

        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-none-sr",
                output_item_id="msg-none-sr",
                chunks_buffer=[],
                logger=logging.getLogger("test-none-sr"),
                stream_result=None,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        assert parsed[-1][0] == "response.completed"


# ---------------------------------------------------------------------------
# Lines 1036-1037: SDK usage path in stream_response_chunks completed event
# ---------------------------------------------------------------------------


class TestStreamResponseChunksSDKUsage:
    @pytest.mark.asyncio
    async def test_sdk_usage_in_completed_response(self):
        """Lines 1036-1037: real SDK usage is used when available."""

        async def source():
            yield {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            }
            yield {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }

        chunks_buffer = []
        stream_result = {}
        lines = [
            line
            async for line in stream_response_chunks(
                chunk_source=source(),
                model="claude-test",
                response_id="resp-usage",
                output_item_id="msg-usage",
                chunks_buffer=chunks_buffer,
                logger=logging.getLogger("test-usage"),
                stream_result=stream_result,
            )
        ]
        parsed = [_parse_response_sse(line) for line in lines]
        completed = parsed[-1]
        assert completed[0] == "response.completed"
        usage = completed[1]["response"]["usage"]
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
