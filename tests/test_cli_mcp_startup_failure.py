"""Pin what the bundled CLI reports for an MCP server that dies while starting (#246).

Pinned on CLI 2.1.283 against the fake Messages API: a stdio server that exits at once and
one that exits only after a delay both end up as ``mcp_servers[]`` entries with
``status: "failed"`` in the turn's ``init`` event — the CLI waits for every server before it
emits ``init``, so a slow failure is not left ``pending`` — and ``mcp_server_errors`` stays
empty (that field is config validation only). The gateway's only trace of such a server is
the warning ``_log_init_mcp_server_errors`` derives from that status. If a CLI bump emits
``init`` before the servers settle, or starts carrying the stderr, revisit that log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import SystemMessage

from src.backends.claude.client import ClaudeCodeCLI
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 90
_SLOW_SECONDS = 2
_HARNESS_ENV = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"})


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


def _dies(after: int) -> Dict[str, Any]:
    script = f"sleep {after}; echo 'error: Failed to discover managed Python' >&2; exit 2"
    return {"type": "stdio", "command": "sh", "args": ["-c", script]}


async def _init_data(home: Path) -> tuple[Dict[str, Any], float]:
    with FakeAnthropicAPI(lambda body: {"text": "done"}) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(home),
            mcp_servers={"dies": _dies(0), "slowdie": _dies(_SLOW_SECONDS)},
            cwd=str(home),
        )
        started = time.monotonic()
        async with GatewayClaudeSDKClient(options=options) as client:
            await client.query("go")
            data: Dict[str, Any] = {}
            elapsed = 0.0
            async for message in client.receive_response():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    data = message.data
                    elapsed = time.monotonic() - started
    return data, elapsed


async def test_a_server_that_dies_while_starting_is_reported_failed(tmp_path, caplog):
    data, elapsed = await asyncio.wait_for(_init_data(tmp_path), _TURN_TIMEOUT)

    statuses = {s["name"]: s["status"] for s in data["mcp_servers"]}
    assert statuses == {"dies": "failed", "slowdie": "failed"}
    assert elapsed >= _SLOW_SECONDS  # init waited for the slow one
    assert not data.get("mcp_server_errors")

    with caplog.at_level(logging.WARNING):
        ClaudeCodeCLI._log_init_mcp_server_errors({"subtype": "init", "data": data})
    assert "MCP server 'dies' failed to start" in caplog.text
    assert "MCP server 'slowdie' failed to start" in caplog.text
