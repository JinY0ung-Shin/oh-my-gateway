"""Additional branch-coverage tests for parser helpers.

Focuses on small, tractable functions in:
- ``src.sanitizer.routes`` — SSE byte-stream parsers
- ``src.sanitizer.openai_bridge`` — Anthropic ↔ OpenAI content/role converters
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Iterable, List

import pytest

from src.sanitizer import openai_bridge, routes as sanitizer_routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSSEResponse:
    """Minimal stand-in for an ``httpx.Response`` exposing ``aiter_lines``."""

    def __init__(self, lines: Iterable[str]) -> None:
        self._lines = list(lines)

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


async def _collect(agen) -> List[dict]:
    out: list[dict] = []
    async for evt in agen:
        out.append(evt)
    return out


# ---------------------------------------------------------------------------
# _iter_sse_events
# ---------------------------------------------------------------------------


class TestIterSSEEvents:
    async def test_yields_parsed_events_split_by_blank_lines(self):
        lines = [
            "event: message_start",
            'data: {"type":"message_start"}',
            "",
            "event: content_block_start",
            'data: {"type":"content_block_start","index":0}',
            "",
        ]
        events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert [e["type"] for e in events] == ["message_start", "content_block_start"]

    async def test_consecutive_blank_lines_do_not_emit_extra_events(self):
        lines = [
            "",
            "",
            'data: {"type":"ping"}',
            "",
            "",
        ]
        events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == [{"type": "ping"}]

    async def test_invalid_json_payload_is_dropped_with_warning(self, caplog):
        lines = [
            "data: not-json",
            "",
            'data: {"type":"ok"}',
            "",
        ]
        with caplog.at_level("WARNING", logger="src.sanitizer.routes"):
            events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == [{"type": "ok"}]
        assert any("non-JSON SSE payload" in rec.message for rec in caplog.records)

    async def test_non_dict_payload_is_dropped(self):
        # JSON parses successfully but yields a list, not a dict.
        lines = [
            "data: [1, 2, 3]",
            "",
            'data: {"type":"ok"}',
            "",
        ]
        events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == [{"type": "ok"}]

    async def test_multi_line_data_field_is_joined(self):
        lines = [
            'data: {"type":',
            'data: "split"}',
            "",
        ]
        events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == [{"type": "split"}]

    async def test_trailing_event_without_blank_line_is_flushed(self):
        lines = ['data: {"type":"trailing"}']
        events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == [{"type": "trailing"}]

    async def test_trailing_event_invalid_json_logs_warning(self, caplog):
        lines = ["data: still-not-json"]
        with caplog.at_level("WARNING", logger="src.sanitizer.routes"):
            events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == []
        assert any("trailing non-JSON" in rec.message for rec in caplog.records)

    async def test_event_id_retry_and_comment_lines_are_ignored(self):
        lines = [
            ": this is a comment",
            "id: 12345",
            "retry: 1000",
            'data: {"type":"survived"}',
            "",
        ]
        events = await _collect(sanitizer_routes._iter_sse_events(_FakeSSEResponse(lines)))
        assert events == [{"type": "survived"}]


# ---------------------------------------------------------------------------
# _iter_openai_sse_chunks
# ---------------------------------------------------------------------------


class TestIterOpenAISSE:
    async def test_done_sentinel_is_skipped(self):
        lines = [
            'data: {"choices":[]}',
            "",
            "data: [DONE]",
            "",
        ]
        events = await _collect(
            sanitizer_routes._iter_openai_sse_chunks(_FakeSSEResponse(lines))
        )
        assert events == [{"choices": []}]

    async def test_consecutive_blanks_emit_nothing(self):
        lines = ["", "", ""]
        events = await _collect(
            sanitizer_routes._iter_openai_sse_chunks(_FakeSSEResponse(lines))
        )
        assert events == []

    async def test_invalid_json_is_dropped_with_warning(self, caplog):
        lines = ["data: not-json", "", 'data: {"choices":[]}', ""]
        with caplog.at_level("WARNING", logger="src.sanitizer.routes"):
            events = await _collect(
                sanitizer_routes._iter_openai_sse_chunks(_FakeSSEResponse(lines))
            )
        assert events == [{"choices": []}]
        assert any("non-JSON OpenAI SSE" in rec.message for rec in caplog.records)

    async def test_trailing_done_returns_without_yield(self):
        lines = ["data: [DONE]"]
        events = await _collect(
            sanitizer_routes._iter_openai_sse_chunks(_FakeSSEResponse(lines))
        )
        assert events == []

    async def test_trailing_event_without_blank_is_flushed(self):
        lines = ['data: {"choices":[{"delta":{"content":"hi"}}]}']
        events = await _collect(
            sanitizer_routes._iter_openai_sse_chunks(_FakeSSEResponse(lines))
        )
        assert events == [{"choices": [{"delta": {"content": "hi"}}]}]

    async def test_trailing_invalid_json_logs_warning(self, caplog):
        lines = ["data: still-broken"]
        with caplog.at_level("WARNING", logger="src.sanitizer.routes"):
            events = await _collect(
                sanitizer_routes._iter_openai_sse_chunks(_FakeSSEResponse(lines))
            )
        assert events == []
        assert any("trailing non-JSON OpenAI" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _format_sse
# ---------------------------------------------------------------------------


class TestFormatSSE:
    def test_serializes_event_with_type(self):
        out = sanitizer_routes._format_sse({"type": "message_start", "data": {"x": 1}})
        text = out.decode("utf-8")
        assert text.startswith("event: message_start\n")
        assert "data: " in text
        assert text.endswith("\n\n")
        # Round-trip the payload.
        data_line = [l for l in text.split("\n") if l.startswith("data: ")][0]
        payload = json.loads(data_line[6:])
        assert payload == {"type": "message_start", "data": {"x": 1}}

    def test_defaults_to_message_when_type_missing(self):
        out = sanitizer_routes._format_sse({"foo": "bar"}).decode("utf-8")
        assert out.startswith("event: message\n")

    def test_preserves_unicode_with_ensure_ascii_false(self):
        out = sanitizer_routes._format_sse({"type": "x", "text": "안녕"}).decode("utf-8")
        assert "안녕" in out  # not escaped to \uXXXX


# ---------------------------------------------------------------------------
# _make_client
# ---------------------------------------------------------------------------


class TestMakeClient:
    def test_make_client_uses_tls_verify(self, monkeypatch):
        captured = {}

        class _FakeClient:
            def __init__(self, timeout, verify):
                captured["timeout"] = timeout
                captured["verify"] = verify

        monkeypatch.setattr(sanitizer_routes.httpx, "AsyncClient", _FakeClient)
        monkeypatch.setenv("SANITIZER_TLS_VERIFY", "false")
        sanitizer_routes._make_client(timeout=12.5)
        assert captured["timeout"] == 12.5
        assert captured["verify"] is False


# ---------------------------------------------------------------------------
# _is_loopback_request
# ---------------------------------------------------------------------------


class TestIsLoopbackRequest:
    def test_loopback_hosts(self):
        class _C:
            def __init__(self, host):
                self.host = host

        class _Req:
            def __init__(self, host):
                self.client = _C(host)

        assert sanitizer_routes._is_loopback_request(_Req("127.0.0.1")) is True
        assert sanitizer_routes._is_loopback_request(_Req("::1")) is True
        assert sanitizer_routes._is_loopback_request(_Req("localhost")) is True
        assert sanitizer_routes._is_loopback_request(_Req("203.0.113.5")) is False

    def test_missing_client_is_not_loopback(self):
        class _Req:
            client = None

        assert sanitizer_routes._is_loopback_request(_Req()) is False


# ---------------------------------------------------------------------------
# _filter_headers
# ---------------------------------------------------------------------------


class TestFilterHeaders:
    def test_drops_listed_headers_case_insensitively(self):
        out = sanitizer_routes._filter_headers(
            [("Host", "x"), ("Authorization", "y"), ("X-Foo", "z")],
            frozenset({"host", "authorization"}),
        )
        assert out == {"X-Foo": "z"}

    def test_keeps_all_when_drop_is_empty(self):
        items = [("Foo", "1"), ("Bar", "2")]
        assert sanitizer_routes._filter_headers(items, frozenset()) == {"Foo": "1", "Bar": "2"}


# ---------------------------------------------------------------------------
# openai_bridge helpers
# ---------------------------------------------------------------------------


class TestFlattenTextBlocks:
    def test_string_returned_verbatim(self):
        assert openai_bridge._flatten_text_blocks("hello") == "hello"

    def test_non_list_non_string_returns_empty(self):
        assert openai_bridge._flatten_text_blocks(42) == ""
        assert openai_bridge._flatten_text_blocks({"type": "text"}) == ""
        assert openai_bridge._flatten_text_blocks(None) == ""

    def test_concatenates_text_blocks(self):
        blocks = [
            {"type": "text", "text": "Hel"},
            {"type": "thinking", "thinking": "skip me"},
            {"type": "text", "text": "lo"},
        ]
        assert openai_bridge._flatten_text_blocks(blocks) == "Hello"

    def test_non_dict_blocks_are_skipped(self):
        assert openai_bridge._flatten_text_blocks([1, "x", {"type": "text", "text": "ok"}]) == "ok"

    def test_text_block_with_non_string_text_is_skipped(self):
        assert openai_bridge._flatten_text_blocks([{"type": "text", "text": 99}]) == ""


class TestConvertAssistantMessage:
    def test_string_content_becomes_content_field(self):
        msg = openai_bridge._convert_assistant_message("hello")
        assert msg == {"role": "assistant", "content": "hello"}

    def test_empty_content_for_tool_only_assistant(self):
        msg = openai_bridge._convert_assistant_message(
            [{"type": "tool_use", "id": "abc", "name": "Read", "input": {"k": 1}}]
        )
        assert msg["content"] == ""
        assert msg["tool_calls"][0]["function"]["name"] == "Read"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"k": 1}
        assert msg["tool_calls"][0]["id"] == "abc"

    def test_missing_tool_use_id_gets_generated(self):
        msg = openai_bridge._convert_assistant_message(
            [{"type": "tool_use", "name": "Read"}]
        )
        assert msg["tool_calls"][0]["id"].startswith("call_")

    def test_thinking_block_is_dropped(self):
        msg = openai_bridge._convert_assistant_message(
            [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": "visible"},
            ]
        )
        assert msg["content"] == "visible"
        assert "tool_calls" not in msg

    def test_non_dict_block_is_ignored(self):
        msg = openai_bridge._convert_assistant_message(
            ["raw", {"type": "text", "text": "ok"}]
        )
        assert msg["content"] == "ok"

    def test_text_block_with_non_string_text_is_skipped(self):
        msg = openai_bridge._convert_assistant_message(
            [{"type": "text", "text": 1}, {"type": "text", "text": "kept"}]
        )
        assert msg["content"] == "kept"

    def test_non_list_non_string_content_becomes_empty(self):
        msg = openai_bridge._convert_assistant_message({"unexpected": "shape"})
        assert msg == {"role": "assistant", "content": ""}


class TestConvertUserMessage:
    def test_string_content_becomes_user_message(self):
        out = openai_bridge._convert_user_message("hi")
        assert out == [{"role": "user", "content": "hi"}]

    def test_non_list_non_string_returns_empty(self):
        assert openai_bridge._convert_user_message(42) == []

    def test_tool_result_becomes_separate_tool_message(self):
        out = openai_bridge._convert_user_message(
            [
                {"type": "text", "text": "follow up"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "result"},
            ]
        )
        # User text comes first, tool message second.
        assert out[0] == {"role": "user", "content": "follow up"}
        assert out[1] == {
            "role": "tool",
            "tool_call_id": "tu_1",
            "content": "result",
        }

    def test_tool_result_with_list_content_is_flattened(self):
        out = openai_bridge._convert_user_message(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "def"}],
                }
            ]
        )
        assert out == [{"role": "tool", "tool_call_id": "tu_1", "content": "abcdef"}]

    def test_tool_result_with_dict_content_is_json_serialized(self):
        out = openai_bridge._convert_user_message(
            [{"type": "tool_result", "tool_use_id": "tu_1", "content": {"k": 1}}]
        )
        assert out[0]["content"] == json.dumps({"k": 1}, ensure_ascii=False)

    def test_tool_result_missing_id_defaults_to_empty_string(self):
        out = openai_bridge._convert_user_message(
            [{"type": "tool_result", "content": "x"}]
        )
        assert out[0]["tool_call_id"] == ""

    def test_text_block_with_non_string_text_is_skipped(self):
        out = openai_bridge._convert_user_message(
            [{"type": "text", "text": 1}, {"type": "text", "text": "keep"}]
        )
        assert out == [{"role": "user", "content": "keep"}]

    def test_non_dict_block_is_ignored(self):
        out = openai_bridge._convert_user_message(["ignore", {"type": "text", "text": "keep"}])
        assert out == [{"role": "user", "content": "keep"}]


class TestConvertToolChoice:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            ("auto", None),  # non-dict input -> None
            ({"type": "auto"}, "auto"),
            ({"type": "any"}, "required"),
            ({"type": "none"}, "none"),
            ({"type": "unknown"}, None),
            ({"type": "tool"}, None),  # missing name
            ({"type": "tool", "name": ""}, None),
        ],
    )
    def test_mappings(self, value, expected):
        assert openai_bridge._convert_tool_choice(value) == expected

    def test_named_tool(self):
        out = openai_bridge._convert_tool_choice({"type": "tool", "name": "my_tool"})
        assert out == {"type": "function", "function": {"name": "my_tool"}}
