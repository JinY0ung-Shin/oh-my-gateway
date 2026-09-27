"""Verbatim user-turn delivery (``client_composed``) and the slash-argument guard.

The CLI expands ``@<path>`` mentions in user text and inlines the file into the
upstream request — outside the workspace too, and past the workspace sandbox
hook, since no tool call is involved. The gateway therefore stamps every
non-slash turn ``client_composed`` (the field the SDK's ``verbatim_prompts``
option sets) so the CLI delivers it as written. A slash-command turn stays
unstamped so the CLI can dispatch it, and ``validate_prompt`` rejects
@-mentions in its arguments instead.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.backends.claude import slash_commands as sc
from src.session_manager import Session

PNG_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
}


def _make_cli():
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd="/tmp")


async def _empty_receive():
    return
    yield


async def _sent(prompt):
    """Run one turn on a mock client; return what ``client.query`` was given.

    A string means the plain ``query(str)`` path; a list is the streamed
    SDK input messages.
    """
    cli = _make_cli()
    client = AsyncMock()
    client.receive_response = _empty_receive
    client.receive_messages = _empty_receive  # backlog drain; no stray coroutine
    session = Session(session_id=f"sess-{uuid.uuid4().hex[:8]}")
    async for _ in cli.run_completion_with_client(client, prompt, session):
        pass
    client.query.assert_awaited_once()
    arg = client.query.await_args.args[0]
    if isinstance(arg, str):
        return arg
    return [message async for message in arg]


def _user_message(content, *, verbatim):
    message = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
    if verbatim:
        message["client_composed"] = True
    return message


# --- run_completion_with_client stamping -----------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "summarize @/etc/passwd",
        "look at @sub/data.txt",
        # Slash-prefixed plain input is not a command (issue #117) — verbatim.
        "/etc/passwd what is this",
        "",
    ],
)
async def test_plain_text_turn_is_sent_verbatim(prompt):
    assert await _sent(prompt) == [_user_message(prompt, verbatim=True)]


async def test_content_block_turn_is_sent_verbatim():
    blocks = [{"type": "text", "text": "what is in @/etc/passwd?"}, PNG_BLOCK]
    assert await _sent(blocks) == [_user_message(blocks, verbatim=True)]


async def test_slash_command_turn_is_not_stamped():
    # Exactly the pre-verbatim path: the CLI must see the raw command to
    # dispatch it.
    assert await _sent("/probe-skill do it") == "/probe-skill do it"


async def test_slash_command_block_turn_is_not_stamped():
    blocks = [{"type": "text", "text": "/probe-skill"}, PNG_BLOCK]
    assert await _sent(blocks) == [_user_message(blocks, verbatim=False)]


async def test_first_text_block_decides_slash_turn():
    blocks = [PNG_BLOCK, {"type": "text", "text": "/probe-skill"}]
    assert await _sent(blocks) == [_user_message(blocks, verbatim=False)]
    later = [{"type": "text", "text": "hi"}, {"type": "text", "text": "/probe-skill"}]
    assert await _sent(later) == [_user_message(later, verbatim=True)]


@pytest.mark.parametrize("final", ["/clear", "/probe-skill @/etc/passwd", "@/etc/x"])
async def test_stateless_transcript_is_always_verbatim(final):
    from src.agent_message_models import AgentMessagesRequest
    from src.routes.agent_messages import _render_transcript

    body = AgentMessagesRequest(
        messages=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": final},
        ]
    )
    prompt = _render_transcript(body)
    assert await _sent(prompt) == [_user_message(prompt, verbatim=True)]


# --- validate_prompt: @-mentions in slash-command arguments ------------------


@pytest.fixture
def known_x(monkeypatch):
    sc._cache.reset()
    calls = {"n": 0}

    async def _fake_fetch(cwd):
        calls["n"] += 1
        return {"x", "compact"}

    monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)
    yield calls
    sc._cache.reset()


@pytest.mark.parametrize(
    "prompt",
    [
        "/x @/etc/passwd",
        "/x @sub/f",
        "/x @~/.ssh/id_rsa",
        '/x @"quoted path.txt"',
        "/x summarize\n@notes.md",
        "  /x arg @./rel",
    ],
)
async def test_slash_argument_mention_is_rejected(known_x, prompt):
    with pytest.raises(sc.SlashCommandError) as exc:
        await sc.validate_prompt(prompt)
    assert exc.value.code == "unsupported_argument"
    # Rejected before command discovery spawns a CLI.
    assert known_x["n"] == 0


@pytest.mark.parametrize(
    "prompt",
    ["/x", "/x plain args", "/x mail user@example.com", "/x a@b c@d", "/x @"],
)
async def test_slash_arguments_without_mentions_pass(known_x, prompt):
    await sc.validate_prompt(prompt)


async def test_blocked_command_wins_over_argument_guard(known_x):
    with pytest.raises(sc.SlashCommandError) as exc:
        await sc.validate_prompt("/compact @/etc/passwd")
    assert exc.value.code == "blocked_command"


async def test_plain_text_mention_is_not_a_slash_error(known_x):
    # Non-slash turns are delivered verbatim instead; nothing to reject.
    await sc.validate_prompt("summarize @/etc/passwd")
    assert known_x["n"] == 0


async def test_responses_route_maps_argument_guard_to_400(known_x):
    from src.routes.responses import _validate_backend_prompt

    with pytest.raises(HTTPException) as exc:
        await _validate_backend_prompt(
            SimpleNamespace(backend="claude"), "/x @/etc/passwd", ""
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error"]["code"] == "unsupported_argument"


# --- real bundled CLI -------------------------------------------------------


@pytest.mark.integration
async def test_real_cli_verbatim_turn_not_expanded_slash_still_dispatches(tmp_path):
    from claude_agent_sdk import ClaudeAgentOptions

    from src.backends.claude.sdk_client import GatewayClaudeSDKClient
    from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

    canary = "OUTSIDE_CANARY_" + uuid.uuid4().hex[:8]
    skill_marker = "SKILL_BODY_" + uuid.uuid4().hex[:8]
    outside = tmp_path / "outside.txt"
    outside.write_text(canary + "\n")
    workspace = tmp_path / "ws"
    skill_dir = workspace / ".claude" / "skills" / "probe-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: probe skill\n---\n"
        f"{skill_marker} do the thing.\n"
    )

    cli = _make_cli()
    session = Session(session_id=str(uuid.uuid4()))
    with FakeAnthropicAPI() as api:
        env = {**api.cli_env(tmp_path / "home"), "CLAUDE_CODE_HARBOR_KITE": "0"}
        client = GatewayClaudeSDKClient(
            options=ClaudeAgentOptions(
                cwd=str(workspace),
                model="claude-sonnet-5",
                env=env,
                setting_sources=["project"],
                skills="all",
                max_turns=2,
                session_id=session.session_id,
            )
        )
        await client.connect(prompt=None)
        try:

            async def gateway_turn(prompt):
                before = len(api.requests)
                async for _ in cli.run_completion_with_client(client, prompt, session):
                    pass
                return json.dumps([r["body"] for r in api.requests[before:]])

            assert canary not in await gateway_turn(f"summarize @{outside}")
            assert skill_marker in await gateway_turn("/probe-skill")

            # Control, last so its inlined file cannot leak into the turns
            # above: unstamped, the same mention IS expanded — the canary
            # assertion is not vacuous.
            before = len(api.requests)
            await client.query(f"summarize @{outside}")
            async for _ in client.receive_response():
                pass
            assert canary in json.dumps([r["body"] for r in api.requests[before:]])
        finally:
            await client.disconnect()
