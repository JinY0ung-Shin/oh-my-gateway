"""Pure event-stream transformation for Anthropic Messages SSE.

The transformation is independent of HTTP/SSE encoding so it can be unit-tested
against plain Python dicts. The route layer is responsible for SSE parsing and
serialization.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional


# Mapping from a delta event's ``delta.type`` to the ``content_block.type`` it
# must live inside, per the Anthropic Messages stream spec.
DELTA_TO_BLOCK_TYPE: Dict[str, str] = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "input_json_delta": "tool_use",
    "signature_delta": "thinking",
}


def _synthetic_block(block_type: str) -> Dict[str, Any]:
    """Build a minimal ``content_block`` payload for a synthesized start event."""
    if block_type == "text":
        return {"type": "text", "text": ""}
    if block_type == "thinking":
        return {"type": "thinking", "thinking": ""}
    if block_type == "tool_use":
        # Real tool_use blocks carry id/name/input — those should arrive from
        # upstream's own content_block_start. When we synthesize one, downstream
        # may still reject; we emit best-effort defaults rather than crashing.
        return {"type": "tool_use", "id": "", "name": "", "input": {}}
    return {"type": block_type}


async def sanitize_events(
    upstream: AsyncIterator[Dict[str, Any]],
) -> AsyncIterator[Dict[str, Any]]:
    """Yield a spec-conforming sequence of Anthropic Messages events.

    Guarantees on the output stream:
    - ``message_start`` is emitted at most once.
    - Every ``content_block_delta.delta.type`` matches the type of the most
      recent (still-open) ``content_block_start``.
    - Indices on emitted events are monotonically increasing, starting at 0.
    - Every ``content_block_start`` has a matching ``content_block_stop``
      before the next start, ``message_delta``, or ``message_stop``.
    """
    seen_message_start = False
    current_index = -1
    current_block_type: Optional[str] = None

    def _close_current() -> Dict[str, Any]:
        return {"type": "content_block_stop", "index": current_index}

    async for evt in upstream:
        etype = evt.get("type")

        if etype == "message_start":
            if seen_message_start:
                continue
            seen_message_start = True
            yield evt
            continue

        if etype == "content_block_start":
            if current_block_type is not None:
                yield _close_current()
                current_block_type = None
            current_index += 1
            current_block_type = evt.get("content_block", {}).get("type")
            new_evt = dict(evt)
            new_evt["index"] = current_index
            yield new_evt
            continue

        if etype == "content_block_delta":
            delta_type = evt.get("delta", {}).get("type")
            expected = DELTA_TO_BLOCK_TYPE.get(delta_type)
            if expected is not None and expected != current_block_type:
                if current_block_type is not None:
                    yield _close_current()
                current_index += 1
                current_block_type = expected
                yield {
                    "type": "content_block_start",
                    "index": current_index,
                    "content_block": _synthetic_block(expected),
                }
            new_evt = dict(evt)
            new_evt["index"] = current_index
            yield new_evt
            continue

        if etype == "content_block_stop":
            if current_block_type is None:
                # Sanitizer already closed this block (or none was ever open).
                continue
            yield _close_current()
            current_block_type = None
            continue

        if etype == "message_delta":
            if current_block_type is not None:
                yield _close_current()
                current_block_type = None
            yield evt
            continue

        if etype == "message_stop":
            if current_block_type is not None:
                yield _close_current()
                current_block_type = None
            yield evt
            continue

        # ping / error / unknown — pass through.
        yield evt
