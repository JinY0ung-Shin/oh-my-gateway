"""Unit tests for WorkspaceManager."""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.session_manager import Session
from src import workspace_manager as wm
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

    def test_accepts_an_email_identity_whole(self, manager):
        """The identity header is routinely an email; the key is the whole thing.

        Keying on the localpart collapsed distinct principals into one workspace
        (issue #188), so ``@`` has to survive into the directory name.
        """
        assert manager._sanitize("alice@a.com") == "alice@a.com"
        assert manager._sanitize("alice@b.com") == "alice@b.com"

    def test_distinct_emails_sharing_a_localpart_get_distinct_workspaces(self, manager):
        a = manager.resolve("alice@a.com", backend="claude")
        b = manager.resolve("alice@b.com", backend="claude")
        bare = manager.resolve("alice", backend="claude")

        assert a != b != bare and a != bare
        (a / "secret.txt").write_text("from a")
        assert not (b / "secret.txt").exists()
        assert not (bare / "secret.txt").exists()

    def test_rejects_too_long(self, manager):
        with pytest.raises(ValueError):
            manager._sanitize("a" * 128)

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

    def test_initial_dirs_seed_only_when_backend_workspace_is_first_created(
        self, manager, monkeypatch
    ):
        monkeypatch.setenv(
            "WORKSPACE_INITIAL_DIRS",
            "Documents, Projects/AI, .config, Documents",
        )

        workspace = manager.resolve("erin", backend="codex")

        assert (workspace / "Documents").is_dir()
        assert (workspace / "Projects" / "AI").is_dir()
        assert (workspace / ".config").is_dir()

        # Initial folders are onboarding defaults, not permanent invariants.
        (workspace / "Documents").rmdir()
        same = manager.resolve("erin", backend="codex")
        assert same == workspace
        assert not (workspace / "Documents").exists()

    def test_initial_dirs_do_not_backfill_existing_workspace(
        self, manager, monkeypatch
    ):
        workspace = manager.resolve("frank", backend="codex")
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents,Projects")

        assert manager.resolve("frank", backend="codex") == workspace
        assert not (workspace / "Documents").exists()
        assert not (workspace / "Projects").exists()

    def test_initial_dirs_do_not_seed_anonymous_or_user_aggregate_roots(
        self, manager, monkeypatch
    ):
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents")

        anonymous = manager.resolve(None, backend="codex")
        aggregate = manager.resolve("grace")

        assert not (anonymous / "Documents").exists()
        assert list(aggregate.iterdir()) == []

    def test_a_concurrent_first_resolve_never_sees_a_half_seeded_workspace(
        self, manager, monkeypatch
    ):
        """Both first resolves must return the whole configured layout.

        The workspace directory is published before its contents exist, so a
        second resolver arriving in that window used to see a directory, call it
        pre-existing, and hand back a partial layout. It is the in-progress
        marker, not the directory, that answers "was this just created".

        The creator is paused inside its own seeding — after the marker and the
        first directory, before the rest — which is exactly the interleaving the
        review described.
        """
        seeded = ("Documents", "Projects", "Uploads")
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", ",".join(seeded))
        second: dict[str, object] = {}

        real_seed = wm._seed_initial_dirs

        def _layout(workspace):
            return sorted(name for name in seeded if (workspace / name).is_dir())

        def _seed_then_let_the_other_resolver_in(workspace, initial_dirs):
            (workspace / initial_dirs[0]).mkdir(parents=True, exist_ok=True)
            # Mid-initialization: the directory exists, the layout does not.
            monkeypatch.setattr(wm, "_seed_initial_dirs", real_seed)
            second["path"] = manager.resolve("iris", backend="codex")
            # What the second caller could see AS IT RETURNED. Reading this
            # after the creator finishes would pass either way.
            second["layout"] = _layout(second["path"])
            real_seed(workspace, initial_dirs)

        monkeypatch.setattr(wm, "_seed_initial_dirs", _seed_then_let_the_other_resolver_in)
        first = manager.resolve("iris", backend="codex")

        assert second["path"] == first
        assert second["layout"] == sorted(seeded), "second caller returned a partial layout"
        assert _layout(first) == sorted(seeded)

    def test_a_crashed_first_seeding_is_finished_by_the_next_resolve(
        self, manager, monkeypatch
    ):
        """A partial layout must not harden into "existing workspace, never seed".

        Seeding can die after the directory is published — a filesystem error, a
        quota or permission failure, the process going away. Before this, every
        later resolve classified the leftover as pre-existing and skipped
        seeding forever, so the workspace stayed permanently half built.
        """
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents,Projects,Uploads")
        boom = OSError("disk went away")

        def _die_partway(workspace, initial_dirs):
            (workspace / initial_dirs[0]).mkdir(parents=True, exist_ok=True)
            raise boom

        monkeypatch.setattr(wm, "_seed_initial_dirs", _die_partway)
        with pytest.raises(OSError, match="disk went away"):
            manager.resolve("jack", backend="codex")

        monkeypatch.undo()
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents,Projects,Uploads")
        workspace = manager.resolve("jack", backend="codex")

        for name in ("Documents", "Projects", "Uploads"):
            assert (workspace / name).is_dir(), name
        assert not (workspace / wm._SEEDING_MARKER).exists(), "marker outlived seeding"

    def test_a_completed_seed_is_not_redone_and_leaves_no_marker(
        self, manager, monkeypatch
    ):
        """The marker is initialization state, not a permanent workspace file."""
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents")

        workspace = manager.resolve("kate", backend="codex")
        assert not (workspace / wm._SEEDING_MARKER).exists()

        (workspace / "Documents").rmdir()
        assert manager.resolve("kate", backend="codex") == workspace
        assert not (workspace / "Documents").exists(), "a deletion must stay deleted"

    def test_a_workspace_from_before_this_feature_is_never_backfilled(
        self, manager, tmp_base, monkeypatch
    ):
        """No marker on a directory nobody seeded means legacy, not partial."""
        legacy = tmp_base / "liam" / "codex"
        legacy.mkdir(parents=True)
        (legacy / "existing.txt").write_text("mine")
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents,Projects")

        workspace = manager.resolve("liam", backend="codex")

        assert workspace == legacy
        assert (workspace / "existing.txt").read_text() == "mine"
        assert sorted(p.name for p in workspace.iterdir()) == ["existing.txt"]

    @pytest.mark.parametrize(
        "value",
        [
            ".claude/skills",
            ".claude/skills/review",
            ".claude/agents",
            ".claude/agents/triage/nested",
        ],
    )
    def test_initial_dirs_reject_the_claude_managed_mirror_namespace(
        self, manager, tmp_base, monkeypatch, value
    ):
        """``.claude/{skills,agents}`` is the backend's, not the operator's.

        The Claude resource bridge migrates a legacy native tree into the
        canonical ``skills``/``agents`` roots and then keeps ``.claude/<kind>``
        as a marked mirror of them. Seeding into it does not do what the
        operator wrote: ``.claude/skills/review`` alone is migrated away to
        ``skills/review``, and seeding both sides at once leaves an unmanaged
        conflict that turns mirror management off for that workspace. The
        canonical roots express the same intent, so this is refused rather than
        quietly rewritten.
        """
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", value)

        with pytest.raises(ValueError, match="WORKSPACE_INITIAL_DIRS"):
            manager.resolve("mia", backend="claude")

        assert not (tmp_base / "mia").exists()

    def test_initial_dirs_still_allow_the_canonical_resource_roots(
        self, manager, monkeypatch
    ):
        """The intent behind a rejected entry stays expressible."""
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "skills/review,agents/triage,.claude")

        workspace = manager.resolve("noah", backend="claude")

        assert (workspace / "skills" / "review").is_dir()
        assert (workspace / "agents" / "triage").is_dir()
        assert (workspace / ".claude").is_dir()

    @pytest.mark.parametrize(
        "value",
        [
            "../escape",
            "safe/../../escape",
            "/absolute/path",
            ".",
        ],
    )
    def test_initial_dirs_reject_unsafe_paths_before_workspace_creation(
        self, manager, tmp_base, monkeypatch, value
    ):
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", value)

        with pytest.raises(ValueError, match="WORKSPACE_INITIAL_DIRS"):
            manager.resolve("heidi", backend="codex")

        assert not (tmp_base / "heidi" / "codex").exists()


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

    def test_returns_zero_when_listing_base_path_raises(self, manager):
        """An unreadable base directory (iterdir raising OSError) is tolerated:
        the sweep reports zero removals instead of crashing startup."""
        manager.base_path.mkdir(parents=True, exist_ok=True)
        with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            assert manager.sweep_orphan_temp_workspaces(max_age_seconds=0) == 0

    def test_skips_child_whose_stat_raises(self, manager):
        """A _tmp_ child that vanishes or is unreadable between listing and
        stat() is skipped, not removed and not fatal."""
        child = manager.resolve(None)
        assert child.name.startswith("_tmp_")

        real_stat = Path.stat

        def _selective_stat(self, *args, **kwargs):
            if self.name.startswith("_tmp_"):
                raise OSError("stat failed")
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _selective_stat):
            removed = manager.sweep_orphan_temp_workspaces(max_age_seconds=0)

        assert removed == 0
        assert child.exists()


class TestResolveBasePath:
    def test_uses_user_workspaces_dir_when_set(self):
        from src import workspace_manager as wm

        with patch.object(wm, "USER_WORKSPACES_DIR", "/srv/workspaces"):
            assert wm._resolve_base_path() == Path("/srv/workspaces")

    def test_falls_back_to_stable_temp_dir_when_unset(self):
        import tempfile

        from src import workspace_manager as wm

        with patch.object(wm, "USER_WORKSPACES_DIR", ""):
            expected = Path(tempfile.gettempdir()) / "oh-my-gateway-workspaces"
            assert wm._resolve_base_path() == expected


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
