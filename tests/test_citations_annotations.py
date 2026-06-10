"""Unit tests for citations_delta -> response.output_text.annotation.added.

The Claude Agent SDK forwards raw Anthropic API stream events through
``StreamEvent.event``; document citations arrive as ``content_block_delta``
events whose delta type is ``citations_delta`` with a ``citation`` payload.
The gateway maps each one to an OpenAI Responses-style
``response.output_text.annotation.added`` event, passing the raw citation
dict through unchanged as the ``annotation`` field.
"""

import json
import logging

import pytest

from src.streaming_utils import stream_response_chunks


CITATION_A = {
    "type": "char_location",
    "cited_text": "the answer is 42",
    "document_index": 0,
    "document_title": "guide.pdf",
    "start_char_index": 100,
    "end_char_index": 116,
}

CITATION_B = {
    "type": "page_location",
    "cited_text": "see appendix",
    "document_index": 1,
    "document_title": "appendix.pdf",
    "start_page_number": 3,
    "end_page_number": 4,
}


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


def _stream_event(event, parent_tool_use_id=None):
    return {
        "type": "stream_event",
        "event": event,
        "session_id": "sess-1",
        "uuid": "uuid-1",
        "parent_tool_use_id": parent_tool_use_id,
    }


def _text_delta(text):
    return _stream_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def _citations_delta(citation, parent_tool_use_id=None):
    return _stream_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "citations_delta", "citation": citation},
        },
        parent_tool_use_id=parent_tool_use_id,
    )


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
            response_id="resp-citations",
            output_item_id="msg-citations",
            chunks_buffer=[],
            logger=logging.getLogger("test-citations"),
            stream_result=stream_result,
        )
    ]
    return [_parse_response_sse(line) for line in lines], stream_result


async def test_citations_delta_maps_to_annotation_added():
    parsed, stream_result = await _collect(
        [
            _text_delta("The answer is 42."),
            _citations_delta(CITATION_A),
            _citations_delta(CITATION_B),
        ]
    )

    annotations = [
        payload
        for event_type, payload in parsed
        if event_type == "response.output_text.annotation.added"
    ]
    assert len(annotations) == 2
    # Raw citation dicts pass through unchanged.
    assert annotations[0]["annotation"] == CITATION_A
    assert annotations[1]["annotation"] == CITATION_B
    # Annotation events address the open message item's text part.
    assert annotations[0]["item_id"] == "msg-citations"
    assert annotations[0]["output_index"] == 0
    assert annotations[0]["content_index"] == 0
    assert annotations[0]["annotation_index"] == 0
    assert annotations[1]["annotation_index"] == 1

    event_types = [event_type for event_type, _payload in parsed]
    # Annotations arrive after the text delta and before the close events.
    assert event_types.index("response.output_text.annotation.added") > event_types.index(
        "response.output_text.delta"
    )
    assert parsed[-1][0] == "response.completed"
    assert stream_result["success"] is True
    assert stream_result["assistant_text"] == "The answer is 42."


async def test_citation_before_text_opens_message_item():
    parsed, _stream_result = await _collect(
        [
            _citations_delta(CITATION_A),
            _text_delta("Cited up front."),
        ]
    )

    event_types = [event_type for event_type, _payload in parsed]
    annotation_pos = event_types.index("response.output_text.annotation.added")
    assert event_types.index("response.output_item.added") < annotation_pos
    assert event_types.index("response.content_part.added") < annotation_pos
    assert parsed[-1][0] == "response.completed"


async def test_subagent_citations_dropped_by_default():
    """parent_tool_use_id chunks are suppressed unless SUBAGENT_STREAM_TEXT."""
    parsed, _stream_result = await _collect(
        [
            _text_delta("Main text."),
            _citations_delta(CITATION_A, parent_tool_use_id="toolu_parent"),
        ]
    )

    event_types = [event_type for event_type, _payload in parsed]
    assert "response.output_text.annotation.added" not in event_types
    assert parsed[-1][0] == "response.completed"


async def test_annotation_index_resets_per_message_item():
    """think-after-text closes the message item; the next item restarts at 0."""
    thinking_start = _stream_event(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "thinking", "thinking": ""},
        }
    )
    thinking_delta = _stream_event(
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }
    )
    thinking_stop = _stream_event({"type": "content_block_stop", "index": 1})

    parsed, _stream_result = await _collect(
        [
            _text_delta("First segment."),
            _citations_delta(CITATION_A),
            thinking_start,
            thinking_delta,
            thinking_stop,
            _text_delta("Second segment."),
            _citations_delta(CITATION_B),
        ]
    )

    annotations = [
        payload
        for event_type, payload in parsed
        if event_type == "response.output_text.annotation.added"
    ]
    assert [a["annotation_index"] for a in annotations] == [0, 0]
    # The two annotations belong to different message items.
    assert annotations[0]["item_id"] == "msg-citations"
    assert annotations[1]["item_id"] != annotations[0]["item_id"]
    assert annotations[1]["output_index"] > annotations[0]["output_index"]
    assert parsed[-1][0] == "response.completed"
