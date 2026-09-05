"""ASGI admission-control middleware for agent-run endpoints.

Deliberately a **pure ASGI** middleware rather than a
:class:`~starlette.middleware.base.BaseHTTPMiddleware` subclass like the rest
of :mod:`src.main`. ``BaseHTTPMiddleware.call_next`` returns as soon as the
response *headers* are ready, which for a ``StreamingResponse`` is before the
agent has done any work — releasing the slot there would leave the cap with no
effect on the streaming path, which is the main path. A pure ASGI app call
does not return until the final body chunk has been sent, so a plain
``try``/``finally`` releases at exactly the right moment and cannot leak a
slot on cancellation or error.

This middleware also carries two request-boundary guards that need raw ASGI
access before FastAPI parses the body:

* enforce ``MAX_REQUEST_SIZE`` against the **actual received bytes**, including
  ``Transfer-Encoding: chunked`` requests with no ``Content-Length``;
* bind an optional ``USER_API_KEYS`` credential-derived principal to request
  state/body/query/header identity so caller-controlled ``user`` values cannot
  select another tenant workspace.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import metrics
from src.auth import auth_manager
from src.concurrency import turn_limiter
from src.constants import MAX_REQUEST_SIZE

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

# Request methods whose bodies are drained once at the ASGI boundary. FastAPI
# would materialize these bodies anyway; doing it here lets us enforce the real
# byte count before pydantic/multipart parsing and then replay the body exactly
# once to the downstream app.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Only peek at bodies small enough to buffer cheaply for legacy per-user
# accounting. Larger bodies still get the global cap; they just fall back to
# anonymous per-user accounting. Credential-scoped callers never need this
# peek because their principal is already on scope state.
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
    """Clear the rejection-warning throttle. Tests only."""
    global _last_warned, _suppressed
    with _log_lock:
        _last_warned = float("-inf")
        _suppressed = 0


# Keys placed on the ASGI ``scope["state"]`` dict (surfaced to routes as
# ``request.state``) so a handler can take over the slot's lifetime.
SLOT_KEY = "turn_slot"
TRANSFERRED_KEY = "turn_slot_transferred"
AUTH_USER_KEY = "auth_user"


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


async def _buffer_body_with_limit(
    receive: Receive, limit: int
) -> Tuple[bytes, Optional[Message], bool]:
    """Drain one HTTP body while enforcing *limit* against received bytes."""
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return bytes(body), message, False
        chunk = message.get("body", b"")
        if len(body) + len(chunk) > limit:
            return b"", None, True
        body.extend(chunk)
        if not message.get("more_body", False):
            return bytes(body), None, False


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
    ``http.disconnect``. ``StreamingResponse`` runs a concurrent task that
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


def _replace_header(scope: Scope, name: str, value: str) -> None:
    """Replace one ASGI request header in-place, case-insensitively."""
    target = name.lower().encode("latin-1")
    headers = [
        (key, val)
        for key, val in scope.get("headers", [])
        if key.lower() != target
    ]
    headers.append((target, value.encode("latin-1")))
    scope["headers"] = headers


def _normalize_buffered_body_headers(scope: Scope, body: bytes) -> None:
    """Expose the replayed body as a fixed-length body to downstream layers."""
    filtered = []
    for key, val in scope.get("headers", []):
        lower = key.lower()
        if lower in {b"content-length", b"transfer-encoding"}:
            continue
        filtered.append((key, val))
    filtered.append((b"content-length", str(len(body)).encode("ascii")))
    scope["headers"] = filtered


def _bearer_token(scope: Scope) -> Optional[str]:
    value = Headers(scope=scope).get("authorization")
    if not value:
        return None
    scheme, sep, token = value.partition(" ")
    if not sep or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _credential_principal(scope: Scope) -> Optional[str]:
    """Return the user bound to a configured USER_API_KEYS token, if any."""
    token = _bearer_token(scope)
    if token is None:
        return None
    valid, principal = auth_manager.authenticate_gateway_key(token)
    return principal if valid else None


def _bind_workspace_identity_header(scope: Scope, principal: str) -> None:
    """Make /files/* consume the same credential-derived tenant identity."""
    header = os.getenv("WORKSPACE_USER_HEADER", "X-User-Email")
    _replace_header(scope, header, principal)


def _bind_response_body_user(body: bytes, principal: str) -> bytes:
    """Overwrite caller-supplied Responses ``user`` with the auth principal."""
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(parsed, dict):
        return body
    if parsed.get("user") == principal:
        return body
    parsed["user"] = principal
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _bind_query_user(scope: Scope, principal: str) -> None:
    """Replace any caller-owned ``user`` query parameter with *principal*."""
    raw = scope.get("query_string", b"")
    try:
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return
    pairs = [(key, value) for key, value in pairs if key != "user"]
    pairs.append(("user", principal))
    scope["query_string"] = urlencode(pairs, doseq=True).encode("utf-8")


def _bind_principal_to_request(scope: Scope, principal: str, body: Optional[bytes]) -> bytes | None:
    """Publish and project the credential-derived principal across identity surfaces."""
    state = scope.setdefault("state", {})
    state[AUTH_USER_KEY] = principal

    # Workspace file routes historically read an identity header. For a
    # credential-scoped request, overwrite that header so a caller cannot point
    # the file browser at another user's persistent workspace.
    _bind_workspace_identity_header(scope, principal)

    path = scope.get("path", "")
    method = scope.get("method", "")
    if path == "/v1/responses" and method == "POST" and body is not None:
        body = _bind_response_body_user(body, principal)

    # Retrieval/cancel routes already implement non-revealing user scoping;
    # inject the authenticated principal into their existing query parameter.
    if path.startswith("/v1/responses/") and method in {"GET", "DELETE", "POST"}:
        _bind_query_user(scope, principal)
    if path.startswith("/v1/sessions/") and path.endswith("/pending-events"):
        _bind_query_user(scope, principal)
    return body


class ConcurrencyLimitMiddleware:
    """Apply request-boundary guards and reject agent runs beyond configured limits."""

    def __init__(self, app: ASGIApp, limiter: Any = None) -> None:
        self.app = app
        self.limiter = limiter if limiter is not None else turn_limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        original_receive = receive
        body: Optional[bytes] = None
        method = scope.get("method", "")

        # Fast reject a declared oversized body, then still count the actual
        # bytes for accepted declarations. This closes the chunked/no-CL bypass
        # in RequestSizeLimitMiddleware and also catches understated lengths.
        headers = Headers(scope=scope)
        raw_length = headers.get("content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > MAX_REQUEST_SIZE:
                    await self._send_413(send)
                    return
            except ValueError:
                # Uvicorn normally rejects malformed Content-Length before ASGI;
                # if one reaches us, let FastAPI/server validation own it.
                pass

        if method in _BODY_METHODS:
            body, disconnected, too_large = await _buffer_body_with_limit(
                original_receive, MAX_REQUEST_SIZE
            )
            if too_large:
                await self._send_413(send)
                return
            if disconnected is not None:
                receive = _replay_message(disconnected, original_receive)
            else:
                principal = _credential_principal(scope)
                if principal is not None:
                    body = _bind_principal_to_request(scope, principal, body) or b""
                _normalize_buffered_body_headers(scope, body)
                receive = _replay(body, original_receive)
        else:
            principal = _credential_principal(scope)
            if principal is not None:
                _bind_principal_to_request(scope, principal, None)

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

        Credential-scoped callers use the authenticated principal from ASGI
        state. Legacy/unscoped callers fall back to the historical body peek.
        A body that is missing, oversized for the peek, or unparseable yields
        ``None``; the global concurrency cap still applies.
        """
        state = scope.get("state") or {}
        principal = state.get(AUTH_USER_KEY)
        if isinstance(principal, str) and principal:
            return principal, receive

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

    async def _send_413(self, send: Send) -> None:
        payload = json.dumps(
            {
                "error": {
                    "message": (
                        f"Request body too large. Maximum size is {MAX_REQUEST_SIZE} bytes."
                    ),
                    "type": "request_too_large",
                    "code": 413,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})

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
