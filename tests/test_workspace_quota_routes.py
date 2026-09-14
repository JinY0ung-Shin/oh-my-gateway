"""Integration coverage for cumulative per-user workspace quota file routes."""

import asyncio
import errno
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.auth as auth_module
import src.workspace_quota as workspace_quota_module
from src.routes import terminal_files as tf
from src.routes.terminal_files import router

_AUTH = {"Authorization": "Bearer testkey"}
_USER = {"X-User-Email": "alice@corp.com"}
_MIB = 1024 * 1024


def _patch_api_key(monkeypatch, value: str) -> None:
    for manager in {tf.auth_manager, auth_module.auth_manager}:
        monkeypatch.setattr(manager, "get_api_key", lambda: value)


def _fail_quota_scan_at(monkeypatch, blocked: Path, failure: OSError) -> None:
    real_scandir = workspace_quota_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == blocked:
            raise failure
        return real_scandir(path)

    monkeypatch.setattr(workspace_quota_module.os, "scandir", guarded_scandir)


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
    # The quota scope is derived from the managed base, so the fixture must own
    # that base too — stubbing only `resolve` would let the route validate the
    # user root against a production path that has nothing to do with tmp_path.
    monkeypatch.setattr(tf.workspace_manager, "base_path", tmp_path)
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
    error = response.json()["detail"]["error"]
    assert error["code"] == "workspace_quota_exceeded"
    assert error["type"] == "insufficient_storage"
    assert error["used_bytes"] == 900 * 1024
    assert error["limit_bytes"] == _MIB
    assert error["projected_bytes"] == 1100 * 1024
    assert not (workspace / "too-much.bin").exists()


def test_upload_fails_closed_when_quota_scan_is_unreadable(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    blocked = workspace / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"h" * (800 * 1024))
    _fail_quota_scan_at(
        monkeypatch,
        blocked,
        PermissionError(errno.EACCES, "simulated unreadable subtree"),
    )

    response = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("new.bin", b"n" * (300 * 1024), "application/octet-stream")},
    )

    assert response.status_code == 503
    error = response.json()["detail"]["error"]
    assert error["code"] == "workspace_quota_accounting_unavailable"
    assert error["type"] == "service_unavailable"
    assert not (workspace / "new.bin").exists()


def test_no_clobber_existing_destination_stays_409_even_when_quota_is_full(
    quota_client, monkeypatch
):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    target = workspace / "existing.bin"
    original = b"x" * _MIB
    target.write_bytes(original)

    response = client.post(
        "/files/upload?directory=/&no_clobber=true",
        headers={**_AUTH, **_USER},
        files={"file": ("existing.bin", b"replacement", "application/octet-stream")},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "destination already exists"
    assert target.read_bytes() == original


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


def test_directory_self_copy_stays_400_before_quota_preflight(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    source = workspace / "foo"
    source.mkdir()
    (source / "payload.bin").write_bytes(b"s" * (700 * 1024))
    (workspace / "other.bin").write_bytes(b"o" * (300 * 1024))

    response = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/foo", "destination": "/foo/bar"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "cannot copy a directory into itself"
    assert not (source / "bar").exists()


def test_copy_fails_closed_when_quota_scan_hits_io_error(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    source = workspace / "source.bin"
    source.write_bytes(b"s" * (200 * 1024))
    blocked = workspace / "blocked"
    blocked.mkdir()
    (blocked / "hidden.bin").write_bytes(b"h" * (700 * 1024))
    _fail_quota_scan_at(
        monkeypatch,
        blocked,
        OSError(errno.EIO, "simulated quota scan I/O failure"),
    )

    destination = workspace / "source copy.bin"
    response = client.post(
        "/files/copy",
        headers={**_AUTH, **_USER},
        json={"source": "/source.bin", "destination": "/source copy.bin"},
    )

    assert response.status_code == 503
    error = response.json()["detail"]["error"]
    assert error["code"] == "workspace_quota_accounting_unavailable"
    assert error["type"] == "service_unavailable"
    assert not destination.exists()


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


def test_quota_endpoint_reports_live_usage_even_when_disabled(quota_client, monkeypatch):
    """Usage is measured with or without a limit.

    "How much am I using?" is a real question when nothing is enforced, and the
    endpoint promises a live figure. Returning 0 to skip the walk would publish a
    number that is simply wrong — the same class of misleading total the
    fail-closed accounting path exists to prevent.
    """
    client, _, workspace = quota_client
    monkeypatch.delenv("USER_WORKSPACE_QUOTA_MB", raising=False)
    (workspace / "payload.bin").write_bytes(b"x" * 4096)

    response = client.get("/files/quota", headers={**_AUTH, **_USER})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["limit_bytes"] == 0
    assert body["remaining_bytes"] is None
    assert body["over_quota"] is False
    assert body["used_bytes"] == 4096, "disabled quota must still report live usage"


def test_quota_endpoint_still_reports_usage_when_enabled(quota_client, monkeypatch):
    client, _, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    (workspace / "payload.bin").write_bytes(b"x" * 4096)

    response = client.get("/files/quota", headers={**_AUTH, **_USER})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["used_bytes"] == 4096


def test_quota_scope_requires_the_managed_user_root_shape(quota_client, monkeypatch):
    """Quota must never be charged against a root we cannot identify.

    ``workspace_manager.resolve`` returns ``<base>/<user>`` when no backend name
    is passed. Taking ``.parent`` of that would silently make the shared base the
    quota scope, so every user would be charged for everyone's bytes. The numbers
    would stay plausible, so this fails closed instead.
    """
    client, user_root, workspace = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")

    def _resolve_without_backend(user, backend=None):
        return user_root

    monkeypatch.setattr(tf.workspace_manager, "resolve", _resolve_without_backend)

    response = client.post(
        "/files/upload?directory=/",
        headers={**_AUTH, **_USER},
        files={"file": ("scoped.txt", b"data", "text/plain")},
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]["error"]["code"]
        == "workspace_quota_accounting_unavailable"
    )
    assert not (user_root / "scoped.txt").exists()


def test_quota_scope_rejects_anonymous_temp_workspaces(quota_client, monkeypatch):
    """An anonymous ``_tmp_*`` workspace has no named user to charge."""
    client, _, _ = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    base = tf.workspace_manager.base_path
    anonymous = base / "_tmp_deadbeef" / "claude"
    anonymous.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        tf.workspace_manager, "resolve", lambda user, backend=None: anonymous
    )

    response = client.get("/files/quota", headers={**_AUTH, **_USER})

    assert response.status_code == 503
    assert (
        response.json()["detail"]["error"]["code"]
        == "workspace_quota_accounting_unavailable"
    )


def test_quota_locks_do_not_accumulate_per_user(quota_client, monkeypatch):
    """The lock registry must not grow for the lifetime of the process.

    A gateway serves an unbounded set of named users, so a plain cache keyed by
    user root only ever grows. Entries are reference-counted, so once a request
    finishes nothing is left behind.
    """
    client, _, _ = quota_client
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    assert tf._QUOTA_LOCKS == {}

    for index in range(5):
        response = client.post(
            "/files/upload?directory=/",
            headers={**_AUTH, **_USER},
            files={"file": (f"lock-{index}.txt", b"x", "text/plain")},
        )
        assert response.status_code == 200

    assert tf._QUOTA_LOCKS == {}, f"lock registry leaked entries: {tf._QUOTA_LOCKS}"


async def test_concurrent_same_user_mutations_share_one_lock(tmp_path, monkeypatch):
    """Reference counting must not split one user's lock into two.

    The count is taken before awaiting, so a second caller arriving while the
    first holds the lock joins the same entry instead of creating a fresh one
    and running concurrently.
    """
    monkeypatch.setattr(tf.workspace_manager, "base_path", tmp_path)
    tf._QUOTA_LOCKS.clear()
    user_root = tmp_path / "alice@corp.com"
    (user_root / "claude").mkdir(parents=True)

    started = asyncio.Event()
    overlapped = False
    inside = 0

    async def hold(delay: float) -> None:
        nonlocal overlapped, inside
        async with tf._quota_lock(user_root):
            inside += 1
            if inside > 1:
                overlapped = True
            started.set()
            await asyncio.sleep(delay)
            inside -= 1

    first = asyncio.create_task(hold(0.05))
    await started.wait()
    # While the first holds it, a second caller must find the same entry.
    second = asyncio.create_task(hold(0.0))
    await asyncio.sleep(0)
    assert len(tf._QUOTA_LOCKS) == 1
    assert tf._QUOTA_LOCKS[str(user_root.resolve())].users == 2

    await asyncio.gather(first, second)

    assert overlapped is False, "same-user quota mutations ran concurrently"
    assert tf._QUOTA_LOCKS == {}, "entry survived after both callers finished"
