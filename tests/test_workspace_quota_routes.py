"""Integration coverage for cumulative per-user workspace quota file routes."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth as auth_module
from src.routes import terminal_files as tf
from src.routes.terminal_files import router

_AUTH = {"Authorization": "Bearer testkey"}
_USER = {"X-User-Email": "alice@corp.com"}
_MIB = 1024 * 1024


def _patch_api_key(monkeypatch, value: str) -> None:
    for manager in {tf.auth_manager, auth_module.auth_manager}:
        monkeypatch.setattr(manager, "get_api_key", lambda: value)


@pytest.fixture
def quota_client(tmp_path: Path, monkeypatch):
    _patch_api_key(monkeypatch, "testkey")
    user_root = tmp_path / "alice@corp.com"
    workspace = user_root / "claude"
    workspace.mkdir(parents=True)

    def _resolve(user, backend=None):
        if user != "alice@corp.com":
            raise ValueError("bad user")
        target = user_root if backend is None else user_root / backend
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve)
    tf._QUOTA_LOCKS.clear()

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    try:
        yield client, user_root, workspace
    finally:
        tf._QUOTA_LOCKS.clear()


def test_limits_keep_single_file_limit_and_publish_user_quota(
    quota_client, monkeypatch
):
    client, _, _ = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")

    response = client.get("/files/limits", headers={**_AUTH, **_USER})

    assert response.status_code == 200
    assert response.json()["max_upload_bytes"] == tf._max_upload_bytes()
    assert response.json()["workspace_quota_bytes"] == _MIB


def test_quota_usage_aggregates_all_backend_directories(quota_client, monkeypatch):
    client, user_root, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    (workspace / "claude.bin").write_bytes(b"a" * 11)
    codex = user_root / "codex"
    codex.mkdir()
    (codex / "codex.bin").write_bytes(b"b" * 13)

    response = client.get("/files/quota", headers={**_AUTH, **_USER})

    assert response.status_code == 200
    assert response.json() == {
        "used_bytes": 24,
        "limit_bytes": _MIB,
        "remaining_bytes": _MIB - 24,
        "enabled": True,
        "over_quota": False,
    }


def test_upload_over_user_quota_is_507_and_does_not_write(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    (workspace / "existing.bin").write_bytes(b"x" * (900 * 1024))

    response = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("too-much.bin", b"y" * (200 * 1024), "application/octet-stream")},
    )

    assert response.status_code == 507
    detail = response.json()["detail"]
    assert detail["error"] == "workspace_quota_exceeded"
    assert detail["used_bytes"] == 900 * 1024
    assert detail["limit_bytes"] == _MIB
    assert detail["projected_bytes"] == 1100 * 1024
    assert not (workspace / "too-much.bin").exists()


def test_overwrite_that_shrinks_is_allowed_at_quota(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    target = workspace / "full.bin"
    target.write_bytes(b"x" * _MIB)

    response = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("full.bin", b"small", "application/octet-stream")},
    )

    assert response.status_code == 200
    assert target.read_bytes() == b"small"


def test_other_backend_usage_can_block_claude_upload(quota_client, monkeypatch):
    client, user_root, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    codex = user_root / "codex"
    codex.mkdir()
    (codex / "large.bin").write_bytes(b"x" * (900 * 1024))

    response = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("new.bin", b"y" * (200 * 1024), "application/octet-stream")},
    )

    assert response.status_code == 507
    assert not (workspace / "new.bin").exists()


def test_copy_growth_is_checked_before_destination_creation(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    (workspace / "source.bin").write_bytes(b"s" * (600 * 1024))
    (workspace / "other.bin").write_bytes(b"o" * (300 * 1024))

    response = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/source.bin", "destination": "/source copy.bin"},
    )

    assert response.status_code == 507
    assert not (workspace / "source copy.bin").exists()


def test_quota_unset_keeps_existing_upload_behavior(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.delenv("USER_WORKSPACE_QUOTA_MB", raising=False)

    response = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("normal.txt", b"ok", "text/plain")},
    )

    assert response.status_code == 200
    assert (workspace / "normal.txt").read_bytes() == b"ok"
