"""Route each request to the gateway instance that owns its session.

Outermost ASGI middleware, active only with ``GATEWAY_INSTANCE`` set
(``docs/zero-downtime-deploy.md`` §5.2–5.3). For a request bound to a session
(a continuation, ``/v1/responses/{id}…``, ``/v1/sessions/{id}…``) it decides:

* this process holds the session, or nobody does → serve it here (while
  draining, nobody-holds goes to the drain target instead);
* another live instance holds it idle → ask it to release, then serve here;
* another live instance holds it busy (turn, card, background) → forward the
  request there byte for byte.

New sessions and admin writes go to the drain target while draining. A request
the app refuses with 421 ``x-gateway-reroute`` (raised before any of its work
started) is re-decided, in a bounded loop. Forwarding retries only what
provably never ran: a connection that sent no request byte, or a dispatch the
owner settles as refused. Anything else is reported as ``forward_interrupted``
and never resent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from src import instance_registry
from src.handoff import peer
from src.handoff.drain import DrainError, drain_state, snapshot
from src.handoff.ledger import HANDOFF_DEADLINE, ledger
from src.handoff.owners import owner_locks

logger = logging.getLogger(__name__)

Scope = Dict[str, Any]
Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]

_RESPONSE_PATH = re.compile(r"^/v1/responses/(resp_[^/]+)(?:/cancel)?$")
_SESSION_PATH = re.compile(r"^/v1/sessions/([^/]+)(?:/.*)?$")
_RESP_ID = re.compile(
    r"^resp_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})_\d+$"
)
_PROBES = frozenset({"/health", "/health/ready", "/metrics"})
_ADMIN_PREFIXES = ("/admin", "/api/")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_HOP_BY_HOP = frozenset(
    {
        b"host",
        b"connection",
        b"keep-alive",
        b"transfer-encoding",
        b"content-length",
        b"upgrade",
        b"proxy-connection",
    }
)
_BODY_LIMIT = 64 * 1024 * 1024
_RELEASE_TIMEOUT = 45.0  # covers the owner's capped CLI disconnect
_SETTLE_TIMEOUT = 5.0
_BACKOFF_START = 0.05
_BACKOFF_MAX = 1.0


def _session_of_response(resp_id: Optional[str]) -> Optional[str]:
    match = _RESP_ID.match(resp_id or "")
    return match.group(1).lower() if match else None


def _headers(scope: Scope) -> Dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}


def _loopback(scope: Scope) -> bool:
    client = scope.get("client")
    return bool(client) and client[0] in ("127.0.0.1", "::1")


async def _send_json(send: Send, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
    raw = json.dumps(body).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": raw})


def _error(code: str, message: str, kind: str = "api_error") -> Dict[str, Any]:
    return {"error": {"message": message, "type": kind, "code": code}}


async def _read_body(receive: Receive) -> Optional[bytes]:
    chunks: List[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > _BODY_LIMIT:
            raise ValueError("body too large to route")
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


class _Replay:
    """``receive`` that hands the app a buffered body, then the real channel
    (for disconnects)."""

    def __init__(self, body: bytes, receive: Receive) -> None:
        self._body: Optional[bytes] = body
        self._receive = receive

    async def __call__(self) -> Dict[str, Any]:
        if self._body is not None:
            body, self._body = self._body, None
            return {"type": "http.request", "body": body, "more_body": False}
        return await self._receive()


class HandoffMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self.forwarding = 0
        self.open_requests = 0
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(connect=2.0, read=None, write=30.0, pool=5.0),
            )
        return self._client

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not instance_registry.enabled():
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/internal/") and not path.startswith("/internal/mcp-relay/"):
            await self._internal(scope, receive, send)
            return
        if path == "/health/drain":
            if not _loopback(scope):
                await _send_json(send, 404, {"detail": "Not Found"})
                return
            await _send_json(send, 200, snapshot(self.forwarding, self.open_requests))
            return
        counted = path not in _PROBES
        if counted:
            self.open_requests += 1
        try:
            await self._route(scope, receive, send)
        finally:
            if counted:
                self.open_requests -= 1

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _route(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = _headers(scope)
        verified = peer.verify(scope, headers)
        if verified is not None:
            await self._serve_forwarded(scope, receive, send, verified[1])
            return
        scope = dict(scope, headers=peer.strip_ours(scope["headers"]))
        method = scope["method"]
        path = scope["path"]

        kind: Optional[str] = None
        session_id: Optional[str] = None
        match = _RESPONSE_PATH.match(path)
        if match:
            kind, session_id = "session", _session_of_response(match.group(1))
        elif _SESSION_PATH.match(path) and path != "/v1/sessions/stats":
            kind, session_id = "session", _SESSION_PATH.match(path).group(1)  # type: ignore[union-attr]
        elif method == "POST" and path in ("/v1/responses", "/v1/agents/messages"):
            kind = "new"
        elif method not in _SAFE_METHODS and path.startswith(_ADMIN_PREFIXES):
            kind = "admin_write"
        if kind is None or (kind == "session" and not session_id):
            await self.app(scope, receive, send)
            return

        try:
            body = await _read_body(receive)
        except ValueError:
            await self.app(scope, receive, send)  # oversized: let the size limit answer
            return
        if body is None:
            return  # client went away
        if kind == "new" and path == "/v1/responses":
            try:
                previous = json.loads(body or b"{}").get("previous_response_id")
            except (ValueError, AttributeError):
                previous = None
            if previous:
                kind, session_id = "session", _session_of_response(previous)
                if not session_id:
                    await self.app(scope, _Replay(body, receive), send)
                    return
        continuation = method == "POST" and path == "/v1/responses"

        deadline = time.monotonic() + HANDOFF_DEADLINE
        backoff = _BACKOFF_START
        while True:
            outcome = await self._attempt(scope, receive, send, body, kind, session_id, continuation)
            if outcome == "done":
                return
            if outcome == "again":
                continue
            if time.monotonic() + backoff > deadline:
                logger.warning(
                    "handoff_deadline_exceeded: %s %s (session %s)", method, path, session_id
                )
                if outcome == "unavailable":
                    await _send_json(
                        send,
                        503,
                        _error("drain_target_unavailable", "This gateway is draining and its target is not reachable; retry shortly"),
                        {"retry-after": "1"},
                    )
                else:
                    await _send_json(
                        send,
                        503,
                        _error("handoff_deadline_exceeded", "Could not determine which gateway instance owns this session; retry shortly"),
                        {"retry-after": "1"},
                    )
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _attempt(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        body: bytes,
        kind: str,
        session_id: Optional[str],
        continuation: bool,
    ) -> str:
        """One routing decision. Returns ``done`` (a response was sent),
        ``again`` (re-decide now), ``wait`` (re-decide after a backoff) or
        ``unavailable`` (drain target gone; backoff, then 503)."""
        me = instance_registry.instance_name()
        if kind in ("new", "admin_write"):
            if not drain_state.draining:
                return await self._local(scope, receive, send, body)
            target = drain_state.live_target()
            if target is None:
                return "unavailable"
            return await self._forward(target, scope, receive, send, body)

        from src.session_manager import session_manager

        assert session_id is not None
        if session_id in session_manager.sessions:
            return await self._local(scope, receive, send, body)
        owner = owner_locks.owner(session_id)
        if owner is None:
            if drain_state.draining:
                target = drain_state.live_target()
                if target is None:
                    return "unavailable"
                return await self._forward(target, scope, receive, send, body)
            return await self._local(scope, receive, send, body)
        if owner == me or owner == "":
            return "wait"  # our own teardown in progress, or the holder is still writing its name
        instance = instance_registry.lookup(owner)
        if instance is None:
            return "wait"
        if continuation and not drain_state.draining:
            released = await self._ask_release(instance, session_id)
            if released:
                return "again"
        return await self._forward(instance, scope, receive, send, body)

    async def _ask_release(self, instance: instance_registry.Instance, session_id: str) -> bool:
        try:
            res = await peer.call(
                instance,
                "POST",
                f"/internal/sessions/{session_id}/release",
                timeout=_RELEASE_TIMEOUT,
            )
            return res.status_code == 200 and res.json().get("result") in ("released", "absent")
        except Exception:
            logger.info("release of %s by %s did not answer", session_id, instance.name, exc_info=True)
            return False

    async def _local(self, scope: Scope, receive: Receive, send: Send, body: bytes) -> str:
        rerouted = False

        async def guarded_send(message: Dict[str, Any]) -> None:
            nonlocal rerouted
            if message["type"] == "http.response.start":
                if message.get("status") == 421 and any(
                    k.lower() == peer.REROUTE.encode() for k, _ in message.get("headers", [])
                ):
                    rerouted = True
                    return
            if rerouted:
                return
            await send(message)

        await self.app(scope, _Replay(body, receive), guarded_send)
        return "wait" if rerouted else "done"

    # ------------------------------------------------------------------
    # Forwarding
    # ------------------------------------------------------------------

    async def _forward(
        self,
        instance: instance_registry.Instance,
        scope: Scope,
        receive: Receive,
        send: Send,
        body: bytes,
    ) -> str:
        assert instance.name != instance_registry.instance_name(), "never forward to self"
        target = peer.request_target(scope)
        sig = peer.signed_headers(instance.boot_id, scope["method"], target)
        dispatch_id = sig[peer.DISPATCH_ID]
        headers = [
            (k.decode("latin-1"), v.decode("latin-1"))
            for k, v in scope["headers"]
            if k.lower() not in _HOP_BY_HOP
        ]
        headers.extend(sig.items())
        query = scope.get("query_string") or b""
        url = instance.url + scope["path"] + (("?" + query.decode("latin-1")) if query else "")
        client = self._http()
        request = client.build_request(scope["method"], url, headers=headers, content=body)
        self.forwarding += 1
        started = False
        try:
            disconnected = asyncio.Event()

            async def watch() -> None:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        disconnected.set()
                        return

            watcher = asyncio.create_task(watch())
            try:
                try:
                    response = await self._until_disconnect(client.send(request, stream=True), disconnected)
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    return "wait"  # no request byte left this process
                except asyncio.CancelledError:
                    if disconnected.is_set():
                        return "done"  # the client left; the owner sees our close as its disconnect
                    raise
                except httpx.HTTPError:
                    return await self._settle_or_report(instance, dispatch_id, send, started)
                try:
                    if response.status_code == 421 and peer.REROUTE in response.headers:
                        return "wait"
                    out_headers = [
                        (k.encode("latin-1"), v.encode("latin-1"))
                        for k, v in response.headers.multi_items()
                        if k.lower().encode() not in _HOP_BY_HOP
                    ]
                    await send({"type": "http.response.start", "status": response.status_code, "headers": out_headers})
                    started = True
                    sse = response.headers.get("content-type", "").startswith("text/event-stream")
                    try:
                        async for chunk in self._until_disconnect_iter(response.aiter_raw(), disconnected):
                            await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    except httpx.HTTPError:
                        logger.warning("forward_interrupted mid-stream to %s (%s)", instance.name, dispatch_id)
                        if sse:
                            event = {
                                "type": "response.failed",
                                "response": {
                                    "status": "failed",
                                    "error": {"code": "forward_interrupted", "message": "The connection to the gateway instance running this turn was lost; the turn's outcome is unknown. Do not retry automatically."},
                                },
                            }
                            payload = f"event: response.failed\ndata: {json.dumps(event)}\n\n".encode()
                            await send({"type": "http.response.body", "body": payload, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return "done"
                finally:
                    await response.aclose()
            finally:
                watcher.cancel()
        finally:
            self.forwarding -= 1

    @staticmethod
    async def _until_disconnect(awaitable, disconnected: asyncio.Event):
        task = asyncio.ensure_future(awaitable)
        stop = asyncio.ensure_future(disconnected.wait())
        try:
            done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            task.cancel()
            raise asyncio.CancelledError()
        finally:
            stop.cancel()

    async def _until_disconnect_iter(self, iterator, disconnected: asyncio.Event):
        it = iterator.__aiter__()
        while True:
            try:
                chunk = await self._until_disconnect(it.__anext__(), disconnected)
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                if disconnected.is_set():
                    return
                raise
            yield chunk

    async def _settle_or_report(
        self, instance: instance_registry.Instance, dispatch_id: str, send: Send, started: bool
    ) -> str:
        verdict = "unknown"
        try:
            res = await peer.call(
                instance, "POST", f"/internal/dispatch/{dispatch_id}/settle", timeout=_SETTLE_TIMEOUT
            )
            if res.status_code == 200:
                verdict = res.json().get("verdict", "unknown")
        except Exception:
            logger.info("settle of %s at %s did not answer", dispatch_id, instance.name, exc_info=True)
        if verdict == "refused":
            return "wait"
        logger.warning("forward_interrupted to %s (%s): settle=%s", instance.name, dispatch_id, verdict)
        if not started:
            await _send_json(
                send,
                502,
                _error("forward_interrupted", "The connection to the gateway instance handling this request was lost; its outcome is unknown. Do not retry automatically."),
                {"x-should-retry": "false"},
            )
        return "done"

    # ------------------------------------------------------------------
    # The owner side of a forward
    # ------------------------------------------------------------------

    async def _serve_forwarded(self, scope: Scope, receive: Receive, send: Send, dispatch_id: str) -> None:
        if not ledger.admit(dispatch_id):
            await _send_json(
                send,
                421,
                _error("dispatch_refused", "Dispatch refused (stale, replayed or for another process)"),
                {peer.REROUTE: "1"},
            )
            return
        state = "failed"
        try:
            await self.app(scope, receive, send)
            state = "completed"
        finally:
            ledger.finish(dispatch_id, state)

    # ------------------------------------------------------------------
    # /internal/*
    # ------------------------------------------------------------------

    async def _internal(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"]
        method = scope["method"]
        headers = _headers(scope)

        if path == "/internal/drain":
            await self._drain_endpoint(scope, receive, send, headers)
            return

        verified = peer.verify(scope, headers)
        if verified is None or not peer.fresh_internal(verified[1]):
            await _send_json(send, 404, {"detail": "Not Found"})
            return
        if path == "/internal/ping" and method == "GET":
            await _send_json(
                send,
                200,
                {
                    "instance": instance_registry.instance_name(),
                    "boot_id": instance_registry.BOOT_ID,
                    "draining": drain_state.draining,
                },
            )
            return
        match = re.match(r"^/internal/sessions/([^/]+)/release$", path)
        if match and method == "POST":
            from src.session_manager import session_manager

            result = await session_manager.release_for_handoff(match.group(1))
            await _send_json(send, 200, {"result": result})
            return
        match = re.match(r"^/internal/dispatch/([^/]+)/settle$", path)
        if match and method == "POST":
            await _send_json(send, 200, {"verdict": ledger.settle(match.group(1))})
            return
        await _send_json(send, 404, {"detail": "Not Found"})

    async def _drain_endpoint(self, scope: Scope, receive: Receive, send: Send, headers: Dict[str, str]) -> None:
        import hmac
        import os

        expected = os.getenv("ADMIN_API_KEY", "")
        auth = headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not _loopback(scope) or not expected:
            await _send_json(send, 404, {"detail": "Not Found"})
            return
        if not hmac.compare_digest(token.encode(), expected.encode()):
            await _send_json(send, 401, {"detail": "Unauthorized"})
            return
        if scope["method"] == "DELETE":
            await drain_state.stop()
            await _send_json(send, 200, {"draining": False})
            return
        if scope["method"] != "POST":
            await _send_json(send, 405, {"detail": "Method Not Allowed"})
            return
        try:
            raw = await _read_body(receive)
            body = json.loads(raw or b"{}")
            await drain_state.start(str(body.get("target", "")), str(body.get("target_boot_id", "")))
        except (ValueError, AttributeError, DrainError) as exc:
            await _send_json(send, 400, {"detail": str(exc)})
            return
        await _send_json(send, 200, {"draining": True, "drain_target": drain_state.describe_target()})

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
