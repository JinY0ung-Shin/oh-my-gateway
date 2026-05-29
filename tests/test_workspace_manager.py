"""Unit tests for WorkspaceManager."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.session_manager import Session
from src.workspace_manager import WorkspaceManager


@pytest.fixture
def tmp_base(tmp_path):
    """Provide a temporary base directory for workspaces."""
    return tmp_path / "workspaces"


@pytest.fixture
def manager(tmp_base):
    return WorkspaceManager(base_path=tmp_base)


class TestSanitize:
    def test_valid_usernames(self, manager):
        assert manager._sanitize("alice") == "alice"
        assert manager._sanitize("user-123") == "user-123"
        assert manager._sanitize("Bob_Smith") == "Bob_Smith"
        assert manager._sanitize("a") == "a"

    def test_rejects_empty_string(self, manager):
        with pytest.raises(ValueError, match="empty"):
            manager._sanitize("")

    def test_rejects_path_traversal(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("../etc/passwd")

    def test_rejects_dots_only(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("..")
        with pytest.raises(ValueError):
            manager._sanitize(".")

    def test_rejects_invalid_characters(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("user/name")
        with pytest.raises(ValueError):
            manager._sanitize("user name")
        with pytest.raises(ValueError):
            manager._sanitize("user@name")

    def test_rejects_too_long(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("a" * 64)

    def test_rejects_starting_with_non_alnum(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("-alice")
        with pytest.raises(ValueError):
            manager._sanitize("_alice")


class TestResolve:
    def test_creates_user_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice")
        assert workspace == tmp_base / "alice"
        assert workspace.is_dir()

    def test_returns_existing_directory(self, manager, tmp_base):
        first = manager.resolve("alice")
        (first / "myfile.txt").write_text("data")
        second = manager.resolve("alice")
        assert first == second
        assert (second / "myfile.txt").read_text() == "data"

    def test_anonymous_creates_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve(None)
        assert workspace.parent == tmp_base
        assert workspace.name.startswith("_tmp_")

    def test_named_user_backend_creates_backend_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice", backend="codex")
        assert workspace == tmp_base / "alice" / "codex"
        assert workspace.is_dir()

    def test_named_user_backend_directories_are_independent(self, manager, tmp_base):
        claude = manager.resolve("alice", backend="claude")
        codex = manager.resolve("alice", backend="codex")
        assert claude == tmp_base / "alice" / "claude"
        assert codex == tmp_base / "alice" / "codex"
        assert claude != codex

    def test_anonymous_ignores_backend_for_tmp_layout(self, manager, tmp_base):
        workspace = manager.resolve(None, backend="opencode")
        assert workspace.parent == tmp_base
        assert workspace.name.startswith("_tmp_")

    def test_rejects_invalid_backend_name(self, manager):
        with pytest.raises(ValueError, match="Invalid backend"):
            manager.resolve("alice", backend="../codex")

    def test_anonymous_returns_different_dirs(self, manager):
        w1 = manager.resolve(None)
        w2 = manager.resolve(None)
        assert w1 != w2

    def test_resolve_creates_empty_workspace(self, manager):
        """Workspaces are created empty — no config is seeded into them."""
        workspace = manager.resolve("carol")
        assert workspace.is_dir()
        assert list(workspace.iterdir()) == []
        assert not (workspace / ".claude").exists()
        assert not (workspace / "CLAUDE.md").exists()
        assert not (workspace / ".agents").exists()
        assert not (workspace / ".opencode").exists()

    def test_resolve_existing_workspace_leaves_user_files_intact(self, manager):
        """Re-resolving an existing user workspace must not wipe user files."""
        first = manager.resolve("dave", backend="claude")
        (first / "keep.txt").write_text("user data")
        second = manager.resolve("dave", backend="claude")
        assert second == first
        assert (second / "keep.txt").read_text() == "user data"


class TestCleanupTempWorkspace:
    def test_removes_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve(None)
        assert workspace.is_dir()
        manager.cleanup_temp_workspace(workspace)
        assert not workspace.exists()

    def test_ignores_non_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice")
        (workspace / "important.txt").write_text("keep")
        manager.cleanup_temp_workspace(workspace)
        assert workspace.exists()

    def test_ignores_nonexistent_directory(self, manager):
        manager.cleanup_temp_workspace(Path("/nonexistent/_tmp_abc"))


class TestSweepOrphanTempWorkspaces:
    def test_sweeps_only_old_tmp_dirs(self, manager, tmp_base):
        old = manager.resolve(None)
        fresh = manager.resolve(None)
        named = manager.resolve("alice")

        # Age `old` well past the cutoff; `fresh` and `named` stay recent.
        past = time.time() - 10_000
        os.utime(old, (past, past))

        removed = manager.sweep_orphan_temp_workspaces(max_age_seconds=3600)

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()  # too recent
        assert named.exists()  # not a _tmp_ workspace

    def test_returns_zero_when_base_path_missing(self, tmp_base):
        mgr = WorkspaceManager(base_path=tmp_base / "does-not-exist")
        assert mgr.sweep_orphan_temp_workspaces(max_age_seconds=0) == 0


class TestSessionUserField:
    def test_session_has_user_field(self):
        session = Session(session_id="test-1", user="alice")
        assert session.user == "alice"

    def test_session_user_defaults_to_none(self):
        session = Session(session_id="test-2")
        assert session.user is None

    def test_session_has_workspace_field(self):
        session = Session(session_id="test-3", workspace="/tmp/ws/alice")
        assert session.workspace == "/tmp/ws/alice"

    def test_session_workspace_defaults_to_none(self):
        session = Session(session_id="test-4")
        assert session.workspace is None


class TestClaudeCLICwdOverride:
    def test_build_sdk_options_uses_override_cwd(self, tmp_path):
        """_build_sdk_options should use cwd param when provided."""
        default_dir = tmp_path / "default"
        override_dir = tmp_path / "override"
        default_dir.mkdir()
        override_dir.mkdir()

        with patch("src.auth.validate_claude_code_auth", return_value=(True, {})):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = ClaudeCodeCLI(cwd=str(default_dir))
            options = cli._build_sdk_options(cwd=override_dir)
            assert str(options.cwd) == str(override_dir)

    def test_build_sdk_options_falls_back_to_self_cwd(self, tmp_path):
        """_build_sdk_options should use self.cwd when cwd param is None."""
        default_dir = tmp_path / "default"
        default_dir.mkdir()

        with patch("src.auth.validate_claude_code_auth", return_value=(True, {})):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = ClaudeCodeCLI(cwd=str(default_dir))
            options = cli._build_sdk_options(cwd=None)
            assert str(options.cwd) == str(default_dir)
