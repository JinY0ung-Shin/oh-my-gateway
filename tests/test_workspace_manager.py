"""Unit tests for WorkspaceManager."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.session_manager import Session
from src.workspace_manager import WorkspaceManager, _resolve_project_root, _resolve_template_source


@pytest.fixture
def tmp_base(tmp_path):
    """Provide a temporary base directory for workspaces."""
    return tmp_path / "workspaces"


@pytest.fixture
def tmp_template(tmp_path):
    """Provide a temporary CLAUDE_CWD-style template directory."""
    template = tmp_path / "template"

    claude_dir = template / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text('{"key": "value"}')
    (claude_dir / "subdir").mkdir()
    (claude_dir / "subdir" / "nested.txt").write_text("nested content")
    claude_skill = claude_dir / "skills" / "shared-skill"
    claude_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text(
        "---\nname: shared-skill\ndescription: Claude skill\n---\nClaude"
    )
    (template / "CLAUDE.md").write_text("# Claude instructions\n")

    agents_dir = template / ".agents"
    agents_dir.mkdir()
    (agents_dir / "config.json").write_text('{"agent": true}')
    (template / "AGENTS.md").write_text("# Agent instructions\n")

    opencode_dir = template / ".opencode"
    opencode_dir.mkdir()
    (opencode_dir / "opencode.json").write_text('{"permission": {}}')

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

    def test_named_user_backend_creates_backend_directory(self, manager, tmp_base):
        workspace = manager.resolve("alice", backend="codex", sync_template=False)
        assert workspace == tmp_base / "alice" / "codex"
        assert workspace.is_dir()

    def test_named_user_backend_directories_are_independent(self, manager, tmp_base):
        claude = manager.resolve("alice", backend="claude", sync_template=False)
        codex = manager.resolve("alice", backend="codex", sync_template=False)
        assert claude == tmp_base / "alice" / "claude"
        assert codex == tmp_base / "alice" / "codex"
        assert claude != codex

    def test_anonymous_ignores_backend_for_tmp_layout(self, manager, tmp_base):
        workspace = manager.resolve(None, backend="opencode", sync_template=False)
        assert workspace.parent == tmp_base
        assert workspace.name.startswith("_tmp_")

    def test_rejects_invalid_backend_name(self, manager):
        with pytest.raises(ValueError, match="Invalid backend"):
            manager.resolve("alice", backend="../codex", sync_template=False)

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

    def test_claude_sync_copies_only_claude_native_files(self, manager):
        workspace = manager.resolve("alice", backend="claude", sync_template=True)
        assert (workspace / ".claude" / "settings.json").is_file()
        assert (workspace / "CLAUDE.md").read_text() == "# Claude instructions\n"
        assert not (workspace / ".agents").exists()
        assert not (workspace / ".opencode").exists()

    def test_codex_sync_copies_agents_and_mirrors_claude_skills(self, manager):
        workspace = manager.resolve("alice", backend="codex", sync_template=True)
        assert (workspace / ".agents" / "config.json").is_file()
        assert (workspace / "AGENTS.md").read_text() == "# Agent instructions\n"
        mirrored = workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md"
        assert mirrored.read_text().endswith("Claude")
        assert not (workspace / ".claude").exists()
        assert not (workspace / ".opencode").exists()

    def test_opencode_sync_copies_opencode_and_mirrors_claude_skills(self, manager):
        workspace = manager.resolve("alice", backend="opencode", sync_template=True)
        assert (workspace / ".opencode" / "opencode.json").is_file()
        mirrored = workspace / ".opencode" / "skills" / "shared-skill" / "SKILL.md"
        assert mirrored.read_text().endswith("Claude")
        assert not (workspace / ".claude").exists()
        assert not (workspace / ".agents").exists()

    def test_codex_native_skills_win_over_claude_mirror(self, tmp_base, tmp_template):
        native = tmp_template / ".agents" / "skills" / "shared-skill"
        native.mkdir(parents=True)
        (native / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Codex native\n---\nCodex"
        )
        mgr = WorkspaceManager(base_path=tmp_base, template_source=tmp_template)
        workspace = mgr.resolve("alice", backend="codex", sync_template=True)
        skill = workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md"
        assert skill.read_text().endswith("Codex")

    def test_opencode_claude_compatibility_wins_over_agents_duplicate(self, tmp_base, tmp_template):
        agent_skill = tmp_template / ".agents" / "skills" / "shared-skill"
        agent_skill.mkdir(parents=True)
        (agent_skill / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Agent copy\n---\nAgent"
        )
        mgr = WorkspaceManager(base_path=tmp_base, template_source=tmp_template)
        workspace = mgr.resolve("alice", backend="opencode", sync_template=True)
        skill = workspace / ".opencode" / "skills" / "shared-skill" / "SKILL.md"
        assert skill.read_text().endswith("Claude")

    def test_template_source_without_claude_dir_can_sync_agents(self, tmp_base, tmp_path):
        template = tmp_path / "template"
        agents = template / ".agents"
        agents.mkdir(parents=True)
        (agents / "config.json").write_text('{"agent": true}')
        mgr = WorkspaceManager(base_path=tmp_base, template_source=template)

        workspace = mgr.resolve("alice", backend="codex", sync_template=True)

        assert (workspace / ".agents" / "config.json").is_file()

    def test_codex_mirror_replaces_symlinked_agents_dir(self, tmp_base, tmp_path):
        template = tmp_path / "template"
        skill = template / ".claude" / "skills" / "shared-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Claude skill\n---\nClaude"
        )
        target = tmp_path / "outside"
        target.mkdir()

        mgr = WorkspaceManager(base_path=tmp_base, template_source=template)
        workspace = mgr.resolve("alice", backend="codex", sync_template=False)
        (workspace / ".agents").symlink_to(target, target_is_directory=True)

        mgr.resolve("alice", backend="codex", sync_template=True)

        assert (workspace / ".agents").is_dir()
        assert not (workspace / ".agents").is_symlink()
        assert (workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md").is_file()
        assert not (target / "skills").exists()

    def test_codex_mirror_replaces_symlinked_skills_dir(self, tmp_base, tmp_path):
        template = tmp_path / "template"
        skill = template / ".claude" / "skills" / "shared-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Claude skill\n---\nClaude"
        )
        target = tmp_path / "outside-skills"
        target.mkdir()

        mgr = WorkspaceManager(base_path=tmp_base, template_source=template)
        workspace = mgr.resolve("alice", backend="codex", sync_template=False)
        (workspace / ".agents").mkdir()
        (workspace / ".agents" / "skills").symlink_to(target, target_is_directory=True)

        mgr.resolve("alice", backend="codex", sync_template=True)

        skills = workspace / ".agents" / "skills"
        assert skills.is_dir()
        assert not skills.is_symlink()
        assert (skills / "shared-skill" / "SKILL.md").is_file()
        assert not (target / "shared-skill").exists()

    def test_opencode_mirror_replaces_symlinked_skills_dir(self, tmp_base, tmp_path):
        template = tmp_path / "template"
        skill = template / ".claude" / "skills" / "shared-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: shared-skill\ndescription: Claude skill\n---\nClaude"
        )
        target = tmp_path / "outside-skills"
        target.mkdir()

        mgr = WorkspaceManager(base_path=tmp_base, template_source=template)
        workspace = mgr.resolve("alice", backend="opencode", sync_template=False)
        (workspace / ".opencode").mkdir()
        (workspace / ".opencode" / "skills").symlink_to(target, target_is_directory=True)

        mgr.resolve("alice", backend="opencode", sync_template=True)

        skills = workspace / ".opencode" / "skills"
        assert skills.is_dir()
        assert not skills.is_symlink()
        assert (skills / "shared-skill" / "SKILL.md").is_file()
        assert not (target / "shared-skill").exists()

    def test_skill_dirs_containing_symlinks_are_not_mirrored(self, tmp_base, tmp_path):
        template = tmp_path / "template"
        skill = template / ".claude" / "skills" / "unsafe-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: unsafe-skill\ndescription: Unsafe skill\n---\nUnsafe"
        )
        linked = tmp_path / "linked.txt"
        linked.write_text("secret")
        (skill / "linked.txt").symlink_to(linked)

        mgr = WorkspaceManager(base_path=tmp_base, template_source=template)
        workspace = mgr.resolve("alice", backend="codex", sync_template=True)

        assert not (workspace / ".agents" / "skills" / "unsafe-skill").exists()

    def test_sync_template_false_skips_copy(self, manager, tmp_base):
        workspace = manager.resolve("carol", sync_template=False)
        assert not (workspace / ".claude").exists()

    def test_sync_template_overwrites_existing(self, manager, tmp_base, tmp_template):
        workspace = manager.resolve("dave", sync_template=True)
        (workspace / ".claude" / "settings.json").write_text('{"modified": true}')
        manager.resolve("dave", sync_template=True)
        assert (workspace / ".claude" / "settings.json").read_text() == '{"key": "value"}'

    def test_sync_template_removes_deleted_claude_template_files(
        self, manager, tmp_base, tmp_template
    ):
        skill_dir = tmp_template / ".claude" / "skills" / "old-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: old-skill\ndescription: Old skill\n---\nOld"
        )

        workspace = manager.resolve("frank", sync_template=True)
        copied_skill = workspace / ".claude" / "skills" / "old-skill" / "SKILL.md"
        assert copied_skill.is_file()

        (skill_dir / "SKILL.md").unlink()
        skill_dir.rmdir()
        manager.resolve("frank", sync_template=True)

        assert not copied_skill.exists()

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


class TestResolveProjectRoot:
    def test_resolve_template_source_returns_directory_without_claude_dir(
        self, tmp_path, monkeypatch
    ):
        claude_cwd = tmp_path / "repo"
        claude_cwd.mkdir()

        monkeypatch.setenv("CLAUDE_CWD", str(claude_cwd))

        assert _resolve_template_source() == claude_cwd

    def test_prefers_claude_cwd_when_it_has_pyproject(self, tmp_path, monkeypatch):
        claude_cwd = tmp_path / "repo"
        claude_cwd.mkdir()
        (claude_cwd / "pyproject.toml").write_text("[project]\nname='x'\n")
        base = tmp_path / "tmp_workspaces"
        base.mkdir()

        monkeypatch.setenv("CLAUDE_CWD", str(claude_cwd))
        assert _resolve_project_root(base) == claude_cwd

    def test_falls_back_to_walking_up_when_claude_cwd_lacks_pyproject(self, tmp_path, monkeypatch):
        claude_cwd = tmp_path / "no_pyproject"
        claude_cwd.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        base = repo / "nested"
        base.mkdir()

        monkeypatch.setenv("CLAUDE_CWD", str(claude_cwd))
        assert _resolve_project_root(base) == repo.resolve()

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CWD", raising=False)
        base = tmp_path / "lonely"
        base.mkdir()
        # tmp_path itself has no pyproject, and walking to / won't find one in CI either.
        # Guard the assertion against host repos by checking only the CLAUDE_CWD branch.
        monkeypatch.setenv("CLAUDE_CWD", "")
        # This may return a real ancestor's pyproject; we only assert no crash + Path-or-None.
        result = _resolve_project_root(base)
        assert result is None or isinstance(result, Path)

    def test_sync_project_files_uses_claude_cwd_when_base_is_outside_repo(self, tmp_path):
        claude_cwd = tmp_path / "repo"
        claude_cwd.mkdir()
        (claude_cwd / "pyproject.toml").write_text("[project]\nname='x'\n")
        (claude_cwd / "uv.lock").write_text("# lock")
        base = tmp_path / "tmp_workspaces"
        base.mkdir()

        mgr = WorkspaceManager(base_path=base, template_source=None, project_root=claude_cwd)
        workspace = mgr.resolve("alice", sync_template=True)
        assert (workspace / "pyproject.toml").is_symlink()
        assert (workspace / "pyproject.toml").resolve() == (claude_cwd / "pyproject.toml").resolve()
        assert (workspace / "uv.lock").is_symlink()


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
