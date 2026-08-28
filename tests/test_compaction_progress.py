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
