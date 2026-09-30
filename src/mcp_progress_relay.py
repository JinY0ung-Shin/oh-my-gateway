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
token bound to the exact server URLs of that session's config, and only same-host callers (the
CLI child) are served. ``MCP_PROGRESS_RELAY=false`` turns it off (servers go direct again).

One opt-in exception to "bytes are never rewritten": ``readOnlyTools`` (ChatDRAGON #471). The
CLI runs the tools of one parallel batch **sequentially unless every one carries
``annotations.readOnlyHint: true``** (pinned in ``tests/test_cli_mcp_readonly.py``), so a
server that never declares the hint makes a 300 ms lookup wait behind a 2-minute research
call in the same batch. An operator who knows a server's tools are side-effect free lists them
on the server entry — ``"readOnlyTools": ["search_internal_docs", "basic_knowledge"]`` or
``"*"`` (exactly that string; ``true`` or any other shape is rejected with a warning) — and the
relay adds ``readOnlyHint: true`` to exactly those tools in the server's ``tools/list`` reply. A
tool the server itself declares ``readOnlyHint: false`` or ``destructiveHint: true`` is never
overridden. The key is gateway-only: it is always stripped before the CLI sees the
config, and it needs the relay (a stdio server, or an unreachable relay, logs that the hint
cannot be applied). Nothing else in any reply is touched.

The relay must never cost the MCP tools themselves — progress is a nicety, the tool call is the
work. So the child always dials the gateway on loopback (never the interface address a request
happened to arrive on: behind Docker port publishing that is the container IP, the child's call
then comes *from* that IP too, and a loopback-only check refused every MCP request), loopback is
added to the child's ``NO_PROXY``, and a one-time self-probe falls back to direct connections if
the relay is not reachable from this process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

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
    # server name → tool names to mark read-only ({"*"} = every tool of that server)
    read_only: Dict[str, frozenset] = field(default_factory=dict)
    queue: "asyncio.Queue[Dict[str, Any]]" = field(default_factory=lambda: asyncio.Queue(_QUEUE_SIZE))
    tokens: "OrderedDict[str, str]" = field(default_factory=OrderedDict)
    # server name → tools/list ids whose reply has not been seen yet. A spec-valid
    # Streamable HTTP server may close the POST's SSE stream before the response and
    # deliver it on a GET resume (Last-Event-ID); the rewrite follows it there.
    pending_lists: Dict[str, set] = field(default_factory=dict)

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
    """Remember the served PORT and dial it back on loopback.

    ``scope["server"]`` is the local address the connection was accepted on, not the bind
    address: a ``--host 0.0.0.0`` gateway reached through Docker port publishing reports its
    container IP. The CLI child runs on this host, so loopback always reaches the listener.
    """
    global _origin
    if _origin is not None or scope.get("type") != "http":
        return
    server = scope.get("server")
    if not server or len(server) != 2 or server[1] is None:
        return
    host, port = server
    ipv6 = ":" in str(host or "") and not str(host).startswith("::ffff:")
    _origin = f"http://{'[::1]' if ipv6 else '127.0.0.1'}:{port}"


def _same_host(request: Request) -> bool:
    """Loopback, or a caller whose address IS the address it reached us on (this host)."""
    client_host = request.client.host if request.client else ""
    if client_host in _LOOPBACK:
        return True
    server = request.scope.get("server")
    return bool(client_host) and bool(server) and client_host == server[0]


class RelayOriginMiddleware:
    """Pure ASGI: record the address this gateway serves on (the CLI child dials it back)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        note_origin(scope)
        await self.app(scope, receive, send)


PROBE_PATH = f"{RELAY_PREFIX}/_probe"
_PROBE_RETRY_SECONDS = 60.0
_probe_ok: Dict[str, bool] = {}
_probe_failed_at: Dict[str, float] = {}


async def reachable() -> bool:
    """Can a same-host caller reach the relay at ``local_base_url()``? Probed once per base.

    A failure is re-probed after a minute (a gateway still starting, a transient refusal);
    meanwhile the servers stay direct so MCP tools keep working.
    """
    base = local_base_url()
    if not base:
        return False
    if _probe_ok.get(base):
        return True
    loop = asyncio.get_running_loop()
    failed_at = _probe_failed_at.get(base)
    if failed_at is not None and loop.time() - failed_at < _PROBE_RETRY_SECONDS:
        return False
    try:
        # trust_env=False: this asks "is the listener reachable on this host", not "does the
        # corporate proxy route loopback" — the child gets loopback in NO_PROXY for that.
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            ok = (await client.get(f"{base}{PROBE_PATH}")).status_code == 204
    except httpx.HTTPError:
        ok = False
    if ok:
        _probe_ok[base] = True
        _probe_failed_at.pop(base, None)
    else:
        _probe_failed_at[base] = loop.time()
        logger.warning(
            "mcp relay: %s is not reachable from this host; HTTP MCP servers stay direct "
            "(no MCP progress). Set MCP_RELAY_BASE_URL to an address the CLI child can dial.",
            base,
        )
    return ok


def with_loopback_no_proxy(env: Dict[str, str]) -> None:
    """Keep the child's relay calls off any HTTP(S)_PROXY: add loopback to NO_PROXY/no_proxy."""
    for key in ("NO_PROXY", "no_proxy"):
        current = env.get(key, os.environ.get(key, ""))
        entries = [part.strip() for part in current.split(",") if part.strip()]
        for host in ("127.0.0.1", "localhost", "::1"):
            if host not in entries:
                entries.append(host)
        env[key] = ",".join(entries)


READ_ONLY_KEY = "readOnlyTools"


def split_read_only(mcp_servers: Any) -> Tuple[Any, Dict[str, frozenset]]:
    """Strip the gateway-only ``readOnlyTools`` key; return (servers for the CLI, rules).

    Always called before the config reaches the CLI — relay on or off — so the CLI never
    sees a key it does not know. Invalid values are dropped with a warning.
    """
    if not isinstance(mcp_servers, dict):
        return mcp_servers, {}
    rules: Dict[str, frozenset] = {}
    cleaned: Dict[str, Any] = {}
    for name, config in mcp_servers.items():
        if not isinstance(config, dict) or READ_ONLY_KEY not in config:
            cleaned[name] = config
            continue
        value = config[READ_ONLY_KEY]
        cleaned[name] = {k: v for k, v in config.items() if k != READ_ONLY_KEY}
        # Fail closed: only the exact string "*" is the wildcard. ``true`` (a common JSON
        # slip), numbers, objects or a list with non-names are NOT read — widening every
        # tool of a server to "safe to run concurrently" must never happen by accident.
        if isinstance(value, str) and value == "*":
            rules[name] = frozenset({"*"})
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(v, str) and v and v != "*" for v in value)
        ):
            rules[name] = frozenset(value)
        else:
            logger.warning(
                "MCP server %r: %s must be \"*\" or a list of tool names; ignored",
                name,
                READ_ONLY_KEY,
            )
    return cleaned, rules


def attach(
    mcp_servers: Any, read_only: Optional[Dict[str, frozenset]] = None
) -> Tuple[Any, Optional[RelayHandle]]:
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
    rules = {name: tools for name, tools in (read_only or {}).items() if name in targets}
    for name in set(read_only or {}) - set(rules):
        logger.warning(
            "MCP server %r: %s needs an HTTP server routed through the relay; not applied",
            name,
            READ_ONLY_KEY,
        )
    handle = RelayHandle(relay_id=relay_id, targets=targets, read_only=rules)
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


def _tools_list_ids(body: bytes) -> set:
    """JSON-RPC ids of the ``tools/list`` requests in a request body."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return set()
    ids = set()
    for message in payload if isinstance(payload, list) else [payload]:
        if isinstance(message, dict) and message.get("method") == "tools/list" and "id" in message:
            ids.add(json.dumps(message["id"]))
    return ids


def _mark_read_only(message: Any, ids: set, tools: frozenset, seen: Optional[set] = None) -> bool:
    """Add ``readOnlyHint: true`` to matching tools of one ``tools/list`` result."""
    if not isinstance(message, dict) or json.dumps(message.get("id")) not in ids:
        return False
    if seen is not None and ("result" in message or "error" in message):
        seen.add(json.dumps(message.get("id")))
    result = message.get("result")
    listed = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(listed, list):
        return False
    changed = False
    for tool in listed:
        if not isinstance(tool, dict):
            continue
        if "*" in tools or tool.get("name") in tools:
            annotations = tool.get("annotations")
            annotations = dict(annotations) if isinstance(annotations, dict) else {}
            # The server's own explicit claim wins: a tool it marks not read-only or
            # destructive stays that way, whatever the operator's list says.
            if annotations.get("readOnlyHint") is False or annotations.get("destructiveHint") is True:
                logger.warning(
                    "mcp relay: %s declares itself not read-only/destructive; readOnlyTools ignored for it",
                    tool.get("name"),
                )
                continue
            if annotations.get("readOnlyHint") is not True:
                annotations["readOnlyHint"] = True
                tool["annotations"] = annotations
                changed = True
    return changed


def _rewrite_payload(text: str, ids: set, tools: frozenset, seen: Optional[set] = None) -> Optional[str]:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    messages = payload if isinstance(payload, list) else [payload]
    changed = False
    for m in messages:
        changed = _mark_read_only(m, ids, tools, seen) or changed
    return json.dumps(payload, ensure_ascii=False) if changed else None


def rewrite_tools_list(
    content: bytes, is_sse: bool, ids: set, tools: frozenset, seen: Optional[set] = None
) -> bytes:
    """The ``tools/list`` reply with ``readOnlyHint`` added; unchanged bytes otherwise.

    ``seen`` (when given) collects the ids whose response was in *content*, so a caller
    can tell a reply that is still owed (resumable SSE) from one that arrived.
    """
    text = content.decode("utf-8", errors="strict")
    if not is_sse:
        rewritten = _rewrite_payload(text, ids, tools, seen)
        return rewritten.encode("utf-8") if rewritten is not None else content
    newline = "\r\n" if "\r\n" in text else "\n"
    sep = newline * 2
    out_events = []
    changed = False
    for event in text.split(sep):
        lines = event.split(newline)
        data_idx = [i for i, line in enumerate(lines) if line.startswith("data:")]
        if not data_idx:
            out_events.append(event)
            continue
        data = "\n".join(lines[i][5:].lstrip() for i in data_idx)
        rewritten = _rewrite_payload(data, ids, tools, seen)
        if rewritten is None:
            out_events.append(event)
            continue
        changed = True
        # Same event, same non-data lines in place; the data lines become one.
        rebuilt = []
        for i, line in enumerate(lines):
            if i == data_idx[0]:
                rebuilt.append(f"data: {rewritten}")
            elif i not in data_idx:
                rebuilt.append(line)
        out_events.append(newline.join(rebuilt))
    return sep.join(out_events).encode("utf-8") if changed else content


_MAX_PENDING_LISTS = 16
_SSE_EVENT_END = re.compile(rb"\r\n\r\n|\n\n|\r\r")


def _remember_pending(handle: RelayHandle, server: str, ids: set) -> None:
    owed = handle.pending_lists.setdefault(server, set())
    owed.update(ids)
    while len(owed) > _MAX_PENDING_LISTS:
        owed.pop()


def _complete_events(buffer: bytes) -> Tuple[list, bytes]:
    """Split *buffer* into complete SSE events (terminator included) and the rest."""
    events, start = [], 0
    for match in _SSE_EVENT_END.finditer(buffer):
        events.append(buffer[start : match.end()])
        start = match.end()
    rest = buffer[start:]
    if len(rest) > _MAX_SSE_BUFFER:
        # Not an event stream we can frame; stop holding bytes back.
        events.append(rest)
        rest = b""
    return events, rest


def _rewrite_owed(handle: RelayHandle, server: str, event: bytes, tools: frozenset) -> bytes:
    owed = handle.pending_lists.get(server)
    if not owed or b'"id"' not in event:
        return event
    seen: set = set()
    try:
        rewritten = rewrite_tools_list(event, True, owed, tools, seen)
    except Exception:  # noqa: BLE001 — an event we cannot parse goes through as it came
        logger.warning("mcp relay: could not apply readOnlyTools to %s", server, exc_info=True)
        return event
    owed.difference_update(seen)
    return rewritten


_http: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        # No read timeout: a tools/call may legitimately stream for minutes. The CLI owns the
        # call timeout (MCP_TOOL_TIMEOUT) and closes its side, which cancels this relay.
        _http = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
    return _http


router = APIRouter(include_in_schema=False)


@router.get(PROBE_PATH, status_code=204)
async def probe(request: Request) -> Response:
    """Self-probe target for ``reachable()`` — applies exactly the relay's caller rule."""
    if not _same_host(request):
        raise HTTPException(status_code=403, detail="relay is same-host only")
    return Response(status_code=204)


@router.api_route(f"{RELAY_PREFIX}/{{relay_id}}/{{server}}", methods=["GET", "POST", "DELETE"])
async def relay(relay_id: str, server: str, request: Request) -> StreamingResponse:
    if not _same_host(request):
        raise HTTPException(status_code=403, detail="relay is same-host only")
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

    read_only = handle.read_only.get(server)
    list_ids = _tools_list_ids(body) if read_only and request.method == "POST" and body else set()
    if list_ids:
        # A tools/list reply is small and finite: read it whole, add the operator-declared
        # readOnlyHint, and answer in one piece. Every other exchange streams untouched.
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
        seen: set = set()
        try:
            content = rewrite_tools_list(content, is_sse, list_ids, read_only, seen)
        except Exception:  # noqa: BLE001 — a reply we cannot parse goes through as it came
            logger.warning("mcp relay: could not apply readOnlyTools to %s", server, exc_info=True)
            seen = set(list_ids)
        if is_sse and list_ids - seen:
            # The server closed this stream before answering; the reply comes on a GET resume.
            _remember_pending(handle, server, list_ids - seen)
        return Response(content=content, status_code=upstream.status_code, headers=out_headers)

    # A GET resume may carry a tools/list reply an earlier POST stream still owed.
    owed = handle.pending_lists.get(server) if read_only and is_sse and request.method == "GET" else None

    async def stream() -> AsyncIterator[bytes]:
        tail = ""
        decoder = None
        pending = b""
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
                if owed:
                    # Only while a reply is owed: pass complete events one at a time, the
                    # tools/list one with its hint; everything else keeps its exact bytes.
                    events, pending = _complete_events(pending + chunk)
                    for event in events:
                        yield _rewrite_owed(handle, server, event, read_only)
                    continue
                if pending:
                    chunk, pending = pending + chunk, b""
                yield chunk
            if pending:
                yield pending
        finally:
            await upstream.aclose()

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=out_headers)
