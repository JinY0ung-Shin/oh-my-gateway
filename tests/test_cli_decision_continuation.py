"""Pin a turn that parks twice (#273) against the bundled CLI and the gateway's readers.

One turn, driven by a fake Messages API: the model asks ``AskUserQuestion``, the
client answers, and the model then calls an approval-gated MCP write on a real MCP
SDK streamable-http server that records every call it runs. The turn is read
through the gateway's own readers — ``run_completion_with_client`` for the new
turn, ``receive_response_from_client`` for each ``function_call_output``
continuation — with the gateway's real permission callback and approval hook.

Before #273's fix the continuation after the answer never stopped on the approval
card: the hook waited ``ASK_USER_TIMEOUT_SECONDS`` and denied the call.
``tests/test_decision_continuation.py`` covers the same chain without a CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from typing import Any, Dict, List

import pytest
import uvicorn
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from mcp.server.fastmcp import FastMCP

from src.backends.claude import client as claude_client
from src.backends.claude import tool_approval
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from src.session_manager import Session
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_READ_TIMEOUT = 60  # far below ASK_USER_TIMEOUT_SECONDS: a regression waits that out
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)
_ARGS = {"to": "alice@example.com", "subject": "test"}
_QUESTION = {
    "questions": [
        {
            "question": "Send the mail?",
            "header": "Confirm",
            "multiSelect": False,
            "options": [
                {"label": "Yes", "description": "send it"},
                {"label": "No", "description": "do not send"},
            ],
        }
    ]
}
_POLICY = tool_approval.ApprovalPolicy({"knob": frozenset({"*"})}, {})


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


@pytest.fixture
def knob():
    ran: List[Dict[str, Any]] = []
    mcp = FastMCP("knob")

    @mcp.tool()
    async def send_mail(to: str, subject: str) -> str:
        """Send a mail."""
        ran.append({"to": to, "subject": subject})
        return "sent"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", ran
    server.should_exit = True


def _plan(body: Dict[str, Any]) -> Dict[str, Any]:
    """Question first, the gated write after its answer, text after the write."""
    results = sum(
        1
        for message in body.get("messages", [])
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    if results == 0:
        return {"tool_use": {"name": "AskUserQuestion", "input": _QUESTION}}
    if results == 1:
        return {"tool_use": {"name": "mcp__knob__send_mail", "input": dict(_ARGS)}}
    return {"text": "finished"}


class _Turn:
    """The route's half of the exchange, over the gateway's readers."""

    def __init__(self, cli, client, session):
        self.cli, self.client, self.session = cli, client, session

    async def _read(self, stream) -> List[Dict[str, Any]]:
        async def collect():
            return [chunk async for chunk in stream]

        return await asyncio.wait_for(collect(), _READ_TIMEOUT)

    async def start(self) -> List[Dict[str, Any]]:
        return await self._read(
            self.cli.run_completion_with_client(self.client, "go", self.session)
        )

    async def answer(self, output: str) -> List[Dict[str, Any]]:
        """``function_call_output``: commit the answer, then read the continuation."""
        session = self.session
        session.input_response = output
        session.input_event.set()
        session.pending_tool_call = None
        return await self._read(
            self.cli.receive_response_from_client(self.client, session)
        )


def _ended(chunks: List[Dict[str, Any]]) -> bool:
    return any(c.get("subtype") == "success" for c in chunks)


def _tool_results(chunks: List[Dict[str, Any]]) -> str:
    return json.dumps(
        [c for c in chunks if c.get("type") == "user" or "tool_use_result" in c],
        default=str,
    )


async def _question_then_card(url, home, run):
    """Run the turn up to the approval card, then hand over to ``run(turn, card)``."""
    cli = claude_client.ClaudeCodeCLI.__new__(claude_client.ClaudeCodeCLI)
    session = Session(session_id="chain-live", backend="claude")
    session.tool_approval_capable = True
    hook = claude_client.make_tool_approval_hook(session, _POLICY)
    with FakeAnthropicAPI(_plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(home),
            mcp_servers={"knob": {"type": "http", "url": url}},
            permission_mode="bypassPermissions",
            can_use_tool=cli._make_ask_user_can_use_tool(session),
            cwd=str(home),
            hooks={"PreToolUse": [HookMatcher(matcher="", hooks=[hook], timeout=600)]},
        )
        async with GatewayClaudeSDKClient(options=options) as client:
            turn = _Turn(cli, client, session)
            assert not _ended(await turn.start())
            question = session.pending_tool_call
            assert question["name"] == "AskUserQuestion"
            assert "approval" not in question

            started = time.monotonic()
            chunks = await turn.answer("Yes")
            card = session.pending_tool_call
            assert not _ended(chunks), "the continuation must stop on the card"
            assert card is not None, "no approval card after the answered question"
            assert time.monotonic() - started < _READ_TIMEOUT
            assert card["call_id"] != question["call_id"]
            assert card["name"] == "mcp__knob__send_mail"
            assert card["arguments"] == _ARGS
            assert card["approval"]["argsDigest"] == tool_approval.args_digest(_ARGS)
            assert session.stream_break_event is None, "no reader left behind"
            return await run(turn, dict(card))


async def test_the_card_after_an_answered_question_arrives_and_approval_runs_once(
    knob, tmp_path
):
    url, ran = knob

    async def run(turn, card):
        await asyncio.sleep(1.0)
        assert ran == [], "the tool must not run before the decision"
        chunks = await turn.answer(
            json.dumps(
                {"decision": "approve", "argsDigest": card["approval"]["argsDigest"]}
            )
        )
        assert _ended(chunks)
        assert turn.session.pending_tool_call is None
        assert turn.session.input_event is None

    await _question_then_card(url, tmp_path, run)
    assert ran == [_ARGS], "exactly one run, with the shown arguments"


@pytest.mark.parametrize(
    "decision",
    [
        json.dumps({"decision": "deny"}),
        json.dumps({"decision": "approve", "argsDigest": "sha256:" + "0" * 64}),
    ],
    ids=["deny", "digest-for-other-arguments"],
)
async def test_the_card_after_an_answered_question_denies_without_running(
    knob, tmp_path, decision
):
    url, ran = knob

    async def run(turn, card):
        chunks = await turn.answer(decision)
        assert _ended(chunks)
        assert "declined" in _tool_results(chunks)

    await _question_then_card(url, tmp_path, run)
    assert ran == []


async def test_an_interrupt_while_the_card_waits_runs_nothing_and_frees_the_slot(
    knob, tmp_path
):
    """The user stops the turn instead of deciding: the parked call is dropped, the
    slot is free for the next decision and the session's stream is read to its end
    by one reader."""
    url, ran = knob

    async def run(turn, card):
        session = turn.session
        await turn.client.interrupt()
        rest = await turn._read(
            turn.cli.receive_response_from_client(turn.client, session)
        )
        for _ in range(100):
            if session.pending_tool_call is None and session.input_event is None:
                break
            await asyncio.sleep(0.05)
        assert session.pending_tool_call is None and session.input_event is None
        assert session.stream_break_event is None
        assert any("subtype" in c for c in rest), "the interrupted turn ends"

    await _question_then_card(url, tmp_path, run)
    assert ran == []
