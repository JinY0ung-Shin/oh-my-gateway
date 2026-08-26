"""Regression coverage for buffered subagent tool attribution."""

import json
import logging

from src.chunk_processing import extract_embedded_tool_blocks
from src.streaming_utils import stream_response_chunks


def _parse_response_sse(line: str) -> tuple[str, dict]:
    event_line, data_line = line.strip().splitlines()
    assert event_line.startswith("event: ")
    assert data_line.startswith("data: ")
    return event_line[len("event: ") :], json.loads(data_line[len("data: ") :])


def test_embedded_tool_inherits_assistant_parent():
    source_block = {
        "type": "tool_use",
        "id": "toolu_search",
        "name": "WebSearch",
        "input": {"query": "subagent query"},
    }
    chunk = {
        "type": "assistant",
        "parent_tool_use_id": "toolu_agent",
        "content": [source_block],
    }

    blocks = extract_embedded_tool_blocks(chunk)

    assert blocks == [{**source_block, "parent_tool_use_id": "toolu_agent"}]
    assert "parent_tool_use_id" not in source_block


def test_embedded_tool_keeps_explicit_block_parent():
    chunk = {
        "type": "assistant",
        "parent_tool_use_id": "toolu_message_parent",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_search",
                "name": "WebSearch",
                "input": {"query": "x"},
                "parent_tool_use_id": "toolu_block_parent",
            }
        ],
    }

    blocks = extract_embedded_tool_blocks(chunk)

    assert blocks[0]["parent_tool_use_id"] == "toolu_block_parent"


def test_embedded_tool_supports_legacy_message_wrapper_parent():
    chunk = {
        "type": "assistant",
        "message": {
            "parent_tool_use_id": "toolu_agent",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_read",
                    "name": "Read",
                    "input": {"file_path": "/tmp/a.txt"},
                }
            ],
        },
    }

    blocks = extract_embedded_tool_blocks(chunk)

    assert blocks[0]["parent_tool_use_id"] == "toolu_agent"


def test_top_level_embedded_tool_does_not_gain_parent():
    chunk = {
        "type": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_read",
                "name": "Read",
                "input": {"file_path": "/tmp/a.txt"},
            }
        ],
    }

    blocks = extract_embedded_tool_blocks(chunk)

    assert "parent_tool_use_id" not in blocks[0]


async def test_buffered_subagent_tool_event_keeps_parent_in_responses_stream():
    async def source():
        yield {
            "type": "assistant",
            "parent_tool_use_id": "toolu_agent",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_search",
                    "name": "WebSearch",
                    "input": {"query": "subagent query"},
                }
            ],
        }
        yield {
            "type": "assistant",
            "content": [{"type": "text", "text": "done"}],
        }
        yield {"type": "result", "subtype": "success", "result": "done"}

    stream_result = {}
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-attribution",
            output_item_id="msg-attribution",
            chunks_buffer=[],
            logger=logging.getLogger("test-subagent-tool-attribution"),
            stream_result=stream_result,
        )
    ]
    parsed = [
        _parse_response_sse(line)
        for line in lines
        if not line.startswith(":")
    ]
    tool_events = [payload for event, payload in parsed if event == "response.tool_use"]

    assert len(tool_events) == 1
    assert tool_events[0]["tool_use_id"] == "toolu_search"
    assert tool_events[0]["name"] == "WebSearch"
    assert tool_events[0]["parent_tool_use_id"] == "toolu_agent"
    assert stream_result["success"] is True
