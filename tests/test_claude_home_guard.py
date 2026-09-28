"""The always-on guard over the shared ``$HOME/.claude`` (#217 review).

Every gateway CLI child shares one HOME, so ``~/.claude/projects/*`` and
``~/.claude/plans`` hold every user's transcripts and plans. The guard must hold
with the workspace sandbox off (the default) and whatever
``WORKSPACE_SANDBOX_ALLOW_OUTSIDE`` says, while this session keeps its own
project state and the shared skills/plugins stay readable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.backends.claude.workspace_sandbox import (
    _cli_path_hash,
    _project_dir_name,
    make_claude_home_guard_hook,
)


async def _call(hook, tool, tool_input):
    return await hook({"tool_name": tool, "tool_input": tool_input}, None, None)


def _is_deny(result) -> bool:
    out = (result or {}).get("hookSpecificOutput", {})
    return out.get("permissionDecision") == "deny"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home" / "app"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_CLONE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", raising=False)
    return home


def _workspace(tmp_path: Path, name: str) -> Path:
    ws = tmp_path / "workspaces" / name
    ws.mkdir(parents=True)
    return ws


def _project(home: Path, ws: Path) -> Path:
    return home / ".claude" / "projects" / _project_dir_name(str(ws.resolve()))


@pytest.fixture
def other_state(tmp_path, home):
    other = _workspace(tmp_path, "bob")
    transcript = _project(home, other) / "sess.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"secret": "bob"}\n')
    plan = home / ".claude" / "plans" / "bobs-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# bob's plan\n")
    return transcript, plan


@pytest.mark.parametrize("allow_outside", [None, "read,write,bash"])
async def test_other_users_state_is_denied_with_the_sandbox_off(
    tmp_path, home, other_state, monkeypatch, allow_outside
):
    if allow_outside:
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", allow_outside)
    transcript, plan = other_state
    hook = make_claude_home_guard_hook(_workspace(tmp_path, "alice"))
    for tool, key, target in [
        ("Read", "file_path", transcript),
        ("Read", "file_path", plan),
        ("Grep", "path", home / ".claude" / "projects"),
        ("Glob", "path", home / ".claude" / "plans"),
        ("Glob", "pattern", f"{home}/.claude/projects/**/*.jsonl"),
        ("Write", "file_path", plan),
        ("Edit", "file_path", transcript),
    ]:
        result = await _call(hook, tool, {key: str(target)})
        assert _is_deny(result), (tool, target)
    # Bash is static defense in depth, not a boundary (see the guard's
    # docstring): these are the statically visible forms it does catch.
    for command in (
        "cat ~/.claude/plans/bobs-plan.md",
        f"cat {transcript}",
        "grep -r secret ~/.claude/projects",
        "ls ~/.claude",
        "cat $HOME/.claude/plans/bobs-plan.md",
        "cat ${HOME}/.claude/projects/*/sess.jsonl",
        "cat ~/.cl*/plans/*",
    ):
        assert _is_deny(await _call(hook, "Bash", {"command": command})), command


async def test_a_workspace_symlink_into_another_users_state_is_denied(
    tmp_path, home, other_state
):
    transcript, _plan = other_state
    ws = _workspace(tmp_path, "alice")
    (ws / "peek").symlink_to(transcript.parent)
    hook = make_claude_home_guard_hook(ws)
    assert _is_deny(await _call(hook, "Read", {"file_path": "peek/sess.jsonl"}))
    assert _is_deny(await _call(hook, "Bash", {"command": "cat peek/sess.jsonl"}))


async def test_own_state_and_shared_assets_stay_usable(tmp_path, home):
    ws = _workspace(tmp_path, "alice")
    own = _project(home, ws)
    hook = make_claude_home_guard_hook(ws)
    result = own / "sess" / "tool-results" / "big.json"
    memory = own / "memory" / "MEMORY.md"
    assert await _call(hook, "Read", {"file_path": str(result)}) == {}
    write = {"file_path": str(memory), "content": "x"}
    assert await _call(hook, "Write", write) == {}
    assert await _call(hook, "Bash", {"command": f"cat {result}"}) == {}
    skill = home / ".claude" / "skills" / "demo" / "SKILL.md"
    plugin = home / ".claude" / "plugins" / "cache" / "p" / "SKILL.md"
    assert await _call(hook, "Read", {"file_path": str(skill)}) == {}
    assert await _call(hook, "Read", {"file_path": str(plugin)}) == {}
    assert await _call(hook, "Bash", {"command": f"cat {skill}"}) == {}
    # Shared assets are every user's: read-only.
    write_skill = {"file_path": str(skill), "content": "x"}
    assert _is_deny(await _call(hook, "Write", write_skill))


async def test_paths_outside_claude_home_are_left_to_the_sandbox(tmp_path, home):
    hook = make_claude_home_guard_hook(_workspace(tmp_path, "alice"))
    assert await _call(hook, "Read", {"file_path": "/etc/hostname"}) == {}
    assert await _call(hook, "Read", {"file_path": "notes.md"}) == {}
    # The workspace's own .claude (plansDirectory) is not the shared state dir.
    plan = {"file_path": ".claude/plans/p.md", "content": "x"}
    assert await _call(hook, "Write", plan) == {}
    command = {"command": "ls /var/log/*.log && cat ~/.bashrc"}
    assert await _call(hook, "Bash", command) == {}


async def test_long_names_sharing_the_first_200_units_do_not_leak(tmp_path, home):
    """Past 200 units the CLI appends a hash; a shared prefix must grant nothing."""
    deep = tmp_path / "workspaces" / ("x" * 120) / ("y" * 120)
    alice, bob = deep / "alice", deep / "bob"
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    a, b = _project(home, alice), _project(home, bob)
    assert a.name[:200] == b.name[:200] and a != b
    for d in (a, b):
        d.mkdir(parents=True)
        (d / "sess.jsonl").write_text("{}")
    hook = make_claude_home_guard_hook(alice)
    assert await _call(hook, "Read", {"file_path": str(a / "sess.jsonl")}) == {}
    assert _is_deny(await _call(hook, "Read", {"file_path": str(b / "sess.jsonl")}))
    write = {"file_path": str(b / "x"), "content": "x"}
    assert _is_deny(await _call(hook, "Write", write))
    assert _is_deny(await _call(hook, "Bash", {"command": f"cat {b}/sess.jsonl"}))


@pytest.mark.parametrize(
    "value, digest",
    # Reference values from the CLI's own JS (``Math.abs(KJ(e)).toString(36)``).
    [("/tmp/x", "o2wb75"), ("/srv/워크스페이스/𝒳", "9fgtsy"), ("a" * 300, "rn408w")],
)
def test_hash_matches_the_cli(value, digest):
    assert _cli_path_hash(value) == digest


def test_encoding_counts_utf16_units_like_the_cli():
    # An astral character is two UTF-16 units, so two dashes.
    assert _project_dir_name("/a/𝒳") == "-a---"


def test_the_guard_is_wired_without_the_sandbox(monkeypatch):
    from unittest.mock import patch

    monkeypatch.delenv("WORKSPACE_SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv("USER_WORKSPACE_QUOTA_MB", raising=False)
    with patch("src.auth.validate_claude_code_auth") as validate:
        with patch("src.auth.auth_manager") as auth:
            validate.return_value = (True, {"method": "anthropic"})
            env = {"ANTHROPIC_AUTH_TOKEN": "k"}
            auth.get_claude_code_env_vars.return_value = env
            from src.backends.claude.client import ClaudeCodeCLI

            cli = ClaudeCodeCLI(cwd="/tmp")
    names = [
        getattr(h, "__qualname__", "")
        for m in cli._pre_tool_use_hooks(os.getcwd(), None)
        for h in m.hooks
    ]
    assert any("make_claude_home_guard_hook" in n for n in names), names
    assert not any("make_workspace_sandbox_hook" in n for n in names), names
