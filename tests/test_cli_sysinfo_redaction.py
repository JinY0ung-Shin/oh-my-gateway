"""Pin host/secret redaction against the real bundled CLI (CLI 2.1.283).

The gateway installs a ``PostToolUse`` hook (``sysinfo_redaction``) whose
``updatedToolOutput`` replaces a successful tool result before the CLI hands
it to the model. These tests drive the real CLI against the fake Messages API
and read the ``tool_result`` the CLI sent upstream, so a CLI bump that stops
honouring the rewrite — or starts falling back to the original — fails here.

They also pin the known limit: a failed tool call goes through
``PostToolUseFailure``, which cannot rewrite the result. When a CLI bump makes
that test fail, the failure path became redactable: extend the hook to it and
flip the assertion.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.backends.claude import sysinfo_redaction
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

LEAK = "leakhost-zz9q"
_TURN_TIMEOUT = 90
_HARNESS_ENV = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"})

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("_isolated_cli_env")]


@pytest.fixture
def _isolated_cli_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    monkeypatch.setenv("SYSINFO_REDACTION", "true")
    monkeypatch.setenv("SYSINFO_REDACT_VALUES", LEAK)
    sysinfo_redaction.reset_redactor_cache()
    yield
    sysinfo_redaction.reset_redactor_cache()


def _gateway_post_tool_use_hooks():
    """The exact matchers ``create_client`` installs."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {}
            from src.backends.claude.client import ClaudeCodeCLI

            hooks = ClaudeCodeCLI._post_tool_use_hooks()
    assert hooks, "redaction enabled but no PostToolUse hook installed"
    return hooks


def _tool_results(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        block
        for message in body.get("messages", [])
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


async def _run(tmp_path: Path, plan, *, extra_env=None, hooks=True, tools=None):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "notes.txt").write_text(f"server is {LEAK}\nsecond line\n")
    stream_results: List[Any] = []
    with FakeAnthropicAPI(plan) as api:
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env={
                **api.cli_env(tmp_path / "home"),
                "CLAUDE_CODE_HARBOR_KITE": "0",
                **(extra_env or {}),
            },
            permission_mode="default",
            allowed_tools=tools or ["Bash", "Read", "Grep", "Glob", "Agent", "Task"],
            max_turns=6,
            session_id=str(uuid.uuid4()),
            hooks={"PostToolUse": _gateway_post_tool_use_hooks()} if hooks else None,
        )
        client = ClaudeSDKClient(options=options)
        await asyncio.wait_for(client.connect(), timeout=_TURN_TIMEOUT)
        try:

            async def turn() -> None:
                await client.query("go")
                async for message in client.receive_response():
                    for block in getattr(message, "content", None) or []:
                        if type(block).__name__ == "ToolResultBlock":
                            stream_results.append(block.content)

            await asyncio.wait_for(turn(), timeout=_TURN_TIMEOUT)
        finally:
            await client.disconnect()
        requests = api.model_requests()
    assert requests, "the CLI never reached the fake Messages API"
    return requests, stream_results


def _single_tool_plan(name: str, tool_input: Dict[str, Any]):
    def plan(body):
        if not _tool_results(body):
            return {"tool_use": {"name": name, "input": tool_input}}
        return {"text": "done"}

    return plan


def _model_tool_result(requests) -> str:
    results = _tool_results(requests[-1]["body"])
    assert results, "no tool_result reached the model"
    return json.dumps([r.get("content") for r in results], ensure_ascii=False)


@pytest.mark.parametrize(
    "name,tool_input",
    [
        ("Bash", {"command": f"echo {LEAK}", "description": "echo"}),
        ("Bash", {"command": f"echo {LEAK} 1>&2", "description": "stderr only"}),
        (
            "Bash",
            {
                "command": f"python3 -c \"print('{LEAK} ' + 'x' * 100000 + ' {LEAK}')\"",
                "description": "large output",
            },
        ),
        ("Bash", {"command": f"printf %s {LEAK}", "description": "no newline"}),
        ("Read", {"file_path": "notes.txt"}),
        ("Grep", {"pattern": "server", "output_mode": "content"}),
    ],
    ids=["bash-stdout", "bash-stderr", "bash-large", "bash-no-eol", "read", "grep"],
)
async def test_successful_tool_result_reaches_the_model_redacted(
    tmp_path, name, tool_input
):
    requests, stream_results = await _run(tmp_path, _single_tool_plan(name, tool_input))
    seen = _model_tool_result(requests)
    assert LEAK not in seen, seen[:400]
    assert "[REDACTED:custom]" in seen, seen[:400]
    # The SDK stream carries the same rewritten result.
    assert LEAK not in json.dumps(stream_results, default=str)


async def test_oversized_result_spill_file_is_read_back_redacted(tmp_path):
    """A result too large for the context is spilled to a file for the model to
    read later. The CLI writes the RAW output there (before the hook rewrite),
    so the guarantee is on the read-back: reading the spill file is a tool call
    like any other and comes back redacted."""
    spill_re = re.compile(r"saved to: (\S+)")

    def plan(body):
        results = _tool_results(body)
        if not results:
            return {
                "tool_use": {
                    "name": "Bash",
                    "input": {
                        "command": f"python3 -c \"print('x' * 100000 + ' {LEAK}')\"",
                        "description": "large",
                    },
                }
            }
        if len(results) == 1:
            content = results[0].get("content")
            text = content if isinstance(content, str) else json.dumps(content)
            match = spill_re.search(text)
            assert match, "output was not large enough to spill"
            path = match.group(1)
            return {
                "tool_use": {
                    "name": "Read",
                    "input": {"file_path": path, "offset": 1, "limit": 5},
                }
            }
        return {"text": "done"}

    requests, _ = await _run(tmp_path, plan)
    results = _tool_results(requests[-1]["body"])
    assert len(results) == 2, results
    spilled = list((tmp_path / "home" / ".claude").rglob("tool-results/*"))
    assert spilled and LEAK in spilled[0].read_text(), "spill file layout changed"
    read_back = json.dumps(results[1].get("content"))
    assert LEAK not in read_back
    assert "[REDACTED:custom]" in read_back, read_back[-300:]


async def test_glob_file_names_are_redacted(tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / f"{LEAK}.log").write_text("x")
    requests, _ = await _run(tmp_path, _single_tool_plan("Glob", {"pattern": "*.log"}))
    seen = _model_tool_result(requests)
    assert LEAK not in seen and "[REDACTED:custom]" in seen, seen


async def test_control_without_the_hook_the_value_reaches_the_model(tmp_path):
    """Proves the assertions above can fail: no hook, raw value upstream."""
    plan = _single_tool_plan("Bash", {"command": f"echo {LEAK}", "description": "e"})
    requests, _ = await _run(tmp_path, plan, hooks=False)
    assert LEAK in _model_tool_result(requests)


async def test_foreground_subagent_tool_results_are_redacted(tmp_path):
    def is_sub(body):
        return "SUBTASK" in json.dumps(body["messages"][0]["content"])

    def plan(body):
        done = bool(_tool_results(body))
        if is_sub(body):
            if not done:
                return {
                    "tool_use": {
                        "name": "Bash",
                        "input": {"command": f"echo {LEAK}", "description": "e"},
                    }
                }
            return {"text": "sub done"}
        if not done:
            return {
                "tool_use": {
                    "name": "Agent",
                    "input": {
                        "description": "d",
                        "prompt": "SUBTASK run it",
                        "subagent_type": "general-purpose",
                        "run_in_background": False,
                    },
                }
            }
        return {"text": "main done"}

    requests, _ = await _run(tmp_path, plan)
    sub_results = [
        json.dumps(r.get("content"))
        for req in requests
        if is_sub(req["body"])
        for r in _tool_results(req["body"])
    ]
    assert sub_results, "the subagent never ran its tool"
    assert all(LEAK not in s for s in sub_results), sub_results
    assert any("[REDACTED:custom]" in s for s in sub_results), sub_results


async def test_gateway_secrets_are_blank_in_the_agent_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret-value-123")
    monkeypatch.setenv("API_KEY", "public-api-secret-456")
    plan = _single_tool_plan(
        "Bash",
        {"command": 'echo "A=[$ADMIN_API_KEY] P=[$API_KEY]"', "description": "env"},
    )
    requests, _ = await _run(
        tmp_path, plan, extra_env=sysinfo_redaction.child_env_mask(), hooks=False
    )
    seen = _model_tool_result(requests)
    assert "A=[] P=[]" in seen, seen


async def test_known_limit_failed_tool_output_is_not_rewritten(tmp_path):
    """PostToolUseFailure cannot rewrite results (CLI 2.1.283).

    If this starts failing, the CLI began honouring a rewrite on the failure
    path: route the redactor there too and invert this test.
    """
    plan = _single_tool_plan(
        "Bash", {"command": f"echo {LEAK}; exit 3", "description": "fails"}
    )
    requests, _ = await _run(tmp_path, plan)
    results = _tool_results(requests[-1]["body"])
    assert results and results[0].get("is_error") is True
    assert LEAK in json.dumps(results[0].get("content"))


async def test_gateway_turn_end_to_end(tmp_path, monkeypatch):
    """The whole gateway path on the real CLI: hook, child env, and the
    outbound stream — including a value the model streams split across deltas."""
    from claude_agent_sdk import ClaudeAgentOptions

    from src.backends.claude.sdk_client import GatewayClaudeSDKClient
    from src.session_manager import Session

    admin = "admin-e2e-secret-value-77"
    monkeypatch.setenv("ADMIN_API_KEY", admin)
    sysinfo_redaction.reset_redactor_cache()
    halves = (LEAK[:5], LEAK[5:])

    def plan(body):
        if not _tool_results(body):
            return {
                "tool_use": {
                    "name": "Bash",
                    "input": {
                        "command": f'echo {LEAK}; echo "admin=[$ADMIN_API_KEY]"',
                        "description": "probe",
                    },
                }
            }
        return {"text_chunks": ["The host is ", halves[0], halves[1], " and done."]}

    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {}
            from src.backends.claude.client import ClaudeCodeCLI

            cli = ClaudeCodeCLI(cwd=str(tmp_path))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = Session(session_id=str(uuid.uuid4()))
    with FakeAnthropicAPI(plan) as api:
        client = GatewayClaudeSDKClient(
            options=ClaudeAgentOptions(
                cwd=str(workspace),
                model="claude-sonnet-5",
                env={
                    **api.cli_env(tmp_path / "home"),
                    "CLAUDE_CODE_HARBOR_KITE": "0",
                    **sysinfo_redaction.child_env_mask(),
                },
                permission_mode="default",
                allowed_tools=["Bash"],
                include_partial_messages=True,
                max_turns=4,
                session_id=session.session_id,
                hooks={"PostToolUse": cli._post_tool_use_hooks()},
            )
        )
        await client.connect(prompt=None)
        try:

            async def consume():
                return [c async for c in cli.run_completion_with_client(client, "go", session)]

            chunks = await asyncio.wait_for(consume(), timeout=_TURN_TIMEOUT)
        finally:
            await client.disconnect()
        requests = api.model_requests()

    seen = _model_tool_result(requests)
    assert LEAK not in seen and "[REDACTED:custom]" in seen, seen
    assert "admin=[]" in seen, seen

    dumped = repr(chunks)
    assert LEAK not in dumped
    assert admin not in dumped
    streamed = "".join(
        c["event"]["delta"].get("text", "")
        for c in chunks
        if isinstance(c, dict)
        and c.get("type") == "stream_event"
        and c["event"].get("type") == "content_block_delta"
        and c["event"]["delta"].get("type") == "text_delta"
    )
    assert streamed == "The host is [REDACTED:custom] and done.", streamed
    # Control: the raw value really was split across deltas upstream.
    assert all(LEAK not in piece for piece in halves)
