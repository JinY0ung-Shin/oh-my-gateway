"""Per-uid config dirs and workspace ACLs for uid isolation (design §3–§4).

Everything here runs as one unprivileged uid: the session uid ``U`` of a layout
is an arbitrary number the kernel never checks access for, so these tests pin
what the gateway builds (dirs, links, ACL entries, markers, Landlock lists) and
that it never follows what a session could plant. That the kernel then lets
``U`` in is the container acceptance test's (docs/isolation-uid-verification.md).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from src import claude_config_paths as cp
from src.backends.claude import landlock
from src.backends.claude import posix_acl
from src.backends.claude import uid_isolation as ui
from src.backends.claude import uid_layout
from src.backends.claude import workspace_sandbox as ws

UID = 23456


def _unset(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, "x")
    monkeypatch.delenv(name)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A uid-mode layout under tmp_path: shared S, per-uid base, workspaces."""
    import tempfile

    if not posix_acl.supported(tmp_path):
        pytest.skip("this filesystem has no POSIX ACL support")
    # The grant filter refuses anything inside the temp dir; the layout here
    # lives in tmp_path, so the temp dir is pointed somewhere else.
    elsewhere = tmp_path / "systemp"
    elsewhere.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(elsewhere))
    shared = tmp_path / "shared"
    for name in ("skills", "agents", "commands", "plugins", "rules"):
        (shared / name).mkdir(parents=True)
    (shared / "settings.json").write_text("{}\n")
    (shared / "CLAUDE.md").write_text("# shared\n")
    (shared / "plugins" / "installed_plugins.json").write_text('{"plugins": {}}')
    (shared / "plugins" / "known_marketplaces.json").write_text("{}")
    (shared / "plugins" / "marketplaces").mkdir()
    base = tmp_path / "users"
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    monkeypatch.setenv(cp.CONFIG_DIR_ENV, str(shared))
    monkeypatch.setenv(cp.CONFIG_BASE_ENV, str(base))
    _unset(monkeypatch, cp.MIGRATE_FROM_ENV)
    monkeypatch.setenv(landlock.MODE_ENV, "uid")
    monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
    monkeypatch.setattr(uid_layout, "_SHM_DIR", str(tmp_path / "shm"))
    (tmp_path / "shm").mkdir()
    from src.workspace_manager import workspace_manager

    monkeypatch.setattr(workspace_manager, "base_path", workspaces)
    landlock.reset_mode_cache()
    uid_layout._granted_anonymous.clear()
    uid_layout._clean_for.clear()
    yield {"shared": shared, "base": base, "workspaces": workspaces, "tmp": tmp_path}
    landlock.reset_mode_cache()


def _named_ws(tree, user="alice") -> Path:
    path = tree["workspaces"] / user / "claude"
    path.mkdir(parents=True)
    return path


def _acl(path: Path, *, default: bool = False):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return posix_acl.get_acl(fd, default=default)
    finally:
        os.close(fd)


def _named(entries):
    return {(e.id, e.perm) for e in entries if e.tag == posix_acl.ACL_USER}


class TestSharedNames:
    def test_links_cover_the_guard_and_landlock_assets(self):
        expected = set(ws._SHARED_CLAUDE_ASSETS) - {"plugins"}
        expected |= set(landlock._LANDLOCK_ONLY_ASSETS)
        assert expected <= set(uid_layout.SHARED_LINKS)

    def test_every_link_target_is_readable_in_the_shared_dir(self):
        readable = set(landlock._shared_dir_readable())
        assert set(uid_layout.SHARED_LINKS) <= readable


class TestEnsureUserConfig:
    def test_builds_the_layout(self, tree):
        layout = cp.layout(UID)
        uid_layout.ensure_user_config(layout)
        p = tree["base"] / str(UID)
        shared = os.path.realpath(tree["shared"])
        for name in uid_layout.SHARED_LINKS:
            assert os.readlink(p / name) == os.path.join(shared, name), name
        assert os.readlink(p / ".claude.json") == "state/.claude.json"
        assert json.loads((p / "state" / ".claude.json").read_text()) == {}
        for name in uid_layout.PLUGIN_LINKS:
            target = os.path.join(shared, "plugins", name)
            assert os.readlink(p / "plugins" / name) == target
        # P and P/plugins: U may read, never write (it must not swap links).
        for path in (p, p / "plugins"):
            assert _named(_acl(path)) == {(UID, posix_acl.R | posix_acl.X)}
            assert _acl(path, default=True) is None
        # The writable dirs: U rwx, and a default ACL keeping both sides in.
        for rel in uid_layout.WRITABLE_DIRS:
            path = p / rel
            assert _named(_acl(path)) == {(UID, 7)}, rel
            assert _named(_acl(path, default=True)) == {(UID, 7), (os.geteuid(), 7)}
        assert stat.S_IMODE(os.stat(p).st_mode) & 0o007 == 0  # o::---

    def test_is_idempotent(self, tree):
        layout = cp.layout(UID)
        uid_layout.ensure_user_config(layout)
        p = tree["base"] / str(UID)
        before = {
            rel: os.stat(p / rel).st_ctime_ns for rel in uid_layout.WRITABLE_DIRS
        }
        uid_layout.ensure_user_config(layout)
        after = {rel: os.stat(p / rel).st_ctime_ns for rel in uid_layout.WRITABLE_DIRS}
        assert before == after  # the ACLs were not rewritten

    def test_a_wrong_link_is_replaced(self, tree):
        layout = cp.layout(UID)
        uid_layout.ensure_user_config(layout)
        p = tree["base"] / str(UID)
        (p / "skills").unlink()
        (p / "skills").symlink_to("/etc")
        uid_layout.ensure_user_config(layout)
        assert os.readlink(p / "skills") == os.path.join(
            os.path.realpath(tree["shared"]), "skills"
        )

    def test_a_real_entry_in_place_of_a_link_is_left_alone(self, tree, caplog):
        p = tree["base"] / str(UID)
        (p / "agents").mkdir(parents=True)
        (p / "agents" / "mine.md").write_text("keep")
        with caplog.at_level(logging.WARNING):
            uid_layout.ensure_user_config(cp.layout(UID))
        assert (p / "agents" / "mine.md").read_text() == "keep"
        assert "not the link" in caplog.text

    def test_a_planted_symlink_for_a_writable_dir_is_refused(self, tree):
        p = tree["base"] / str(UID)
        p.mkdir(parents=True)
        outside = tree["tmp"] / "outside"
        outside.mkdir()
        (p / "projects").symlink_to(outside)
        with pytest.raises(landlock.WorkspaceRefused):
            uid_layout.ensure_user_config(cp.layout(UID))
        assert _acl(outside) is None  # nothing was granted through the link


class TestGrantWorkspace:
    def test_the_root_gets_access_and_default_acls(self, tree):
        workspace = _named_ws(tree)
        uid_layout.grant_workspace(str(workspace), UID, named=True)
        assert _named(_acl(workspace)) == {(UID, 7)}
        assert _named(_acl(workspace, default=True)) == {(UID, 7), (os.geteuid(), 7)}

    def test_the_existing_tree_is_granted_once(self, tree, monkeypatch):
        workspace = _named_ws(tree)
        (workspace / "old").mkdir()
        (workspace / "old" / "notes.txt").write_text("x")
        calls = []
        real = posix_acl.grant_tree

        def counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(posix_acl, "grant_tree", counting)
        uid_layout.grant_workspace(str(workspace), UID, named=True)
        uid_layout.grant_workspace(str(workspace), UID, named=True)
        assert len(calls) == 1
        assert _named(_acl(workspace / "old")) == {(UID, 7)}
        marker = workspace.parent / (uid_layout._TREE_MARKER_PREFIX + "claude")
        assert marker.read_text().strip() == str(UID)
        # Another uid (a restored registry) grants the tree again.
        uid_layout.grant_workspace(str(workspace), UID + 1, named=True)
        assert len(calls) == 2

    def test_mirrored_skills_are_granted_and_the_mirror_rebuilt(self, tree):
        from src.backends.claude import workspace_resources as wr

        workspace = _named_ws(tree)
        skill = workspace / "skills" / "mine" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: mine\n---\n")
        wr.ensure_workspace_resources(workspace)
        mirrored = workspace / ".claude" / "skills" / "mine" / "SKILL.md"
        assert skill.stat().st_nlink == 2  # canonical + mirror: one inode
        uid_layout.grant_workspace(str(workspace), UID, named=True)
        fd = os.open(skill, os.O_RDONLY)
        try:
            assert (UID, 6) in _named(posix_acl.get_acl(fd))  # rw like the owner
        finally:
            os.close(fd)
        assert mirrored.exists()
        assert os.path.samestat(skill.stat(), mirrored.stat())

    def test_an_anonymous_tree_is_granted_once_per_process(self, tree, monkeypatch):
        workspace = tree["workspaces"] / "_tmp_abc"
        workspace.mkdir()
        calls = []
        monkeypatch.setattr(
            posix_acl,
            "grant_tree",
            lambda *a, **k: calls.append(a) or posix_acl.GrantStats(0, 0, 0, 0, False),
        )
        uid_layout.grant_workspace(str(workspace), UID, named=False)
        uid_layout.grant_workspace(str(workspace), UID, named=False)
        assert len(calls) == 1
        assert not list(tree["workspaces"].glob(".oh-my-gateway-acl-*"))

    def test_a_symlinked_workspace_is_refused(self, tree):
        target = tree["tmp"] / "elsewhere"
        target.mkdir()
        link = tree["workspaces"] / "_tmp_link"
        link.symlink_to(target)
        with pytest.raises(landlock.WorkspaceRefused):
            uid_layout.grant_workspace(str(link), UID, named=False)
        assert _acl(target) is None


def _transcript(cwd: str, *prompts: str) -> str:
    lines = [
        {"type": "user", "cwd": cwd, "message": {"content": p}} for p in prompts
    ]
    lines.append({"type": "assistant", "cwd": cwd, "message": {"content": "ok"}})
    return "".join(json.dumps(line) + "\n" for line in lines)


class TestMigrateLegacy:
    @pytest.fixture
    def legacy(self, tree, monkeypatch):
        legacy = tree["tmp"] / "legacy"
        (legacy / "projects").mkdir(parents=True)
        monkeypatch.setenv(cp.MIGRATE_FROM_ENV, str(legacy))
        return legacy

    def _project(self, legacy: Path, workspace: Path) -> Path:
        project = legacy / "projects" / ws._project_dir_name(str(workspace))
        project.mkdir()
        return project

    def test_copies_the_users_transcripts_sidecars_and_memory(self, tree, legacy):
        workspace = _named_ws(tree)
        project = self._project(legacy, workspace)
        (project / "s1.jsonl").write_text(_transcript(str(workspace), "hi"))
        (project / "s1" / "subagents").mkdir(parents=True)
        (project / "s1" / "subagents" / "a.jsonl").write_text("{}\n")
        (project / "memory").mkdir()
        (project / "memory" / "MEMORY.md").write_text("remember")
        uid_layout.ensure_user_config(cp.layout(UID))
        uid_layout.migrate_legacy("alice", UID, str(workspace))
        dst = tree["base"] / str(UID) / "projects" / project.name
        assert (dst / "s1.jsonl").read_text() == (project / "s1.jsonl").read_text()
        assert (dst / "s1" / "subagents" / "a.jsonl").exists()
        assert (dst / "memory" / "MEMORY.md").read_text() == "remember"
        assert (project / "s1.jsonl").exists()  # the source stays (rollback)
        # The copies inherit the default ACL: the session uid reads and writes
        # them (its named entry is rwx, the 0666 create mode caps the mask rw-).
        fd = os.open(dst / "s1.jsonl", os.O_RDONLY)
        try:
            entries = posix_acl.get_acl(fd)
        finally:
            os.close(fd)
        assert (UID, 7) in _named(entries)
        mask = [e.perm for e in entries if e.tag == posix_acl.ACL_MASK]
        assert mask == [posix_acl.R | posix_acl.W]

    def test_another_users_transcript_and_the_shared_memory_stay(self, tree, legacy):
        workspace = _named_ws(tree, "a.b")
        project = self._project(legacy, workspace)
        (project / "mine.jsonl").write_text(_transcript(str(workspace), "mine"))
        other = str(tree["workspaces"] / "a_b" / "claude")
        (project / "theirs.jsonl").write_text(_transcript(other, "theirs"))
        (project / "memory").mkdir()
        (project / "memory" / "MEMORY.md").write_text("whose?")
        uid_layout.ensure_user_config(cp.layout(UID))
        uid_layout.migrate_legacy("a.b", UID, str(workspace))
        dst = tree["base"] / str(UID) / "projects" / project.name
        assert (dst / "mine.jsonl").exists()
        assert not (dst / "theirs.jsonl").exists()
        assert not (dst / "memory").exists()

    def test_runs_once_and_skips_corrupt_and_linked(self, tree, legacy):
        workspace = _named_ws(tree)
        project = self._project(legacy, workspace)
        (project / "bad.jsonl").write_text("not json\n")
        secret = tree["tmp"] / "secret.jsonl"
        secret.write_text(_transcript(str(workspace), "secret"))
        (project / "link.jsonl").symlink_to(secret)
        uid_layout.ensure_user_config(cp.layout(UID))
        uid_layout.migrate_legacy("alice", UID, str(workspace))
        dst = tree["base"] / str(UID) / "projects" / project.name
        assert not (dst / "bad.jsonl").exists()
        assert not (dst / "link.jsonl").exists()
        (project / "late.jsonl").write_text(_transcript(str(workspace), "late"))
        uid_layout.migrate_legacy("alice", UID, str(workspace))
        assert not (dst / "late.jsonl").exists()  # recorded as migrated

    def test_off_copies_nothing(self, tree, legacy, monkeypatch):
        monkeypatch.setenv(cp.MIGRATE_FROM_ENV, "off")
        workspace = _named_ws(tree)
        project = self._project(legacy, workspace)
        (project / "s1.jsonl").write_text(_transcript(str(workspace), "hi"))
        uid_layout.ensure_user_config(cp.layout(UID))
        uid_layout.migrate_legacy("alice", UID, str(workspace))
        assert not (tree["base"] / str(UID) / "projects" / project.name).exists()


class TestEphemeralClean:
    def test_a_clean_uid_needs_no_broker(self, tree, monkeypatch):
        from src.backends.claude import broker_ops

        uid_layout.ensure_user_config(cp.layout(UID))
        monkeypatch.setattr(
            broker_ops, "cleanup", lambda *a, **k: pytest.fail("broker called")
        )
        uid_layout.ensure_clean_ephemeral(UID, "/ws/a")

    def test_leftovers_are_wiped_as_the_uid_first(self, tree, monkeypatch):
        from src.backends.claude import broker_ops

        uid_layout.ensure_user_config(cp.layout(UID))
        stale = tree["base"] / str(UID) / "projects" / "-old"
        stale.mkdir()
        seen = {}

        def cleanup(uid, **kwargs):
            seen.update(kwargs, uid=uid)
            stale.rmdir()
            return True

        monkeypatch.setattr(broker_ops, "cleanup", cleanup)
        uid_layout.ensure_clean_ephemeral(UID, "/ws/a")
        assert seen["uid"] == UID and seen["shm"] is True
        assert str(tree["base"] / str(UID) / "projects") in seen["clear"]
        uid_layout.ensure_clean_ephemeral(UID, "/ws/a")  # checked once per key

    def test_a_failed_wipe_refuses_retryably(self, tree, monkeypatch):
        from src.backends.claude import broker_ops

        uid_layout.ensure_user_config(cp.layout(UID))
        (tree["base"] / str(UID) / "state" / "junk").write_text("x")
        monkeypatch.setattr(broker_ops, "cleanup", lambda *a, **k: False)
        with pytest.raises(ui.UidAllocationError):
            uid_layout.ensure_clean_ephemeral(UID, "/ws/b")

    def test_shm_leftovers_count(self, tree, monkeypatch):
        uid_layout.ensure_user_config(cp.layout(UID))
        (tree["tmp"] / "shm" / "sem.x").write_text("")
        # Same-uid test: the test's own uid owns the object.
        assert uid_layout._has_leftovers(os.getuid())
        assert not uid_layout._has_leftovers(UID)


class TestSessionLookup:
    def test_off_mode_is_the_shared_layout(self, tree, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "landlock")
        landlock.reset_mode_cache()
        assert not uid_layout.enabled()
        assert uid_layout.projects_root("alice", "/ws") == cp.projects_root()

    def test_named_user_without_uid_reads_the_legacy_dir(self, tree, monkeypatch):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tree["tmp"] / "uids.json"))
        ui.reset_registry_cache()
        legacy = tree["tmp"] / "legacy"
        monkeypatch.setenv(cp.MIGRATE_FROM_ENV, str(legacy))
        assert uid_layout.projects_root("bob", "/ws") == legacy / "projects"
        uid = ui.resolve_uid("bob")
        assert uid_layout.projects_root("bob", "/ws") == (
            tree["base"] / str(uid) / "projects"
        )
        ui.reset_registry_cache()

    def test_an_unmigrated_legacy_workspace_reads_the_legacy_dir(
        self, tree, monkeypatch
    ):
        # A named user with a uid resumes a session of the legacy <base>/<user>
        # layout: until a spawn there copies its project dir, it is only in the
        # legacy config dir (#3 of the cleanup review).
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tree["tmp"] / "uids.json"))
        ui.reset_registry_cache()
        legacy = tree["tmp"] / "legacy"
        monkeypatch.setenv(cp.MIGRATE_FROM_ENV, str(legacy))
        uid = ui.resolve_uid("carol")
        uid_layout.ensure_user_config(cp.layout(uid))
        old_ws = tree["workspaces"] / "carol"
        old_ws.mkdir()
        name = ws._project_dir_name(str(old_ws))
        (legacy / "projects" / name).mkdir(parents=True)
        (legacy / "projects" / name / "s.jsonl").write_text(
            _transcript(str(old_ws), "hi")
        )
        assert uid_layout.projects_root("carol", old_ws) == legacy / "projects"
        uid_layout.migrate_legacy("carol", uid, str(old_ws))
        p_projects = tree["base"] / str(uid) / "projects"
        assert uid_layout.projects_root("carol", old_ws) == p_projects
        # A workspace with nothing in the legacy dir uses P straight away.
        new_ws = tree["workspaces"] / "carol" / "claude"
        new_ws.mkdir()
        assert uid_layout.projects_root("carol", new_ws) == p_projects
        ui.reset_registry_cache()

    def test_anonymous_without_lease_has_no_root(self, tree):
        assert uid_layout.projects_root(None, "/ws/_tmp_x") is None


class TestPerUidLists:
    def test_the_lists_of_a_per_uid_spawn(self, tree):
        layout = cp.layout(UID)
        uid_layout.ensure_user_config(layout)
        workspace = tree["workspaces"] / "_tmp_run"
        workspace.mkdir()
        ro, rw = landlock._ro_rw_roots(
            workspace, "sess-1", precreate=False, layout=layout
        )
        shared = os.path.realpath(tree["shared"])
        p = os.path.realpath(tree["base"] / str(UID))
        assert rw[0] == os.path.realpath(workspace)
        name = ws._project_dir_name(os.path.realpath(workspace))
        for want in (
            f"{p}/projects/{name}",
            f"{p}/session-env/sess-1",
            f"{p}/tasks/sess-1",
            f"{p}/plugins/data",
            f"{p}/state",
        ):
            assert want in rw, want
        for want in (f"{shared}/skills", f"{shared}/plugins", f"{p}/plugins"):
            assert want in ro, want
        assert f"{shared}/settings.json" in ro
        # Never the base, another uid's dir, S itself or S's own state.
        everything = ro + rw
        assert os.path.realpath(tree["base"]) not in everything
        assert shared not in everything
        assert not any(e.startswith(f"{shared}/projects") for e in everything)

    def test_a_planted_project_link_is_not_granted(self, tree):
        layout = cp.layout(UID)
        uid_layout.ensure_user_config(layout)
        workspace = tree["workspaces"] / "_tmp_run"
        workspace.mkdir()
        name = ws._project_dir_name(os.path.realpath(workspace))
        other = tree["base"] / str(UID + 1)
        other.mkdir()
        (tree["base"] / str(UID) / "projects" / name).symlink_to(other)
        _ro, rw = landlock._ro_rw_roots(workspace, None, precreate=False, layout=layout)
        assert not any(str(other) in entry for entry in rw)

    def test_another_uids_dir_is_never_readable(self, tree):
        layout = cp.layout(UID)
        grants = landlock._GrantFilter(
            Path(os.path.realpath(tree["base"] / str(UID))), None, layout=layout
        )
        other = tree["base"] / str(UID + 1)
        other.mkdir(parents=True)
        assert grants.ro(other) is None
        assert grants.ro(tree["shared"] / "projects") is None
        assert grants.ro(tree["shared"] / "skills") is not None


class TestGuard:
    async def _decide(self, hook, tool, **tool_input):
        out = await hook({"tool_name": tool, "tool_input": tool_input}, None, None)
        return "deny" if out else "allow"

    async def test_per_uid_guard(self, tree):
        layout = cp.layout(UID)
        uid_layout.ensure_user_config(layout)
        workspace = tree["workspaces"] / "_tmp_g"
        workspace.mkdir()
        hook = ws.make_claude_home_guard_hook(workspace, layout=layout)
        p = tree["base"] / str(UID)
        own = p / "projects" / ws._project_dir_name(str(workspace.resolve()))
        shared = tree["shared"]
        assert await self._decide(hook, "Write", file_path=str(own / "x")) == "allow"
        assert await self._decide(hook, "Read", file_path=str(p / "skills")) == "allow"
        assert (
            await self._decide(hook, "Read", file_path=str(shared / "skills"))
            == "allow"
        )
        assert (
            await self._decide(hook, "Write", file_path=str(shared / "skills" / "x"))
            == "deny"
        )
        for denied in (shared / "projects" / "x", p / "session-env" / "s", p / "state"):
            assert await self._decide(hook, "Read", file_path=str(denied)) == "deny"


class TestSharedReadable:
    def test_widens_only_gateway_owned_entry_points(self, tree):
        settings = tree["shared"] / "settings.json"
        settings.chmod(0o600)
        (tree["shared"] / "skills").chmod(0o700)
        target = tree["tmp"] / "private.txt"
        target.write_text("x")
        target.chmod(0o600)
        (tree["shared"] / "CLAUDE.md").unlink()
        (tree["shared"] / "CLAUDE.md").symlink_to(target)
        changed = uid_layout.ensure_shared_readable()
        assert stat.S_IMODE(settings.stat().st_mode) == 0o644
        assert stat.S_IMODE((tree["shared"] / "skills").stat().st_mode) == 0o755
        assert stat.S_IMODE(target.stat().st_mode) == 0o600  # links never followed
        assert str(settings) in changed


class TestConfigBaseErrors:
    def test_unset_is_an_error(self, tree, monkeypatch):
        _unset(monkeypatch, cp.CONFIG_BASE_ENV)
        errors = landlock._config_base_errors()
        assert errors and cp.CONFIG_BASE_ENV in errors[0]

    def test_relative_is_an_error(self, tree, monkeypatch):
        monkeypatch.setenv(cp.CONFIG_BASE_ENV, "users")
        assert "absolute" in landlock._config_base_errors()[0]

    @pytest.mark.parametrize("where", ["shared", "workspaces"])
    def test_overlaps_are_errors(self, tree, monkeypatch, where):
        monkeypatch.setenv(cp.CONFIG_BASE_ENV, str(tree[where] / "users"))
        assert any("overlaps" in e for e in landlock._config_base_errors())

    def test_legacy_localpart_keys_are_refused(self, tree, monkeypatch):
        monkeypatch.setenv("WORKSPACE_LEGACY_LOCALPART_KEY", "true")
        assert any("LOCALPART" in e for e in landlock._config_base_errors())

    def test_a_good_base_passes(self, tree):
        assert landlock._config_base_errors() == []
