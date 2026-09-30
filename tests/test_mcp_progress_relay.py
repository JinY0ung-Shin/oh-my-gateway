"""Unit coverage for the MCP progress relay (ChatDRAGON #489) — see tests/test_cli_mcp_progress.py
for the real-CLI pin that proves why it exists."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Optional
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


# ---------------------------------------------------------------------------
# #220 regression: the relay took down every HTTP MCP server behind Docker.
#
# ``scope["server"]`` is the address a connection was *accepted on*. A ``--host 0.0.0.0`` gateway
# reached through port publishing reports its container IP, the CLI child was told to dial that IP,
# its call then came *from* that IP, and the loopback-only check refused every MCP request (403).


@pytest.fixture
def _fresh_origin(monkeypatch):
    monkeypatch.delenv("MCP_RELAY_BASE_URL", raising=False)
    monkeypatch.setattr(relay, "_origin", None)
    monkeypatch.setattr(relay, "_probe_ok", {}, raising=False)
    monkeypatch.setattr(relay, "_probe_failed_at", {}, raising=False)


@pytest.mark.parametrize(
    ("server", "origin"),
    [
        (("172.17.0.3", 8000), "http://127.0.0.1:8000"),
        (("0.0.0.0", 8000), "http://127.0.0.1:8000"),
        (("127.0.0.1", 17995), "http://127.0.0.1:17995"),
        (("fd00::5", 8000), "http://[::1]:8000"),
        (("::ffff:10.0.0.4", 8000), "http://127.0.0.1:8000"),
    ],
)
def test_origin_always_dials_the_served_port_on_loopback(_fresh_origin, server, origin):
    relay.note_origin({"type": "http", "server": server})
    assert relay.local_base_url() == origin


def _scope_app():
    """Relay router behind an app whose ``scope["server"]`` we control (the accepting address)."""
    inner = _relay_app()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "server": ("172.17.0.3", 8000)}
        await inner(scope, receive, send)

    return app


async def _call_on(client_host: str, path: str, method: str = "GET"):
    transport = httpx.ASGITransport(app=_scope_app(), client=(client_host, 5555))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.request(method, path, content=b"{}" if method == "POST" else None)


async def test_a_caller_on_this_hosts_own_interface_address_is_served():
    _servers_out, handle = relay.attach({"docs": {"type": "http", "url": "https://docs.internal/mcp"}})
    assert (await _call_on("172.17.0.3", relay.PROBE_PATH)).status_code == 204
    # A remote peer (the Docker bridge gateway, another host) is still refused.
    assert (await _call_on("172.17.0.1", relay.PROBE_PATH)).status_code == 403
    assert (await _call_on("172.17.0.1", f"/internal/mcp-relay/{handle.relay_id}/docs", "POST")).status_code == 403


class _ProbeClient:
    """Stands in for the probe's httpx.AsyncClient; records calls and answers from ``outcome``."""

    calls: list = []
    outcome: Any = 204

    def __init__(self, **kwargs):
        assert kwargs.get("trust_env") is False, "the probe must not go through HTTP(S)_PROXY"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        _ProbeClient.calls.append(url)
        if isinstance(_ProbeClient.outcome, Exception):
            raise _ProbeClient.outcome
        return httpx.Response(_ProbeClient.outcome)


@pytest.fixture
def probe_client(monkeypatch, _fresh_origin):
    _ProbeClient.calls = []
    _ProbeClient.outcome = 204
    monkeypatch.setattr(relay.httpx, "AsyncClient", _ProbeClient)
    relay.note_origin({"type": "http", "server": ("172.17.0.3", 8000)})
    return _ProbeClient


async def test_reachable_probes_once_and_caches_success(probe_client):
    assert await relay.reachable() and await relay.reachable()
    assert probe_client.calls == ["http://127.0.0.1:8000" + relay.PROBE_PATH]


@pytest.mark.parametrize("outcome", [403, httpx.ConnectError("refused")])
async def test_an_unreachable_relay_is_reported_and_retried_only_after_a_while(probe_client, outcome):
    probe_client.outcome = outcome
    assert not await relay.reachable()
    assert not await relay.reachable(), "a failure is not re-probed on every client"
    assert len(probe_client.calls) == 1


def test_loopback_joins_the_childs_no_proxy_without_losing_existing_entries(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "corp.internal,localhost")
    monkeypatch.delenv("no_proxy", raising=False)
    env = {"no_proxy": ".intra"}
    relay.with_loopback_no_proxy(env)
    assert env["NO_PROXY"] == "corp.internal,localhost,127.0.0.1,::1"
    assert env["no_proxy"] == ".intra,127.0.0.1,localhost,::1"


async def test_client_keeps_mcp_servers_direct_when_the_relay_is_unreachable(probe_client):
    from src.backends.claude.client import ClaudeCodeCLI
    from src.session_manager import Session

    servers = {"docs": {"type": "http", "url": "https://docs.internal/mcp"}}
    probe_client.outcome = 403
    options = SimpleNamespace(mcp_servers=dict(servers), env={})
    session = Session(session_id="sess-direct")
    await ClaudeCodeCLI._attach_mcp_progress_relay(options, session)
    assert options.mcp_servers == servers, "MCP tools must keep working without the relay"
    assert session.mcp_progress_relay is None and options.env == {}

    relay._probe_failed_at.clear()
    probe_client.outcome = 204
    await ClaudeCodeCLI._attach_mcp_progress_relay(options, session)
    assert options.mcp_servers["docs"]["url"].startswith("http://127.0.0.1:8000/internal/mcp-relay/")
    assert session.mcp_progress_relay is not None
    assert "127.0.0.1" in options.env["NO_PROXY"].split(",")


def _non_loopback_ipv4() -> Optional[str]:
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent for a UDP connect
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


async def test_real_listener_on_all_interfaces_relays_when_first_reached_on_its_interface(_fresh_origin, monkeypatch):
    """End to end on a real uvicorn socket — the exact topology that broke (Docker port publish)."""
    import socket

    import uvicorn

    address = _non_loopback_ipv4()
    if address is None:
        pytest.skip("no non-loopback IPv4 address on this host")

    upstream_calls = []

    async def upstream_mcp(scope, receive, send):  # a minimal HTTP MCP server
        upstream_calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"jsonrpc":"2.0","id":1,"result":{}}'})

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    gateway_app = FastAPI()
    gateway_app.add_middleware(relay.RelayOriginMiddleware)
    gateway_app.include_router(relay.router)

    @gateway_app.get("/first")
    async def first():
        return {}

    gw_port, mcp_port = free_port(), free_port()
    # The relay's pooled client is process-global; one left by an earlier test belongs to that
    # test's (closed) event loop.
    monkeypatch.setattr(relay, "_http", None)
    servers = [
        uvicorn.Server(uvicorn.Config(gateway_app, host="0.0.0.0", port=gw_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(upstream_mcp, host="127.0.0.1", port=mcp_port, log_level="warning")),
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        for server in servers:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.02)
        async with httpx.AsyncClient(trust_env=False) as client:
            # The first request arrives on the interface address, like a published Docker port.
            assert (await client.get(f"http://{address}:{gw_port}/first")).status_code == 200
            rewritten, handle = relay.attach({"docs": {"type": "http", "url": f"http://127.0.0.1:{mcp_port}/mcp"}})
            # What the CLI child does: dial the URL it was given.
            res = await client.post(rewritten["docs"]["url"], json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            assert res.status_code == 200, res.text
            assert upstream_calls == ["/mcp"]
            assert relay.local_base_url() == f"http://127.0.0.1:{gw_port}"
            assert await relay.reachable()
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        if relay._http is not None:
            await relay._http.aclose()


# ---------------------------------------------------------------------------
# readOnlyTools (ChatDRAGON #471) — see tests/test_cli_mcp_readonly.py for the real-CLI pin
# ---------------------------------------------------------------------------


def test_read_only_key_is_stripped_for_every_server_type_and_parsed():
    servers = {
        "docs": {"type": "http", "url": "https://d/mcp", relay.READ_ONLY_KEY: ["search_internal_docs", "basic_knowledge"]},
        "all": {"type": "http", "url": "https://a/mcp", relay.READ_ONLY_KEY: "*"},
        "local": {"type": "stdio", "command": "x", relay.READ_ONLY_KEY: ["t"]},
        "bad": {"type": "http", "url": "https://b/mcp", relay.READ_ONLY_KEY: 3},
        "plain": {"type": "http", "url": "https://p/mcp"},
    }
    cleaned, rules = relay.split_read_only(servers)
    assert all(relay.READ_ONLY_KEY not in c for c in cleaned.values()), "the CLI never sees the key"
    assert rules == {
        "docs": frozenset({"search_internal_docs", "basic_knowledge"}),
        "all": frozenset({"*"}),
        "local": frozenset({"t"}),
    }
    assert servers["docs"][relay.READ_ONLY_KEY], "the shared config object is not mutated"
    assert cleaned["plain"] is servers["plain"]


def test_attach_keeps_rules_only_for_relayed_servers():
    servers, rules = relay.split_read_only(
        {
            "docs": {"type": "http", "url": "https://d/mcp", relay.READ_ONLY_KEY: "*"},
            "local": {"type": "stdio", "command": "x", relay.READ_ONLY_KEY: "*"},
        }
    )
    _, handle = relay.attach(servers, rules)
    assert handle.read_only == {"docs": frozenset({"*"})}


def _tools_reply(tools, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}}


def test_tools_list_rewrite_marks_only_the_listed_tools_json_and_sse():
    tools = [
        {"name": "search_internal_docs", "inputSchema": {}},
        {"name": "basic_knowledge", "annotations": {"title": "용어"}, "inputSchema": {}},
        {"name": "write_note", "inputSchema": {}},
    ]
    ids = {json.dumps(1)}
    want = frozenset({"search_internal_docs", "basic_knowledge"})
    out = json.loads(relay.rewrite_tools_list(json.dumps(_tools_reply(tools)).encode(), False, ids, want))
    by_name = {t["name"]: t for t in out["result"]["tools"]}
    assert by_name["search_internal_docs"]["annotations"] == {"readOnlyHint": True}
    assert by_name["basic_knowledge"]["annotations"] == {"title": "용어", "readOnlyHint": True}
    assert "annotations" not in by_name["write_note"], "an unlisted tool is never claimed read-only"

    for newline in ("\n", "\r\n"):
        sse = f"event: message{newline}data: {json.dumps(_tools_reply(tools), ensure_ascii=False)}{newline}{newline}".encode()
        rewritten = relay.rewrite_tools_list(sse, True, ids, frozenset({"*"}))
        text = rewritten.decode()
        assert text.startswith(f"event: message{newline}data: ") and text.endswith(newline * 2)
        payload = json.loads(text.split("data: ", 1)[1].split(newline)[0])
        assert all(t["annotations"]["readOnlyHint"] is True for t in payload["result"]["tools"])


def test_tools_list_rewrite_leaves_other_replies_byte_identical():
    ids = {json.dumps(7)}
    other = json.dumps({"jsonrpc": "2.0", "id": 8, "result": {"tools": [{"name": "x"}]}}).encode()
    assert relay.rewrite_tools_list(other, False, ids, frozenset({"*"})) == other
    already = json.dumps(_tools_reply([{"name": "x", "annotations": {"readOnlyHint": True}}], 7)).encode()
    assert relay.rewrite_tools_list(already, False, ids, frozenset({"*"})) == already
    assert relay._tools_list_ids(b'[{"jsonrpc":"2.0","id":"a","method":"tools/list"},{"jsonrpc":"2.0","method":"notifications/initialized"}]') == {json.dumps("a")}


async def test_relay_applies_read_only_to_tools_list_and_streams_everything_else(monkeypatch):
    servers, rules = relay.split_read_only({"docs": {"type": "http", "url": "https://d/mcp", relay.READ_ONLY_KEY: ["fast"]}})
    _, handle = relay.attach(servers, rules)
    call_reply = b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"fast"}]}}\n\n'

    def upstream(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "tools/list":
            body = f"event: message\ndata: {json.dumps(_tools_reply([{'name': 'fast'}, {'name': 'other'}]))}\n\n".encode()
        else:
            body = call_reply
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(body))

    monkeypatch.setattr(relay, "_http", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    path = f"/internal/mcp-relay/{handle.relay_id}/docs"
    listed = await _call("127.0.0.1", path, json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode())
    payload = json.loads(listed.text.split("data: ", 1)[1].split("\n")[0])
    by_name = {t["name"]: t for t in payload["result"]["tools"]}
    assert by_name["fast"]["annotations"] == {"readOnlyHint": True} and "annotations" not in by_name["other"]
    assert listed.headers.get("content-length") in (None, str(len(listed.content))), "length matches the rewrite"

    # Not a tools/list request (even with a reply that looks like one): bytes untouched.
    called = await _call("127.0.0.1", path, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fast"}}).encode())
    assert called.content == call_reply


def test_every_options_build_strips_read_only_and_keeps_the_rules():
    from claude_agent_sdk import ClaudeAgentOptions

    from src.backends.claude.client import ClaudeCodeCLI

    cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    options = ClaudeAgentOptions()
    with patch("src.backends.claude.client.resolve_mcp_servers", side_effect=lambda s: s), patch.object(
        ClaudeCodeCLI, "_set_allowed_tools", lambda self, o, tools: setattr(o, "allowed_tools", tools)
    ):
        cli._configure_mcp_servers(
            options, {"docs": {"type": "http", "url": "https://d/mcp", relay.READ_ONLY_KEY: "*"}}, None, None
        )
    assert relay.READ_ONLY_KEY not in options.mcp_servers["docs"]
    assert options._gateway_read_only_tools == {"docs": frozenset({"*"})}



@pytest.mark.parametrize("value", [True, False, 1, 0, {"*": True}, {}, [], ["ok", 3], ["*"], "all", "", None])
def test_read_only_values_other_than_star_or_names_fail_closed(value, caplog):
    """Only the exact string "*" widens a whole server; a slip like ``true`` must not."""
    cleaned, rules = relay.split_read_only(
        {"docs": {"type": "http", "url": "https://d/mcp", relay.READ_ONLY_KEY: value}}
    )
    assert rules == {} and relay.READ_ONLY_KEY not in cleaned["docs"]
    assert any(relay.READ_ONLY_KEY in r.getMessage() for r in caplog.records)


def test_server_declared_not_read_only_or_destructive_is_never_overridden():
    tools = [
        {"name": "writer", "annotations": {"readOnlyHint": False}},
        {"name": "wiper", "annotations": {"destructiveHint": True}},
        {"name": "reader"},
    ]
    ids = {json.dumps(1)}
    out = json.loads(relay.rewrite_tools_list(json.dumps(_tools_reply(tools)).encode(), False, ids, frozenset({"*"})))
    by_name = {t["name"]: t for t in out["result"]["tools"]}
    assert by_name["writer"]["annotations"] == {"readOnlyHint": False}
    assert by_name["wiper"]["annotations"] == {"destructiveHint": True}
    assert by_name["reader"]["annotations"] == {"readOnlyHint": True}
