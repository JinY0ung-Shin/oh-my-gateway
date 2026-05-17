"""FastAPI route that proxies ``POST /v1/messages`` to an upstream LiteLLM-like
service and sanitizes the SSE response stream.

The module only takes one gateway-side dependency — ``verify_api_key`` — so the
endpoint participates in the same ``API_KEY`` protection as the rest of the
``/v1/*`` surface. All sanitizer-specific logic stays self-contained.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from src.auth import security, verify_api_key
from src.sanitizer.config import (
    get_request_timeout_seconds,
    get_upstream_api_key,
    get_upstream_url,
)
from src.sanitizer.stream_sanitizer import sanitize_events

logger = logging.getLogger(__name__)

router = APIRouter()


def _make_client(timeout: float | None) -> httpx.AsyncClient:
    """AsyncClient factory; tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=timeout)

# Hop-by-hop headers (RFC 7230 §6.1) plus transport-framing headers set by
# httpx/Starlette automatically. Never forwarded in either direction.
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

# Stripped from the request before forwarding to upstream:
# - ``authorization``: the gateway has its own API key; upstream is a different
#   trust boundary and gets its bearer from SANITIZER_UPSTREAM_API_KEY.
# - ``accept-encoding``: httpx negotiates and auto-decompresses internally; if
#   we forwarded the client's value, upstream would compress and we'd decode
#   anyway — wasted upstream CPU.
_DROP_FROM_REQUEST = _HOP_BY_HOP | frozenset({"authorization", "accept-encoding"})

# Stripped from the upstream response before returning to the client:
# - ``content-encoding``: httpx returns decoded bytes from .content/.aread()/
#   .aiter_lines(), and the SSE branch re-serializes events as plain UTF-8.
#   Forwarding the original encoding would mislead the client into decompressing
#   already-decoded data.
_DROP_FROM_RESPONSE = _HOP_BY_HOP | frozenset({"content-encoding"})


def _filter_headers(items, drop: frozenset) -> Dict[str, str]:
    return {k: v for k, v in items if k.lower() not in drop}


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
async def sanitize_messages(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Response:
    """Proxy ``/v1/messages`` to the upstream and rewrite the SSE stream."""
    await verify_api_key(request, credentials)

    body = await request.body()
    fwd_headers = _filter_headers(request.headers.items(), _DROP_FROM_REQUEST)
    upstream_key = get_upstream_api_key()
    if upstream_key is not None:
        fwd_headers["Authorization"] = f"Bearer {upstream_key}"

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
            resp_headers = _filter_headers(upstream.headers.items(), _DROP_FROM_RESPONSE)
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream.headers.get("content-type"),
            )
        finally:
            await client.aclose()

    upstream = await client.send(upstream_req, stream=True)

    # The sanitizer only knows how to rewrite Anthropic-style SSE. If upstream
    # returned a JSON/HTML error (or anything else), passing it through the
    # SSE parser would silently drop the body and leave the client with an
    # empty event stream — so we forward non-SSE responses verbatim.
    upstream_ctype = upstream.headers.get("content-type", "")
    if not upstream_ctype.lower().startswith("text/event-stream"):
        try:
            content = await upstream.aread()
            resp_headers = _filter_headers(upstream.headers.items(), _DROP_FROM_RESPONSE)
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream_ctype or None,
            )
        finally:
            await upstream.aclose()
            await client.aclose()

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for sanitized in sanitize_events(_iter_sse_events(upstream)):
                yield _format_sse(sanitized)
        finally:
            await upstream.aclose()
            await client.aclose()

    resp_headers = _filter_headers(upstream.headers.items(), _DROP_FROM_RESPONSE)
    # Streaming-specific headers must not be cached or buffered by proxies.
    resp_headers["Cache-Control"] = "no-cache"
    resp_headers["X-Accel-Buffering"] = "no"

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type="text/event-stream",
    )
