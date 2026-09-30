"""Relay MCP ``notifications/progress`` from HTTP MCP servers into the turn stream (issue ChatDRAGON #489).

Why this exists: the bundled Claude CLI (2.1.283) *does* opt in to MCP progress — every
``tools/call`` carries ``_meta.progressToken`` (and ``_meta["claudecode/toolUseId"]``) and the
CLI receives the server's ``notifications/progress``. But in SDK (stream-json) mode it never
writes them to its output: its ``tool_progress`` frames cover Bash/REPL/heartbeat/agent-retry
only, and ``mcp_progress`` feeds the interactive TUI alone (pinned in
``tests/test_cli_mcp_progress.py``). A long MCP tool (a 2-minute document search) therefore
reaches clients as silence plus elapsed-time heartbeats, and the server's progress text is lost.

So the gateway sits on the wire instead: for HTTP (streamable-http) MCP servers the CLI child is
pointed at ``/internal/mcp-relay/<relay_id>/<server>`` on this gateway, which forwards every
request verbatim to the configured URL and streams the reply back unbuffered. While relaying it

* remembers ``progressToken → toolUseId`` from each ``tools/call`` request body, and
* scans the reply's SSE events for ``notifications/progress`` and publishes
  ``{tool_use_id, progress, total, message}`` to the session's queue,

which the turn loop turns into ``response.tool_progress`` (``source="mcp"``). Bytes are never
rewritten — the relay only reads. It is not an open proxy: a relay id is a random per-client
token bound to the exact server URLs of that session's config, and only loopback callers (the
CLI child) are served. ``MCP_PROGRESS_RELAY=false`` turns it off (servers go direct again).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

RELAY_PREFIX = "/internal/mcp-relay"
RELAYED_TYPES = frozenset({"http", "streamable-http"})
_MAX_TOKENS = 256
_QUEUE_SIZE = 256
_MAX_SSE_BUFFER = 1 << 20
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# Hop-by-hop headers (RFC 7230 §6.1) plus the ones the relay recomputes.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
        "content-encoding",
    }
)


def relay_enabled() -> bool:
    return os.getenv("MCP_PROGRESS_RELAY", "true").strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class RelayHandle:
    """One client's relay registration: its target URLs and its progress queue."""

    relay_id: str
    targets: Dict[str, str]
    queue: "asyncio.Queue[Dict[str, Any]]" = field(default_factory=lambda: asyncio.Queue(_QUEUE_SIZE))
    tokens: "OrderedDict[str, str]" = field(default_factory=OrderedDict)

    def remember(self, token: Any, tool_use_id: str) -> None:
        self.tokens[str(token)] = tool_use_id
        self.tokens.move_to_end(str(token))
        while len(self.tokens) > _MAX_TOKENS:
            self.tokens.popitem(last=False)

    def publish(self, params: Dict[str, Any]) -> None:
        tool_use_id = self.tokens.get(str(params.get("progressToken")))
        if not tool_use_id:
            return  # not a tools/call this relay saw (or an unrelated token)
        event: Dict[str, Any] = {
            "type": "tool_progress",
            "source": "mcp",
            "tool_use_id": tool_use_id,
            "tool_name": "",
            "elapsed_time_seconds": 0,
        }
        progress, total, message = params.get("progress"), params.get("total"), params.get("message")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            event["progress"] = progress
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            event["total"] = total
        if isinstance(message, str) and message.strip():
            event["message"] = message.strip()[:500]
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A reader that stopped draining (turn over) must not grow memory; the next
            # notification carries the newer state anyway.
            pass

    def drain(self) -> None:
        """Drop events from a previous turn — a new turn must not start with stale progress."""
        while not self.queue.empty():
            self.queue.get_nowait()


_registry: Dict[str, RelayHandle] = {}
_origin: Optional[str] = None


def local_base_url() -> Optional[str]:
    """Where the CLI child reaches this gateway: ``MCP_RELAY_BASE_URL`` or the served origin."""
    override = os.getenv("MCP_RELAY_BASE_URL", "").strip().rstrip("/")
    return override or _origin


def note_origin(scope: Dict[str, Any]) -> None:
    global _origin
    if _origin is not None or scope.get("type") != "http":
        return
    server = scope.get("server")
    if not server or len(server) != 2 or server[1] is None:
        return
    host, port = server
    if host in {"0.0.0.0", "::", "", None}:
        host = "127.0.0.1"
    if ":" in str(host):
        host = f"[{host}]"
    _origin = f"http://{host}:{port}"


class RelayOriginMiddleware:
    """Pure ASGI: record the address this gateway serves on (the CLI child dials it back)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        note_origin(scope)
        await self.app(scope, receive, send)


def attach(mcp_servers: Any) -> Tuple[Any, Optional[RelayHandle]]:
    """Point this client's HTTP MCP servers at the relay. Returns (servers for the CLI, handle|None)."""
    if not isinstance(mcp_servers, dict) or not mcp_servers or not relay_enabled():
        return mcp_servers, None
    base = local_base_url()
    if not base:
        return mcp_servers, None
    relay_id = secrets.token_urlsafe(24)
    targets: Dict[str, str] = {}
    rewritten: Dict[str, Any] = {}
    for name, config in mcp_servers.items():
        if (
            isinstance(config, dict)
            and config.get("type") in RELAYED_TYPES
            and isinstance(config.get("url"), str)
            and config["url"].startswith(("http://", "https://"))
        ):
            targets[name] = config["url"]
            rewritten[name] = {**config, "url": f"{base}{RELAY_PREFIX}/{relay_id}/{quote(name, safe='')}"}
        else:
            rewritten[name] = config
    if not targets:
        return mcp_servers, None
    handle = RelayHandle(relay_id=relay_id, targets=targets)
    _registry[relay_id] = handle
    return rewritten, handle


def detach(handle: Optional[RelayHandle]) -> None:
    if handle is not None:
        _registry.pop(handle.relay_id, None)


def _record_tokens(handle: RelayHandle, body: bytes) -> None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return
    for message in payload if isinstance(payload, list) else [payload]:
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            continue
        params = message.get("params")
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict):
            continue
        token, tool_use_id = meta.get("progressToken"), meta.get("claudecode/toolUseId")
        if token is not None and isinstance(tool_use_id, str) and tool_use_id:
            handle.remember(token, tool_use_id)


def _scan_events(handle: RelayHandle, text: str) -> str:
    """Publish progress from complete SSE events in *text*; return the unfinished tail."""
    normalized = text.replace("\r\n", "\n")
    *events, tail = normalized.split("\n\n")
    for event in events:
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if not data:
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        for message in payload if isinstance(payload, list) else [payload]:
            if isinstance(message, dict) and message.get("method") == "notifications/progress":
                params = message.get("params")
                if isinstance(params, dict):
                    handle.publish(params)
    return tail if len(tail) <= _MAX_SSE_BUFFER else ""


_http: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        # No read timeout: a tools/call may legitimately stream for minutes. The CLI owns the
        # call timeout (MCP_TOOL_TIMEOUT) and closes its side, which cancels this relay.
        _http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
    return _http


router = APIRouter(include_in_schema=False)


@router.api_route(f"{RELAY_PREFIX}/{{relay_id}}/{{server}}", methods=["GET", "POST", "DELETE"])
async def relay(relay_id: str, server: str, request: Request) -> StreamingResponse:
    client_host = request.client.host if request.client else ""
    if client_host not in _LOOPBACK:
        raise HTTPException(status_code=403, detail="relay is loopback-only")
    handle = _registry.get(relay_id)
    target = handle.targets.get(server) if handle else None
    if handle is None or target is None:
        raise HTTPException(status_code=404, detail="unknown relay")

    body = await request.body()
    if request.method == "POST" and body:
        _record_tokens(handle, body)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    headers["accept-encoding"] = "identity"  # the relay reads SSE text; never ask for gzip
    url = target + (f"?{request.url.query}" if request.url.query else "")
    upstream_request = _client().build_request(request.method, url, headers=headers, content=body or None)
    try:
        upstream = await _client().send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("mcp relay: %s unreachable: %s", server, exc)
        raise HTTPException(status_code=502, detail="MCP server unreachable") from exc

    is_sse = "text/event-stream" in upstream.headers.get("content-type", "")
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_HEADERS}

    async def stream() -> AsyncIterator[bytes]:
        tail = ""
        decoder = None
        if is_sse:
            import codecs

            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            async for chunk in upstream.aiter_raw():
                if decoder is not None:
                    try:
                        tail = _scan_events(handle, tail + decoder.decode(chunk))
                    except Exception:  # noqa: BLE001 — reading must never break the relayed bytes
                        logger.debug("mcp relay: progress scan failed", exc_info=True)
                        tail = ""
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=out_headers)
