"""FastAPI route that proxies ``POST /v1/messages`` to an upstream LiteLLM-like
service and sanitizes the SSE response stream.

This module is intentionally self-contained: it does **not** import from the
rest of the gateway (auth, sessions, rate limiter, logging adapters) so it can
be excised into its own service later without untangling dependencies.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from src.sanitizer.config import get_request_timeout_seconds, get_upstream_url
from src.sanitizer.stream_sanitizer import sanitize_events

logger = logging.getLogger(__name__)

router = APIRouter()


def _make_client(timeout: float | None) -> httpx.AsyncClient:
    """AsyncClient factory; tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=timeout)

# Headers that must not be forwarded upstream or back to the client because
# they describe the *transport* (length, encoding, host) rather than the
# request/response itself. ``httpx`` and ``Starlette`` set them automatically.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)


def _filter_headers(items) -> Dict[str, str]:
    return {k: v for k, v in items if k.lower() not in _HOP_BY_HOP}


async def _iter_sse_events(
    response: httpx.Response,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield parsed event dicts from an Anthropic-style SSE response.

    Anthropic SSE frames have the form:

        event: <type>
        data: <json>
        <blank line>

    The ``event:`` line is informational — the canonical type lives in the
    JSON payload's ``type`` field, which is what we operate on.
    """
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("sanitizer: skipping non-JSON SSE payload: %r", payload[:200])
                continue
            if isinstance(evt, dict):
                yield evt
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # ``event:``, ``id:``, ``retry:``, comments (``:``) — ignored; the JSON
        # payload's ``type`` field is authoritative.

    # Flush a final event missing a trailing blank line.
    if data_lines:
        payload = "\n".join(data_lines)
        try:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                yield evt
        except json.JSONDecodeError:
            logger.warning("sanitizer: dropping trailing non-JSON SSE payload")


def _format_sse(evt: Dict[str, Any]) -> bytes:
    """Serialize an event dict back into an SSE frame."""
    etype = evt.get("type", "message")
    data = json.dumps(evt, separators=(",", ":"), ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n".encode("utf-8")


@router.post("/v1/messages")
async def sanitize_messages(request: Request) -> Response:
    """Proxy ``/v1/messages`` to the upstream and rewrite the SSE stream."""
    body = await request.body()
    fwd_headers = _filter_headers(request.headers.items())

    # Decide stream vs non-stream from the request body, falling back to
    # non-stream on parse failure (matches Anthropic API behavior).
    is_stream = False
    try:
        parsed = json.loads(body) if body else {}
        if isinstance(parsed, dict):
            is_stream = bool(parsed.get("stream", False))
    except json.JSONDecodeError:
        pass

    upstream_url = f"{get_upstream_url()}/v1/messages"
    timeout = get_request_timeout_seconds()
    client = _make_client(timeout)

    upstream_req = client.build_request(
        "POST",
        upstream_url,
        content=body,
        headers=fwd_headers,
    )

    if not is_stream:
        try:
            upstream = await client.send(upstream_req)
            content = upstream.content
            resp_headers = _filter_headers(upstream.headers.items())
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream.headers.get("content-type"),
            )
        finally:
            await client.aclose()

    upstream = await client.send(upstream_req, stream=True)

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for sanitized in sanitize_events(_iter_sse_events(upstream)):
                yield _format_sse(sanitized)
        finally:
            await upstream.aclose()
            await client.aclose()

    resp_headers = _filter_headers(upstream.headers.items())
    # Streaming-specific headers must not be cached or buffered by proxies.
    resp_headers["Cache-Control"] = "no-cache"
    resp_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type="text/event-stream",
    )
