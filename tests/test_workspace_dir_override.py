"""Tests for backend workspace directory aliases."""

import pytest

from src.workspace_manager import WorkspaceManager


def test_claude_workspace_dir_override_changes_only_filesystem_name(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", "pro")

    workspace = manager.resolve("alice", backend="claude")

    assert workspace == tmp_path / "workspaces" / "alice" / "pro"
    assert workspace.is_dir()
    assert not (tmp_path / "workspaces" / "alice" / "claude").exists()


def test_claude_workspace_dir_defaults_to_backend_name(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.delenv("CLAUDE_WORKSPACE_DIR", raising=False)

    workspace = manager.resolve("alice", backend="claude")

    assert workspace == tmp_path / "workspaces" / "alice" / "claude"


def test_blank_claude_workspace_dir_preserves_default(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", "   ")

    workspace = manager.resolve("alice", backend="claude")

    assert workspace == tmp_path / "workspaces" / "alice" / "claude"


def test_claude_workspace_dir_override_does_not_affect_other_backends(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", "pro")

    workspace = manager.resolve("alice", backend="codex")

    assert workspace == tmp_path / "workspaces" / "alice" / "codex"


@pytest.mark.parametrize("value", ["../pro", "pro/child", ".pro", "PRO", "pro space"])
def test_invalid_claude_workspace_dir_override_is_rejected(tmp_path, monkeypatch, value):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", value)

    with pytest.raises(ValueError, match="CLAUDE_WORKSPACE_DIR"):
        manager.resolve("alice", backend="claude")


@pytest.mark.parametrize("value", ["codex", "opencode"])
def test_claude_workspace_dir_cannot_collide_with_other_backend(tmp_path, monkeypatch, value):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", value)

    with pytest.raises(ValueError, match="collides with"):
        manager.resolve("alice", backend="claude")

    assert not (tmp_path / "workspaces" / "alice" / value).exists()


def test_claude_workspace_dir_may_explicitly_keep_claude_name(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", "claude")

    workspace = manager.resolve("alice", backend="claude")

    assert workspace == tmp_path / "workspaces" / "alice" / "claude"


def test_anonymous_workspace_layout_ignores_claude_override(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", "pro")

    workspace = manager.resolve(None, backend="claude")

    assert workspace.parent == tmp_path / "workspaces"
    assert workspace.name.startswith("_tmp_")


def test_anonymous_workspace_ignores_invalid_claude_override(tmp_path, monkeypatch):
    manager = WorkspaceManager(base_path=tmp_path / "workspaces")
    monkeypatch.setenv("CLAUDE_WORKSPACE_DIR", "../pro")

    workspace = manager.resolve(None, backend="claude")

    assert workspace.parent == tmp_path / "workspaces"
    assert workspace.name.startswith("_tmp_")
