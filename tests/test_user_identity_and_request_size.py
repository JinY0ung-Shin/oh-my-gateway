"""Regression tests for request-size enforcement and credential-bound user isolation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import src.auth as auth_module
import src.concurrency_middleware as middleware_module
import src.routes.sessions as sessions_route
from src.concurrency_middleware import ConcurrencyLimitMiddleware
from src.session_manager import SessionManager


class _Slot:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _Limiter:
    def __init__(self):
        self.in_flight = 0
        self.users = []
        self.slot = _Slot()

    def try_acquire(self, user):
        self.users.append(user)
        self.in_flight += 1
        return self.slot, "global"


async def _invoke(middleware, scope, messages):
    sent = []
    queue = list(messages)

    async def receive():
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _scope(method, path, headers=None, query=b""):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }


@pytest.mark.asyncio
async def test_chunked_body_is_rejected_by_actual_byte_count(monkeypatch):
    monkeypatch.setattr(middleware_module, "MAX_REQUEST_SIZE", 5)
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    middleware = ConcurrencyLimitMiddleware(app)
    scope = _scope("POST", "/echo", [(b"transfer-encoding", b"chunked")])
    sent = await _invoke(
        middleware,
        scope,
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    )

    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_understated_content_length_is_rejected(monkeypatch):
    monkeypatch.setattr(middleware_module, "MAX_REQUEST_SIZE", 5)
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    middleware = ConcurrencyLimitMiddleware(app)
    scope = _scope("POST", "/echo", [(b"content-length", b"3")])
    sent = await _invoke(
        middleware,
        scope,
        [{"type": "http.request", "body": b"abcdef", "more_body": False}],
    )

    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_user_api_key_overwrites_response_user_and_workspace_header(monkeypatch):
    captured = {}
    limiter = _Limiter()
    monkeypatch.setattr(
        middleware_module.auth_manager,
        "authenticate_gateway_key",
        lambda token: (True, "alice") if token == "alice-key" else (False, None),
    )

    async def app(scope, receive, send):
        message = await receive()
        captured["body"] = json.loads(message["body"])
        captured["state"] = dict(scope["state"])
        captured["headers"] = dict(scope["headers"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    body = json.dumps(
        {"model": "sonnet", "input": "hello", "user": "mallory"}
    ).encode()
    scope = _scope(
        "POST",
        "/v1/responses",
        [
            (b"authorization", b"Bearer alice-key"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    )

    middleware = ConcurrencyLimitMiddleware(app, limiter=limiter)
    sent = await _invoke(
        middleware,
        scope,
        [{"type": "http.request", "body": body, "more_body": False}],
    )

    assert sent[0]["status"] == 200
    assert captured["body"]["user"] == "alice"
    assert captured["state"]["auth_user"] == "alice"
    assert captured["headers"][b"x-user-email"] == b"alice"
    assert limiter.users == ["alice"]
    assert limiter.slot.released is True


@pytest.mark.asyncio
async def test_user_api_key_overwrites_response_lookup_query(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        middleware_module.auth_manager,
        "authenticate_gateway_key",
        lambda token: (True, "alice") if token == "alice-key" else (False, None),
    )

    async def app(scope, receive, send):
        captured["query"] = scope["query_string"]
        captured["state"] = dict(scope["state"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = ConcurrencyLimitMiddleware(app)
    scope = _scope(
        "GET",
        "/v1/responses/resp_deadbeef_1",
        [(b"authorization", b"Bearer alice-key")],
        query=b"user=mallory&foo=bar",
    )
    sent = await _invoke(middleware, scope, [])

    assert sent[0]["status"] == 200
    assert b"user=alice" in captured["query"]
    assert b"mallory" not in captured["query"]
    assert captured["state"]["auth_user"] == "alice"


@pytest.mark.asyncio
async def test_verify_api_key_sets_user_principal(monkeypatch):
    monkeypatch.setattr(auth_module.auth_manager, "env_api_key", None)
    monkeypatch.setattr(auth_module.auth_manager, "runtime_api_key", None)
    monkeypatch.setattr(auth_module.auth_manager, "user_api_keys", {"alice": "alice-key"})

    request = SimpleNamespace(state=SimpleNamespace())
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="alice-key")
    assert await auth_module.verify_api_key(request, credentials) is True
    assert request.state.auth_user == "alice"


@pytest.mark.asyncio
async def test_session_list_is_filtered_to_authenticated_user(monkeypatch):
    manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)
    alice = manager.get_or_create_session("11111111-1111-4111-8111-111111111111")
    bob = manager.get_or_create_session("22222222-2222-4222-8222-222222222222")
    alice.user = "alice"
    bob.user = "bob"

    monkeypatch.setattr(sessions_route, "session_manager", manager)
    monkeypatch.setattr(sessions_route, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(sessions_route, "get_authenticated_user", lambda request: "alice")

    request = SimpleNamespace(state=SimpleNamespace(auth_user="alice"))
    result = await sessions_route.list_sessions(request, None)

    assert result.total == 1
    assert result.sessions[0].session_id == alice.session_id


@pytest.mark.asyncio
async def test_foreign_session_is_hidden_from_authenticated_user(monkeypatch):
    manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)
    bob = manager.get_or_create_session("22222222-2222-4222-8222-222222222222")
    bob.user = "bob"

    monkeypatch.setattr(sessions_route, "session_manager", manager)
    monkeypatch.setattr(sessions_route, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(sessions_route, "get_authenticated_user", lambda request: "alice")

    request = SimpleNamespace(state=SimpleNamespace(auth_user="alice"))
    with pytest.raises(HTTPException) as exc_info:
        await sessions_route.get_session(request, bob.session_id, None)
    assert exc_info.value.status_code == 404
