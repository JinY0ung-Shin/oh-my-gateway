import json
import logging

import pytest

from src.streaming_utils import (
    extract_context_tokens,
    resolve_usage_details,
    stream_response_chunks,
)


def _assistant(
    *,
    input_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    output_tokens: int = 7,
    parent_tool_use_id: str | None = None,
) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent_tool_use_id,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
        },
    }


def _streamed_assistant(*, output_tokens: int = 7) -> dict:
    """Assembled assistant message as it arrives under token streaming.

    ``include_partial_messages`` splits the usage: prompt counters usually ride
    on ``message_start`` and only ``message_delta`` output counts survive on
    the assembled message, with ``input_tokens`` null. LiteLLM is a notable
    proxy shape where ``message_start`` is zero and the real prompt counters
    arrive on the raw ``message_delta`` stream event instead.
    """
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "usage": {
            "input_tokens": None,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": output_tokens,
        },
    }


def _message_start(
    *,
    input_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    parent_tool_use_id: str | None = None,
) -> dict:
    return {
        "type": "stream_event",
        "parent_tool_use_id": parent_tool_use_id,
        "event": {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": 1,
                }
            },
        },
    }


def _message_delta(
    *,
    input_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
    output_tokens: int = 7,
    parent_tool_use_id: str | None = None,
) -> dict:
    """Anthropic message_delta carrying usage in the LiteLLM proxy shape."""
    return {
        "type": "stream_event",
        "parent_tool_use_id": parent_tool_use_id,
        "event": {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
            },
        },
    }


def test_context_snapshot_uses_latest_main_agent_request_not_turn_total() -> None:
    chunks = [
        _assistant(input_tokens=100, cache_creation=20, cache_read=30),
        _assistant(
            input_tokens=50_000,
            cache_creation=10_000,
            cache_read=40_000,
            parent_tool_use_id="toolu_subagent",
        ),
        _assistant(input_tokens=150, cache_creation=40, cache_read=50),
        {
            "type": "result",
            "usage": {
                "input_tokens": 200_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 70_000,
                "output_tokens": 999,
            },
        },
    ]

    # Prompt size of the final top-level AssistantMessage: 150+40+50. The much
    # larger cumulative ResultMessage and the subagent request are irrelevant,
    # and the request's own output is not part of the prompt it carried.
    assert extract_context_tokens(chunks) == 240
    details = resolve_usage_details(chunks)
    assert details.context_tokens == 240

    # Billing/cache fields keep their existing turn-cumulative ResultMessage semantics.
    assert details.cached_tokens == 70_000
    assert details.cache_creation_tokens == 30_000


def test_context_snapshot_does_not_fall_back_to_cumulative_result_usage() -> None:
    chunks = [
        {
            "type": "result",
            "usage": {
                "input_tokens": 200_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 70_000,
                "output_tokens": 999,
            },
        }
    ]

    assert extract_context_tokens(chunks) is None
    assert resolve_usage_details(chunks).context_tokens is None


def test_context_snapshot_reads_message_start_under_token_streaming() -> None:
    # The assembled assistant message carries output-only usage here, so the
    # snapshot has to come from the request's message_start event.
    chunks = [
        _message_start(input_tokens=2_000, cache_read=90_000),
        _streamed_assistant(),
        _message_start(input_tokens=1_500, cache_creation=500, cache_read=120_000),
        _streamed_assistant(output_tokens=640),
        {
            "type": "result",
            "usage": {
                "input_tokens": 400_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 210_000,
                "output_tokens": 1_200,
            },
        },
    ]

    assert extract_context_tokens(chunks) == 122_000
    assert resolve_usage_details(chunks).context_tokens == 122_000


def test_context_snapshot_reads_litellm_message_delta_when_start_is_zero() -> None:
    """LiteLLM initializes message_start usage to zero, then reports real usage at the end."""
    chunks = [
        _message_start(input_tokens=0),
        _message_delta(input_tokens=2_000, cache_read=90_000, output_tokens=143),
        _streamed_assistant(output_tokens=143),
    ]

    assert extract_context_tokens(chunks) == 92_000
    assert resolve_usage_details(chunks).context_tokens == 92_000


def test_context_snapshot_uses_latest_litellm_message_delta_round() -> None:
    chunks = [
        _message_start(input_tokens=0),
        _message_delta(input_tokens=1_200, cache_read=20_000, output_tokens=32),
        _streamed_assistant(output_tokens=32),
        _message_start(input_tokens=0),
        _message_delta(input_tokens=1_500, cache_creation=500, cache_read=90_000, output_tokens=640),
        _streamed_assistant(output_tokens=640),
    ]

    # Same rule as message_start snapshots: newest top-level request wins.
    assert extract_context_tokens(chunks) == 92_000


def test_context_snapshot_ignores_subagent_message_start() -> None:
    chunks = [
        _message_start(input_tokens=1_000, cache_read=20_000),
        _streamed_assistant(),
        _message_start(
            input_tokens=90_000,
            cache_read=300_000,
            parent_tool_use_id="toolu_subagent",
        ),
    ]

    # A subagent owns a separate context window; the main-agent request stands.
    assert extract_context_tokens(chunks) == 21_000


def test_context_snapshot_ignores_subagent_litellm_message_delta() -> None:
    chunks = [
        _message_start(input_tokens=0),
        _message_delta(input_tokens=1_000, cache_read=57_000),
        _message_delta(
            input_tokens=90_000,
            cache_read=300_000,
            parent_tool_use_id="toolu_subagent",
        ),
    ]

    assert extract_context_tokens(chunks) == 58_000


def test_context_snapshot_prefers_assistant_usage_over_message_start() -> None:
    chunks = [
        _message_start(input_tokens=1_000, cache_read=20_000),
        _assistant(input_tokens=1_100, cache_read=21_000),
    ]

    # Non-streaming shape: the assembled message has the real counters and is
    # the later of the two, so the backwards scan takes it.
    assert extract_context_tokens(chunks) == 22_100


def test_context_snapshot_tracks_post_compaction_request() -> None:
    chunks = [
        _assistant(input_tokens=8_000, cache_read=180_000),
        {"type": "system", "subtype": "compact_boundary", "trigger": "auto"},
        _assistant(input_tokens=3_000, cache_read=22_000),
    ]

    # The latest main-agent request is the post-compaction window, so the
    # snapshot falls instead of accumulating both requests.
    assert extract_context_tokens(chunks) == 25_000


# ---------------------------------------------------------------------------
# End-to-end: a token-streamed turn must keep its snapshot in the chunk buffer
# ---------------------------------------------------------------------------


def _text_delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _parse_completed_usage(lines: list) -> dict:
    payloads = []
    for line in lines:
        for raw in line.splitlines():
            if raw.startswith("data: "):
                try:
                    payloads.append(json.loads(raw[len("data: ") :]))
                except json.JSONDecodeError:
                    pass
    completed = [p for p in payloads if p.get("type") == "response.completed"]
    assert completed, "stream never completed"
    return completed[-1]["response"]["usage"]


@pytest.mark.asyncio
async def test_streamed_turn_reports_the_latest_round_not_the_first() -> None:
    """Regression: a token-streamed turn reported a pre-tool-round snapshot.

    ``stream_response_chunks`` drops ``stream_event`` chunks once text deltas
    start flowing, and the prompt counters only exist on ``message_start`` in
    the first-party shape. The first round's start event slipped into the
    buffer before the token-streaming flag flipped, so every later round's
    growth was invisible and the completed response reported the turn's
    SMALLEST prompt.
    """

    async def source():
        # Round 1: initial prompt, then a tool call.
        yield _message_start(input_tokens=1_200, cache_read=20_000)
        yield _text_delta("looking")
        yield _streamed_assistant(output_tokens=32)
        # Round 2: the transcript has grown by the tool result.
        yield _message_start(input_tokens=1_500, cache_creation=500, cache_read=120_000)
        yield _text_delta("hi")
        yield _streamed_assistant(output_tokens=640)
        yield {
            "type": "result",
            "usage": {
                "input_tokens": 400_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 210_000,
                "output_tokens": 1_200,
            },
        }

    chunks_buffer: list = []
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-ctx",
            output_item_id="msg-ctx",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-resp-ctx"),
        )
    ]

    usage = _parse_completed_usage(lines)
    # Round 2's prompt (1500+500+120000), not round 1's 21_200.
    assert usage["input_tokens_details"]["context_tokens"] == 122_000
    # Billing totals stay on the cumulative ResultMessage.
    assert usage["input_tokens"] == 640_000


@pytest.mark.asyncio
async def test_streamed_turn_keeps_litellm_message_delta_snapshot() -> None:
    """LiteLLM's useful final usage event must survive token-stream duplicate suppression."""

    async def source():
        yield _message_start(input_tokens=0)
        yield _text_delta("looking")
        yield _message_delta(input_tokens=1_200, cache_read=20_000, output_tokens=32)
        yield _streamed_assistant(output_tokens=32)

        yield _message_start(input_tokens=0)
        yield _text_delta("done")
        yield _message_delta(
            input_tokens=1_500,
            cache_creation=500,
            cache_read=90_000,
            output_tokens=640,
        )
        yield _streamed_assistant(output_tokens=640)
        yield {
            "type": "result",
            "usage": {
                "input_tokens": 400_000,
                "cache_creation_input_tokens": 30_000,
                "cache_read_input_tokens": 210_000,
                "output_tokens": 1_200,
            },
        }

    chunks_buffer: list = []
    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-litellm-ctx",
            output_item_id="msg-litellm-ctx",
            chunks_buffer=chunks_buffer,
            logger=logging.getLogger("test-resp-litellm-ctx"),
        )
    ]

    usage = _parse_completed_usage(lines)
    assert usage["input_tokens_details"]["context_tokens"] == 92_000
    assert usage["input_tokens"] == 640_000


def test_context_snapshot_spans_leader_session_handoff() -> None:
    """Two parent-less leader sessions in one turn: the newest one wins.

    The compaction lifecycle keys its stream state by ``(parent_tool_use_id,
    session_id)`` because it pairs starts with ends. The occupancy snapshot
    deliberately ignores ``session_id``: across a leader handoff the window the
    NEXT request will carry is described by the newest parent-less request, so
    "latest parent-less wins" already lands on the live session without naming
    it. This pins that contract — a future parent-less stream shape that is NOT
    the leader (today every non-leader stream carries ``parent_tool_use_id``)
    must show up here as a loud failure, not silently clobber the main number.
    """
    old_leader = _message_start(input_tokens=9_000)
    old_leader["session_id"] = "sess-old"
    new_leader = _message_start(input_tokens=2_500, cache_read=500)
    new_leader["session_id"] = "sess-new"

    assert extract_context_tokens([old_leader, new_leader]) == 3_000
    # Order, not session identity, decides — reversed input picks the other one.
    assert extract_context_tokens([new_leader, old_leader]) == 9_000
