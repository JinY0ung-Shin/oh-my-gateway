"""Pin how the bundled CLI reads the blanks the gateway uses for isolation.

The SDK merges ``ClaudeAgentOptions.env`` over the inherited process env and has
no way to remove a variable, so ``_configure_sdk_env`` blanks the credentials a
Claude child must not get (``OPENAI_API_KEY``; in api_key auth mode also a
leftover ``CLAUDE_CODE_OAUTH_TOKEN``) instead of popping them from ``os.environ``
around ``connect()`` (see ``tests/test_sdk_env_race.py``). These tests run the
real bundled CLI against a fake Messages API (zero cost), with the real values
still in the process env as in production (except the unset baseline). What
each one pins (CLI 2.1.283):

* ``test_blank_token_reads_as_unset`` — with no other credential, a blank token
  ends the turn exactly like an unset one: an error, "Not logged in", and no
  request at all (so no blank bearer either).
* ``test_set_token_is_a_credential`` — the control: the same variable set to a
  value does log in, so the CLI reads it and the pin above is not vacuous.
* ``test_blanks_reach_the_cli_and_its_bash`` — the gateway's api_key setup: the
  blanks override the inherited values in the CLI process itself (its environ,
  which the agent's Bash can read at ``/proc/$PPID/environ``) and in that Bash
  (``O=[]``). ``C=[]`` alone proves nothing: the CLI strips the OAuth token from
  its Bash children on its own. The bearer check is a smoke check only: with a
  custom base URL ``ANTHROPIC_AUTH_TOKEN`` already beats a set OAuth token, so it
  passes whether a blank counts as set or not.

If a CLI bump starts treating a blank token as set, the first test fails: find
another way to keep the token out of the child before shipping that bump.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

OPENAI_SECRET = "sk-openai-pin-sentinel"
OAUTH_SECRET = "oauth-pin-sentinel"
# The agent's own view, then the CLI process's environ (the Bash tool's parent).
SHELL_PROBE = (
    'printf "bash O=[%s] C=[%s]\\n" "$OPENAI_API_KEY" "$CLAUDE_CODE_OAUTH_TOKEN"; '
    'printf "parent %s\\n" "$(cat /proc/$PPID/comm)"; '
    'tr "\\0" "\\n" < /proc/$PPID/environ '
    '| grep -E "^(OPENAI_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)=" | sed "s/^/cli /"'
)


def _bash_then_done(body: Dict[str, Any]) -> Dict[str, Any]:
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        ):
            return {"text": "done"}
    return {"tool_use": {"name": "Bash", "input": {"command": SHELL_PROBE}}}


async def _run(root, api: FakeAnthropicAPI, env: Dict[str, str], **options: Any):
    """Run one turn in ``root/ws``; return its ResultMessage."""
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    result = None

    async def turn() -> None:
        nonlocal result
        client_options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env=env,
            session_id=str(uuid.uuid4()),
            **options,
        )
        async with ClaudeSDKClient(options=client_options) as client:
            await client.query("hello")
            async for message in client.receive_response():
                if type(message).__name__ == "ResultMessage":
                    result = message

    # No pytest-timeout in this repo: bound the real CLI ourselves.
    await asyncio.wait_for(turn(), timeout=90)
    return result


def _headers(api: FakeAnthropicAPI) -> List[Dict[str, str]]:
    return [{k.lower(): v for k, v in r["headers"].items()} for r in api.requests]


def _tool_output_lines(api: FakeAnthropicAPI) -> Set[str]:
    """Every line of every tool result the CLI sent back upstream."""
    lines: Set[str] = set()
    for request in api.requests:
        for message in request["body"].get("messages") or []:
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                inner = block.get("content")
                if isinstance(inner, str):
                    lines.update(inner.splitlines())
                    continue
                for part in inner or []:
                    if isinstance(part, dict):
                        lines.update(str(part.get("text", "")).splitlines())
    return lines


@pytest.fixture
def leaky_process_env(monkeypatch):
    """The gateway process env still carries both credentials."""
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_SECRET)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_SECRET)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


async def _turn_without_credentials(
    root, monkeypatch, token: Optional[str]
) -> Tuple[Any, List[Dict[str, str]]]:
    """One turn with no other credential; a ``None`` *token* means unset.

    A blank or set *token* rides options.env over the real token left in the
    process env, the way the gateway passes it.
    """
    if token is None:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_SECRET)
    with FakeAnthropicAPI() as api:
        env = api.cli_env(root / "home")
        for key in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            env.pop(key)
        if token is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        result = await _run(root, api, env, max_turns=1)
        headers = _headers(api)
    return result, headers


async def test_blank_token_reads_as_unset(tmp_path, leaky_process_env, monkeypatch):
    outcomes = {}
    for case, token in (("unset", None), ("blank", "")):
        result, headers = await _turn_without_credentials(
            tmp_path / case, monkeypatch, token
        )
        assert result is not None, case
        outcomes[case] = (result.is_error, result.result, len(headers))

    assert outcomes["blank"] == outcomes["unset"]
    is_error, text, requests = outcomes["unset"]
    assert is_error and "Not logged in" in (text or "")
    assert requests == 0  # no request, so no blank bearer either


async def test_set_token_is_a_credential(tmp_path, leaky_process_env, monkeypatch):
    result, headers = await _turn_without_credentials(
        tmp_path, monkeypatch, OAUTH_SECRET
    )

    assert result is not None and not result.is_error
    assert headers
    assert all(
        sent.get("authorization") == f"Bearer {OAUTH_SECRET}" for sent in headers
    )


async def test_blanks_reach_the_cli_and_its_bash(tmp_path, leaky_process_env):
    with FakeAnthropicAPI(plan=_bash_then_done) as api:
        env = api.cli_env(tmp_path / "home")
        env.pop("CLAUDE_CODE_OAUTH_TOKEN")  # the fixture's own blank: test ours
        # The blanks _configure_sdk_env adds in api_key auth mode.
        env.update(OPENAI_API_KEY="", CLAUDE_CODE_OAUTH_TOKEN="")
        result = await _run(
            tmp_path, api, env, max_turns=3, permission_mode="bypassPermissions"
        )
        headers = _headers(api)
        lines = _tool_output_lines(api)
        bodies = json.dumps([r["body"] for r in api.requests])

    assert result is not None and not result.is_error
    parent = sorted(line for line in lines if line.startswith("parent ")) or "none"
    assert {"cli OPENAI_API_KEY=", "cli CLAUDE_CODE_OAUTH_TOKEN="} <= lines, (
        f"Bash's parent should be the CLI process, got {parent}: a wrapper in "
        "between means /proc/$PPID/environ is no longer the CLI's own env. "
        f"Tool output: {sorted(lines)}"
    )
    assert "bash O=[] C=[]" in lines
    assert OPENAI_SECRET not in bodies and OAUTH_SECRET not in bodies
    # Smoke only (see the module docstring): the gateway's bearer still goes out.
    assert len(headers) >= 2  # the Bash call, then its result
    for sent in headers:
        assert sent.get("authorization") == "Bearer sk-fake-test"
        assert "x-api-key" not in sent
        assert "oauth" not in sent.get("anthropic-beta", "")
