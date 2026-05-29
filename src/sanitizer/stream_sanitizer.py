"""Pure event-stream transformation for Anthropic Messages SSE.

The transformation is independent of HTTP/SSE encoding so it can be unit-tested
against plain Python dicts. The route layer is responsible for SSE parsing and
serialization.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, FrozenSet, Optional


# Block types a given ``delta.type`` is allowed to appear inside. The Anthropic
# Messages stream emits ``input_json_delta`` for both regular ``tool_use`` and
# server-side variants (e.g. ``server_tool_use`` for web search), so the
# compatibility is a set rather than a single type.
DELTA_COMPATIBLE_BLOCKS: Dict[str, FrozenSet[str]] = {
    "text_delta": frozenset({"text"}),
    "thinking_delta": frozenset({"thinking"}),
    "signature_delta": frozenset({"thinking"}),
    "input_json_delta": frozenset({"tool_use", "server_tool_use"}),
}

# When the sanitizer must synthesize a new ``content_block_start`` for a delta
# that has no compatible open block, this is the block type it picks. Falls
# back to a regular ``tool_use`` for tool deltas since synthesizing a
# ``server_tool_use`` without an id/name is even less useful.
DELTA_PRIMARY_BLOCK: Dict[str, str] = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "signature_delta": "thinking",
    "input_json_delta": "tool_use",
}


def _is_empty_delta(delta: Dict[str, Any]) -> bool:
    """Return True when a ``content_block_delta`` carries no payload.

    Some upstreams (notably LiteLLM in the #21128 family) interleave zero-byte
    deltas — empty ``text_delta`` events scattered through a thinking stream,
    or a run of ``input_json_delta`` events whose ``partial_json`` is ``""``
    with no preceding ``content_block_start(type=tool_use, ...)`` so the tool
    name/id are unrecoverable.

    Such events carry no content and can only do harm: they trigger spurious
    block splits (text↔thinking thrashing) or cause the sanitizer to
    synthesize a placeholder ``tool_use`` block with empty ``name``/``id``,
    which downstream SDKs reject as ``No such tool available``. Dropping them
    is always safe — adjacent non-empty deltas of the same type concatenate as
    if the empty one were never there.
    """
    dt = delta.get("type")
    if dt == "text_delta":
        return not delta.get("text")
    if dt == "thinking_delta":
        return not delta.get("thinking")
    if dt == "signature_delta":
        return not delta.get("signature")
    if dt == "input_json_delta":
        return not delta.get("partial_json")
    return False


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
            delta = evt.get("delta", {}) or {}
            if _is_empty_delta(delta):
                # Drop zero-payload deltas before any split logic runs. See
                # ``_is_empty_delta`` for why this is unconditionally safe.
                continue
            delta_type_raw = delta.get("type")
            delta_type = delta_type_raw if isinstance(delta_type_raw, str) else ""
            compatible = DELTA_COMPATIBLE_BLOCKS.get(delta_type)
            if compatible is not None and current_block_type not in compatible:
                # Current block can't validly hold this delta — close it and
                # open a synthetic block of the delta's primary type. This is
                # the LiteLLM #21128 path (text block opened, thinking_delta
                # arrives) but it must NOT fire when an already-compatible
                # block (e.g. server_tool_use receiving input_json_delta) is
                # open, since that would strip the upstream id/name/type.
                if current_block_type is not None:
                    yield _close_current()
                current_index += 1
                primary = DELTA_PRIMARY_BLOCK.get(delta_type, "text")
                current_block_type = primary
                yield {
                    "type": "content_block_start",
                    "index": current_index,
                    "content_block": _synthetic_block(primary),
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
