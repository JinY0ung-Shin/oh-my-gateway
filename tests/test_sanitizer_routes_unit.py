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
from src.runtime_config import runtime_config


@pytest.fixture(autouse=True)
def _enable_sanitizer(monkeypatch):
    """Most tests assume the sanitizer accepts requests; the disabled-path test
    overrides this with its own ``runtime_config.set(..., False)`` call.
    """
    monkeypatch.setenv("SANITIZER_ENABLED", "true")
    runtime_config.reset("sanitizer_enabled")
    yield
    runtime_config.reset("sanitizer_enabled")


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


def test_streaming_upstream_returns_json_error_is_passed_through_verbatim():
    """Upstream may return a JSON error even when client asked for SSE.

    The sanitizer must forward the raw payload (not silently drop it through
    the SSE parser), so the client can see the real error.
    """
    err_body = json.dumps({"error": {"message": "model not found", "type": "not_found_error"}}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "application/json"}, content=err_body)

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "missing", "stream": True, "messages": []},
    )
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.content == err_body


def test_verify_api_key_rejects_unauthorized_request(monkeypatch):
    """When API_KEY is configured, /v1/messages must require Bearer auth."""
    from src.auth import auth_manager

    monkeypatch.setattr(auth_manager, "runtime_api_key", "secret-key")

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        # Should never be called — auth must reject before forwarding.
        raise AssertionError("upstream was contacted despite missing API key")

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
    )
    assert resp.status_code == 401


def test_verify_api_key_accepts_correct_bearer(monkeypatch):
    from src.auth import auth_manager

    monkeypatch.setattr(auth_manager, "runtime_api_key", "secret-key")

    body = json.dumps({"id": "msg_1"}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert resp.status_code == 200
    assert resp.content == body


def _capture_request_handler(response: httpx.Response):
    """Return (handler, captured) where captured["request"] holds the last call."""
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return response

    return handler, captured


def test_client_authorization_is_not_forwarded_upstream(monkeypatch):
    """Client bearer authenticates the gateway; localhost upstream gets no bearer.

    Even though localhost is the same trust boundary, we still strip the
    client's Authorization so the gateway key doesn't accidentally become a
    de-facto credential for whatever runs on the loopback port.
    """
    from src.auth import auth_manager

    monkeypatch.setattr(auth_manager, "runtime_api_key", "secret-key")

    handler, captured = _capture_request_handler(
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")
    )
    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert resp.status_code == 200
    assert "authorization" not in {k.lower() for k in captured["request"].headers.keys()}


def test_disabled_via_runtime_config_returns_503():
    """Admin toggle sets sanitizer_enabled=False → route short-circuits to 503."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("upstream was contacted despite sanitizer being disabled")

    app = _make_app(handler)
    runtime_config.set("sanitizer_enabled", False)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
    )
    assert resp.status_code == 503
    assert json.loads(resp.content)["error"]["type"] == "service_unavailable"


def test_accept_encoding_is_not_forwarded_upstream():
    """We re-serialize SSE as plain UTF-8, so requesting compression upstream is wasteful."""
    handler, captured = _capture_request_handler(
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")
    )
    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
        headers={"Accept-Encoding": "gzip, deflate, br"},
    )
    assert resp.status_code == 200
    # httpx may add its own default accept-encoding; the important contract is
    # that the *client's* value did not bleed through verbatim.
    forwarded = captured["request"].headers.get("accept-encoding", "")
    assert "br" not in forwarded


def test_content_encoding_stripped_from_response():
    """httpx auto-decodes bodies; passing content-encoding to the client would mislead it."""
    import gzip

    body = json.dumps({"id": "msg_1"}).encode()
    gz_body = gzip.compress(body)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
            content=gz_body,
        )

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
    )
    assert resp.status_code == 200
    # httpx auto-decoded the body; we must not advertise it as still encoded.
    assert "content-encoding" not in {k.lower() for k in resp.headers.keys()}
    assert resp.content == body
