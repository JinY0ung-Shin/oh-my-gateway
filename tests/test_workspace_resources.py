"""Tests for backend-neutral Claude workspace skills/subagents."""

from pathlib import Path

from src.backends.claude.workspace_resources import ensure_workspace_resources
from src.workspace_manager import WorkspaceManager


def _write(path: Path, text: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fresh_workspace_gets_visible_resources_and_real_claude_mirrors(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ensure_workspace_resources(workspace)

    for name in ("skills", "agents"):
        visible = workspace / name
        native = workspace / ".claude" / name
        assert visible.is_dir() and not visible.is_symlink()
        assert native.is_dir() and not native.is_symlink()
        assert (native / ".oh-my-gateway-managed").is_file()


def test_visible_skill_is_materialized_under_claude_native_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    source = workspace / "skills" / "review" / "SKILL.md"
    _write(source, "---\nname: review\n---\nfirst\n")

    ensure_workspace_resources(workspace)

    mirror = workspace / ".claude" / "skills" / "review" / "SKILL.md"
    assert mirror.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert not mirror.is_symlink()


def test_materialization_refreshes_replaced_file_and_removes_stale_entry(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    source = workspace / "agents" / "reviewer.md"
    _write(source, "v1\n")
    ensure_workspace_resources(workspace)

    # Replacing the canonical inode (not just writing through the hard link) must
    # still be picked up on the next workspace resolve/materialization.
    replacement = workspace / "agents" / "replacement.tmp"
    replacement.write_text("v2\n", encoding="utf-8")
    replacement.replace(source)
    stale = workspace / "agents" / "stale.md"
    stale.write_text("stale\n", encoding="utf-8")
    ensure_workspace_resources(workspace)
    stale.unlink()

    ensure_workspace_resources(workspace)

    native = workspace / ".claude" / "agents"
    assert (native / "reviewer.md").read_text(encoding="utf-8") == "v2\n"
    assert not (native / "stale.md").exists()


def test_legacy_native_resources_migrate_to_visible_canonical_location(tmp_path):
    workspace = tmp_path / "workspace"
    legacy = workspace / ".claude" / "agents" / "reviewer.md"
    _write(legacy, "legacy\n")

    ensure_workspace_resources(workspace)

    visible = workspace / "agents" / "reviewer.md"
    native = workspace / ".claude" / "agents" / "reviewer.md"
    assert visible.read_text(encoding="utf-8") == "legacy\n"
    assert native.read_text(encoding="utf-8") == "legacy\n"
    assert (workspace / ".claude" / "agents" / ".oh-my-gateway-managed").is_file()


def test_old_directory_symlink_bridge_is_upgraded_to_real_native_directory(tmp_path):
    workspace = tmp_path / "workspace"
    visible = workspace / "skills"
    _write(visible / "review" / "SKILL.md", "skill\n")
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "skills").symlink_to(Path("..") / "skills", target_is_directory=True)

    ensure_workspace_resources(workspace)

    native = claude / "skills"
    assert native.is_dir()
    assert not native.is_symlink()
    assert (native / "review" / "SKILL.md").read_text(encoding="utf-8") == "skill\n"


def test_conflicting_unmanaged_native_and_visible_trees_are_preserved(tmp_path):
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "visible" / "SKILL.md", "visible\n")
    _write(workspace / ".claude" / "skills" / "native" / "SKILL.md", "native\n")

    ensure_workspace_resources(workspace)

    assert (workspace / "skills" / "visible" / "SKILL.md").read_text() == "visible\n"
    assert (workspace / ".claude" / "skills" / "native" / "SKILL.md").read_text() == "native\n"
    assert not (workspace / ".claude" / "skills" / ".oh-my-gateway-managed").exists()


def test_symlinked_visible_entry_is_not_promoted_into_claude_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    outside = tmp_path / "outside"
    _write(outside / "SKILL.md", "outside\n")
    (workspace / "skills" / "linked").symlink_to(outside, target_is_directory=True)

    ensure_workspace_resources(workspace)

    assert not (workspace / ".claude" / "skills" / "linked").exists()


def test_workspace_manager_prepares_resources_only_for_named_claude_workspace(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")

    claude = manager.resolve("alice", backend="claude")
    codex = manager.resolve("alice", backend="codex")
    anonymous = manager.resolve(None, backend="claude")

    assert (claude / "skills").is_dir()
    assert (claude / "agents").is_dir()
    assert (claude / ".claude" / "skills").is_dir()
    assert not (codex / "skills").exists()
    assert not (codex / ".claude").exists()
    assert not (anonymous / "skills").exists()
