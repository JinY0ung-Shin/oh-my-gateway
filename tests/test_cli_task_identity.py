"""Pin agent identity across a named spawn and its SendMessage resume.

The session task registry (``src.session_outbox``, served as ``active_tasks`` by
``GET /v1/sessions/{id}/pending-events``) labels a task with the ``name`` input
of the Agent call that spawned it: the call's tool_use id joins the name to the
task's ``task_started``, and the name is then remembered per task id. These
tests drive the real bundled CLI against a fake Messages API (zero cost) and pin
what that join rests on.

Pinned on CLI 2.1.283:

* the Agent tool offers ``name`` (with the deprecated ``team_name`` and
  ``mode``) only while ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` is on, and the
  CLI parses that gate as a boolean: ``0`` is off, like blank or unset;
* a named Agent call's ``task_started`` (``local_agent``) carries the call's
  tool_use id;
* a later turn's ``SendMessage{to: name}`` resumes the finished agent in the
  background under the SAME task id, announced again with the SendMessage
  call's tool_use id, while the resumed run's own messages still hang off the
  original Agent call's id.

The real messages are then replayed through the gateway's own tracking, the
turn path (``_convert_message`` → ``apply_turn_task_chunk``) and the idle
reader's ``_handle_idle_message``, so the registry outcome is pinned too.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from src.backends.claude.client import ClaudeCodeCLI
from src.session_manager import Session
from src.session_outbox import _handle_idle_message, apply_turn_task_chunk, get_outbox
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI, Plan

pytestmark = pytest.mark.integration

# No pytest-timeout in this repo: bound every real-CLI wait ourselves.
_TURN_TIMEOUT = 60
_SETTLE_TIMEOUT = 15

_TEAMS_GATE = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
_TEAM_PARAMS = frozenset({"name", "team_name", "mode"})
_TERMINAL = frozenset({"completed", "failed", "stopped", "killed"})

_LEADER_MARK = "LEADER-START"
_RESUME_MARK = "LEADER-RESUME"
_SUBAGENT_MARK = "SUBAGENT-REPORT"
_NAME = "worker-a"

_SPAWN = {
    "name": "Agent",
    "input": {
        "description": "named worker",
        "prompt": f"{_SUBAGENT_MARK}: report back",
        "subagent_type": "general-purpose",
        "name": _NAME,
        # Foreground: the first run ends inside the turn that spawned it.
        "run_in_background": False,
    },
}
_RESUME = {
    "name": "SendMessage",
    "input": {"to": _NAME, "message": "report again", "summary": "resume worker"},
}

# Besides CLAUDE_CODE_*, the vars a Claude Code session exports to its tools.
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    """Keep the runner's own Claude env out of the CLI child.

    The SDK spawns the child with ``{**os.environ, **options.env}``. A suite run
    from inside a Claude Code session would otherwise hand the CLI under test
    that session's peer-messaging socket and token, its agent-teams gate and its
    session id. Unset them rather than blanking them: to the CLI, blank is not
    always the same as off.
    """
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "")
        for block in content or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _is_tool_result(message: Dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _since_last_reply(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the model must answer now: everything after its last reply."""
    pending: List[Dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant":
            pending = []
        else:
            pending.append(message)
    return pending


def _plan(body: Dict[str, Any]) -> Dict[str, Any]:
    """Turn 1 spawns the named agent, turn 2 messages it; the agent reports."""
    messages = body.get("messages") or []
    if not body.get("tools") or not messages:
        return {"text": "ok"}  # side requests
    first = _text(messages[0].get("content"))
    if _SUBAGENT_MARK in first:
        return {"text": "SUBAGENT-DONE"}
    if _LEADER_MARK not in first:
        return {"text": "ok"}
    # The CLI may wrap the new prompt in reminders (role "system" included).
    pending = _since_last_reply(messages)
    if any(_is_tool_result(message) for message in pending):
        return {"text": "LEADER-DONE"}
    prompt = "\n".join(_text(message.get("content")) for message in pending)
    if _RESUME_MARK in prompt:
        return {"tool_use": _RESUME}
    if _LEADER_MARK in prompt:
        return {"tool_use": _SPAWN}
    return {"text": "ok"}  # e.g. a background-task notification


@asynccontextmanager
async def _cli(
    tmp_path, *, teams: Optional[str], plan: Optional[Plan] = None
) -> AsyncIterator[Tuple[ClaudeSDKClient, FakeAnthropicAPI]]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Keep anything the CLI binds (sockets) inside the test's own tmp dir.
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir(mode=0o700)
    # Its per-uid temp dir (task output files) too, not /tmp/claude-<uid>.
    cli_tmp = tmp_path / "tmp"
    cli_tmp.mkdir(mode=0o700)
    with FakeAnthropicAPI(plan=plan) as api:
        env = {
            **api.cli_env(tmp_path / "home"),
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "CLAUDE_CODE_TMPDIR": str(cli_tmp),
            # The gateway's process default (src/constants.py).
            "CLAUDE_CODE_HARBOR_KITE": "0",
        }
        if teams is not None:
            env[_TEAMS_GATE] = teams
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env=env,
            permission_mode="bypassPermissions",
            max_turns=4,
            session_id=str(uuid.uuid4()),
        )
        client = ClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
        try:
            yield client, api
        finally:
            # Bounded inside the SDK; an asyncio timeout around it could cut
            # the terminate/kill escalation short and orphan the child.
            await client.disconnect()


async def _turn(client: ClaudeSDKClient, prompt: str) -> List[Any]:
    seen: List[Any] = []

    async def run() -> None:
        await client.query(prompt)
        async for message in client.receive_response():
            seen.append(message)

    await asyncio.wait_for(run(), timeout=_TURN_TIMEOUT)
    return seen


def _ended(messages: List[Any], task_id: str) -> bool:
    """Whether *messages* report a terminal status for *task_id*."""
    for message in messages:
        if isinstance(message, TaskUpdatedMessage):
            status = message.status or (message.patch or {}).get("status")
        elif isinstance(message, TaskNotificationMessage):
            status = message.status
        else:
            continue
        if message.task_id == task_id and status in _TERMINAL:
            return True
    return False


async def _read_until_ended(
    client: ClaudeSDKClient, turn: List[Any], task_id: str
) -> List[Any]:
    """The between-turn messages, read until *task_id* reports its end."""
    after: List[Any] = []
    if _ended(turn, task_id):
        return after
    ended = asyncio.Event()

    async def pump() -> None:
        async for message in client.receive_messages():
            after.append(message)
            if _ended([message], task_id):
                ended.set()

    reader = asyncio.ensure_future(pump())
    try:
        # A miss fails the assertions below, with the messages to inspect.
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ended.wait(), timeout=_SETTLE_TIMEOUT)
    finally:
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader
    return after


def _starts(messages: List[Any]) -> List[TaskStartedMessage]:
    return [message for message in messages if isinstance(message, TaskStartedMessage)]


def _leader_call(messages: List[Any], tool: str) -> ToolUseBlock:
    calls = [
        block
        for message in messages
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None
        for block in message.content
        if isinstance(block, ToolUseBlock) and block.name == tool
    ]
    assert len(calls) == 1, f"expected one leader {tool} call, got {calls}"
    return calls[0]


def _leader_result_text(messages: List[Any], tool_use_id: str) -> str:
    for message in messages:
        if not isinstance(message, UserMessage) or message.parent_tool_use_id:
            continue
        for block in message.content if isinstance(message.content, list) else []:
            if not isinstance(block, ToolResultBlock):
                continue
            if block.tool_use_id == tool_use_id:
                content = block.content
                return content if isinstance(content, str) else _text(content)
    raise AssertionError(f"no leader tool_result for {tool_use_id}")


def _replay(session: Session, messages: List[Any], *, in_turn: bool) -> List[Any]:
    """Feed *messages* through the gateway's task tracking, in order.

    ``in_turn`` takes the Responses turn path, which sees the chunks the Claude
    backend converts; otherwise the idle reader's handler gets the raw SDK
    objects. Returns a copy of the registry entry right after each
    ``task_started``.
    """
    # _convert_message reads class state only; skip the auth-checking __init__.
    backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    entries: List[Any] = []
    for message in messages:
        if in_turn:
            apply_turn_task_chunk(session, backend._convert_message(message))
        else:
            _handle_idle_message(session, message)
        if isinstance(message, TaskStartedMessage):
            entry = get_outbox(session).active_tasks.get(message.task_id)
            entries.append(dict(entry) if entry else None)
    return entries


@pytest.mark.parametrize(
    ("gate", "offered"),
    [("1", True), ("0", False), ("", False), (None, False)],
    ids=["1", "0", "blank", "unset"],
)
async def test_agent_offers_name_only_while_the_teams_gate_is_on(
    tmp_path, gate, offered
):
    async with _cli(tmp_path, teams=gate) as (client, api):
        await _turn(client, "hello")
    tools = api.model_requests()[0]["body"]["tools"]
    (agent,) = [tool for tool in tools if tool["name"] == "Agent"]
    params = set(agent["input_schema"]["properties"])
    if offered:
        assert _TEAM_PARAMS <= params
    else:
        assert not _TEAM_PARAMS & params


async def test_send_message_resumes_a_named_agent_under_its_task_id(tmp_path):
    async with _cli(tmp_path, teams="1", plan=_plan) as (client, api):
        first = await _turn(client, f"{_LEADER_MARK}: spawn {_NAME}")
        starts = _starts(first)
        assert len(starts) == 1, starts
        task_id = starts[0].task_id
        second = await _turn(client, f"{_RESUME_MARK}: message {_NAME}")
        # The resumed run is backgrounded: its end may land after the turn.
        later = await _read_until_ended(client, second, task_id)

    # Offered inline to the fake API's model: callable without a ToolSearch.
    offered = {tool["name"] for tool in api.model_requests()[0]["body"]["tools"]}
    assert "SendMessage" in offered

    # The named spawn is announced under the Agent call's id and, running in
    # the foreground, ends inside turn 1.
    spawn = _leader_call(first, "Agent")
    assert starts[0].tool_use_id == spawn.id
    assert starts[0].task_type == "local_agent"
    assert starts[0].data.get("is_backgrounded") is False
    assert _ended(first, task_id)

    # SendMessage resumes it: the SAME task id, announced again under the
    # SendMessage call's id, now in the background.
    send = _leader_call(second, "SendMessage")
    resumed = _starts(second + later)
    assert [message.task_id for message in resumed] == [task_id]
    assert resumed[0].tool_use_id == send.id
    assert resumed[0].task_type == "local_agent"
    assert resumed[0].data.get("is_backgrounded") is True
    reply = _leader_result_text(second, send.id)
    assert "Resuming agent" in reply
    assert task_id in reply
    assert _ended(second + later, task_id)
    # The resumed run's own messages still hang off the ORIGINAL Agent call,
    # not the SendMessage call its task is now announced under.
    parents = {
        message.parent_tool_use_id
        for message in second + later
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id
    }
    assert parents == {spawn.id}

    # The registry, fed as the gateway feeds it: the turns' chunks through the
    # turn path, whatever followed through the idle reader.
    session = Session(session_id=str(uuid.uuid4()))
    (spawned,) = _replay(session, first, in_turn=True)
    assert (spawned["name"], spawned["tool_use_id"]) == (_NAME, spawn.id)
    assert spawned["task_type"] == "local_agent"
    # Finished in turn 1: the resume has to find the name by task id alone.
    assert task_id not in get_outbox(session).active_tasks
    again = _replay(session, second, in_turn=True)
    again += _replay(session, later, in_turn=False)
    assert len(again) == 1
    assert again[0]["task_id"] == task_id
    # The entry keeps the spawning call's id: the one the resumed run's own
    # messages carry as parent_tool_use_id (asserted above).
    assert (again[0]["name"], again[0]["tool_use_id"]) == (_NAME, spawn.id)
    assert get_outbox(session).active_tasks == {}

    # The idle reader alone (raw SDK objects, as when a notification wakes the
    # leader between turns) names both starts in the events pollers get.
    idle = Session(session_id=str(uuid.uuid4()))
    _replay(idle, first + second + later, in_turn=False)
    announced = [
        (event["task_id"], event["tool_use_id"], event["name"])
        for event in get_outbox(idle).events_after(0)
        if event["type"] == "task_started"
    ]
    assert announced == [(task_id, spawn.id, _NAME), (task_id, send.id, _NAME)]
