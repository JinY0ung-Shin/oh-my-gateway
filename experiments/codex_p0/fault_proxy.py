#!/usr/bin/env python3
"""Isolated Responses fault-injection proxy for Codex P0a-2 (#163).

This proxy is intentionally destructive to the *client connection*. Run it only
in front of an isolated LiteLLM/model-gateway replica or dedicated test route.
It never logs request bodies or header values.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
}

# httpx aiter_bytes()/aread() returns decoded content, so do not advertise the
# upstream content-encoding to the downstream Codex client.
RESPONSE_HOP_BY_HOP = HOP_BY_HOP | {"content-encoding"}


@dataclass(slots=True)
class FaultConfig:
    upstream_base_url: str
    mode: str
    after_events: int
    delay_s: float
    upstream_connect_timeout_s: float


CONFIG: FaultConfig | None = None
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _config() -> FaultConfig:
    if CONFIG is None:
        raise RuntimeError("fault proxy is not configured")
    return CONFIG


def _request_headers(request: Request) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP
    ]


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in RESPONSE_HOP_BY_HOP
    }


def _find_sse_delimiter(buffer: bytes) -> tuple[int, int] | None:
    candidates: list[tuple[int, int]] = []
    for delimiter in (b"\r\n\r\n", b"\n\n"):
        index = buffer.find(delimiter)
        if index >= 0:
            candidates.append((index, len(delimiter)))
    return min(candidates) if candidates else None


async def _iter_sse_frames(response: httpx.Response) -> AsyncIterator[bytes]:
    buffer = b""
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        buffer += chunk
        while True:
            found = _find_sse_delimiter(buffer)
            if found is None:
                break
            index, delimiter_len = found
            end = index + delimiter_len
            yield buffer[:end]
            buffer = buffer[end:]
    if buffer:
        yield buffer


async def _raw_passthrough(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()


async def _faulted_sse(response: httpx.Response, config: FaultConfig) -> AsyncIterator[bytes]:
    index = 0
    held: bytes | None = None
    try:
        async for frame in _iter_sse_frames(response):
            index += 1

            if config.mode == "delay_first_event" and index == 1:
                await asyncio.sleep(config.delay_s)
            elif config.mode == "delay_each_event":
                await asyncio.sleep(config.delay_s)

            if config.mode == "truncate_after_events":
                if index > config.after_events:
                    return
                yield frame
                continue

            if config.mode == "abort_after_events":
                if index > config.after_events:
                    raise ConnectionResetError("P0 injected stream abort")
                yield frame
                continue

            if config.mode == "malformed_event_after":
                yield frame
                if index == config.after_events:
                    yield b"data: {P0_MALFORMED_JSON\n\n"
                continue

            if config.mode == "duplicate_event":
                yield frame
                if index == config.after_events:
                    yield frame
                continue

            if config.mode == "reorder_adjacent":
                if index < config.after_events:
                    yield frame
                    continue
                if index == config.after_events:
                    held = frame
                    continue
                if index == config.after_events + 1 and held is not None:
                    yield frame
                    yield held
                    held = None
                    continue
                yield frame
                continue

            yield frame

        if held is not None:
            yield held
    finally:
        await response.aclose()


@app.get("/__p0_health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": _config().mode}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    config = _config()

    if config.mode == "http_429":
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "1"},
            content={
                "error": {
                    "message": "P0 injected 429",
                    "type": "rate_limit_error",
                    "code": "p0_injected_429",
                }
            },
        )
    if config.mode == "http_500":
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "P0 injected 500",
                    "type": "server_error",
                    "code": "p0_injected_500",
                }
            },
        )

    body = await request.body()
    upstream_url = f"{config.upstream_base_url.rstrip('/')}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    timeout = httpx.Timeout(
        connect=config.upstream_connect_timeout_s,
        read=None,
        write=30.0,
        pool=config.upstream_connect_timeout_s,
    )
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=_request_headers(request),
            content=body,
        )
        upstream = await client.send(upstream_request, stream=True)
    except Exception:
        await client.aclose()
        raise

    headers = _response_headers(upstream)
    content_type = upstream.headers.get("content-type", "")

    if config.mode == "drop_before_body":

        async def _drop() -> AsyncIterator[bytes]:
            try:
                raise ConnectionResetError("P0 injected drop before first response body byte")
                yield b""  # pragma: no cover - keeps this an async generator
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(_drop(), status_code=upstream.status_code, headers=headers)

    if "text/event-stream" not in content_type.lower():
        try:
            content = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        return Response(content=content, status_code=upstream.status_code, headers=headers)

    async def _stream() -> AsyncIterator[bytes]:
        try:
            if config.mode == "passthrough":
                async for chunk in _raw_passthrough(upstream):
                    yield chunk
            else:
                async for chunk in _faulted_sse(upstream, config):
                    yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(_stream(), status_code=upstream.status_code, headers=headers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--mode",
        required=True,
        choices=[
            "passthrough",
            "http_429",
            "http_500",
            "drop_before_body",
            "truncate_after_events",
            "abort_after_events",
            "malformed_event_after",
            "duplicate_event",
            "reorder_adjacent",
            "delay_first_event",
            "delay_each_event",
        ],
    )
    parser.add_argument("--after-events", type=int, default=2)
    parser.add_argument("--delay-s", type=float, default=2.0)
    parser.add_argument("--upstream-connect-timeout-s", type=float, default=10.0)
    parser.add_argument(
        "--i-understand-isolated-test-only",
        action="store_true",
        help="required acknowledgement that this is not pointed at shared production",
    )
    args = parser.parse_args()

    if not args.i_understand_isolated_test_only:
        parser.error("refusing to start without --i-understand-isolated-test-only")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.after_events <= 0 or args.delay_s < 0 or args.upstream_connect_timeout_s <= 0:
        parser.error("fault parameters are out of range")

    global CONFIG
    CONFIG = FaultConfig(
        upstream_base_url=args.upstream_base_url,
        mode=args.mode,
        after_events=args.after_events,
        delay_s=args.delay_s,
        upstream_connect_timeout_s=args.upstream_connect_timeout_s,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
