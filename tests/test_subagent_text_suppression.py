"""Subagent text must not leak into the main visible answer stream.

``SUBAGENT_STREAM_TEXT`` defaults to false: a background/subagent's inner
narration (its assistant text) is internal. The citations path always gated on
``parent_tool_use_id``; these tests pin the same gate on the two text paths —
token-streaming ``text_delta`` events and whole assistant content chunks —
which previously leaked (visible as foreign narration spliced into answers
while background agents ran).
"""

import json
import logging

from src.streaming_utils import stream_response_chunks


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


def _stream_text_delta(text, parent_tool_use_id=None):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
        "session_id": "sess-1",
        "uuid": "uuid-1",
        "parent_tool_use_id": parent_tool_use_id,
    }


def _assistant_chunk(text, parent_tool_use_id=None):
    return {
        "type": "assistant",
        "content": [{"type": "text", "text": text}],
        "parent_tool_use_id": parent_tool_use_id,
    }


async def _collect(chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-subagent",
            output_item_id="msg-subagent",
            chunks_buffer=[],
            logger=logging.getLogger("test-subagent-text"),
            stream_result=stream_result,
        )
    ]
    parsed = []
    for line in lines:
        if line.startswith(":"):
            continue  # keepalive comment
        parsed.append(_parse_response_sse(line))
    return parsed, stream_result


def _visible_text(parsed) -> str:
    return "".join(
        payload.get("delta", "")
        for event_type, payload in parsed
        if event_type == "response.output_text.delta"
    )


async def test_subagent_stream_text_delta_suppressed():
    parsed, _ = await _collect(
        [
            _stream_text_delta("메인 답변 "),
            _stream_text_delta(
                "This is a large file. Let me delegate...",
                parent_tool_use_id="toolu_parent",
            ),
            _stream_text_delta("이어서 계속"),
            {"subtype": "success", "result": "메인 답변 이어서 계속", "type": "result"},
        ]
    )
    assert _visible_text(parsed) == "메인 답변 이어서 계속"


async def test_subagent_assistant_chunk_suppressed():
    parsed, _ = await _collect(
        [
            _assistant_chunk("배경 에이전트 내레이션", parent_tool_use_id="toolu_parent"),
            _assistant_chunk("메인 답변"),
            {"subtype": "success", "result": "메인 답변", "type": "result"},
        ]
    )
    assert _visible_text(parsed) == "메인 답변"


async def test_main_agent_text_still_streams():
    parsed, _ = await _collect(
        [
            _stream_text_delta("그대로 "),
            _stream_text_delta("전달"),
            {"subtype": "success", "result": "그대로 전달", "type": "result"},
        ]
    )
    assert _visible_text(parsed) == "그대로 전달"
