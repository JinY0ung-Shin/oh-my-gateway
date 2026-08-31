"""A built-in slash command's answer has to reach the client.

``/context``, ``/usage`` and ``/cost`` are answered by the CLI itself: the turn
produces one ``system``/``local_command_output`` message and no assistant text
at all. The gateway dropped that subtype, so the command ran and the client
rendered an empty turn — which is why ``/context``, the only per-category
breakdown of what actually occupies the window, was unreachable through the
gateway.
"""

from __future__ import annotations

import json
import logging

import src.sse_builders as sse_builders
from src.sse_builders import _build_progress_event
from src.streaming_utils import stream_response_chunks

_CONTEXT_TEXT = (
    "Context Usage\n  System prompt   3.2k tokens (3%)\n  Messages   2.7k tokens (2%)"
)


def _chunk(content=_CONTEXT_TEXT, **extra):
    """A ``system``/``local_command_output`` chunk shaped like the real one.

    Wire schema (claude-code 2.1.251): ``{type, subtype, content, uuid,
    session_id}`` — ``content`` is top level. The SDK has no dedicated class for
    this subtype, so it parses as a generic ``SystemMessage`` and the gateway's
    converter leaves the whole payload under ``data``.
    """
    payload = {
        "type": "system",
        "subtype": "local_command_output",
        "content": content,
        "uuid": "uuid-1",
        "session_id": "sess-1",
        **extra,
    }
    return {"type": "system", "subtype": "local_command_output", "data": payload}


def test_the_commands_output_becomes_its_own_event():
    assert _build_progress_event(_chunk()) == {
        "type": "local_command_output",
        "content": _CONTEXT_TEXT,
        "session_id": "sess-1",
    }


def test_content_is_passed_through_verbatim():
    # The CLI's rendering is column-aligned text whose alignment IS the meaning;
    # trimming or re-wrapping it here would destroy the table before any client
    # sees it.
    raw = "  a\t b \n\n   c   \n"
    assert _build_progress_event(_chunk(raw))["content"] == raw


def test_an_empty_body_is_not_an_event():
    # Nothing to render: an event with no content would open a blank bubble.
    assert _build_progress_event(_chunk("")) is None
    assert _build_progress_event(_chunk(None)) is None
    assert (
        _build_progress_event({"type": "system", "subtype": "local_command_output"})
        is None
    )


def test_the_flag_can_turn_it_off(monkeypatch):
    monkeypatch.setattr(sse_builders, "STREAM_LOCAL_COMMAND_OUTPUT", False)
    assert _build_progress_event(_chunk()) is None


def test_a_subagents_command_output_names_its_parent():
    # Same routing contract as every other system event: the parent lives inside
    # ``data`` because the generic SystemMessage models no parent of its own.
    event = _build_progress_event(_chunk(parent_tool_use_id="toolu_parent"))
    assert event["parent_tool_use_id"] == "toolu_parent"


async def _events(chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-local-command",
            output_item_id="msg-local-command",
            chunks_buffer=[],
            logger=logging.getLogger("test-local-command"),
        )
    ]
    out = []
    for line in lines:
        for raw in line.splitlines():
            if raw.startswith("data: "):
                try:
                    out.append(json.loads(raw[len("data: ") :]))
                except json.JSONDecodeError:
                    pass
    return out


_RESULT = {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 0}}


async def test_a_slash_command_turn_is_no_longer_silent():
    events = await _events([_chunk(), _RESULT])
    emitted = [e for e in events if e.get("type") == "response.local_command_output"]
    assert len(emitted) == 1
    assert emitted[0]["content"] == _CONTEXT_TEXT
    assert emitted[0]["session_id"] == "sess-1"
    assert "sequence_number" in emitted[0]


async def test_it_does_not_disturb_the_compaction_state_machine():
    # The tracker sees every progress event, not just compaction ones. A command
    # output passing through must neither be swallowed nor close a compaction
    # that is still running on the same stream.
    compacting = {
        "type": "system",
        "subtype": "status",
        "data": {"type": "system", "subtype": "status", "status": "compacting"},
    }
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "data": {
            "compact_metadata": {
                "trigger": "auto",
                "pre_tokens": 67200,
                "post_tokens": 2700,
            }
        },
    }
    events = await _events([compacting, _chunk(), boundary, _RESULT])
    kinds = [
        e["type"]
        for e in events
        if e["type"].startswith("response.local") or e["type"] == "response.compaction"
    ]
    assert kinds == [
        "response.compaction",  # start
        "response.local_command_output",
        "response.compaction",  # end
    ]
