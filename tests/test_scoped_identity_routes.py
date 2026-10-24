"""Strict per-user identity enforcement (#251) across the identity surfaces.

Complements the file-API matrix in ``test_workspace_upload_auth_modes.py`` with
``/v1/responses`` and ``/v1/sessions`` so a later route or refactor that drops the
``enforce_scoped_identity`` call on one of them is caught. Every refusal fires
right after ``verify_api_key``, before any backend work, so these need no SDK.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth as auth_module
import src.concurrency_middleware as cm
from src.routes.responses import router as responses_router
from src.routes.sessions import router as sessions_router


def _configure_auth(monkeypatch, *, legacy_key=None, user_keys=None):
    managers = []
    for manager in (auth_module.auth_manager, cm.auth_manager):
        if not any(manager is seen for seen in managers):
            managers.append(manager)
    for manager in managers:
        monkeypatch.setattr(manager, "env_api_key", legacy_key)
        monkeypatch.setattr(manager, "runtime_api_key", None)
        monkeypatch.setattr(manager, "user_api_keys", dict(user_keys or {}))


def _client(*routers):
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    app.add_middleware(cm.ConcurrencyLimitMiddleware)
    return TestClient(app)


# --- /v1/responses --------------------------------------------------------


def test_responses_create_legacy_key_refused_in_strict_mode(monkeypatch):
    """A legacy API_KEY carries no principal, so a strict-mode create is 403 —
    body.user cannot pick the tenant."""
    _configure_auth(monkeypatch, legacy_key="svc-key")
    monkeypatch.setenv("GATEWAY_REQUIRE_SCOPED_IDENTITY", "true")
    client = _client(responses_router)

    resp = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer svc-key"},
        json={"model": "claude", "input": "hi", "user": "victim"},
    )

    assert resp.status_code == 403, resp.text


def test_responses_create_user_token_passes_enforcement(monkeypatch):
    """A USER_API_KEYS token is credential-bound: it clears enforcement (any
    later status is not the 403 this test guards against)."""
    _configure_auth(monkeypatch, user_keys={"alice": "alice-key"})
    monkeypatch.setenv("GATEWAY_REQUIRE_SCOPED_IDENTITY", "true")
    client = _client(responses_router)

    resp = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer alice-key"},
        json={"model": "claude", "input": "hi", "user": "victim"},
    )

    assert resp.status_code != 403, resp.text


# --- /v1/sessions ---------------------------------------------------------


def test_sessions_get_legacy_key_refused_in_strict_mode(monkeypatch):
    _configure_auth(monkeypatch, legacy_key="svc-key")
    monkeypatch.setenv("GATEWAY_REQUIRE_SCOPED_IDENTITY", "true")
    client = _client(sessions_router)

    resp = client.get(
        "/v1/sessions/does-not-exist?user=victim",
        headers={"Authorization": "Bearer svc-key"},
    )

    assert resp.status_code == 403, resp.text


def test_sessions_get_user_token_passes_enforcement(monkeypatch):
    """User token clears enforcement; an unknown session then 404s (not 403)."""
    _configure_auth(monkeypatch, user_keys={"alice": "alice-key"})
    monkeypatch.setenv("GATEWAY_REQUIRE_SCOPED_IDENTITY", "true")
    client = _client(sessions_router)

    resp = client.get(
        "/v1/sessions/does-not-exist",
        headers={"Authorization": "Bearer alice-key"},
    )

    assert resp.status_code != 403, resp.text


def test_no_strict_mode_allows_legacy_key(monkeypatch):
    """With the flag off, the legacy service key keeps its historical behavior."""
    _configure_auth(monkeypatch, legacy_key="svc-key")
    monkeypatch.delenv("GATEWAY_REQUIRE_SCOPED_IDENTITY", raising=False)
    client = _client(sessions_router)

    resp = client.get(
        "/v1/sessions/does-not-exist?user=victim",
        headers={"Authorization": "Bearer svc-key"},
    )

    assert resp.status_code != 403, resp.text
