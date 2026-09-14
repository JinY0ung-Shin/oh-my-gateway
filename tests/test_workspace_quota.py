from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.workspace_quota import (
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
    assert exc.as_detail()["error"] == "workspace_quota_exceeded"


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
