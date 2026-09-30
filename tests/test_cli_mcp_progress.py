"""Pin MCP progress against the bundled CLI, and prove the gateway relay recovers it (ChatDRAGON #489).

Pinned on CLI 2.1.283, driving the real CLI against a fake Messages API and a real MCP SDK
streamable-http server whose tool reports progress three times:

* the CLI opts in — ``tools/call`` carries ``_meta.progressToken`` and
  ``_meta["claudecode/toolUseId"]`` (the id of the tool_use block);
* but it does **not** forward ``notifications/progress`` into its SDK output: no
  ``tool_progress`` frame of the call carries the server's progress text. If a CLI bump starts
  forwarding it, this pin fails — then prefer the CLI's frame and retire the relay;
* routed through ``src.mcp_progress_relay`` the same call yields every progress update, keyed
  to the right tool_use_id, and the tool result is unchanged.
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
from claude_agent_sdk.types import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP

from src import mcp_progress_relay
from src.backends.claude.sdk_client import GatewayClaudeSDKClient, ToolProgressMessage
from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

pytestmark = pytest.mark.integration

_TURN_TIMEOUT = 90
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
def progress_server():
    seen_meta: List[Dict[str, Any]] = []
    mcp = FastMCP("probe")

    @mcp.tool()
    async def research(ctx: Context) -> str:
        """Slow research tool that reports progress."""
        meta = ctx.request_context.meta
        seen_meta.append(meta.model_dump() if meta else {})
        for step in (1, 2, 3):
            await ctx.report_progress(step, 50, f"실행 중: step{step}")
            await asyncio.sleep(0.6)
        return "research-done"

    port, server = _serve(mcp.streamable_http_app())
    yield f"http://127.0.0.1:{port}/mcp", seen_meta
    server.should_exit = True


def _plan(body: Dict[str, Any]) -> Dict[str, Any]:
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in content
        ):
            return {"text": "finished"}
    return {"tool_use": {"name": "mcp__probe__research", "input": {}}}


async def _run(url: str, home: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {"tool_use_ids": [], "results": [], "frames": []}
    with FakeAnthropicAPI(_plan) as api:
        options = ClaudeAgentOptions(
            env=api.cli_env(home),
            mcp_servers={"probe": {"type": "http", "url": url}},
            allowed_tools=["mcp__probe__research"],
            cwd=str(home),
        )
        async with GatewayClaudeSDKClient(options=options) as client:
            await client.query("go")
            async for message in client.receive_response():
                if isinstance(message, ToolProgressMessage):
                    out["frames"].append(message)
                elif isinstance(message, AssistantMessage):
                    out["tool_use_ids"] += [b.id for b in message.content if isinstance(b, ToolUseBlock)]
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    out["results"] += [b for b in message.content if isinstance(b, ToolResultBlock)]
    return out


async def test_cli_opts_in_but_does_not_forward_mcp_progress(progress_server, tmp_path):
    url, seen_meta = progress_server
    out = await asyncio.wait_for(_run(url, tmp_path), _TURN_TIMEOUT)

    assert out["tool_use_ids"], "the fake model must have called the MCP tool"
    assert seen_meta and "progressToken" in seen_meta[0], "the CLI opts in to MCP progress"
    assert seen_meta[0].get("claudecode/toolUseId") == out["tool_use_ids"][0], (
        "the relay keys progress to the call by this _meta field — if it is renamed, the relay goes dark"
    )
    leaked = [f for f in out["frames"] if "실행 중" in str(f.data)]
    assert not leaked, (
        "the CLI now forwards MCP progress text itself — use its frame and retire src/mcp_progress_relay.py"
    )


async def test_relay_recovers_every_progress_update_for_the_right_call(progress_server, tmp_path, monkeypatch):
    url, _seen = progress_server
    app = FastAPI()
    app.include_router(mcp_progress_relay.router)
    relay_port, relay_server = _serve(app)
    monkeypatch.setenv("MCP_RELAY_BASE_URL", f"http://127.0.0.1:{relay_port}")

    async def scenario():
        servers, handle = mcp_progress_relay.attach({"probe": {"type": "http", "url": url}})
        assert handle is not None and servers["probe"]["url"].startswith(f"http://127.0.0.1:{relay_port}/internal/mcp-relay/")
        events: List[Dict[str, Any]] = []

        async def collect():
            while True:
                events.append(await handle.queue.get())

        collector = asyncio.create_task(collect())
        try:
            out = await _run(servers["probe"]["url"], tmp_path)
        finally:
            collector.cancel()
            mcp_progress_relay.detach(handle)
        return out, events

    try:
        out, events = await asyncio.wait_for(scenario(), _TURN_TIMEOUT)
    finally:
        relay_server.should_exit = True

    assert [e["message"] for e in events] == ["실행 중: step1", "실행 중: step2", "실행 중: step3"]
    assert [(e["progress"], e["total"]) for e in events] == [(1, 50), (2, 50), (3, 50)]
    assert {e["tool_use_id"] for e in events} == {out["tool_use_ids"][0]}
    assert all(e["type"] == "tool_progress" and e["source"] == "mcp" for e in events)
    assert "research-done" in str(out["results"][0].content), "the relay must not alter the tool result"
