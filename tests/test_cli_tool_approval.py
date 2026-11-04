"""Pin the MCP tool approval gate (ChatDRAGON #798) against the bundled CLI.

Measured on CLI 2.1.283 with a fake Messages API that calls one MCP tool, against a real MCP SDK
streamable-http server that records every call it runs:

* the permission callback (``can_use_tool``) is **not** a gate for MCP calls: under
  ``bypassPermissions`` — the gateway's usual production mode — or when ``allowed_tools`` lists
  the tool, the CLI runs it without ever asking (``test_can_use_tool_is_not_asked_*``, the
  control that rules out the obvious fix);
* the gateway's PreToolUse approval hook parks the call in every mode, the tool does not run
  while it waits, an approval for the shown arguments runs it exactly once with those
  arguments, and anything else (deny, a digest for other arguments, a timeout, a client that
  cannot show approvals) runs it zero times;
* ``approvalTools: "*"`` gates every tool except the operator's ``readOnlyTools``, so a tool the
  server renamed or added still parks while a read-only lookup runs untouched.

If a CLI bump changes any of this, revisit ``src/backends/claude/tool_approval.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import pytest
import uvicorn
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import PermissionResultAllow, ToolResultBlock, UserMessage
from mcp.server.fastmcp import FastMCP

from src.backends.claude import client as claude_client
from src.backends.claude import tool_approval
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from src.session_manager import Session
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 90
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)
_ARGS = {"to": "alice@example.com", "subject": "test"}


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def knob():
    """A stand-in for knob-mcp: one write (send_mail), one renamed write, one read."""
    ran: List[Dict[str, Any]] = []
    mcp = FastMCP("knob")

    @mcp.tool()
    async def send_mail(to: str, subject: str) -> str:
        """Send a mail."""
        ran.append({"tool": "send_mail", "to": to, "subject": subject})
        return "sent"

    @mcp.tool()
    async def send_mail_v2(to: str, subject: str) -> str:
        """Renamed write tool."""
        ran.append({"tool": "send_mail_v2", "to": to, "subject": subject})
        return "sent"

    @mcp.tool()
    async def list_mail(to: str, subject: str) -> str:
        """Read-only lookup."""
        ran.append({"tool": "list_mail", "to": to, "subject": subject})
        return "listed"

    port = _free_port()
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


def _plan_for(tool: str):
    def plan(body: Dict[str, Any]) -> Dict[str, Any]:
        for message in body.get("messages", []):
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content
            ):
                return {"text": "finished"}
        return {"tool_use": {"name": f"mcp__knob__{tool}", "input": dict(_ARGS)}}

    return plan


def _session(capable: bool = True) -> Session:
    session = Session(session_id="approval-test", backend="claude")
    session.tool_approval_capable = capable
    return session


_POLICY = tool_approval.ApprovalPolicy(
    {"knob": frozenset({"*"})}, {"knob": frozenset({"list_mail"})}
)


async def _run(
    url: str,
    home,
    tool: str,
    session: Session,
    answer: Optional[Any],
    *,
    mode: str = "bypassPermissions",
    ran: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """One turn: the CLI calls ``tool``; ``answer(pending)`` plays the client's decision."""
    seen: Dict[str, Any] = {"pending": None, "results": []}

    async def client_decides() -> None:
        while session.pending_tool_call is None:
            await asyncio.sleep(0.05)
        pending = dict(session.pending_tool_call)
        seen["pending"] = pending
        await asyncio.sleep(1.0)
        seen["ran_while_parked"] = len(ran)
        if answer is None:
            return  # never answered: the hook's own timeout decides
        session.input_response = answer(pending)
        session.pending_tool_call = None
        session.input_event.set()

    hook = claude_client.make_tool_approval_hook(session, _POLICY)
    with FakeAnthropicAPI(_plan_for(tool)) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(home),
            mcp_servers={"knob": {"type": "http", "url": url}},
            allowed_tools=[f"mcp__knob__{tool}"],
            permission_mode=mode,
            cwd=str(home),
            hooks={"PreToolUse": [HookMatcher(matcher="", hooks=[hook], timeout=600)]},
        )
        decider = asyncio.create_task(client_decides())
        try:
            async with GatewayClaudeSDKClient(options=options) as client:
                await client.query("go")
                async for message in client.receive_response():
                    if isinstance(message, UserMessage) and isinstance(
                        message.content, list
                    ):
                        seen["results"] += [
                            str(b.content)
                            for b in message.content
                            if isinstance(b, ToolResultBlock)
                        ]
        finally:
            decider.cancel()
    return seen


def _approve(pending: Dict[str, Any]) -> str:
    return json.dumps(
        {"decision": "approve", "argsDigest": pending["approval"]["argsDigest"]}
    )


@pytest.mark.parametrize("mode", ["bypassPermissions", "default"])
async def test_an_approved_call_runs_once_with_the_shown_arguments(
    knob, tmp_path, mode
):
    url, ran = knob
    seen = await asyncio.wait_for(
        _run(url, tmp_path, "send_mail", _session(), _approve, mode=mode, ran=ran),
        _TURN_TIMEOUT,
    )
    pending = seen["pending"]
    assert pending["name"] == "mcp__knob__send_mail"
    assert pending["arguments"] == _ARGS
    assert pending["approval"] == {
        "kind": "tool_approval",
        "version": 1,
        "server": "knob",
        "tool": "send_mail",
        "argsDigest": tool_approval.args_digest(_ARGS),
    }
    assert seen["ran_while_parked"] == 0, "the tool must not run before the decision"
    assert ran == [{"tool": "send_mail", **_ARGS}]


@pytest.mark.parametrize(
    "answer",
    [
        lambda p: json.dumps({"decision": "deny"}),
        lambda p: json.dumps(
            {"decision": "approve", "argsDigest": "sha256:" + "0" * 64}
        ),
        lambda p: "approve",
        lambda p: "",
    ],
    ids=["deny", "digest-for-other-arguments", "bare-string", "empty"],
)
async def test_anything_but_an_approval_for_these_arguments_runs_nothing(
    knob, tmp_path, answer
):
    url, ran = knob
    seen = await asyncio.wait_for(
        _run(url, tmp_path, "send_mail", _session(), answer, ran=ran), _TURN_TIMEOUT
    )
    assert seen["pending"] is not None
    assert ran == []
    assert any("declined" in r for r in seen["results"]), seen["results"]


async def test_an_unanswered_call_runs_nothing_after_the_timeout(
    knob, tmp_path, monkeypatch
):
    url, ran = knob
    monkeypatch.setattr(claude_client, "ASK_USER_TIMEOUT_SECONDS", 2)
    seen = await asyncio.wait_for(
        _run(url, tmp_path, "send_mail", _session(), None, ran=ran), _TURN_TIMEOUT
    )
    assert seen["pending"] is not None
    assert ran == []
    assert any("did not approve" in r for r in seen["results"]), seen["results"]


async def test_a_client_that_cannot_show_approvals_gets_a_denial_not_a_park(
    knob, tmp_path
):
    url, ran = knob
    session = _session(capable=False)
    seen = await asyncio.wait_for(
        _run(url, tmp_path, "send_mail", session, _approve, ran=ran), _TURN_TIMEOUT
    )
    assert seen["pending"] is None, "no card the client cannot render"
    assert ran == []
    assert any("needs the user's approval" in r for r in seen["results"]), seen[
        "results"
    ]


async def test_a_renamed_write_still_parks_and_a_read_only_tool_runs_untouched(
    knob, tmp_path
):
    url, ran = knob
    renamed = await asyncio.wait_for(
        _run(
            url,
            tmp_path / "a",
            "send_mail_v2",
            _session(),
            lambda p: json.dumps({"decision": "deny"}),
            ran=ran,
        ),
        _TURN_TIMEOUT,
    )
    assert renamed["pending"] is not None and ran == []
    read = await asyncio.wait_for(
        _run(url, tmp_path / "b", "list_mail", _session(), _approve, ran=ran),
        _TURN_TIMEOUT,
    )
    assert read["pending"] is None
    assert ran == [{"tool": "list_mail", **_ARGS}]


@pytest.mark.parametrize("mode", ["bypassPermissions"])
async def test_can_use_tool_is_not_asked_for_an_mcp_call(knob, tmp_path, mode):
    """Control: why the gate is a hook. The permission callback never sees the call."""
    url, ran = knob
    asked: List[str] = []

    async def can_use_tool(name, data, ctx):
        asked.append(name)
        return PermissionResultAllow()

    with FakeAnthropicAPI(_plan_for("send_mail")) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(tmp_path),
            mcp_servers={"knob": {"type": "http", "url": url}},
            allowed_tools=["mcp__knob__send_mail"],
            permission_mode=mode,
            can_use_tool=can_use_tool,
            cwd=str(tmp_path),
        )
        async with GatewayClaudeSDKClient(options=options) as client:
            await client.query("go")
            async for _ in client.receive_response():
                pass
    assert (
        asked == [] and len(ran) == 1
    ), "the CLI now asks the permission callback for MCP calls"


_TWO = [
    {"to": "alice@example.com", "subject": "first"},
    {"to": "bob@example.com", "subject": "second"},
]


def _plan_two(body: Dict[str, Any]) -> Dict[str, Any]:
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        ):
            return {"text": "finished"}
    return {
        "tool_uses": [
            {"name": "mcp__knob__send_mail", "input": dict(args)} for args in _TWO
        ]
    }


async def test_two_gated_calls_in_one_turn_each_get_their_own_decision(
    knob, tmp_path
):
    """One assistant message calls two approval-gated tools (#269 review). Measured on the
    bundled CLI: it runs a non-read-only MCP batch one call at a time, so the second hook
    fires only after the first call's decision — each call gets its own card, its own
    digest and its own outcome; approving one never runs or settles the other."""
    url, ran = knob
    session = _session()
    hook = claude_client.make_tool_approval_hook(session, _POLICY)
    parked: List[Dict[str, Any]] = []
    overlaps: List[str] = []

    async def client_decides() -> None:
        handled: set = set()
        while True:
            while (
                session.pending_tool_call is None
                or session.pending_tool_call["call_id"] in handled
            ):
                await asyncio.sleep(0.02)
            pending = dict(session.pending_tool_call)
            handled.add(pending["call_id"])
            parked.append(pending)
            await asyncio.sleep(0.8)
            if len(ran) != len(parked) - 1:
                overlaps.append("a call ran before its decision")
            # approve the first, deny the second
            approve = len(parked) == 1
            session.input_response = (
                _approve(pending) if approve else json.dumps({"decision": "deny"})
            )
            session.pending_tool_call = None
            session.input_event.set()

    results: List[str] = []
    with FakeAnthropicAPI(_plan_two) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(tmp_path),
            mcp_servers={"knob": {"type": "http", "url": url}},
            permission_mode="bypassPermissions",
            cwd=str(tmp_path),
            hooks={"PreToolUse": [HookMatcher(matcher="", hooks=[hook], timeout=600)]},
        )
        decider = asyncio.create_task(client_decides())

        async def turn() -> None:
            async with GatewayClaudeSDKClient(options=options) as client:
                await client.query("go")
                async for message in client.receive_response():
                    if isinstance(message, UserMessage) and isinstance(
                        message.content, list
                    ):
                        results.extend(
                            str(b.content)
                            for b in message.content
                            if isinstance(b, ToolResultBlock)
                        )

        try:
            await asyncio.wait_for(turn(), _TURN_TIMEOUT)
        finally:
            decider.cancel()
    assert [p["arguments"] for p in parked] == _TWO
    assert parked[0]["call_id"] != parked[1]["call_id"]
    assert [p["approval"]["argsDigest"] for p in parked] == [
        tool_approval.args_digest(a) for a in _TWO
    ]
    assert overlaps == []
    assert ran == [{"tool": "send_mail", **_TWO[0]}], "only the approved call ran"
    assert any("declined" in r for r in results), results
