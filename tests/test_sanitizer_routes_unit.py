"""Route-level tests for the sanitizer proxy.

Verifies that ``POST /v1/messages`` forwards to the upstream and the response
SSE stream is rewritten so block-type/delta-type pairs are consistent.
"""

from __future__ import annotations

import json
from typing import Iterable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.sanitizer.routes as sanitizer_routes


def _sse_payload(events: Iterable[dict]) -> bytes:
    """Encode a sequence of event dicts as Anthropic-style SSE bytes."""
    parts = []
    for evt in events:
        parts.append(f"event: {evt['type']}\ndata: {json.dumps(evt)}\n\n")
    return "".join(parts).encode("utf-8")


def _make_app(handler) -> FastAPI:
    app = FastAPI()
    app.include_router(sanitizer_routes.router)
    transport = httpx.MockTransport(handler)

    def _factory(timeout):  # noqa: ARG001
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    sanitizer_routes._make_client = _factory  # type: ignore[assignment]
    return app


@pytest.fixture
def buggy_litellm():
    """MockTransport that mimics LiteLLM #21128: text block start with thinking_delta."""

    events = [
        {"type": "message_start", "message": {"id": "msg_1", "role": "assistant"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "User asks..."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello!"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    body = _sse_payload(events)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    return handler


def _parse_sse(raw: str) -> list[dict]:
    events: list[dict] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].lstrip()))
    return events


def test_streaming_rewrites_buggy_litellm_stream(buggy_litellm):
    app = _make_app(buggy_litellm)
    client = TestClient(app)

    resp = client.post(
        "/v1/messages",
        json={"model": "GLM-5.1-FP8", "stream": True, "messages": []},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)

    # The thinking_delta must live inside a thinking block, not the text block.
    for evt in events:
        if evt["type"] == "content_block_delta" and evt["delta"]["type"] == "thinking_delta":
            # find enclosing block start
            idx = evt["index"]
            block_start = next(
                e
                for e in events
                if e["type"] == "content_block_start" and e["index"] == idx
            )
            assert block_start["content_block"]["type"] == "thinking"


def test_non_streaming_passes_through_verbatim():
    body = json.dumps({"id": "msg_1", "content": [{"type": "text", "text": "hi"}]}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "GLM-5.1-FP8", "stream": False, "messages": []},
    )
    assert resp.status_code == 200
    assert resp.content == body
