"""Unit tests for WorkspaceManager."""

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
def tmp_template(tmp_path):
    """Provide a temporary CLAUDE_CWD with a .claude folder."""
    template = tmp_path / "template"
    claude_dir = template / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text('{"key": "value"}')
    (claude_dir / "subdir").mkdir()
    (claude_dir / "subdir" / "nested.txt").write_text("nested content")
    return template


@pytest.fixture
def manager(tmp_base, tmp_template):
    return WorkspaceManager(base_path=tmp_base, template_source=tmp_template)


@pytest.fixture
def manager_no_template(tmp_base):
    return WorkspaceManager(base_path=tmp_base, template_source=None)


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
        workspace = manager.resolve("alice", sync_template=False)
        assert workspace == tmp_base / "alice"
        assert workspace.is_dir()

    def test_returns_existing_directory(self, manager, tmp_base):
        first = manager.resolve("alice", sync_template=False)
        (first / "myfile.txt").write_text("data")
        second = manager.resolve("alice", sync_template=False)
        assert first == second
        assert (second / "myfile.txt").read_text() == "data"

    def test_anonymous_creates_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve(None, sync_template=False)
        assert workspace.parent == tmp_base
        assert workspace.name.startswith("_tmp_")

    def test_anonymous_returns_different_dirs(self, manager):
        w1 = manager.resolve(None, sync_template=False)
        w2 = manager.resolve(None, sync_template=False)
        assert w1 != w2

    def test_sync_template_copies_claude_dir(self, manager, tmp_base):
        workspace = manager.resolve("bob", sync_template=True)
        claude_dir = workspace / ".claude"
        assert claude_dir.is_dir()
        assert (claude_dir / "settings.json").read_text() == '{"key": "value"}'
        assert (claude_dir / "subdir" / "nested.txt").read_text() == "nested content"

    def test_sync_template_false_skips_copy(self, manager, tmp_base):
        workspace = manager.resolve("carol", sync_template=False)
        assert not (workspace / ".claude").exists()

    def test_sync_template_overwrites_existing(self, manager, tmp_base, tmp_template):
        workspace = manager.resolve("dave", sync_template=True)
        (workspace / ".claude" / "settings.json").write_text('{"modified": true}')
        manager.resolve("dave", sync_template=True)
        assert (workspace / ".claude" / "settings.json").read_text() == '{"key": "value"}'

    def test_no_template_source_skips_sync(self, manager_no_template, tmp_base):
        workspace = manager_no_template.resolve("eve", sync_template=True)
        assert not (workspace / ".claude").exists()


class TestCleanupTempWorkspace:
    def test_removes_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve(None, sync_template=False)
        assert workspace.is_dir()
        manager.cleanup_temp_workspace(workspace)
        assert not workspace.exists()

    def test_ignores_non_tmp_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice", sync_template=False)
        (workspace / "important.txt").write_text("keep")
        manager.cleanup_temp_workspace(workspace)
        assert workspace.exists()

    def test_ignores_nonexistent_directory(self, manager):
        manager.cleanup_temp_workspace(Path("/nonexistent/_tmp_abc"))


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
