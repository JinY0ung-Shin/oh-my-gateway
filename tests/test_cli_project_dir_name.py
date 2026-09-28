"""Pin the CLI's ``projects/`` naming and the ``~/.claude`` guard on the bundled CLI.

The always-on guard grants a session exactly its own
``~/.claude/projects/<name>``. Past 200 UTF-16 units the CLI keeps the first 200
and appends a hash, so a prefix match would grant other users' directories
(#217 review): ``_project_dir_name`` reproduces the CLI instead, and this file
fails loudly if a CLI bump changes the naming. It also pins that the guard
holds under ``permission_mode="bypassPermissions"`` with the workspace sandbox
off.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

from src.backends.claude.workspace_sandbox import (
    _project_dir_name,
    make_claude_home_guard_hook,
)
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 60
_HARNESS_ENV = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"}
)


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    monkeypatch.delenv("WORKSPACE_SANDBOX_ENABLED", raising=False)


async def _one_turn(
    workspace: Path, home: Path, plan=None, **extra
) -> FakeAnthropicAPI:
    with FakeAnthropicAPI(plan=plan) as api:
        env = {**api.cli_env(home), "CLAUDE_CODE_HARBOR_KITE": "0"}
        # bypassPermissions refuses to start as root unless the host says it
        # is sandboxed; a test container often runs as root.
        env["IS_SANDBOX"] = "1"
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env=env,
            max_turns=3,
            session_id=str(uuid.uuid4()),
            **extra,
        )
        client = ClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
        try:

            async def run() -> None:
                await client.query("hello")
                async for _ in client.receive_response():
                    pass

            await asyncio.wait_for(run(), timeout=_TURN_TIMEOUT)
        finally:
            await client.disconnect()
    return api


@pytest.mark.parametrize("long", [False, True])
async def test_project_dir_name_matches_the_bundled_cli(tmp_path, long):
    workspace = tmp_path / "ws"
    if long:
        # > 200 encoded units, with Korean and an astral character.
        workspace = tmp_path / ("워크" * 40) / ("x" * 100) / "𝒳-alice"
    workspace.mkdir(parents=True)
    home = tmp_path / "home"
    await _one_turn(workspace, home)
    made = sorted(p.name for p in (home / ".claude" / "projects").iterdir())
    assert made == [_project_dir_name(str(workspace))], made
    assert (len(made[0]) > 200) is long


async def test_guard_denies_other_users_state_under_bypass(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspaces" / "alice"
    workspace.mkdir(parents=True)
    other = tmp_path / "workspaces" / "bob"
    canary = "BOB_TRANSCRIPT_" + uuid.uuid4().hex[:8]
    transcript = (
        home / ".claude" / "projects" / _project_dir_name(str(other)) / "s.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"secret": canary}) + "\n")

    def plan(body):
        messages = body.get("messages") or []
        if not body.get("tools") or not messages:
            return {"text": "ok"}
        last = messages[-1].get("content")
        if isinstance(last, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in last
        ):
            return {"text": "done"}
        return {"tool_use": {"name": "Read", "input": {"file_path": str(transcript)}}}

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        guard = make_claude_home_guard_hook(workspace)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    api = await _one_turn(
        workspace,
        home,
        plan=plan,
        permission_mode="bypassPermissions",
        hooks={"PreToolUse": [HookMatcher(matcher="", hooks=[guard])]},
    )
    results = [
        block
        for request in api.model_requests()
        for message in request["body"].get("messages") or []
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert results, "the Read never produced a tool result"
    text = json.dumps(results)
    assert canary not in text
    assert "shared Claude state directory" in text
