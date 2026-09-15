"""Workspace-upload auth contract across request boundary and file route."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth as auth_module
import src.concurrency_middleware as cm
from src import main as gateway_main
from src.routes import terminal_files as tf
from src.routes.terminal_files import router
from src.runtime_config import runtime_config


def _auth_managers():
    """Return every live/stale singleton binding once.

    Some auth tests reload ``src.auth`` mid-suite, while modules imported before
    the reload can retain the previous manager object. Production has one
    singleton; the test suite can have more, so configure each binding to keep
    this regression independent of execution order.
    """
    managers = []
    for manager in (auth_module.auth_manager, cm.auth_manager, tf.auth_manager):
        if not any(manager is existing for existing in managers):
            managers.append(manager)
    return managers


def _configure_auth(monkeypatch, *, legacy_key=None, user_keys=None):
    for manager in _auth_managers():
        monkeypatch.setattr(manager, "env_api_key", legacy_key)
        monkeypatch.setattr(manager, "runtime_api_key", None)
        monkeypatch.setattr(manager, "user_api_keys", dict(user_keys or {}))


def _scope(method, path, headers=None):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }


@pytest.mark.asyncio
async def test_auth_disabled_chunked_upload_cannot_use_widened_allowance(monkeypatch):
    """A disabled file route must not get a larger pre-auth memory budget."""
    _configure_auth(monkeypatch)
    monkeypatch.setattr(cm, "MAX_REQUEST_SIZE", 5)
    monkeypatch.setattr(cm, "get_workspace_upload_request_max_bytes", lambda: 20)
    called = False
    sent = []
    queue = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = _scope("POST", "/files/upload", [(b"transfer-encoding", b"chunked")])
    await cm.ConcurrencyLimitMiddleware(app)(scope, receive, send)

    assert cm._request_body_limit(scope) == 5
    assert called is False
    assert sent[0]["status"] == 413
    assert b"5 bytes" in sent[1]["body"]


def test_workspace_file_route_still_fails_closed_without_gateway_auth(
    tmp_path: Path, monkeypatch
):
    _configure_auth(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(tf.workspace_manager, "resolve", lambda user, backend=None: workspace)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/files/upload?directory=/",
        files={"file": ("blocked.txt", b"x", "text/plain")},
    )

    assert response.status_code == 503
    assert not (workspace / "blocked.txt").exists()


def test_user_api_keys_only_upload_uses_widened_limit_and_succeeds(
    tmp_path: Path, monkeypatch
):
    """USER_API_KEYS is a complete file-auth mode, not a boundary-only mode."""
    _configure_auth(monkeypatch, user_keys={"alice": "alice-key"})
    monkeypatch.setattr(cm, "MAX_REQUEST_SIZE", 4 * 1024)

    workspace = tmp_path / "alice" / "claude"
    workspace.mkdir(parents=True)

    def resolve(user, backend=None):
        if user == "alice":
            return workspace
        raise ValueError(f"unexpected workspace user: {user}")

    monkeypatch.setattr(tf.workspace_manager, "resolve", resolve)
    runtime_config.reset("workspace_upload_max_bytes")
    runtime_config.set("workspace_upload_max_bytes", 16 * 1024)

    app = FastAPI()
    app.include_router(router)
    app.add_middleware(cm.ConcurrencyLimitMiddleware)
    app.add_middleware(gateway_main.RequestSizeLimitMiddleware)
    client = TestClient(app)

    try:
        payload = b"x" * (8 * 1024)  # larger than generic cap, below file cap
        response = client.post(
            "/files/upload?directory=/",
            headers={"Authorization": "Bearer alice-key"},
            files={"file": ("user-only.bin", payload, "application/octet-stream")},
        )

        assert response.status_code == 200, response.text
        assert response.json()["size"] == len(payload)
        assert (workspace / "user-only.bin").read_bytes() == payload
    finally:
        runtime_config.reset("workspace_upload_max_bytes")
