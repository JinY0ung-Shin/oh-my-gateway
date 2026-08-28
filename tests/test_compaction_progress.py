"""Compaction progress: the pause has to be announced while it is happening.

``compact_boundary`` is the CLI's past-tense marker — it carries
``content: "Conversation compacted"`` and ``messagesSummarized``, so it only
arrives once the summarisation is over. A client that waits for it shows nothing
during the very pause it is meant to explain, which is exactly the wait users
report. The CLI does say when it starts, on the same stream, as a
``system``/``status`` message; the gateway simply dropped that subtype.
"""

from __future__ import annotations

import json
import logging

from src.sse_builders import _build_progress_event
from src.streaming_utils import stream_response_chunks


def _status_chunk(status, **extra):
    """A ``system``/``status`` chunk shaped like the real one.

    The SDK has no dedicated type for this subtype, so it parses as a generic
    ``SystemMessage`` and the gateway's converter leaves the payload under
    ``data`` — captured from claude-code 2.1.220.
    """
    payload = {"type": "system", "subtype": "status", "status": status, **extra}
    return {"type": "system", "subtype": "status", "data": payload}


def test_status_compacting_opens_the_pause():
    event = _build_progress_event(_status_chunk("compacting"))
    assert event == {
        "type": "compaction",
        "subtype": "status",
        "phase": "start",
        "trigger": None,
        "session_id": None,
    }


def test_status_close_carries_the_cli_verdict():
    event = _build_progress_event(_status_chunk(None, compact_result="success"))
    assert event is not None
    assert event["phase"] == "end"
    assert event["result"] == "success"

    failed = _build_progress_event(
        _status_chunk(None, compact_result="failed", compact_error="too short")
    )
    assert failed is not None and failed["result"] == "failed"


def test_status_without_a_compaction_verdict_is_not_a_compaction_event():
    # Other run phases share this subtype (``requesting`` opens every turn). Only
    # the compaction pair may claim the compaction event.
    assert _build_progress_event(_status_chunk("requesting")) is None
    assert _build_progress_event(_status_chunk(None)) is None


def test_boundary_still_reports_and_now_says_which_end_it_is():
    event = _build_progress_event(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "data": {"compact_metadata": {"trigger": "auto"}},
        }
    )
    assert event is not None
    assert event["subtype"] == "compact_boundary"
    assert event["trigger"] == "auto"
    # The boundary is past tense, so it can only ever close a compaction.
    assert event["phase"] == "end"


async def test_client_is_told_when_the_pause_starts_not_only_when_it_ended():
    async def source():
        yield _status_chunk("compacting")
        yield {
            "type": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
        yield _status_chunk(None, compact_result="success")
        yield {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 3}}

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-compaction",
            output_item_id="msg-compaction",
            chunks_buffer=[],
            logger=logging.getLogger("test-compaction"),
        )
    ]

    events = []
    for line in lines:
        for raw in line.splitlines():
            if raw.startswith("data: "):
                try:
                    events.append(json.loads(raw[len("data: ") :]))
                except json.JSONDecodeError:
                    pass

    types = [event.get("type") for event in events]
    kind = "response.compaction"
    compaction = [event for event in events if event.get("type") == kind]
    assert [event["phase"] for event in compaction] == ["start", "end"]
    assert compaction[-1]["result"] == "success"
    # The point of the change: the opening event precedes the turn's own output.
    assert types.index(kind) < types.index("response.output_text.delta")


# ---------------------------------------------------------------------------
# Lifecycle: the raw markers repeat and close twice, the wire must not.
# Sequences below are the real ones, captured from claude-code 2.1.220.
# ---------------------------------------------------------------------------


def _boundary_chunk(trigger="manual", **metadata):
    # Numbers as measured on claude-code 2.1.220.
    meta = {
        "trigger": trigger,
        "pre_tokens": 36157,
        "post_tokens": 1585,
        "duration_ms": 12426,
        **metadata,
    }
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "data": {"compact_metadata": meta},
    }


async def _events(chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    lines = [
        line
        async for line in stream_response_chunks(
            chunk_source=source(),
            model="claude-test",
            response_id="resp-lifecycle",
            output_item_id="msg-lifecycle",
            chunks_buffer=[],
            logger=logging.getLogger("test-compaction-lifecycle"),
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


def _compaction(events):
    return [e for e in events if e.get("type") == "response.compaction"]


_TEXT = {
    "type": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "usage": {"input_tokens": 10, "output_tokens": 3},
}
_RESULT = {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 3}}


async def test_successful_compaction_closes_once_though_the_cli_closes_twice():
    """Measured success: the status closes it, then the boundary closes it again.

    Emitting both would make a client that tracks one progress state close twice,
    with the verdict and the trigger split across two events.
    """
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="success"),
            {"type": "system", "subtype": "init"},
            _boundary_chunk(trigger="manual"),
            _TEXT,
            _RESULT,
        ]
    )
    compaction = _compaction(events)
    assert [e["phase"] for e in compaction] == ["start", "end"]
    # Both halves of the outcome survive on the single terminal.
    assert compaction[-1]["result"] == "success"
    assert compaction[-1]["trigger"] == "manual"


async def test_repeated_compacting_status_announces_the_pause_once():
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk("compacting"),
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="success"),
            _boundary_chunk(),
            _RESULT,
        ]
    )
    assert [e["phase"] for e in _compaction(events)] == ["start", "end"]


async def test_failed_compaction_closes_without_waiting_for_a_boundary():
    """A failed compaction sends no boundary — measured. It must still close."""
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="failed", compact_error="too short"),
            _TEXT,
            _RESULT,
        ]
    )
    compaction = _compaction(events)
    assert [e["phase"] for e in compaction] == ["start", "end"]
    assert compaction[-1]["result"] == "failed"
    # Closed on the turn's next chunk, not withheld until the turn ended.
    types = [e.get("type") for e in events]
    delta = "response.output_text.delta"
    assert types.index("response.compaction") < types.index(delta)
    last_compaction = len(types) - 1 - types[::-1].index("response.compaction")
    assert last_compaction < types.index("response.completed")


async def test_a_second_compaction_in_one_turn_opens_again():
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="success"),
            _boundary_chunk(),
            _TEXT,
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="success"),
            _boundary_chunk(),
            _RESULT,
        ]
    )
    assert [e["phase"] for e in _compaction(events)] == ["start", "end", "start", "end"]


async def test_a_turn_ending_on_a_held_terminal_still_closes_it():
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="failed", compact_error="too short"),
        ]
    )
    assert [e["phase"] for e in _compaction(events)] == ["start", "end"]


async def test_terminal_carries_what_the_compaction_actually_did():
    """A client should be able to say 36k → 1.6k in 12s, not just "it happened"."""
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="success"),
            _boundary_chunk(trigger="auto"),
            _RESULT,
        ]
    )
    end = _compaction(events)[-1]
    assert end["phase"] == "end"
    assert (end["pre_tokens"], end["post_tokens"]) == (36157, 1585)
    assert end["duration_ms"] == 12426
    assert end["trigger"] == "auto"
    assert end["result"] == "success"


async def test_a_compaction_without_a_boundary_reports_no_numbers():
    """The failed path never reaches a boundary, so it has nothing to report."""
    events = await _events(
        [
            _status_chunk("compacting"),
            _status_chunk(None, compact_result="failed", compact_error="too short"),
            _RESULT,
        ]
    )
    end = _compaction(events)[-1]
    assert end["result"] == "failed"
    assert end["pre_tokens"] is None
    assert end["post_tokens"] is None
    assert end["duration_ms"] is None


def test_bookkeeping_metadata_stays_off_the_wire():
    """``compact_metadata`` also carries transcript uuids — those are not ours."""
    event = _build_progress_event(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "data": {
                "compact_metadata": {
                    "trigger": "auto",
                    "pre_tokens": 36157,
                    "post_tokens": 1585,
                    "duration_ms": 12426,
                    "preserved_messages": {"anchor_uuid": "…", "uuids": ["…"]},
                    "preserved_segment": {"head_uuid": "…"},
                }
            },
        }
    )
    assert event is not None
    assert "preserved_messages" not in event
    assert "preserved_segment" not in event
    assert event["pre_tokens"] == 36157


def test_a_non_numeric_token_count_is_reported_as_unknown():
    event = _build_progress_event(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "data": {"compact_metadata": {"trigger": "auto", "pre_tokens": "n/a"}},
        }
    )
    assert event is not None and event["pre_tokens"] is None


# ---------------------------------------------------------------------------
# One turn, several streams. A leader and its subagents compact independently.
# ---------------------------------------------------------------------------


def _in_stream(chunk, *, session_id, parent_tool_use_id=None):
    """Tag a system chunk with the stream it came from, where the CLI puts it.

    The SDK's generic ``SystemMessage`` carries only ``subtype`` and ``data``, so
    the routing fields ride inside ``data`` — not at the top level, where the
    typed messages keep them.
    """
    data = {**chunk["data"], "session_id": session_id}
    if parent_tool_use_id is not None:
        data["parent_tool_use_id"] = parent_tool_use_id
    return {**chunk, "data": data}


async def test_interleaved_streams_each_get_their_own_start_and_end():
    """A subagent compacting while the leader does must not share its state.

    With one state for the whole turn the subagent's start is swallowed as the
    leader's duplicate, and whichever verdict is held last merges onto whichever
    boundary lands first — telling the client the wrong stream's outcome.
    """
    sub = dict(session_id="sess-sub", parent_tool_use_id="toolu_sub")
    events = await _events(
        [
            _in_stream(_status_chunk("compacting"), session_id="sess-leader"),
            _in_stream(_status_chunk("compacting"), **sub),
            _in_stream(
                _status_chunk(None, compact_result="success"),
                session_id="sess-leader",
            ),
            # The subagent's compaction fails, so it never reaches a boundary.
            _in_stream(
                _status_chunk(None, compact_result="failed", compact_error="short"),
                **sub,
            ),
            _in_stream(_boundary_chunk(trigger="auto"), session_id="sess-leader"),
            {**_TEXT, "parent_tool_use_id": "toolu_sub", "session_id": "sess-sub"},
            _TEXT,
            _RESULT,
        ]
    )

    by_stream = {}
    for event in _compaction(events):
        key = (event.get("parent_tool_use_id"), event.get("session_id"))
        by_stream.setdefault(key, []).append(event)

    leader = by_stream[(None, "sess-leader")]
    subagent = by_stream[("toolu_sub", "sess-sub")]
    assert [e["phase"] for e in leader] == ["start", "end"]
    assert [e["phase"] for e in subagent] == ["start", "end"]

    # Each terminal reports its own stream's outcome, not the other's.
    assert leader[-1]["result"] == "success"
    assert (leader[-1]["trigger"], leader[-1]["pre_tokens"]) == ("auto", 36157)
    assert subagent[-1]["result"] == "failed"
    assert subagent[-1]["pre_tokens"] is None


async def test_another_streams_output_does_not_close_a_held_terminal():
    """Only the compacting stream's own chunks may decide it is over.

    The held terminal waits for a boundary that may still be coming; a chunk from
    a different stream is no evidence either way, and closing on it would spend
    the terminal before the trigger and token counts are known.
    """
    events = await _events(
        [
            _in_stream(_status_chunk("compacting"), session_id="sess-leader"),
            _in_stream(
                _status_chunk(None, compact_result="success"),
                session_id="sess-leader",
            ),
            # A subagent writes while the leader's boundary is still in flight.
            {**_TEXT, "parent_tool_use_id": "toolu_sub", "session_id": "sess-sub"},
            _in_stream(_boundary_chunk(trigger="auto"), session_id="sess-leader"),
            _RESULT,
        ]
    )
    leader = _compaction(events)
    assert [e["phase"] for e in leader] == ["start", "end"]
    # The boundary still got to merge, so the numbers survived.
    assert leader[-1]["result"] == "success"
    assert leader[-1]["post_tokens"] == 1585


# ---------------------------------------------------------------------------
# The subagent gate. SUBAGENT_STREAM_PROGRESS decides whether a subagent's
# liveness reaches the client at all — it has to see the same stream identity
# the event builder does.
# ---------------------------------------------------------------------------


async def test_a_subagents_compaction_is_held_by_the_subagent_gate(monkeypatch):
    """``SUBAGENT_STREAM_PROGRESS=false`` must actually hold a subagent's compaction.

    The gate read ``parent_tool_use_id`` off the top level, where the generic
    ``SystemMessage`` never has it — so a deployment that had switched subagent
    progress off still saw subagent compactions on the wire, while the event
    built right below it read the parent out of ``data`` and labelled them.
    """
    monkeypatch.setattr("src.streaming_utils.SUBAGENT_STREAM_PROGRESS", False)
    sub = dict(session_id="sess-sub", parent_tool_use_id="toolu_sub")
    events = await _events(
        [
            _in_stream(_status_chunk("compacting"), **sub),
            _in_stream(_status_chunk(None, compact_result="success"), **sub),
            _in_stream(_boundary_chunk(trigger="auto"), **sub),
            _TEXT,
            _RESULT,
        ]
    )
    assert _compaction(events) == []


async def test_the_leaders_compaction_still_passes_that_gate(monkeypatch):
    """The gate holds subagents, not the run the user is actually watching."""
    monkeypatch.setattr("src.streaming_utils.SUBAGENT_STREAM_PROGRESS", False)
    events = await _events(
        [
            _in_stream(_status_chunk("compacting"), session_id="sess-leader"),
            _in_stream(
                _status_chunk(None, compact_result="success"), session_id="sess-leader"
            ),
            _in_stream(_boundary_chunk(trigger="auto"), session_id="sess-leader"),
            _RESULT,
        ]
    )
    assert [e["phase"] for e in _compaction(events)] == ["start", "end"]


async def test_a_subagents_hook_event_is_held_by_the_same_gate(monkeypatch):
    """Hook liveness shares the gate, and ``HookEventMessage`` models no parent.

    So a subagent's hook can only name its parent from inside ``data`` — the
    same blind spot, on the same line, for the other kind of progress event.
    """
    monkeypatch.setattr("src.streaming_utils.SUBAGENT_STREAM_PROGRESS", False)
    hook = {
        "type": "system",
        "subtype": "hook_started",
        "hook_event_name": "PreToolUse",
        "data": {
            "hook_event": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "tu_child",
            "parent_tool_use_id": "toolu_sub",
            "session_id": "sess-sub",
        },
    }
    events = await _events([hook, _TEXT, _RESULT])
    assert [e for e in events if e.get("type") == "response.hook_event"] == []


async def test_a_forwarded_subagent_hook_names_its_parent():
    """With the gate open the event still has to say whose hook it was."""
    hook = {
        "type": "system",
        "subtype": "hook_started",
        "hook_event_name": "PreToolUse",
        "data": {
            "hook_event": "PreToolUse",
            "tool_name": "Bash",
            "tool_use_id": "tu_child",
            "parent_tool_use_id": "toolu_sub",
            "session_id": "sess-sub",
        },
    }
    events = await _events([hook, _RESULT])
    forwarded = [e for e in events if e.get("type") == "response.hook_event"]
    assert len(forwarded) == 1
    assert forwarded[0]["parent_tool_use_id"] == "toolu_sub"
    assert forwarded[0]["session_id"] == "sess-sub"
