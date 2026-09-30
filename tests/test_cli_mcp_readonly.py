"""Pin how the bundled CLI schedules a parallel MCP batch, and prove ``readOnlyTools`` fixes it
(ChatDRAGON #471).

Pinned on CLI 2.1.283 with a fake Messages API whose first reply calls ``slow`` (3 s) and then
``fast`` (instant) in ONE assistant message, against a real MCP SDK streamable-http server whose
tools declare no annotations:

* direct, the CLI runs the batch **sequentially** — ``fast`` starts only after ``slow`` ends, so
  a light lookup waits behind a long research call (the #471 symptom);
* through the relay with ``readOnlyTools: "*"``, both tools carry ``readOnlyHint: true`` and
  the CLI runs them **concurrently** — ``fast`` starts while ``slow`` is still running;
* marking only ONE of the two keeps the batch sequential: concurrency needs every tool of the
  batch to be read-only. If a CLI bump changes any of this, revisit the relay rewrite.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import uvicorn
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import ToolResultBlock, UserMessage
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

from src import mcp_progress_relay
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 90
_SLOW_SECONDS = 3.0
_HARNESS_ENV = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"})


@pytest.fixture(autouse=True)
def _no_inherited_claude_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app: Any) -> tuple[int, uvicorn.Server]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    return port, server


@pytest.fixture
def batch_server():
    events: Dict[str, float] = {}
    mcp = FastMCP("probe")

    @mcp.tool()
    async def slow() -> str:
        """Long research call."""
        events["slow_start"] = time.monotonic()
        await asyncio.sleep(_SLOW_SECONDS)
        events["slow_end"] = time.monotonic()
        return "slow-done"

    @mcp.tool()
    async def fast() -> str:
        """Light lookup."""
        events["fast_start"] = time.monotonic()
        return "fast-done"

    port, server = _serve(mcp.streamable_http_app())
    yield f"http://127.0.0.1:{port}/mcp", events
    server.should_exit = True


@pytest.fixture
def relay_base(monkeypatch):
    app = FastAPI()
    app.include_router(mcp_progress_relay.router)
    port, server = _serve(app)
    monkeypatch.setenv("MCP_RELAY_BASE_URL", f"http://127.0.0.1:{port}")
    yield
    server.should_exit = True


def _plan(body: Dict[str, Any]) -> Dict[str, Any]:
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        ):
            return {"text": "finished"}
    return {"tool_uses": [{"name": "mcp__probe__slow"}, {"name": "mcp__probe__fast"}]}


async def _run(servers: Dict[str, Any], home: Path) -> List[str]:
    results: List[str] = []
    with FakeAnthropicAPI(_plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(home),
            mcp_servers=servers,
            allowed_tools=["mcp__probe__slow", "mcp__probe__fast"],
            cwd=str(home),
        )
        async with GatewayClaudeSDKClient(options=options) as client:
            await client.query("go")
            async for message in client.receive_response():
                if isinstance(message, UserMessage) and isinstance(message.content, list):
                    results += [str(b.content) for b in message.content if isinstance(b, ToolResultBlock)]
    return results


async def _run_through_relay(url: str, home: Path, read_only: Dict[str, frozenset]) -> List[str]:
    servers, handle = mcp_progress_relay.attach({"probe": {"type": "http", "url": url}}, read_only)
    assert handle is not None
    try:
        return await _run(servers, home)
    finally:
        mcp_progress_relay.detach(handle)


def _overlapped(events: Dict[str, float]) -> bool:
    return events["fast_start"] < events["slow_end"]


async def test_cli_runs_an_unannotated_mcp_batch_sequentially(batch_server, tmp_path):
    url, events = batch_server
    results = await asyncio.wait_for(_run({"probe": {"type": "http", "url": url}}, tmp_path), _TURN_TIMEOUT)
    assert any("slow-done" in r for r in results) and any("fast-done" in r for r in results)
    assert not _overlapped(events), (
        "the CLI now runs unannotated MCP tools concurrently — readOnlyTools may be unnecessary"
    )


async def test_read_only_tools_through_the_relay_run_concurrently(batch_server, relay_base, tmp_path):
    url, events = batch_server
    results = await asyncio.wait_for(
        _run_through_relay(url, tmp_path, {"probe": frozenset({"*"})}), _TURN_TIMEOUT
    )
    assert any("slow-done" in r for r in results) and any("fast-done" in r for r in results)
    assert _overlapped(events), "the light lookup must not wait behind the long call"
    assert events["fast_start"] - events["slow_start"] < _SLOW_SECONDS / 2


async def test_marking_only_one_tool_keeps_the_batch_sequential(batch_server, relay_base, tmp_path):
    url, events = batch_server
    await asyncio.wait_for(_run_through_relay(url, tmp_path, {"probe": frozenset({"fast"})}), _TURN_TIMEOUT)
    assert not _overlapped(events), "concurrency needs every tool of the batch to be read-only"
