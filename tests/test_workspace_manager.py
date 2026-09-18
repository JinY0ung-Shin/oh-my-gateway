"""Unit tests for WorkspaceManager."""

import os
import threading
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

    @staticmethod
    def _layout(workspace, seeded):
        return sorted(name for name in seeded if (workspace / name).is_dir())

    def test_a_claim_loser_creates_nothing_and_returns_only_the_published_layout(
        self, manager, monkeypatch
    ):
        """The review's interleaving, made impossible rather than narrowed.

        Earlier protocols let a claim loser build state, so the winner's
        post-claim root check could see a root the loser was still filling.
        Now the loser is a pure waiter. This pins that with real concurrency:
        A holds the claim and is paused before publishing; B runs in a thread,
        loses the claim, and must have created nothing while waiting; when A
        publishes (atomically), B returns the complete layout — recorded at the
        moment B returns, not afterwards.
        """
        seeded = ("Documents", "Projects", "Uploads")
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", ",".join(seeded))
        real_publish = wm._publish_workspace
        a_holds_claim, let_a_publish = threading.Event(), threading.Event()
        b_result: dict[str, object] = {}

        def _pause_before_publishing(workspace, initial_dirs):
            a_holds_claim.set()
            assert let_a_publish.wait(5), "test deadlock"
            real_publish(workspace, initial_dirs)

        monkeypatch.setattr(wm, "_publish_workspace", _pause_before_publishing)

        def _a():
            b_result["a"] = manager.resolve("iris", backend="codex")

        def _b():
            path = manager.resolve("iris", backend="codex")
            b_result["b"] = path
            b_result["b_layout"] = self._layout(path, seeded)

        ta = threading.Thread(target=_a)
        ta.start()
        assert a_holds_claim.wait(5)
        tb = threading.Thread(target=_b)
        tb.start()
        time.sleep(0.1)  # B is waiting on A's claim
        workspace = manager.base_path / "iris" / "codex"
        assert not workspace.exists(), "the loser published a root"
        assert tb.is_alive(), "the loser returned before anything was published"
        assert wm._seeding_claim(workspace).exists(), "A's claim was disturbed"

        let_a_publish.set()
        ta.join(5)
        tb.join(5)
        assert not ta.is_alive() and not tb.is_alive()
        assert b_result["a"] == b_result["b"] == workspace
        assert b_result["b_layout"] == sorted(seeded), "loser saw a partial layout"
        assert not wm._seeding_claim(workspace).exists()

    def test_publication_is_atomic_so_no_observer_sees_a_partial_root(
        self, manager, monkeypatch
    ):
        """Between claim and rename the root does not exist at all.

        The staging directory is a sibling; only the rename makes the workspace
        path visible, complete. Pins the mechanism the whole protocol rests on.
        """
        seeded = ("Documents", "Projects")
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", ",".join(seeded))
        real_rename = os.rename
        seen: dict[str, object] = {}

        def _observe_then_rename(src, dst):
            src, dst = Path(src), Path(dst)
            seen["root_visible_before_rename"] = dst.exists()
            seen["staging_complete"] = self._layout(src, seeded) == sorted(seeded)
            real_rename(src, dst)

        monkeypatch.setattr(wm.os, "rename", _observe_then_rename)
        workspace = manager.resolve("juno", backend="codex")

        assert seen == {"root_visible_before_rename": False, "staging_complete": True}
        assert self._layout(workspace, seeded) == sorted(seeded)
        leftovers = list(workspace.parent.glob(f"{wm._STAGING_PREFIX}-*"))
        assert leftovers == [], "staging copy was not cleaned up"

    def test_a_holder_that_died_before_publishing_is_taken_over(
        self, manager, monkeypatch
    ):
        """Claim left behind, no root: the next resolve must not wait forever.

        A waiter that sees the claim outlive the staleness threshold takes it
        over, publishes, and releases. Threshold shrunk so the test is fast.
        """
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents,Projects")
        monkeypatch.setattr(wm, "_STALE_CLAIM_SECONDS", 0.05)
        workspace = manager.base_path / "jack" / "codex"
        claim = wm._seeding_claim(workspace)
        claim.parent.mkdir(parents=True)
        claim.touch()  # the dead holder's claim; it never published
        stale_staging = workspace.parent / f"{wm._STAGING_PREFIX}-codex-deadbeef"
        (stale_staging / "Documents").mkdir(parents=True)

        resolved = manager.resolve("jack", backend="codex")

        assert resolved == workspace
        for name in ("Documents", "Projects"):
            assert (workspace / name).is_dir(), name
        assert not claim.exists(), "stale claim outlived the takeover"

    def test_a_failed_build_releases_the_claim_and_the_next_resolve_publishes(
        self, manager, monkeypatch
    ):
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents,Projects,Uploads")
        real_publish = wm._publish_workspace

        def _die_mid_build(workspace, initial_dirs):
            try:
                raise OSError("disk went away")
            finally:
                wm._release_claim(workspace)  # what the real finally does

        monkeypatch.setattr(wm, "_publish_workspace", _die_mid_build)
        with pytest.raises(OSError, match="disk went away"):
            manager.resolve("kate", backend="codex")
        workspace = manager.base_path / "kate" / "codex"
        assert not workspace.exists(), "a failed build must publish nothing"
        assert not wm._seeding_claim(workspace).exists()

        monkeypatch.setattr(wm, "_publish_workspace", real_publish)
        resolved = manager.resolve("kate", backend="codex")
        for name in ("Documents", "Projects", "Uploads"):
            assert (resolved / name).is_dir(), name

    def test_a_stale_absent_observation_cannot_reseed_a_completed_workspace(
        self, manager, monkeypatch
    ):
        """Two callers observe "no root"; only the first may initialize.

        B observes no root and is descheduled; A observes the same, claims,
        publishes, releases and returns; the user deletes a starter folder; B
        resumes with its stale observation. B's claim now wins (A released),
        but B re-checks the root, finds a complete workspace that is not its
        to seed, releases, and returns it untouched — the deleted folder stays
        deleted.
        """
        seeded = ("Documents", "Projects")
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", ",".join(seeded))
        real_claim = wm._claim_initialization
        state: dict[str, object] = {}

        def _b_claims_after_a_finished_and_user_deleted(workspace):
            monkeypatch.setattr(wm, "_claim_initialization", real_claim)
            state["a"] = manager.resolve("olive", backend="codex")
            assert (state["a"] / "Documents").is_dir(), "A must have seeded"
            assert not wm._seeding_claim(state["a"]).exists(), "A released it"
            (state["a"] / "Documents").rmdir()
            return real_claim(workspace)  # B's own, late claim

        monkeypatch.setattr(
            wm, "_claim_initialization", _b_claims_after_a_finished_and_user_deleted
        )
        b = manager.resolve("olive", backend="codex")

        assert b == state["a"]
        assert not (b / "Documents").exists(), "B re-seeded a completed workspace"
        assert (b / "Projects").is_dir()
        assert not wm._seeding_claim(b).exists(), "B left its late claim behind"

    def test_a_completed_seed_is_not_redone_and_leaves_no_claim(
        self, manager, monkeypatch
    ):
        """The claim is initialization state, not a permanent workspace file."""
        monkeypatch.setenv("WORKSPACE_INITIAL_DIRS", "Documents")

        workspace = manager.resolve("kira", backend="codex")
        assert not wm._seeding_claim(workspace).exists()

        (workspace / "Documents").rmdir()
        assert manager.resolve("kira", backend="codex") == workspace
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
