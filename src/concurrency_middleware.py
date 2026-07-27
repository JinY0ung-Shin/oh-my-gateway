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
import threading
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
#
# Matched EXACTLY, never by prefix. ``POST /v1/responses/{id}/cancel`` lives
# under the same prefix but spawns no work — it is the control that *frees* a
# slot. Admission-controlling it deadlocks the gateway: a saturated pool
# (background turns can hold slots for BACKGROUND_RESPONSE_TIMEOUT_S) would
# 503 the only request able to drain it.
GUARDED_PATHS: Tuple[str, ...] = ("/v1/responses", "/v1/agents/messages")

# Only peek at bodies small enough to buffer cheaply, matching the existing
# limit in ``RequestLoggingMiddleware``. Larger bodies still get the global
# cap; they just fall back to anonymous per-user accounting.
_MAX_PEEK_BYTES = 100_000

_RETRY_AFTER_SECONDS = 30

# Minimum gap between rejection WARNINGs. Overload is a sustained condition,
# not a per-request event, so one line per window plus a suppressed count says
# everything the log needs to; the Prometheus counter carries the exact rate.
_REJECTION_LOG_WINDOW_S = 10.0

# Throttle state is module-level, not per-instance: the middleware is
# constructed once per app and the condition it reports (gateway overloaded)
# is global, so one window across the process is the right granularity.
_log_lock = threading.Lock()
_last_warned = float("-inf")
_suppressed = 0


def reset_rejection_log_throttle() -> None:
    """Clear the rejection-warning throttle.  Tests only."""
    global _last_warned, _suppressed
    with _log_lock:
        _last_warned = float("-inf")
        _suppressed = 0

# Keys placed on the ASGI ``scope["state"]`` dict (surfaced to routes as
# ``request.state``) so a handler can take over the slot's lifetime.
SLOT_KEY = "turn_slot"
TRANSFERRED_KEY = "turn_slot_transferred"


def take_turn_slot(request) -> Optional[Any]:
    """Transfer ownership of this request's admission slot to the caller.

    ``background: true`` returns a ``queued`` response immediately and keeps
    running the turn in a detached task. Without this handoff the middleware
    would free the slot when the queued response is sent, and background turns
    would escape ``MAX_CONCURRENT_TURNS`` entirely.

    The caller becomes responsible for calling ``release()`` — always from a
    ``finally``. Returns ``None`` when no slot is held (limits disabled, or an
    unguarded path), so callers can treat it as optional.
    """
    slot = getattr(request.state, SLOT_KEY, None)
    if slot is not None:
        setattr(request.state, TRANSFERRED_KEY, True)
    return slot


def _is_guarded(scope: Scope) -> bool:
    if scope.get("type") != "http":
        return False
    if scope.get("method") != "POST":
        return False
    return scope.get("path", "") in GUARDED_PATHS


async def _buffer_body(receive: Receive) -> Tuple[bytes, Optional[Message]]:
    """Drain the request body, returning ``(body, disconnect_message)``.

    A client that vanishes mid-upload sends ``http.disconnect``, which carries
    neither ``body`` nor ``more_body``. Treating it as a clean end-of-body
    would hand the route an empty payload and turn a dropped request into a
    fabricated 422 in the logs and request metrics, so it is returned
    separately for the caller to replay verbatim.
    """
    body = b""
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return body, message
        body += message.get("body", b"")
        if not message.get("more_body", False):
            return body, None


def _replay_message(message: Message, original: Receive) -> Receive:
    """Return a ``receive`` that re-delivers *message* once, then delegates."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return message
        return await original()

    return receive


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
            metrics.record_turn_rejected(reason)
            self._log_rejection(scope.get("path", ""), reason)
            await self._send_503(send)
            return

        metrics.set_turns_in_flight(self.limiter.in_flight)
        # Published on request.state so a handler that outlives the response
        # (background mode) can take ownership; see take_turn_slot().
        state = scope.setdefault("state", {})
        state[SLOT_KEY] = slot
        state.pop(TRANSFERRED_KEY, None)

        started = time.monotonic()
        try:
            await self.app(scope, receive, send)
        finally:
            if not state.get(TRANSFERRED_KEY):
                slot.release()
            state.pop(SLOT_KEY, None)
            metrics.set_turns_in_flight(self.limiter.in_flight)
            # This middleware is one of the few places that observes a
            # streaming response all the way to its final body chunk, so it
            # is also where the true end-to-end duration becomes measurable.
            metrics.record_stream_duration(
                path=scope.get("path", ""),
                duration_seconds=time.monotonic() - started,
            )

    def _log_rejection(self, path: str, reason: str) -> None:
        """Warn about a rejection at most once per window, with a suppressed count.

        Rejections short-circuit before the per-IP rate limiter, so the
        limiter structurally cannot damp this: a client retrying against a
        full gateway would otherwise write one WARNING per attempt and turn
        an overload into a log flood. ``gateway_turns_rejected_total`` still
        counts every single rejection exactly.
        """
        global _last_warned, _suppressed

        now = time.monotonic()
        with _log_lock:
            _suppressed += 1
            if now - _last_warned < _REJECTION_LOG_WINDOW_S:
                return
            suppressed = _suppressed - 1
            _suppressed = 0
            _last_warned = now

        logger.warning(
            "Rejecting %s: gateway at %s concurrency limit (in-flight=%d)%s",
            path,
            reason,
            self.limiter.in_flight,
            f"; {suppressed} more suppressed in the last {_REJECTION_LOG_WINDOW_S:g}s"
            if suppressed
            else "",
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

        body, disconnected = await _buffer_body(receive)
        if disconnected is not None:
            # Let the disconnect reach the app instead of replaying an empty
            # body as if the client had sent one.
            return None, _replay_message(disconnected, receive)

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
