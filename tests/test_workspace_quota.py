from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import src.workspace_quota as workspace_quota_module
from src.workspace_quota import (
    WorkspaceQuotaAccountingError,
    WorkspaceQuotaConfigError,
    WorkspaceQuotaExceeded,
    copy_growth_bytes,
    ensure_growth_fits,
    logical_size_bytes,
    quota_snapshot,
    workspace_quota_limit_bytes,
)


def test_quota_unset_is_unlimited(monkeypatch):
    monkeypatch.delenv("USER_WORKSPACE_QUOTA_MB", raising=False)
    assert workspace_quota_limit_bytes() == 0


def test_quota_mb_uses_mib_bytes(monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "500")
    assert workspace_quota_limit_bytes() == 500 * 1024 * 1024


@pytest.mark.parametrize("value", ["-1", "nope", "1.5"])
def test_invalid_quota_is_configuration_error(monkeypatch, value):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", value)
    with pytest.raises(WorkspaceQuotaConfigError):
        workspace_quota_limit_bytes()


def test_logical_usage_spans_backend_directories(tmp_path: Path):
    user_root = tmp_path / "alice"
    (user_root / "claude").mkdir(parents=True)
    (user_root / "codex").mkdir()
    (user_root / "claude" / "a.bin").write_bytes(b"a" * 11)
    (user_root / "codex" / "b.bin").write_bytes(b"b" * 13)

    assert logical_size_bytes(user_root) == 24


def test_disappearing_subtree_is_skipped_without_failing_usage(
    tmp_path: Path, monkeypatch
):
    user_root = tmp_path / "alice"
    vanished = user_root / "vanished"
    vanished.mkdir(parents=True)
    (user_root / "visible.bin").write_bytes(b"v" * 11)
    (vanished / "gone.bin").write_bytes(b"g" * 13)

    real_scandir = workspace_quota_module.os.scandir

    def racing_scandir(path):
        if Path(path) == vanished:
            raise FileNotFoundError(errno.ENOENT, "simulated concurrent removal")
        return real_scandir(path)

    monkeypatch.setattr(workspace_quota_module.os, "scandir", racing_scandir)

    assert logical_size_bytes(user_root) == 11


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError(errno.EACCES, "simulated unreadable subtree"),
        OSError(errno.EIO, "simulated I/O failure"),
    ],
)
def test_unreadable_or_io_failed_subtree_fails_accounting(
    tmp_path: Path, monkeypatch, failure: OSError
):
    user_root = tmp_path / "alice"
    blocked = user_root / "blocked"
    blocked.mkdir(parents=True)
    (user_root / "visible.bin").write_bytes(b"v" * 11)
    (blocked / "hidden.bin").write_bytes(b"h" * 13)

    real_scandir = workspace_quota_module.os.scandir

    def guarded_scandir(path):
        if Path(path) == blocked:
            raise failure
        return real_scandir(path)

    monkeypatch.setattr(workspace_quota_module.os, "scandir", guarded_scandir)

    with pytest.raises(WorkspaceQuotaAccountingError) as raised:
        logical_size_bytes(user_root)
    assert raised.value.errno == failure.errno


def test_symlinks_are_not_followed(tmp_path: Path):
    user_root = tmp_path / "alice"
    user_root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 100)
    (user_root / "link").symlink_to(outside)

    assert logical_size_bytes(user_root) == 0


def test_hardlinks_count_once_for_usage_but_twice_for_copy_growth(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    first = source / "a.bin"
    first.write_bytes(b"x" * 17)
    os.link(first, source / "b.bin")

    assert logical_size_bytes(source) == 17
    assert copy_growth_bytes(source) == 34


def test_overwrite_can_shrink_at_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    user_root = tmp_path / "alice"
    user_root.mkdir()
    existing = user_root / "old.bin"
    existing.write_bytes(b"x" * (1024 * 1024))

    ensure_growth_fits(
        user_root,
        added_bytes=512 * 1024,
        reclaimed_bytes=existing.stat().st_size,
    )


def test_growth_over_limit_is_rejected_with_projection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    user_root = tmp_path / "alice"
    user_root.mkdir()
    (user_root / "existing.bin").write_bytes(b"x" * (900 * 1024))

    with pytest.raises(WorkspaceQuotaExceeded) as raised:
        ensure_growth_fits(user_root, added_bytes=200 * 1024)

    exc = raised.value
    assert exc.limit_bytes == 1024 * 1024
    assert exc.used_bytes == 900 * 1024
    assert exc.projected_bytes == 1100 * 1024
    error = exc.as_detail()["error"]
    assert error["code"] == "workspace_quota_exceeded"
    assert error["type"] == "insufficient_storage"
    assert error["used_bytes"] == 900 * 1024
    assert error["projected_bytes"] == 1100 * 1024


def test_snapshot_reports_remaining_and_overage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("USER_WORKSPACE_QUOTA_MB", "1")
    user_root = tmp_path / "alice"
    user_root.mkdir()
    (user_root / "a.bin").write_bytes(b"x" * 100)

    snap = quota_snapshot(user_root)
    assert snap.enabled is True
    assert snap.used_bytes == 100
    assert snap.limit_bytes == 1024 * 1024
    assert snap.remaining_bytes == 1024 * 1024 - 100
    assert snap.over_quota is False


class TestSessionTmpPrivateDirs:
    """uid mode: the CLI always makes ``.cli-tmp/claude-<uid>`` 0700 as the
    session uid, which the gateway can never read; that dir must not fail every
    quota-growing write, while every other unreadable subtree still does."""

    @staticmethod
    def _tree(tmp_path: Path):
        user_root = tmp_path / "alice"
        workspace = user_root / "claude"
        private = workspace / ".cli-tmp" / "claude-20001"
        private.mkdir(parents=True)
        (private / "task.out").write_bytes(b"p" * 7)
        (workspace / ".cli-tmp" / "npm-cache").mkdir()
        (workspace / ".cli-tmp" / "npm-cache" / "pkg").write_bytes(b"n" * 5)
        (workspace / "visible.bin").write_bytes(b"v" * 11)
        return user_root, workspace, private

    @staticmethod
    def _deny(monkeypatch, blocked: Path) -> None:
        real_scandir = workspace_quota_module.os.scandir

        def guarded(path):
            if Path(path) == blocked:
                raise PermissionError(errno.EACCES, "denied", str(path))
            return real_scandir(path)

        monkeypatch.setattr(workspace_quota_module.os, "scandir", guarded)

    def test_a_session_uids_private_tmp_dir_is_skipped(self, tmp_path, monkeypatch):
        user_root, _workspace, private = self._tree(tmp_path)
        self._deny(monkeypatch, private)
        # The dir belongs to "another" uid: the gateway is someone else here.
        real_uid = os.geteuid()
        monkeypatch.setattr(workspace_quota_module.os, "geteuid", lambda: real_uid + 1)
        assert logical_size_bytes(user_root) == 11 + 5

    def test_the_gateways_own_unreadable_tmp_dir_still_fails(
        self, tmp_path, monkeypatch
    ):
        user_root, _workspace, private = self._tree(tmp_path)
        self._deny(monkeypatch, private)
        with pytest.raises(WorkspaceQuotaAccountingError):
            logical_size_bytes(user_root)

    def test_a_foreign_unreadable_dir_elsewhere_still_fails(self, tmp_path, monkeypatch):
        user_root, workspace, _private = self._tree(tmp_path)
        locked = workspace / "locked"
        locked.mkdir()
        self._deny(monkeypatch, locked)
        real_uid = os.geteuid()
        monkeypatch.setattr(workspace_quota_module.os, "geteuid", lambda: real_uid + 1)
        with pytest.raises(WorkspaceQuotaAccountingError):
            logical_size_bytes(user_root)
