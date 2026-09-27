"""Pin the bundled CLI's ``stop_task`` control request for subagent tasks.

A per-task stop relays ``ClaudeSDKClient.stop_task``. That is only useful if the
CLI really stops the task in SDK mode, including a *foreground* subagent: the
gateway forces ``run_in_background: false`` on every Agent call
(``FORCE_FOREGROUND_SUBAGENTS``), so most subagents run inside the turn. These
tests drive the real bundled CLI against a fake Messages API (zero cost); the
subagent's first tool call is a long ``sleep``, so every stop lands on a task
that is still running.

Pinned on CLI 2.1.283:

* a foreground subagent stopped mid-turn reports ``killed``, the leader's Agent
  call gets an error tool_result ("interrupted"), and the turn still ends with
  a normal ``ResultMessage``;
* a background subagent or Bash task stopped after the turn reports ``killed``
  between turns;
* an unknown or already-finished task id is a silent no-op: the SDK raises
  nothing, and a running task is left alone. A caller that needs "not found"
  must check its own task registry.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

# No pytest-timeout in this repo: bound every real-CLI wait ourselves.
_TURN_TIMEOUT = 60
_STOP_TIMEOUT = 15

_LEADER_MARK = "LEADER-START"
_SUBAGENT_MARK = "SUBAGENT-SLEEP"
_SLEEP = {"command": "sleep 60", "description": "long sleep"}

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


def _called_a_tool(messages: List[Dict[str, Any]]) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") == "tool_use"
        for message in messages
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), list)
        for block in message["content"]
    )


def _plan(leader_tool: Dict[str, Any]):
    """The leader calls *leader_tool* once, then answers; a subagent sleeps."""

    def plan(body: Dict[str, Any]) -> Dict[str, Any]:
        messages = body.get("messages") or []
        if not body.get("tools") or not messages:
            return {"text": "ok"}  # side requests
        first = _text(messages[0].get("content"))
        if _SUBAGENT_MARK in first:
            if _called_a_tool(messages):
                return {"text": "SUBAGENT-DONE"}
            return {"tool_use": {"name": "Bash", "input": _SLEEP}}
        if _LEADER_MARK in first:
            if _called_a_tool(messages):
                return {"text": "LEADER-DONE"}
            return {"tool_use": leader_tool}
        return {"text": "ok"}

    return plan


def _agent(*, run_in_background: bool) -> Dict[str, Any]:
    return {
        "name": "Agent",
        "input": {
            "description": "sleeper",
            "prompt": f"{_SUBAGENT_MARK}: run the long sleep",
            "subagent_type": "general-purpose",
            "run_in_background": run_in_background,
        },
    }


_BACKGROUND_BASH = {"name": "Bash", "input": {**_SLEEP, "run_in_background": True}}


@asynccontextmanager
async def _cli(tmp_path, leader_tool: Dict[str, Any]) -> AsyncIterator[ClaudeSDKClient]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Keep anything the CLI binds (sockets) inside the test's own tmp dir.
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir(mode=0o700)
    # ... and the task output files the CLI writes under its tmp dir.
    cli_tmp = tmp_path / "cli-tmp"
    cli_tmp.mkdir()
    with FakeAnthropicAPI(plan=_plan(leader_tool)) as api:
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env={
                **api.cli_env(tmp_path / "home"),
                "XDG_RUNTIME_DIR": str(runtime_dir),
                "CLAUDE_CODE_TMPDIR": str(cli_tmp),
                # The gateway's process default (src/constants.py).
                "CLAUDE_CODE_HARBOR_KITE": "0",
            },
            permission_mode="bypassPermissions",
            max_turns=4,
            session_id=str(uuid.uuid4()),
        )
        client = ClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
        try:
            yield client
        finally:
            await client.disconnect()


def _is_subagent_bash(message: Any, agent_tool_use_id: Optional[str]) -> bool:
    return (
        agent_tool_use_id is not None
        and isinstance(message, AssistantMessage)
        and message.parent_tool_use_id == agent_tool_use_id
        and any(
            isinstance(block, ToolUseBlock) and block.name == "Bash"
            for block in message.content
        )
    )


def _updated_statuses(messages: List[Any], task_id: str) -> List[Optional[str]]:
    return [
        message.status or (message.patch or {}).get("status")
        for message in messages
        if isinstance(message, TaskUpdatedMessage) and message.task_id == task_id
    ]


def _ended(messages: List[Any], task_id: str) -> bool:
    """A terminal ``task_updated`` for *task_id* (non-terminal patches exist)."""
    return any(
        status in TERMINAL_TASK_STATUSES
        for status in _updated_statuses(messages, task_id)
    )


def _notified_statuses(messages: List[Any], task_id: str) -> List[str]:
    return [
        message.status
        for message in messages
        if isinstance(message, TaskNotificationMessage) and message.task_id == task_id
    ]


def _leader_tool_result(messages: List[Any], tool_use_id: str) -> ToolResultBlock:
    for message in messages:
        if isinstance(message, UserMessage) and message.parent_tool_use_id is None:
            for block in message.content if isinstance(message.content, list) else []:
                if (
                    isinstance(block, ToolResultBlock)
                    and block.tool_use_id == tool_use_id
                ):
                    return block
    raise AssertionError(f"no leader tool_result for {tool_use_id}")


def _result_text(block: ToolResultBlock) -> str:
    if isinstance(block.content, str):
        return block.content
    return _text(block.content)


def _leader_said(messages: List[Any], marker: str) -> bool:
    return any(
        isinstance(message, AssistantMessage)
        and message.parent_tool_use_id is None
        and any(isinstance(b, TextBlock) and marker in b.text for b in message.content)
        for message in messages
    )


async def test_stopping_a_foreground_subagent_mid_turn_lets_the_turn_finish(tmp_path):
    async with _cli(tmp_path, _agent(run_in_background=False)) as client:
        await client.query(f"{_LEADER_MARK} foreground")
        seen: List[Any] = []
        task: Dict[str, Any] = {}
        sleeping = asyncio.Event()

        async def turn() -> None:
            async for message in client.receive_response():
                seen.append(message)
                if isinstance(message, TaskStartedMessage) and not task:
                    task.update(
                        id=message.task_id,
                        tool_use_id=message.tool_use_id,
                        data=message.data,
                    )
                if _is_subagent_bash(message, task.get("tool_use_id")):
                    sleeping.set()

        async def stop() -> int:
            await sleeping.wait()
            # Issued while receive_response() is still streaming the turn.
            await client.stop_task(task["id"])
            return len(seen)

        stopper = asyncio.ensure_future(stop())
        try:
            await asyncio.wait_for(turn(), timeout=_TURN_TIMEOUT)
            seen_at_stop = await asyncio.wait_for(stopper, timeout=_STOP_TIMEOUT)
        finally:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper

    # Really a foreground subagent: the spawning tool call blocks on it.
    assert task["data"].get("task_type") == "local_agent"
    assert task["data"].get("is_backgrounded") is False

    # The stop landed inside the turn and the task reported its end there.
    assert "killed" in _updated_statuses(seen, task["id"])
    assert set(_notified_statuses(seen, task["id"])) <= {"stopped"}

    # The leader's Agent call returns an error result instead of a report...
    agent_result = _leader_tool_result(seen, task["tool_use_id"])
    assert agent_result.is_error is True
    assert "interrupted" in _result_text(agent_result).lower()

    # ...and the turn carries on to a normal end after the stop.
    assert _leader_said(seen[seen_at_stop:], "LEADER-DONE")
    result = seen[-1]
    assert isinstance(result, ResultMessage)
    assert result.subtype == "success"
    assert result.is_error is False


@pytest.mark.parametrize(
    ("leader_tool", "task_type"),
    [
        (_agent(run_in_background=True), "local_agent"),
        (_BACKGROUND_BASH, "local_bash"),
    ],
    ids=["agent", "bash"],
)
async def test_stopping_a_background_task_between_turns(
    tmp_path, leader_tool, task_type
):
    async with _cli(tmp_path, leader_tool) as client:
        await client.query(f"{_LEADER_MARK} background")
        seen: List[Any] = []

        async def turn() -> None:
            async for message in client.receive_response():
                seen.append(message)

        await asyncio.wait_for(turn(), timeout=_TURN_TIMEOUT)
        started = [m for m in seen if isinstance(m, TaskStartedMessage)]
        assert len(started) == 1
        task = started[0]
        assert task.data.get("task_type") == task_type
        assert task.data.get("is_backgrounded") is True
        # The turn is over while the task still runs.
        assert isinstance(seen[-1], ResultMessage)
        assert not _ended(seen, task.task_id)

        after: List[Any] = []
        ready = asyncio.Event()
        ended = asyncio.Event()
        if task_type == "local_bash" or any(
            _is_subagent_bash(m, task.tool_use_id) for m in seen
        ):
            ready.set()

        async def pump() -> None:
            async for message in client.receive_messages():
                after.append(message)
                if _is_subagent_bash(message, task.tool_use_id):
                    ready.set()
                if _ended([message], task.task_id):
                    ended.set()

        reader = asyncio.ensure_future(pump())
        try:
            await asyncio.wait_for(ready.wait(), timeout=_STOP_TIMEOUT)

            # A wrong id is accepted silently and leaves the running task alone.
            await asyncio.wait_for(
                client.stop_task("task-that-never-existed"), timeout=_STOP_TIMEOUT
            )
            await asyncio.sleep(0.5)
            assert not ended.is_set()

            await asyncio.wait_for(
                client.stop_task(task.task_id), timeout=_STOP_TIMEOUT
            )
            await asyncio.wait_for(ended.wait(), timeout=_STOP_TIMEOUT)

            # Stopping it again is a silent no-op as well.
            await asyncio.wait_for(
                client.stop_task(task.task_id), timeout=_STOP_TIMEOUT
            )
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader

    assert "killed" in _updated_statuses(after, task.task_id)
    assert set(_notified_statuses(after, task.task_id)) <= {"stopped"}
