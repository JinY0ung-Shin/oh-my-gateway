"""A turn can park on a human decision more than once (#273).

The reader of a ``function_call_output`` continuation used to iterate the SDK
stream with no break event. A second decision in the same turn — question
answered, then an approval-gated MCP call — parked with nobody to stop the
stream: the client never got the card and the hook denied the call at
``ASK_USER_TIMEOUT_SECONDS`` (300 s live). These tests drive the real
``can_use_tool`` callback and approval hook from a scripted SDK client, so the
whole sequence runs in milliseconds.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.backends.claude import tool_approval
from src.backends.claude.client import ClaudeCodeCLI, make_tool_approval_hook
from src.backends.claude.tool_approval import ApprovalPolicy
from src.session_manager import Session

MAIL = "mcp__knob__send_mail"
WAIT = 2  # generous bound: a regression hangs, it does not run slow


class ScriptedClient:
    """The SDK side of one turn: a background "CLI" that calls the real gates.

    Each step is ``(tool_name, tool_input, call_id)``. Like the real CLI, the
    gate runs in its own task while the gateway reads messages; a tool counts
    as executed only when its gate lets it through.
    """

    def __init__(self, session, steps):
        cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        self._can_use_tool = cli._make_ask_user_can_use_tool(session)
        self._hook = make_tool_approval_hook(
            session, ApprovalPolicy({"knob": frozenset({"*"})}, {})
        )
        self._steps = steps
        self._messages: asyncio.Queue = asyncio.Queue()
        self._turn = None
        self.queries = 0
        self.executed = []
        self.gate_results = {}

    async def query(self, prompt):
        self.queries += 1
        self._turn = asyncio.create_task(self._run())

    async def _run(self):
        for name, tool_input, call_id in self._steps:
            await self._messages.put(
                SimpleNamespace(type="assistant", content=f"calling {name}")
            )
            if name.startswith("mcp__"):
                out = await self._hook(
                    {"tool_name": name, "tool_input": tool_input}, call_id, None
                )
                decision = (out.get("hookSpecificOutput") or {}).get(
                    "permissionDecision"
                )
                self.gate_results[call_id] = out
                if decision != "deny":
                    self.executed.append(call_id)
            else:
                self.gate_results[call_id] = await self._can_use_tool(
                    name, tool_input, SimpleNamespace(tool_use_id=call_id)
                )
        await self._messages.put(
            SimpleNamespace(type="result", subtype="success", result="done")
        )

    async def receive_response(self):
        while True:
            message = await self._messages.get()
            yield message
            if getattr(message, "type", None) == "result":
                return

    async def close(self):
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            await asyncio.gather(self._turn, return_exceptions=True)


@pytest.fixture
def cli():
    return ClaudeCodeCLI.__new__(ClaudeCodeCLI)


def _session():
    session = Session(session_id="chain", backend="claude")
    session.tool_approval_capable = True
    return session


async def _first_read(cli, client, session):
    return await asyncio.wait_for(
        _collect(cli._run_completion_with_client_raw(client, "go", session)), WAIT
    )


async def _continue(cli, client, session):
    return await asyncio.wait_for(
        _collect(cli._receive_response_from_client_raw(client, session)), WAIT
    )


async def _collect(stream):
    return [chunk async for chunk in stream]


def _answer(session, output):
    """What the route does on function_call_output: stash, clear the card, wake."""
    event = session.input_event
    session.input_response = output
    session.pending_tool_call = None
    event.set()


def _approve(pending):
    return json.dumps(
        {"decision": "approve", "argsDigest": pending["approval"]["argsDigest"]}
    )


def _ended(chunks):
    return any(c.get("subtype") == "success" for c in chunks)


QUESTION = ("AskUserQuestion", {"questions": []}, "Q1")
SEND = (MAIL, {"to": "a@x"}, "M1")


async def test_question_then_gated_call_parks_again_and_runs_on_approval(cli):
    """Tests 1 and 2 of the issue: the continuation stops on the approval card,
    and the same-digest approval runs the tool exactly once."""
    session = _session()
    client = ScriptedClient(session, [QUESTION, SEND])
    try:
        assert not _ended(await _first_read(cli, client, session))
        assert session.pending_tool_call["call_id"] == "Q1"
        _answer(session, "yes, delete it")

        chunks = await _continue(cli, client, session)
        pending = session.pending_tool_call
        assert not _ended(chunks)
        assert pending["call_id"] == "M1" and pending["name"] == MAIL
        assert pending["approval"]["tool"] == "send_mail"
        assert client.executed == [], "parked, not run"
        assert session.stream_break_event is None

        _answer(session, _approve(pending))
        assert _ended(await _continue(cli, client, session))
        assert client.executed == ["M1"]
        assert client.queries == 1, "a continuation never writes a new query"
        assert session.pending_tool_call is None and session.input_event is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"decision": "deny"}),
        json.dumps({"decision": "approve", "argsDigest": "0" * 64}),
        "ok",
    ],
    ids=["deny", "digest-mismatch", "free-text"],
)
async def test_the_second_card_denies_unless_approved_for_its_digest(cli, answer):
    session = _session()
    client = ScriptedClient(session, [QUESTION, SEND])
    try:
        await _first_read(cli, client, session)
        _answer(session, "go ahead")
        await _continue(cli, client, session)
        assert session.pending_tool_call["call_id"] == "M1"
        _answer(session, answer)
        assert _ended(await _continue(cli, client, session))
        assert client.executed == []
        reason = client.gate_results["M1"]["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        assert reason == tool_approval.DENIED_BY_USER
    finally:
        await client.close()


async def test_the_second_card_times_out_as_a_denial(cli, monkeypatch):
    monkeypatch.setattr("src.backends.claude.client.ASK_USER_TIMEOUT_SECONDS", 0.05)
    session = _session()
    client = ScriptedClient(session, [QUESTION, SEND])
    try:
        await _first_read(cli, client, session)
        _answer(session, "go ahead")
        await _continue(cli, client, session)
        assert session.pending_tool_call["call_id"] == "M1"
        await asyncio.wait_for(client._turn, WAIT)
        assert client.executed == []
        reason = client.gate_results["M1"]["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        assert reason == tool_approval.NOT_ANSWERED
        assert session.pending_tool_call is None and session.input_event is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "steps",
    [
        [QUESTION, ("AskUserQuestion", {"questions": []}, "Q2")],
        [("ExitPlanMode", {"plan": "p"}, "P1"), SEND],
        [SEND, (MAIL, {"to": "b@x"}, "M2")],
        [SEND, ("AskUserQuestion", {"questions": []}, "Q2")],
    ],
    ids=["question-question", "plan-approval", "approval-approval", "approval-question"],
)
async def test_every_chain_of_decisions_parks_on_each_one(cli, steps):
    """Test 4 of the issue: any decision after a continuation stops the reader."""
    session = _session()
    client = ScriptedClient(session, steps)
    try:
        await _first_read(cli, client, session)
        for index, (_name, _input, call_id) in enumerate(steps):
            pending = session.pending_tool_call
            assert pending is not None and pending["call_id"] == call_id
            _answer(session, _approve(pending) if "approval" in pending else "approve")
            chunks = await _continue(cli, client, session)
            assert _ended(chunks) is (index == len(steps) - 1)
        assert session.pending_tool_call is None
        assert client.queries == 1
    finally:
        await client.close()


async def test_a_decision_parked_before_the_reader_starts_still_stops_it(cli):
    """The route wakes the SDK before the continuation stream is first read, so
    the next gate can park while no break event is installed yet."""
    session = _session()
    client = ScriptedClient(session, [QUESTION, SEND])
    try:
        await _first_read(cli, client, session)
        _answer(session, "go ahead")
        for _ in range(200):
            if (session.pending_tool_call or {}).get("call_id") == "M1":
                break
            await asyncio.sleep(0.005)
        assert session.pending_tool_call["call_id"] == "M1"
        assert session.stream_break_event is None

        chunks = await _continue(cli, client, session)
        assert not _ended(chunks)
        assert session.pending_tool_call["call_id"] == "M1"
        assert client.executed == []
    finally:
        await client.close()


async def test_a_continuation_with_no_further_decision_reads_to_the_end(cli):
    session = _session()
    client = ScriptedClient(session, [QUESTION])
    try:
        await _first_read(cli, client, session)
        _answer(session, "blue")
        chunks = await _continue(cli, client, session)
        assert _ended(chunks)
        assert client.gate_results["Q1"].message == "User responded: blue"
        assert session.stream_break_event is None
    finally:
        await client.close()


# -- the route's stream: a continuation that parks again never "completes" (#275) --


def _sse_events(lines):
    events = []
    for line in lines:
        for part in line.split("\n"):
            if part.startswith("data:"):
                try:
                    events.append(json.loads(part[5:].strip()))
                except ValueError:
                    pass
    return events


async def _continuation_sse(chunks, next_pending):
    """Answer a parked question over the real streaming route; the backend yields
    ``chunks`` and, like the real reader, ends once ``next_pending`` has parked."""
    from unittest.mock import MagicMock, patch

    from fastapi.responses import StreamingResponse

    from src.backends import ResolvedModel
    from src.constants import DEFAULT_MODEL
    from src.response_models import ResponseCreateRequest
    from src.routes import responses as responses_module

    session_id = "00000000-0000-0000-0000-000000000000"
    session = Session(session_id=session_id, backend="claude")
    session.pending_tool_call = {
        "call_id": "toolu_q",
        "name": "AskUserQuestion",
        "arguments": {"questions": []},
    }
    session.input_event = asyncio.Event()
    session.client = MagicMock(name="sdk_client")
    session.turn_counter = 1
    session.workspace = "/tmp/ws/test"

    async def fake_receive(client, sess):
        for chunk in chunks:
            yield chunk
        if next_pending is not None:
            sess.pending_tool_call = next_pending

    backend = MagicMock()
    backend.run_completion_with_client = MagicMock()
    backend.receive_response_from_client = fake_receive
    body = ResponseCreateRequest(
        model=DEFAULT_MODEL,
        input=[{"type": "function_call_output", "call_id": "toolu_q", "output": "Yes"}],
        previous_response_id=f"resp_{session_id}_1",
        stream=True,
    )
    with patch.object(responses_module, "session_manager") as mock_sm:
        mock_sm.add_assistant_response = MagicMock()
        response = await responses_module._handle_function_call_output(
            body,
            ResolvedModel(public_model=DEFAULT_MODEL, backend="claude", provider_model=None),
            backend,
            session,
            session_id,
            "/tmp/ws/test",
            {"call_id": "toolu_q", "output": "Yes"},
        )
        assert isinstance(response, StreamingResponse)
        lines = [chunk async for chunk in response.body_iterator]
    return _sse_events(lines), session


_CARD = {
    "call_id": "toolu_mail",
    "name": MAIL,
    "arguments": {"to": "a@x"},
    "approval": tool_approval.approval_payload(
        "knob", "send_mail", tool_approval.args_digest({"to": "a@x"})
    ),
}
_THINKING = {
    "type": "assistant",
    "content": [{"type": "thinking", "thinking": "the user said yes; send it"}],
}
_TEXT = {"type": "assistant", "content": [{"type": "text", "text": "Sending now."}]}


@pytest.mark.parametrize(
    "chunks",
    [[], [_THINKING], [_TEXT], [_THINKING, _TEXT]],
    ids=["no-output", "thinking", "text", "thinking-and-text"],
)
async def test_a_continuation_that_parks_again_ends_only_with_requires_action(chunks):
    """The live failure: the model thinks (or writes) and then calls the gated tool.
    The stream used to close the turn as ``completed`` first, so the client dropped
    the card that followed and the call timed out as a denial."""
    events, session = await _continuation_sse(chunks, dict(_CARD))
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert len(completed) == 1, [e["response"]["status"] for e in completed]
    assert completed[0]["response"]["status"] == "requires_action"
    assert events[-1] is completed[0], "nothing after the terminal event"
    calls = [
        e["item"]
        for e in events
        if e.get("type") == "response.output_item.added"
        and e["item"].get("type") == "function_call"
    ]
    assert [c["call_id"] for c in calls] == ["toolu_mail"]
    assert calls[0]["approval"] == _CARD["approval"]
    sequence = [e["sequence_number"] for e in events if "sequence_number" in e]
    assert sequence == sorted(set(sequence)), "sequence numbers must keep rising"
    # The card is one ``added`` frame; every other item the turn opened is closed.
    opened = sum(e.get("type") == "response.output_item.added" for e in events)
    closed = sum(e.get("type") == "response.output_item.done" for e in events)
    assert opened - len(calls) == closed
    assert session.turn_counter == 2


async def test_a_continuation_with_no_new_decision_still_completes():
    """Control: no parked call, so the turn ends as an ordinary completed response."""
    result = {"type": "result", "subtype": "success", "result": "Sending now."}
    events, _session = await _continuation_sse([_THINKING, _TEXT, result], None)
    completed = [e for e in events if e.get("type") == "response.completed"]
    assert [e["response"]["status"] for e in completed] == ["completed"]
