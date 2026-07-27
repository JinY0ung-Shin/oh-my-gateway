"""ASGI admission-control middleware for agent-run endpoints.

Deliberately a **pure ASGI** middleware rather than a
:class:`~starlette.middleware.base.BaseHTTPMiddleware` subclass like the rest
of :mod:`src.main`.  ``BaseHTTPMiddleware.call_next`` returns as soon as the
response *headers* are ready, which for a ``StreamingResponse`` is before the
agent has done any work — releasing the slot there would leave the cap with no
effect on the streaming path, which is the main path.  A pure ASGI app call
does not return until the final body chunk has been sent, so a plain
``try``/``finally`` releases at exactly the right moment and cannot leak a
slot on cancellation or error.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import metrics
from src.concurrency import turn_limiter

logger = logging.getLogger(__name__)

# Endpoints that spawn agent work. Everything else (models, health, admin,
# workspace file I/O) is cheap and must never be blocked by a full gateway —
# in particular ``/admin`` has to stay reachable to diagnose the overload.
GUARDED_PATHS: Tuple[str, ...] = ("/v1/responses", "/v1/agents/messages")

# Only peek at bodies small enough to buffer cheaply, matching the existing
# limit in ``RequestLoggingMiddleware``. Larger bodies still get the global
# cap; they just fall back to anonymous per-user accounting.
_MAX_PEEK_BYTES = 100_000

_RETRY_AFTER_SECONDS = 30


def _is_guarded(scope: Scope) -> bool:
    if scope.get("type") != "http":
        return False
    if scope.get("method") != "POST":
        return False
    path = scope.get("path", "")
    return any(path == p or path.startswith(p + "/") for p in GUARDED_PATHS)


async def _buffer_body(receive: Receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


def _replay(body: bytes, original: Receive) -> Receive:
    """Return a ``receive`` that replays an already-consumed body, once.

    After the replay it delegates back to *original* rather than returning
    ``http.disconnect``.  ``StreamingResponse`` runs a concurrent task that
    polls ``receive()`` to notice client disconnects, so a replay that
    reports disconnect as soon as the body is drained would cancel every
    stream immediately after its first event.
    """
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()

    return receive


class ConcurrencyLimitMiddleware:
    """Reject agent runs beyond the configured in-flight limits with 503."""

    def __init__(self, app: ASGIApp, limiter: Any = None) -> None:
        self.app = app
        self.limiter = limiter if limiter is not None else turn_limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_guarded(scope):
            await self.app(scope, receive, send)
            return

        user, receive = await self._extract_user(scope, receive)

        slot, reason = self.limiter.try_acquire(user)
        if slot is None:
            logger.warning(
                "Rejecting %s: gateway at %s concurrency limit (in-flight=%d)",
                scope.get("path"),
                reason,
                self.limiter.in_flight,
            )
            metrics.record_turn_rejected(reason)
            await self._send_503(send)
            return

        metrics.set_turns_in_flight(self.limiter.in_flight)
        started = time.monotonic()
        try:
            await self.app(scope, receive, send)
        finally:
            slot.release()
            metrics.set_turns_in_flight(self.limiter.in_flight)
            # This middleware is one of the few places that observes a
            # streaming response all the way to its final body chunk, so it
            # is also where the true end-to-end duration becomes measurable.
            metrics.record_stream_duration(
                path=scope.get("path", ""),
                duration_seconds=time.monotonic() - started,
            )

    async def _extract_user(self, scope: Scope, receive: Receive) -> Tuple[Optional[str], Receive]:
        """Return ``(user, receive)`` — the body is replayed when consumed.

        A body that is missing, oversized, or unparseable yields ``None``:
        per-user accounting degrades to a shared anonymous bucket rather than
        failing the request, since the global cap still applies.
        """
        headers = Headers(scope=scope)
        raw_length = headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError:
            length = None
        if length is None or length > _MAX_PEEK_BYTES:
            return None, receive

        body = await _buffer_body(receive)
        replayed = _replay(body, receive)
        if not body:
            return None, replayed
        try:
            parsed: Dict[str, Any] = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None, replayed
        if not isinstance(parsed, dict):
            return None, replayed
        user = parsed.get("user")
        return (user if isinstance(user, str) and user else None), replayed

    async def _send_503(self, send: Send) -> None:
        payload = json.dumps(
            {
                "error": {
                    "message": (
                        "Gateway is at capacity — too many agent runs in flight. "
                        f"Retry in {_RETRY_AFTER_SECONDS} seconds."
                    ),
                    "type": "server_overloaded",
                    "code": "concurrency_limit_exceeded",
                    "retry_after": _RETRY_AFTER_SECONDS,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"retry-after", str(_RETRY_AFTER_SECONDS).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})


__all__ = ["ConcurrencyLimitMiddleware", "GUARDED_PATHS"]
