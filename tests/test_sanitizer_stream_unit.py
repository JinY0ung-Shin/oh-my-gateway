"""Unit tests for src.sanitizer.stream_sanitizer.

The sanitizer rewrites a malformed Anthropic Messages SSE event stream into a
spec-conforming one. The canonical bug it fixes is LiteLLM's
``AnthropicStreamWrapper`` (#21128), which emits the first content block with
``type: "text"`` regardless of whether the model is actually thinking — causing
``thinking_delta`` events to land inside a ``type=text`` block.
"""

from __future__ import annotations

from typing import AsyncIterator, Iterable, List

import pytest

from src.sanitizer.stream_sanitizer import DELTA_TO_BLOCK_TYPE, sanitize_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _as_async(events: Iterable[dict]) -> AsyncIterator[dict]:
    for e in events:
        yield e


async def _collect(aiter: AsyncIterator[dict]) -> List[dict]:
    return [e async for e in aiter]


def assert_spec_conforming(events: List[dict]) -> None:
    """Validate that *events* matches Anthropic Messages stream spec."""
    seen_message_start = False
    open_block: tuple[int, str] | None = None
    last_index = -1
    for evt in events:
        t = evt["type"]
        if t == "message_start":
            assert not seen_message_start, "duplicate message_start"
            seen_message_start = True
        elif t == "content_block_start":
            assert open_block is None, f"prior block {open_block} still open at {evt}"
            idx = evt["index"]
            assert idx == last_index + 1, f"index {idx} not monotonic after {last_index}"
            last_index = idx
            open_block = (idx, evt["content_block"]["type"])
        elif t == "content_block_delta":
            assert open_block is not None, f"delta with no open block: {evt}"
            idx, block_type = open_block
            assert evt["index"] == idx, f"delta index {evt['index']} != open {idx}"
            delta_type = evt["delta"]["type"]
            expected = DELTA_TO_BLOCK_TYPE.get(delta_type, block_type)
            assert expected == block_type, (
                f"delta type {delta_type} in {block_type} block (idx={idx})"
            )
        elif t == "content_block_stop":
            assert open_block is not None, f"stop with no open block: {evt}"
            assert evt["index"] == open_block[0]
            open_block = None
        elif t in ("message_delta", "message_stop", "ping", "error"):
            pass
        else:
            pytest.fail(f"unknown event type: {t}")
    assert open_block is None, f"stream ended with block {open_block} still open"


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


class TestSanitizerSpecCompliance:
    async def test_pattern4_clean_stream_passthrough(self):
        """Anthropic-direct (correct) stream should pass through unchanged structurally."""
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "role": "assistant"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "User asks..."}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello!"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        # No structural splits needed → same length and type sequence.
        assert [e["type"] for e in out] == [e["type"] for e in events]

    async def test_pattern2_text_block_with_thinking_delta_splits(self):
        """LiteLLM bug: content_block_start says type=text but first delta is thinking_delta."""
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "User says..."}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "..."}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello!"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        types = [e["type"] for e in out]
        # Expected structure: text-start (empty, immediately closed) → thinking → text → stop → end
        assert types == [
            "message_start",
            "content_block_start",   # upstream text start (idx 0)
            "content_block_stop",    # synthetic close because next delta is thinking
            "content_block_start",   # synthetic thinking start (idx 1)
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",    # synthetic close because next delta is text
            "content_block_start",   # synthetic text start (idx 2)
            "content_block_delta",
            "content_block_stop",    # remapped upstream stop
            "message_delta",
            "message_stop",
        ]
        # Verify the synthesized blocks carry correct types.
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["text", "thinking", "text"]
        assert [s["index"] for s in starts] == [0, 1, 2]

    async def test_pattern3_duplicate_message_start_deduped(self):
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {"type": "message_start", "message": {"id": "msg_1"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        assert sum(1 for e in out if e["type"] == "message_start") == 1

    async def test_delta_without_content_block_start_synthesizes_one(self):
        """Some upstreams skip content_block_start entirely."""
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert len(starts) == 1
        assert starts[0]["content_block"]["type"] == "text"

    async def test_dangling_block_at_message_stop_is_auto_closed(self):
        """Upstream drops final content_block_stop — sanitizer must inject one."""
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            # NO content_block_stop here
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        types = [e["type"] for e in out]
        assert types[-2:] == ["content_block_stop", "message_stop"]

    async def test_message_delta_closes_dangling_block(self):
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        # message_delta must be preceded by a content_block_stop.
        idx = next(i for i, e in enumerate(out) if e["type"] == "message_delta")
        assert out[idx - 1]["type"] == "content_block_stop"

    async def test_tool_use_delta_transition(self):
        """input_json_delta after a text block must trigger split to tool_use."""
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "let me check"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"q\":"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["text", "tool_use"]

    async def test_ping_and_unknown_events_passthrough(self):
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {"type": "ping"},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert any(e["type"] == "ping" for e in out)
        assert_spec_conforming(out)

    async def test_explicit_block_start_after_synthetic_split_remains_consistent(self):
        """Upstream emits content_block_start mid-stream after we've already split."""
        events = [
            {"type": "message_start", "message": {"id": "msg_1"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            # First delta is thinking → forces split.
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "..."}},
            # Upstream then opens a real text block at index=1; sanitizer remaps.
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "answer"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_stop"},
        ]
        out = await _collect(sanitize_events(_as_async(events)))
        assert_spec_conforming(out)
        starts = [e for e in out if e["type"] == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["text", "thinking", "text"]
        # Indices must be 0, 1, 2 (monotonic), not the upstream values.
        assert [s["index"] for s in starts] == [0, 1, 2]
