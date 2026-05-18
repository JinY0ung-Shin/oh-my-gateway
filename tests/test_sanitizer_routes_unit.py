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
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://upstream.test")
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


def test_upstream_uses_anthropic_base_url(monkeypatch):
    """ANTHROPIC_BASE_URL remains the real upstream the sanitizer forwards to."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm:4000")
    body = json.dumps({"id": "msg_1"}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://litellm:4000/v1/messages"
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "GLM-5.1-FP8", "stream": False, "messages": []},
    )
    assert resp.status_code == 200
    assert resp.content == body


def test_enabled_without_anthropic_base_url_returns_404(monkeypatch):
    """SANITIZER_ENABLED alone should not route traffic without an explicit upstream."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("upstream was contacted without ANTHROPIC_BASE_URL")

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
    )
    assert resp.status_code == 404


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
    """External client bearer authenticates the gateway, not the upstream.

    Non-loopback requests use gateway auth, so that credential must not become
    a de-facto upstream credential.
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


def test_loopback_authorization_is_forwarded_to_upstream(monkeypatch):
    """Claude SDK self-calls carry the real Anthropic/LiteLLM bearer upstream."""
    monkeypatch.setattr(sanitizer_routes, "_is_loopback_request", lambda request: True)

    handler, captured = _capture_request_handler(
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")
    )
    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
        headers={"Authorization": "Bearer upstream-key"},
    )
    assert resp.status_code == 200
    assert captured["request"].headers.get("authorization") == "Bearer upstream-key"


def test_disabled_via_runtime_config_returns_404():
    """Admin toggle off → route must look exactly absent to clients (404)."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("upstream was contacted despite sanitizer being disabled")

    app = _make_app(handler)
    runtime_config.set("sanitizer_enabled", False)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={"model": "x", "stream": False, "messages": []},
    )
    assert resp.status_code == 404


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


def test_bridge_streaming_translates_openai_to_anthropic(monkeypatch):
    """When ``SANITIZER_USE_OPENAI_BRIDGE`` is on, the route must hit the
    upstream's OpenAI chat-completions endpoint and translate the resulting
    SSE back into Anthropic shape end-to-end.

    Verifies the integration glue: upstream URL switch, request body
    rewrite, and OpenAI → Anthropic SSE conversion all wired together.
    """
    monkeypatch.setenv("SANITIZER_USE_OPENAI_BRIDGE", "true")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)

        # Mimic a vLLM-style OpenAI streaming response with reasoning_content
        # and a tool call — the exact pattern LiteLLM's Anthropic adapter
        # mishandles.
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "Bash",
                                        "arguments": '{"command":"ls"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "GaussO3.2-260402-vllm",
            "stream": True,
            "max_tokens": 1024,
            "tools": [
                {
                    "name": "Bash",
                    "description": "shell",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
            "messages": [{"role": "user", "content": "ls"}],
        },
    )

    # The upstream call must target the OpenAI route — not /v1/messages.
    assert captured["url"].endswith("/v1/chat/completions")
    # The body must have been translated to OpenAI shape.
    assert captured["body"]["messages"] == [{"role": "user", "content": "ls"}]
    assert captured["body"]["tools"][0]["type"] == "function"

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    # Expect proper Anthropic SSE structure: message_start, thinking block,
    # text block, tool_use block (with real id/name), message_delta, stop.
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"

    starts = [e for e in events if e["type"] == "content_block_start"]
    block_types = [s["content_block"]["type"] for s in starts]
    assert block_types == ["thinking", "text", "tool_use"]
    assert starts[2]["content_block"]["id"] == "call_1"
    assert starts[2]["content_block"]["name"] == "Bash"

    md = next(e for e in events if e["type"] == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use"


def test_bridge_non_streaming_translates_response_body(monkeypatch):
    """Non-streaming bridge path: upstream returns an OpenAI completion, the
    route must rewrite it to Anthropic shape before returning."""
    monkeypatch.setenv("SANITIZER_USE_OPENAI_BRIDGE", "true")

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        openai_body = {
            "id": "chatcmpl-x",
            "model": "GaussO3.2-260402-vllm",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "Let me think.",
                        "content": "Hello!",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(openai_body).encode(),
        )

    app = _make_app(handler)
    client = TestClient(app)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "GaussO3.2-260402-vllm",
            "stream": False,
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert captured["url"].endswith("/v1/chat/completions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 12, "output_tokens": 3}
    types = [b["type"] for b in body["content"]]
    assert types == ["thinking", "text"]


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
