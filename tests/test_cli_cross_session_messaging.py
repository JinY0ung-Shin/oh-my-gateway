"""Pin the bundled CLI's cross-session messaging gate (see ``src/constants.py``).

Claude CLI 2.1.224+ binds a peer-inbox socket in every child and offers the
model ``ListAgents`` plus cross-session ``SendMessage``. Every gateway child
shares one config dir, so with the gate on, one user's agent can discover and
inject turns into another user's live session. ``src.constants`` defaults the
gate env var to ``0`` in the process env; these tests run the real bundled CLI
against a fake Messages API (zero cost) and assert both directions:

* as the gateway leaves the env — no socket, no ``ListAgents``;
* with the gate forced on — socket and ``ListAgents`` are back, proving the env
  var is what turns the feature off.

If an SDK bump renames the undocumented gate, the first test fails; if the CLI
drops the feature, the second one does. Either way the pin is revisited instead
of silently protecting nothing.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, Optional, Tuple

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.constants import (
    CROSS_SESSION_MESSAGING_ENV,
    _ensure_cross_session_messaging_off,
)
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration


async def _first_turn(
    tmp_path, extra_env: Dict[str, str]
) -> Tuple[Optional[Dict[str, Any]], set]:
    """Run one turn; return the init frame's data and the tools offered upstream."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Keep any peer socket the CLI binds inside the test's own tmp dir.
    runtime_dir = tmp_path / "run"
    runtime_dir.mkdir(mode=0o700)
    with FakeAnthropicAPI() as api:
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-5",
            env={
                **api.cli_env(tmp_path / "home"),
                "XDG_RUNTIME_DIR": str(runtime_dir),
                **extra_env,
            },
            max_turns=1,
            session_id=str(uuid.uuid4()),
        )
        init = None

        async def turn() -> None:
            nonlocal init
            async with ClaudeSDKClient(options=options) as client:
                await client.query("hello")
                async for message in client.receive_response():
                    if getattr(message, "subtype", None) == "init":
                        init = message.data

        # No pytest-timeout in this repo: bound the real CLI ourselves.
        await asyncio.wait_for(turn(), timeout=90)
        offered = {
            tool.get("name")
            for request in api.model_requests()
            for tool in request["body"]["tools"]
        }
    return init, offered


def test_gateway_process_env_defaults_the_gate_off():
    # Effectively off: the gateway default "0" or any explicit non-truthy value.
    value = (os.environ.get(CROSS_SESSION_MESSAGING_ENV) or "").strip().lower()
    assert value not in {"", "1", "true", "yes", "on"}


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_blank_gate_is_normalized_off(monkeypatch, value):
    """A blank value reads as unset to the CLI, i.e. ON — normalize it too."""
    if value is None:
        monkeypatch.delenv(CROSS_SESSION_MESSAGING_ENV, raising=False)
    else:
        monkeypatch.setenv(CROSS_SESSION_MESSAGING_ENV, value)
    _ensure_cross_session_messaging_off()
    assert os.environ[CROSS_SESSION_MESSAGING_ENV] == "0"


@pytest.mark.parametrize("value", ["1", "0", "false", "on"])
def test_explicit_operator_value_is_kept(monkeypatch, value):
    monkeypatch.setenv(CROSS_SESSION_MESSAGING_ENV, value)
    _ensure_cross_session_messaging_off()
    assert os.environ[CROSS_SESSION_MESSAGING_ENV] == value


async def test_cli_child_has_no_peer_socket_or_list_agents(tmp_path):
    # Inherit the gateway's process env untouched — the production path.
    init, offered = await _first_turn(tmp_path, {})
    assert init is not None
    assert init.get("messaging_socket_path") is None
    assert "ListAgents" not in (init.get("tools") or [])
    assert "ListAgents" not in offered


async def test_forcing_the_gate_on_restores_the_feature(tmp_path):
    init, offered = await _first_turn(tmp_path, {CROSS_SESSION_MESSAGING_ENV: "1"})
    assert init is not None
    assert init.get("messaging_socket_path")
    assert "ListAgents" in offered


async def test_a_blank_gate_reads_as_on_to_the_cli(tmp_path):
    """Why blank must be normalized: the CLI treats it exactly like unset."""
    init, offered = await _first_turn(tmp_path, {CROSS_SESSION_MESSAGING_ENV: ""})
    assert init is not None
    assert init.get("messaging_socket_path")
    assert "ListAgents" in offered
