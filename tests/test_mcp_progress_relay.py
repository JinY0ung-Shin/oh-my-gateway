"""Unit coverage for the MCP progress relay (ChatDRAGON #489) — see tests/test_cli_mcp_progress.py
for the real-CLI pin that proves why it exists."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from src import mcp_progress_relay as relay
from src.streaming_utils import _cli_tool_progress_events
from src.tool_stats import ToolStatsCollector


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    monkeypatch.setenv("MCP_RELAY_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.delenv("MCP_PROGRESS_RELAY", raising=False)
    yield
    relay._registry.clear()


def _servers():
    return {
        "docs": {"type": "http", "url": "https://docs.internal/mcp", "headers": {"Authorization": "Bearer x"}},
        "stream": {"type": "streamable-http", "url": "http://10.0.0.5:9100/mcp"},
        "local": {"type": "stdio", "command": "run-me"},
        "legacy": {"type": "sse", "url": "http://old/sse"},
    }


def test_attach_rewrites_only_http_servers_and_keeps_their_headers():
    servers, handle = relay.attach(_servers())
    assert handle is not None
    prefix = f"http://127.0.0.1:9999/internal/mcp-relay/{handle.relay_id}/"
    assert servers["docs"]["url"] == prefix + "docs"
    assert servers["docs"]["headers"] == {"Authorization": "Bearer x"}, "the CLI still sends the configured headers"
    assert servers["stream"]["url"] == prefix + "stream"
    assert servers["local"] == _servers()["local"] and servers["legacy"] == _servers()["legacy"]
    assert handle.targets == {"docs": "https://docs.internal/mcp", "stream": "http://10.0.0.5:9100/mcp"}
    relay.detach(handle)
    assert handle.relay_id not in relay._registry


@pytest.mark.parametrize("disabled", ["false", "0", "off"])
def test_attach_is_a_no_op_when_disabled_or_without_a_base(monkeypatch, disabled):
    monkeypatch.setenv("MCP_PROGRESS_RELAY", disabled)
    assert relay.attach(_servers()) == (_servers(), None)
    monkeypatch.delenv("MCP_PROGRESS_RELAY")
    monkeypatch.delenv("MCP_RELAY_BASE_URL")
    monkeypatch.setattr(relay, "_origin", None)
    assert relay.attach(_servers())[1] is None
    assert relay.attach({"local": {"type": "stdio", "command": "x"}})[1] is None


def _handle_with(token="7", tool_use_id="toolu_1"):
    _servers_out, handle = relay.attach({"docs": {"type": "http", "url": "https://docs.internal/mcp"}})
    handle.remember(token, tool_use_id)
    return handle


def _progress(token, progress, total=None, message=None):
    params = {"progressToken": token, "progress": progress}
    if total is not None:
        params["total"] = total
    if message is not None:
        params["message"] = message
    return {"jsonrpc": "2.0", "method": "notifications/progress", "params": params}


def test_sse_scan_handles_split_chunks_crlf_batches_and_foreign_tokens():
    handle = _handle_with(token=7)
    stream = (
        f"event: message\r\ndata: {json.dumps(_progress(7, 3, 50, '실행 중: grep_pages'), ensure_ascii=False)}\r\n\r\n"
        f"data: {json.dumps([_progress(7, 4, 50, 'read_pages'), _progress(99, 1, 2, 'someone else')])}\n\n"
        f"data: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'content': []}})}\n\n"
    )
    tail = ""
    for i in range(0, len(stream), 17):  # arbitrary network chunking
        tail = relay._scan_events(handle, tail + stream[i : i + 17])
    events = []
    while not handle.queue.empty():
        events.append(handle.queue.get_nowait())
    assert [(e["progress"], e["total"], e["message"]) for e in events] == [
        (3, 50, "실행 중: grep_pages"),
        (4, 50, "read_pages"),
    ], "only this relay's tokens, in order; the result frame is not progress"
    assert all(e["tool_use_id"] == "toolu_1" and e["source"] == "mcp" for e in events)


def test_tokens_are_recorded_from_tools_call_bodies_single_and_batched():
    _servers_out, handle = relay.attach({"docs": {"type": "http", "url": "https://x/mcp"}})
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "s", "arguments": {}, "_meta": {"progressToken": 4, "claudecode/toolUseId": "toolu_a"}}}
    other = {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"_meta": {"progressToken": 5}}}
    relay._record_tokens(handle, json.dumps([call, other]).encode())
    assert dict(handle.tokens) == {"4": "toolu_a"}
    relay._record_tokens(handle, b"not json")  # never raises


def test_queue_is_bounded_and_drained_per_turn():
    handle = _handle_with(token=1)
    for i in range(relay._QUEUE_SIZE + 10):
        handle.publish({"progressToken": 1, "progress": i})
    assert handle.queue.qsize() == relay._QUEUE_SIZE
    handle.drain()
    assert handle.queue.empty()


def _relay_app():
    app = FastAPI()
    app.include_router(relay.router)
    return app


async def _call(client_host: str, path: str, body: bytes):
    transport = httpx.ASGITransport(app=_relay_app(), client=(client_host, 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(path, content=body, headers={"content-type": "application/json", "mcp-session-id": "s1"})


async def test_relay_passes_bytes_through_and_publishes_progress(monkeypatch):
    _servers_out, handle = relay.attach({"docs": {"type": "http", "url": "https://docs.internal/mcp"}})
    upstream_body = (
        f"event: message\ndata: {json.dumps(_progress(8, 1, 50, '실행 중: grep_pages'), ensure_ascii=False)}\n\n"
        'event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"ok"}]}}\n\n'
    ).encode()
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"}, stream=httpx.ByteStream(upstream_body))

    monkeypatch.setattr(relay, "_http", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    request_body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {"task": "D 불량"}, "_meta": {"progressToken": 8, "claudecode/toolUseId": "toolu_z"}}}).encode()

    res = await _call("127.0.0.1", f"/internal/mcp-relay/{handle.relay_id}/docs", request_body)

    assert res.status_code == 200 and res.content == upstream_body, "the relay never rewrites bytes"
    assert res.headers["mcp-session-id"] == "s1"
    assert seen["url"] == "https://docs.internal/mcp" and seen["body"] == request_body
    assert seen["headers"]["mcp-session-id"] == "s1" and seen["headers"]["accept-encoding"] == "identity"
    event = handle.queue.get_nowait()
    assert event == {"type": "tool_progress", "source": "mcp", "tool_use_id": "toolu_z", "tool_name": "", "elapsed_time_seconds": 0, "progress": 1, "total": 50, "message": "실행 중: grep_pages"}


async def test_relay_is_loopback_only_and_not_an_open_proxy():
    _servers_out, handle = relay.attach({"docs": {"type": "http", "url": "https://docs.internal/mcp"}})
    assert (await _call("10.1.2.3", f"/internal/mcp-relay/{handle.relay_id}/docs", b"{}")).status_code == 403
    assert (await _call("127.0.0.1", "/internal/mcp-relay/guessed/docs", b"{}")).status_code == 404
    assert (await _call("127.0.0.1", f"/internal/mcp-relay/{handle.relay_id}/other", b"{}")).status_code == 404


def test_relayed_progress_becomes_response_tool_progress_for_in_flight_calls_only():
    stats = ToolStatsCollector()
    stats.record_use("toolu_z", "mcp__docs__search_internal_docs")
    seq = iter(range(100))
    chunk = {"type": "tool_progress", "source": "mcp", "tool_use_id": "toolu_z", "tool_name": "", "elapsed_time_seconds": 0, "progress": 12, "total": 50, "message": "실행 중: grep_pages, read_pages"}
    [line] = _cli_tool_progress_events(chunk, lambda: next(seq), stats)
    data = json.loads(line.split("data: ", 1)[1])
    assert data["type"] == "response.tool_progress" and data["source"] == "mcp"
    assert data["name"] == "mcp__docs__search_internal_docs"
    assert (data["progress"], data["total"], data["message"]) == (12, 50, "실행 중: grep_pages, read_pages")
    late = {**chunk, "tool_use_id": "toolu_done"}
    assert _cli_tool_progress_events(late, lambda: next(seq), stats) == [], "a finished call is not re-announced"


async def test_turn_loop_interleaves_relayed_progress_without_losing_sdk_messages():
    from src.backends.claude.client import ClaudeCodeCLI
    from src.session_manager import Session

    with patch("src.auth.validate_claude_code_auth", return_value=(True, {"method": "anthropic"})), patch("src.auth.auth_manager") as auth:
        auth.get_claude_code_env_vars.return_value = {"ANTHROPIC_AUTH_TOKEN": "k"}
        cli = ClaudeCodeCLI(cwd="/tmp")
    session = Session(session_id="sess-relay")
    _servers_out, handle = relay.attach({"docs": {"type": "http", "url": "https://x/mcp"}})
    handle.remember(1, "toolu_1")
    handle.publish({"progressToken": 1, "progress": 0, "message": "stale from last turn"})
    session.mcp_progress_relay = handle

    first, second = SimpleNamespace(tag="first"), SimpleNamespace(tag="second")

    async def receive_response():
        yield first
        await asyncio.sleep(0.05)
        handle.publish({"progressToken": 1, "progress": 5, "total": 50, "message": "mid"})
        await asyncio.sleep(0.05)
        yield second

    client = AsyncMock()
    client.receive_response = receive_response
    with patch.object(ClaudeCodeCLI, "_convert_message", side_effect=lambda m: {"tag": m.tag}):
        out = [m async for m in cli.run_completion_with_client(client, "go", session)]

    assert out == [
        {"tag": "first"},
        {"type": "tool_progress", "source": "mcp", "tool_use_id": "toolu_1", "tool_name": "", "elapsed_time_seconds": 0, "progress": 5, "total": 50, "message": "mid"},
        {"tag": "second"},
    ], "stale progress is dropped, live progress rides between messages, no SDK message is lost"
