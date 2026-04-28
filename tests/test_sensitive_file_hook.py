"""Unit tests for the sensitive-file PreToolUse hook.

The hook (``ClaudeCodeCLI._make_sensitive_file_hook``) intercepts
Edit/Write/MultiEdit/NotebookEdit calls whose path matches a sensitive
pattern (``.claude/``, ``.env``, ssh keys, …) and surfaces them to the
user as an AskUserQuestion-shaped pause via the same
``session.pending_tool_call`` plumbing AskUserQuestion uses.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pytest

from src.backends.claude.client import ClaudeCodeCLI


# ---------------------------------------------------------------------------
# Path detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/tmp/workspaces/foo/.claude/MEMORY.md", True),
        ("/tmp/workspaces/foo/.claude/skills/x.md", True),
        ("/home/u/.env", True),
        ("/home/u/.env.local", True),
        ("/home/u/.ssh/known_hosts", True),
        ("/home/u/id_rsa", True),
        ("/home/u/id_ed25519.pub", True),
        ("/home/u/keys/server.pem", True),
        ("/home/u/credentials.json", True),
        ("/home/u/secrets.toml", True),
        ("/home/u/main.py", False),
        ("/tmp/normal.txt", False),
        ("/home/u/myproject/README.md", False),
        ("", False),
    ],
)
def test_is_sensitive_path(path: str, expected: bool):
    assert ClaudeCodeCLI._is_sensitive_path(path) is expected


def test_extract_sensitive_paths_file_path():
    paths = ClaudeCodeCLI._extract_sensitive_paths(
        {
            "file_path": "/tmp/workspaces/foo/.claude/MEMORY.md",
            "old_string": "a",
            "new_string": "b",
        }
    )
    assert paths == ["/tmp/workspaces/foo/.claude/MEMORY.md"]


def test_extract_sensitive_paths_notebook_path():
    paths = ClaudeCodeCLI._extract_sensitive_paths(
        {"notebook_path": "/home/u/.ssh/config", "cell": "..."}
    )
    assert paths == ["/home/u/.ssh/config"]


def test_extract_sensitive_paths_returns_empty_for_normal_paths():
    assert ClaudeCodeCLI._extract_sensitive_paths(
        {"file_path": "/tmp/normal.py"}
    ) == []
    assert ClaudeCodeCLI._extract_sensitive_paths({}) == []
    assert ClaudeCodeCLI._extract_sensitive_paths(None) == []


def test_sensitive_tools_matcher_includes_all_file_writers():
    matcher = ClaudeCodeCLI._SENSITIVE_FILE_TOOLS_MATCHER
    assert "Edit" in matcher
    assert "Write" in matcher
    assert "MultiEdit" in matcher
    assert "NotebookEdit" in matcher
    # Must use pipe-separated form per HookMatcher contract
    assert "|" in matcher


# ---------------------------------------------------------------------------
# Hook integration — uses a fake session that mimics the fields client.py
# touches.
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    session_id: str = "sess_test"
    pending_tool_call: Optional[Dict[str, Any]] = None
    input_event: Optional[asyncio.Event] = None
    input_response: Optional[str] = None
    stream_break_event: Optional[asyncio.Event] = field(default_factory=asyncio.Event)


def _make_hook():
    cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    session = _FakeSession()
    return cli._make_sensitive_file_hook(session), session


async def test_hook_passes_through_for_non_sensitive_tool():
    hook, session = _make_hook()
    result = await hook(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t1"},
        "t1",
        None,
    )
    assert result == {}
    assert session.pending_tool_call is None


async def test_hook_passes_through_for_non_sensitive_path():
    hook, session = _make_hook()
    result = await hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/main.py", "old_string": "a", "new_string": "b"},
            "tool_use_id": "t1",
        },
        "t1",
        None,
    )
    assert result == {}
    assert session.pending_tool_call is None
    assert not session.stream_break_event.is_set()


async def test_hook_auto_allows_when_extension_in_whitelist(monkeypatch):
    """Operator opts in via SENSITIVE_FILE_AUTO_ALLOW_EXTS=.md so a
    .claude/MEMORY.md edit skips the prompt entirely."""
    import src.backends.claude.client as client_mod

    monkeypatch.setattr(client_mod, "SENSITIVE_FILE_AUTO_ALLOW_EXTS", (".md",))

    hook, session = _make_hook()
    result = await hook(
        {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/workspaces/foo/.claude/MEMORY.md",
                "old_string": "a",
                "new_string": "b",
            },
            "tool_use_id": "tu_md",
        },
        "tu_md",
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    # No card was surfaced.
    assert session.pending_tool_call is None
    assert not session.stream_break_event.is_set()


async def test_hook_does_not_auto_allow_when_one_path_outside_whitelist(monkeypatch):
    """Mixed extensions with at least one non-whitelisted path still
    falls through to the prompt — auto-allow is all-or-nothing per call."""
    import src.backends.claude.client as client_mod

    monkeypatch.setattr(client_mod, "SENSITIVE_FILE_AUTO_ALLOW_EXTS", (".md",))

    hook, session = _make_hook()

    async def respond_deny():
        for _ in range(100):
            if session.input_event is not None:
                break
            await asyncio.sleep(0.001)
        session.input_response = "Deny"
        session.input_event.set()

    # NotebookEdit on a non-whitelisted sensitive path forces the prompt
    # even though the file_path looks like .md.
    result, _ = await asyncio.gather(
        hook(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "/home/u/.env",
                },
                "tool_use_id": "tu_env",
            },
            "tu_env",
            None,
        ),
        respond_deny(),
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hook_default_strict_no_auto_allow_for_md(monkeypatch):
    """With the env var unset (default), even .md edits still prompt —
    the strictest behaviour, matching the historical default."""
    import src.backends.claude.client as client_mod

    monkeypatch.setattr(client_mod, "SENSITIVE_FILE_AUTO_ALLOW_EXTS", ())

    hook, session = _make_hook()

    async def respond_allow():
        for _ in range(100):
            if session.input_event is not None:
                break
            await asyncio.sleep(0.001)
        session.input_response = "Allow"
        session.input_event.set()

    result, _ = await asyncio.gather(
        hook(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "/tmp/workspaces/foo/.claude/MEMORY.md",
                },
                "tool_use_id": "tu_md_strict",
            },
            "tu_md_strict",
            None,
        ),
        respond_allow(),
    )
    # Even though it's .md, no whitelist => prompt fired and we got Allow.
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


async def test_hook_parks_session_for_sensitive_path_and_allows():
    hook, session = _make_hook()

    async def respond_after_park():
        # Wait for the hook to set up the event, then deliver Allow.
        for _ in range(100):
            if session.input_event is not None:
                break
            await asyncio.sleep(0.001)
        assert session.input_event is not None
        assert session.pending_tool_call is not None
        assert session.pending_tool_call["name"] == "AskUserQuestion"
        questions = session.pending_tool_call["arguments"]["questions"]
        assert questions[0]["options"][0]["label"] == "Allow"
        session.input_response = "Allow"
        session.input_event.set()

    hook_task = asyncio.create_task(
        hook(
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "/tmp/workspaces/foo/.claude/MEMORY.md",
                    "old_string": "a",
                    "new_string": "b",
                },
                "tool_use_id": "tool_use_42",
            },
            "tool_use_42",
            None,
        )
    )
    responder_task = asyncio.create_task(respond_after_park())

    result, _ = await asyncio.gather(hook_task, responder_task)

    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert session.stream_break_event.is_set()
    # Hook should clear the parking state on the way out
    assert session.input_response is None
    assert session.input_event is None


async def test_hook_denies_when_user_says_deny():
    hook, session = _make_hook()

    async def respond_deny():
        for _ in range(100):
            if session.input_event is not None:
                break
            await asyncio.sleep(0.001)
        session.input_response = "Deny"
        session.input_event.set()

    result, _ = await asyncio.gather(
        hook(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/home/u/.env", "content": "X"},
                "tool_use_id": "tu_1",
            },
            "tu_1",
            None,
        ),
        respond_deny(),
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "/home/u/.env" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hook_denies_on_timeout(monkeypatch):
    # Patch the timeout to something tiny so the test runs fast.
    import src.backends.claude.client as client_mod

    monkeypatch.setattr(client_mod, "ASK_USER_TIMEOUT_SECONDS", 0)

    hook, session = _make_hook()
    result = await hook(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/workspaces/foo/.claude/MEMORY.md"},
            "tool_use_id": "tu_2",
        },
        "tu_2",
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "timed out" in result["hookSpecificOutput"]["permissionDecisionReason"].lower()
    # Session state should be cleared even on timeout
    assert session.pending_tool_call is None
    assert session.input_event is None


async def test_hook_treats_custom_text_as_deny_with_reason():
    """Anything that's not exactly 'Allow' (case-insensitive) should deny,
    but propagate the user's text as the reason so the model sees feedback."""
    hook, session = _make_hook()

    async def respond_custom():
        for _ in range(100):
            if session.input_event is not None:
                break
            await asyncio.sleep(0.001)
        session.input_response = "절대 안 됨, 다른 경로 써"
        session.input_event.set()

    result, _ = await asyncio.gather(
        hook(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/workspaces/x/.claude/MEMORY.md"},
                "tool_use_id": "tu_3",
            },
            "tu_3",
            None,
        ),
        respond_custom(),
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "절대 안 됨" in result["hookSpecificOutput"]["permissionDecisionReason"]


async def test_hook_allow_is_case_insensitive():
    hook, session = _make_hook()

    async def respond_lowercase_allow():
        for _ in range(100):
            if session.input_event is not None:
                break
            await asyncio.sleep(0.001)
        session.input_response = "allow"
        session.input_event.set()

    result, _ = await asyncio.gather(
        hook(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "/home/u/.env"},
                "tool_use_id": "tu_4",
            },
            "tu_4",
            None,
        ),
        respond_lowercase_allow(),
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
