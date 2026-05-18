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
    get_tls_verify,
    get_upstream_url,
    is_enabled,
    is_openai_bridge_enabled,
)
from src.sanitizer.openai_bridge import (
    anthropic_request_to_openai_body,
    openai_response_to_anthropic_body,
    openai_stream_to_anthropic_events,
)
from src.sanitizer.stream_sanitizer import sanitize_events

logger = logging.getLogger(__name__)

router = APIRouter()


def _make_client(timeout: float | None) -> httpx.AsyncClient:
    """AsyncClient factory; tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=timeout, verify=get_tls_verify())

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

# Stripped from requests before forwarding to upstream:
# - ``accept-encoding``: httpx negotiates and auto-decompresses internally; if
#   we forwarded the client's value, upstream would compress and we'd decode
#   anyway — wasted upstream CPU.
_DROP_FROM_REQUEST = _HOP_BY_HOP | frozenset({"accept-encoding"})
_DROP_FROM_EXTERNAL_REQUEST = _DROP_FROM_REQUEST | frozenset({"authorization"})

# Stripped from the upstream response before returning to the client:
# - ``content-encoding``: httpx returns decoded bytes from .content/.aread()/
#   .aiter_lines(), and the SSE branch re-serializes events as plain UTF-8.
#   Forwarding the original encoding would mislead the client into decompressing
#   already-decoded data.
_DROP_FROM_RESPONSE = _HOP_BY_HOP | frozenset({"content-encoding"})


def _filter_headers(items, drop: frozenset) -> Dict[str, str]:
    return {k: v for k, v in items if k.lower() not in drop}


def _is_loopback_request(request: Request) -> bool:
    """Return True for SDK self-calls from the same host/container."""
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


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
                # TEMP DIAG: capture raw upstream events to keep verifying the
                # zero-payload delta fix in production traffic. Remove once the
                # behavior is fully confirmed.
                logger.warning("sanitizer raw upstream evt: %s", json.dumps(evt, ensure_ascii=False)[:500])
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
                logger.warning("sanitizer raw upstream evt (trailing): %s", json.dumps(evt, ensure_ascii=False)[:500])
                yield evt
        except json.JSONDecodeError:
            logger.warning("sanitizer: dropping trailing non-JSON SSE payload")


def _format_sse(evt: Dict[str, Any]) -> bytes:
    """Serialize an event dict back into an SSE frame."""
    etype = evt.get("type", "message")
    data = json.dumps(evt, separators=(",", ":"), ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n".encode("utf-8")


async def _iter_openai_sse_chunks(
    response: httpx.Response,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield parsed chunk dicts from an OpenAI ``/v1/chat/completions`` SSE.

    OpenAI's wire format is the spec-minimal SSE: each frame is one
    ``data: <json>\\n\\n`` block. The terminal ``data: [DONE]`` sentinel
    signals end-of-stream and carries no payload to forward.
    """
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            if payload.strip() == "[DONE]":
                continue
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("bridge: skipping non-JSON OpenAI SSE payload: %r", payload[:200])
                continue
            if isinstance(evt, dict):
                yield evt
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        payload = "\n".join(data_lines)
        if payload.strip() == "[DONE]":
            return
        try:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                yield evt
        except json.JSONDecodeError:
            logger.warning("bridge: dropping trailing non-JSON OpenAI SSE payload")


@router.post("/v1/messages")
async def sanitize_messages(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Response:
    """Proxy ``/v1/messages`` to the upstream and rewrite the SSE stream."""
    if not is_enabled():
        # The route is always mounted so admins can flip it on/off at runtime,
        # but to the outside world a disabled sanitizer must look exactly as if
        # ``/v1/messages`` were never registered. This preserves the legacy
        # contract that tests/clients depend on (``status in (404, 405)``).
        return Response(
            status_code=404,
            content=json.dumps(
                {"error": {"type": "not_found", "message": "Not Found"}}
            ).encode("utf-8"),
            media_type="application/json",
        )

    is_loopback = _is_loopback_request(request)
    if not is_loopback:
        await verify_api_key(request, credentials)

    body = await request.body()
    drop_headers = _DROP_FROM_REQUEST if is_loopback else _DROP_FROM_EXTERNAL_REQUEST
    fwd_headers = _filter_headers(request.headers.items(), drop_headers)

    # Decide stream vs non-stream from the request body, falling back to
    # non-stream on parse failure (matches Anthropic API behavior).
    is_stream = False
    parsed_body: Dict[str, Any] = {}
    try:
        parsed_body = json.loads(body) if body else {}
        if not isinstance(parsed_body, dict):
            parsed_body = {}
        is_stream = bool(parsed_body.get("stream", False))
    except json.JSONDecodeError:
        pass

    use_bridge = is_openai_bridge_enabled() and isinstance(parsed_body, dict) and parsed_body
    timeout = get_request_timeout_seconds()
    client = _make_client(timeout)

    if use_bridge:
        # Translate Anthropic → OpenAI and target the upstream's OpenAI route.
        # ``content-type`` must reflect the rewritten JSON body; ``accept`` is
        # left as-is so the upstream knows we want SSE when streaming.
        openai_body = anthropic_request_to_openai_body(parsed_body)
        openai_bytes = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")
        bridge_headers = dict(fwd_headers)
        bridge_headers["content-type"] = "application/json"
        upstream_url = f"{get_upstream_url()}/v1/chat/completions"
        # TEMP DIAG: log the outgoing OpenAI body so we can diagnose 5xx
        # responses from the upstream (LiteLLM/vLLM rejecting specific
        # message shapes). Remove once the bridge is stable in production.
        logger.warning(
            "sanitizer bridge OpenAI request body: %s",
            json.dumps(openai_body, ensure_ascii=False)[:4000],
        )
        upstream_req = client.build_request(
            "POST",
            upstream_url,
            content=openai_bytes,
            headers=bridge_headers,
        )
    else:
        upstream_url = f"{get_upstream_url()}/v1/messages"
        upstream_req = client.build_request(
            "POST",
            upstream_url,
            content=body,
            headers=fwd_headers,
        )

    if not is_stream:
        try:
            upstream = await client.send(upstream_req)
            resp_headers = _filter_headers(upstream.headers.items(), _DROP_FROM_RESPONSE)
            content = upstream.content
            media_type = upstream.headers.get("content-type")
            # On the bridge path the upstream returned an OpenAI-shaped JSON;
            # translate it to Anthropic before handing back. Anything else
            # (error responses, non-JSON) is forwarded verbatim so the client
            # can see what really happened.
            if (
                use_bridge
                and 200 <= upstream.status_code < 300
                and (media_type or "").lower().startswith("application/json")
            ):
                try:
                    openai_resp = json.loads(content)
                    if isinstance(openai_resp, dict):
                        anthropic_resp = openai_response_to_anthropic_body(openai_resp)
                        content = json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8")
                        media_type = "application/json"
                        # ``content-length`` is invalidated by the rewrite;
                        # leave Starlette to recompute it.
                        resp_headers.pop("content-length", None)
                except json.JSONDecodeError:
                    pass
            return Response(
                content=content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=media_type,
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
            # TEMP DIAG: surface upstream error bodies so 5xx responses can
            # be diagnosed from a single log line instead of needing a tcpdump.
            if upstream.status_code >= 400:
                logger.warning(
                    "sanitizer upstream error %s ctype=%s body=%s",
                    upstream.status_code,
                    upstream_ctype,
                    (content[:2000].decode("utf-8", errors="replace") if content else "<empty>"),
                )
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

    if use_bridge:
        bridge_model = parsed_body.get("model") or openai_body.get("model") or ""

        async def body_iter() -> AsyncIterator[bytes]:
            try:
                anthropic_events = openai_stream_to_anthropic_events(
                    _iter_openai_sse_chunks(upstream),
                    model=str(bridge_model),
                )
                # Run the bridge output through ``sanitize_events`` too so any
                # invariant slip in the conversion still gets caught (empty
                # delta drop, monotonic indices, dangling-block close).
                async for sanitized in sanitize_events(anthropic_events):
                    yield _format_sse(sanitized)
            finally:
                await upstream.aclose()
                await client.aclose()
    else:
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
