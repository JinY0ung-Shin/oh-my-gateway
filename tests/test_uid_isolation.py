"""Gateway-side uid mapping + spawn ticket (issue #241, PR-C).

Pure logic, no privilege: uid allocation/persistence and the HMAC ticket. The
privileged broker that consumes a ticket and switches uid is container-only
(docker/isolation_broker.py); its protocol is exercised same-uid in
test_isolation_broker.py.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.backends.claude import uid_isolation as ui


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        ui.UID_MIN_ENV,
        ui.UID_MAX_ENV,
        ui.UID_REGISTRY_ENV,
        ui.EPHEMERAL_UIDS_ENV,
        ui.BROKER_UID_ENV,
        ui.BROKER_GID_ENV,
        ui.TICKET_SECRET_ENV,
        "APP_UID",
        "APP_GID",
    ):
        monkeypatch.delenv(var, raising=False)
    # Reset the module-level registry + ephemeral-pool caches between tests.
    monkeypatch.setattr(ui, "_registry", None)
    monkeypatch.setattr(ui, "_ephemeral_pool", None)
    yield
    ui._release_registry_lock()  # the lock a get_registry() call took


class TestUidRange:
    def test_defaults(self):
        assert ui.uid_range() == (ui._DEFAULT_UID_MIN, ui._DEFAULT_UID_MAX)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "30000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "30010")
        assert ui.uid_range() == (30000, 30010)

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "notanint")
        assert ui.uid_range()[0] == ui._DEFAULT_UID_MIN


class TestUidRegistry:
    def test_stable_and_distinct(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)
        a1 = reg.resolve("alice")
        b1 = reg.resolve("bob")
        a2 = reg.resolve("alice")
        assert a1 == a2  # stable per key
        assert a1 != b1  # distinct keys, distinct uids
        assert 20000 <= a1 <= 20005

    def test_lowest_free_allocation(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)
        assert reg.resolve("a") == 20000
        assert reg.resolve("b") == 20001

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "uids.json"
        reg = ui.UidRegistry(path, 20000, 20005)
        uid = reg.resolve("carol")
        reg2 = ui.UidRegistry(path, 20000, 20005)
        assert reg2.resolve("carol") == uid  # survived a "restart"
        # and a new key does not collide with the persisted one
        assert reg2.resolve("dave") != uid

    def test_exhaustion_refuses(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20001)
        reg.resolve("a")
        reg.resolve("b")
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("c")

    def test_resolve_uid_uses_env_range(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setenv(ui.UID_MIN_ENV, "25000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "25100")
        uid = ui.resolve_uid("someone")
        assert 25000 <= uid <= 25100


class TestReviewFixes:
    """Fixes from the PR #244 review: an ephemeral path that never touches the
    registry (#9; its leases are TestEphemeralLeases), reserved-uid exclusion +
    band validation (#15), registry path, persist fail-closed."""

    def test_default_registry_path_is_data_volume(self):
        # /app/data (durable volume), never /app/src/data (code tree).
        p = ui._registry_path()
        assert p.name == "isolation_uids.json"
        assert p.parent.name == "data"
        assert p.parent.parent.name != "src"

    def _three_slot_pool(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20010")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "3")
        return ui.get_ephemeral_pool()

    @staticmethod
    def _workspace(tmp_path, i):
        ws = tmp_path / f"_tmp_{i}"
        ws.mkdir()
        return str(ws)

    def test_ephemeral_pool_never_shares_a_live_uid(self, monkeypatch, tmp_path):
        # #244 review 4: with the band full the pool used to recycle the LRU
        # uid while its run was still alive, so pool size + 1 concurrent
        # anonymous runs put two of them on one uid.
        pool = self._three_slot_pool(monkeypatch, tmp_path)
        live = [self._workspace(tmp_path, i) for i in range(4)]
        first = [pool.resolve(ws) for ws in live[:3]]
        assert len(set(first)) == 3
        with pytest.raises(ui.UidAllocationError, match="leased"):
            pool.resolve(live[3])
        # A repeated key keeps its lease.
        assert pool.resolve(live[1]) == first[1]

    def test_a_removed_workspace_releases_its_uid(self, monkeypatch, tmp_path):
        pool = self._three_slot_pool(monkeypatch, tmp_path)
        live = [self._workspace(tmp_path, i) for i in range(4)]
        first = [pool.resolve(ws) for ws in live[:3]]
        shutil.rmtree(live[1])  # cleanup_temp_workspace: that run ended
        assert pool.resolve(live[3]) == first[1]
        # The others are still leased: nothing else frees up.
        with pytest.raises(ui.UidAllocationError):
            pool.resolve(self._workspace(tmp_path, 4))

    def test_anonymous_runs_do_not_touch_the_registry(self, monkeypatch, tmp_path):
        reg_path = tmp_path / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(reg_path))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20100")
        # 50 runs, one after another, on a 25-uid ephemeral slice: each lease
        # ends with its run, so sequential runs never exhaust it.
        for i in range(50):
            ui.resolve_uid(f"anon{i}", persist=False)
            ui.release_ephemeral(f"anon{i}")
        # Nothing persisted: the ephemeral path never writes the registry file.
        assert not reg_path.exists()

    def test_reserved_uids_excluded_from_allocation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_UID", "20000")
        monkeypatch.setenv(ui.BROKER_UID_ENV, "20001")
        reg = ui.UidRegistry(tmp_path / "u.json", 20000, 20010, ui.reserved_uids())
        got = {reg.resolve(f"u{i}") for i in range(5)}
        assert 20000 not in got and 20001 not in got and 0 not in got

    def test_validate_bands_flags_root_and_empty(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "0")
        monkeypatch.setenv(ui.UID_MAX_ENV, "10")
        errs = ui.validate_bands()
        assert any("uid 0" in e or ">= 1" in e for e in errs)

        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "19999")
        assert any("empty" in e for e in ui.validate_bands())

    def test_validate_bands_ok_for_default(self):
        assert ui.validate_bands() == []

    def test_persist_failure_fails_closed(self, tmp_path, monkeypatch):
        # A registry that cannot be written must refuse to allocate rather than
        # hand out a uid that will be lost on restart.
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)

        def _boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as m:
            m.setattr(ui.os, "replace", _boom)
            with pytest.raises(ui.UidAllocationError):
                reg.resolve("alice")
        # the in-memory grant was rolled back, so a retry may try again
        assert reg.resolve("alice") == 20000

    def test_unusable_registry_path_fails_closed(self, tmp_path):
        # A registry path that cannot be read (here: its parent is a file) is
        # refused at load, not treated as an empty registry.
        target = tmp_path / "blocker"
        target.write_text("x")  # a file where a directory is needed
        with pytest.raises(ui.UidAllocationError):
            ui.UidRegistry(target / "uids.json", 20000, 20005)


class TestRegistryFailClosed:
    """PR #244 review 2 (#8): a damaged registry must never load as empty."""

    def test_missing_file_is_an_empty_registry(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)
        assert reg.resolve("alice") == 20000

    @pytest.mark.parametrize("content", ['{"alice": 20000', "[1, 2]", "\udcff"])
    def test_corrupt_or_non_object_registry_refuses(self, tmp_path, content):
        path = tmp_path / "uids.json"
        path.write_bytes(content.encode("utf-8", "surrogateescape"))
        with pytest.raises(ui.UidAllocationError):
            ui.UidRegistry(path, 20000, 20005)
        # the file is left untouched for the operator to restore
        assert path.read_bytes() == content.encode("utf-8", "surrogateescape")

    def test_deeply_nested_registry_refuses_cleanly(self, monkeypatch, tmp_path):
        # json.loads raises RecursionError, not ValueError, on deep nesting;
        # it must surface as the registry error, not a startup traceback.
        path = tmp_path / "uids.json"
        path.write_text("[" * 100_000 + "]" * 100_000)
        with pytest.raises(ui.UidAllocationError):
            ui.UidRegistry(path, 20000, 20005)
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        errs = ui.registry_errors()
        assert len(errs) == 1 and "corrupt" in errs[0]

    def test_unreadable_registry_refuses(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root reads a 000 file")
        path = tmp_path / "uids.json"
        path.write_text('{"alice": 20000}')
        path.chmod(0)
        try:
            with pytest.raises(ui.UidAllocationError):
                ui.UidRegistry(path, 20000, 20005)
        finally:
            path.chmod(0o600)

    def test_registry_errors_reports_a_corrupt_file(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text("{truncated")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        errs = ui.registry_errors()
        assert len(errs) == 1 and "corrupt" in errs[0]
        # and a named user's spawn refuses instead of getting a fresh uid
        with pytest.raises(ui.UidAllocationError):
            ui.resolve_uid("alice")

    def test_registry_errors_ok_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        assert ui.registry_errors() == []

    def test_out_of_band_entry_is_held_not_reassigned(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 31000, "bob": 20001}))
        reg = ui.UidRegistry(path, 20000, 20005)
        assert reg.resolve("bob") == 20001  # honored
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("alice")  # held: no silently different uid
        # a new user allocates, and the held entry survives the rewrite
        assert reg.resolve("carol") == 20000
        on_disk = json.loads(path.read_text())
        assert on_disk == {"alice": 31000, "bob": 20001, "carol": 20000}

    def test_shared_uid_is_held_for_both_keys_and_never_reused(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 20000, "mallory": 20000}))
        reg = ui.UidRegistry(path, 20000, 20005)
        for key in ("alice", "mallory"):
            with pytest.raises(ui.UidAllocationError):
                reg.resolve(key)
        assert reg.resolve("carol") == 20001  # 20000 is never handed out again

    def test_reserved_and_malformed_entries_are_held(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"r": 20002, "b": True, "s": "20003"}))
        reg = ui.UidRegistry(path, 20000, 20005, reserved={20002})
        for key in ("r", "b", "s"):
            with pytest.raises(ui.UidAllocationError):
                reg.resolve(key)

    def test_persist_writes_0600(self, tmp_path):
        path = tmp_path / "uids.json"
        reg = ui.UidRegistry(path, 20000, 20005)
        reg.resolve("alice")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {"alice": 20000}
        assert not (tmp_path / "uids.json.tmp").exists()

    def test_dir_fsync_failure_after_replace_keeps_the_allocation(
        self, tmp_path, monkeypatch
    ):
        # Once the new map replaced the file the grant is on disk; rolling it
        # back in memory would free a uid the file still assigns (review 2).
        path = tmp_path / "uids.json"
        reg = ui.UidRegistry(path, 20000, 20005)
        real_fsync = ui.os.fsync

        def _fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        with monkeypatch.context() as m:
            m.setattr(ui.os, "fsync", _fsync)
            assert reg.resolve("alice") == 20000
        assert reg.resolve("bob") == 20001  # 20000 was not handed out again
        assert json.loads(path.read_text()) == {"alice": 20000, "bob": 20001}

    def test_held_entries_fail_startup(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 31000}))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        errs = ui.registry_errors()
        assert len(errs) == 1 and "cannot honor" in errs[0]

    def test_ephemeral_pool_never_hands_out_a_held_uid(self, monkeypatch, tmp_path):
        # A benign band change (a larger ephemeral slice) moves a named user's
        # uid into the ephemeral band: that user is held, and the uid must not
        # go to an anonymous run (cross-user file access in uid mode).
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"bob": 20009}))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20009")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "3")  # ephemeral [20007, 20009]
        assert ui.registry_errors()  # bob is held: startup refuses
        handed = {ui.resolve_uid(f"/ws/anon-{i}", persist=False) for i in range(20)}
        assert 20009 not in handed
        assert handed <= {20007, 20008}

    def test_corrupt_registry_fails_the_anonymous_path_closed(
        self, monkeypatch, tmp_path
    ):
        path = tmp_path / "uids.json"
        path.write_text("{nope")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        with pytest.raises(ui.UidAllocationError):
            ui.resolve_uid("/ws/anon", persist=False)

    def test_persist_refuses_a_planted_tmp_symlink(self, tmp_path):
        path = tmp_path / "uids.json"
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (tmp_path / "uids.json.tmp").symlink_to(victim)
        reg = ui.UidRegistry(path, 20000, 20005)
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("alice")
        assert victim.read_text() == "keep"


class TestEphemeralLeases:
    """PR #244 review 4 (#7): an anonymous run keeps its uid while it lives.

    The pool recycled the uid of the least-recently-*resolved* key, with no
    notion of whether that run was alive and no release. A live anonymous
    session resolves only when its client is created, so 256 later stateless
    runs took its uid while its CLI still ran: two principals on one uid.
    """

    @staticmethod
    def _pool(size: int) -> ui._EphemeralPool:
        return ui._EphemeralPool(20000, 20000 + size - 1)

    @staticmethod
    def _ws(tmp_path, name: str) -> str:
        path = tmp_path / name
        path.mkdir()
        return str(path)

    @pytest.fixture
    def uid_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")

    @staticmethod
    def _spawn_uid(workspace) -> int:
        """The uid an anonymous spawn in *workspace* runs as, keyed exactly as
        ``landlock.options_env`` keys it."""
        from src.backends.claude import landlock

        cwd = landlock._absolute(workspace)
        return landlock._uid_spawn_env(cwd, [], [], user=None)[1]

    def test_a_lease_is_stable_and_distinct_per_key(self, tmp_path):
        pool = self._pool(3)
        a, b = self._ws(tmp_path, "a"), self._ws(tmp_path, "b")
        ua, ub = pool.resolve(a), pool.resolve(b)
        assert ua != ub
        assert pool.resolve(a) == ua  # every spawn of one run: the same uid

    def test_a_full_pool_refuses_instead_of_sharing_a_live_uid(self, tmp_path):
        pool = self._pool(3)
        live = [self._ws(tmp_path, f"ws{i}") for i in range(3)]
        uids = [pool.resolve(key) for key in live]
        with pytest.raises(ui.UidAllocationError) as excinfo:
            pool.resolve(self._ws(tmp_path, "ws3"))
        # The plain, retryable kind: on /v1/responses a 503 "retry shortly".
        assert type(excinfo.value) is ui.UidAllocationError
        assert "leased to live anonymous runs" in str(excinfo.value)
        assert [pool.resolve(key) for key in live] == uids  # nobody lost theirs

    def test_release_frees_the_uid(self, tmp_path):
        pool = self._pool(1)
        a, b = self._ws(tmp_path, "a"), self._ws(tmp_path, "b")
        ua = pool.resolve(a)
        assert pool.release(a) == ua
        assert pool.release(a) is None  # a second release is a no-op
        assert pool.resolve(b) == ua

    def test_never_used_uids_first_then_least_recently_released(self, tmp_path):
        pool = self._pool(3)
        keys = [self._ws(tmp_path, f"ws{i}") for i in range(5)]
        u0, u1 = pool.resolve(keys[0]), pool.resolve(keys[1])
        pool.release(keys[1])
        pool.release(keys[0])
        assert pool.resolve(keys[2]) not in (u0, u1)  # the never-used one
        assert pool.resolve(keys[3]) == u1  # released first
        assert pool.resolve(keys[4]) == u0

    def test_a_full_pool_reclaims_a_lease_whose_workspace_is_gone(
        self, tmp_path, caplog
    ):
        pool = self._pool(2)
        gone, live = self._ws(tmp_path, "gone"), self._ws(tmp_path, "live")
        u_gone, u_live = pool.resolve(gone), pool.resolve(live)
        os.rmdir(gone)  # removed, but its release never came
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            assert pool.resolve(self._ws(tmp_path, "new")) == u_gone
        assert "never released" in caplog.text
        assert pool.resolve(live) == u_live  # an existing workspace keeps it
        with pytest.raises(ui.UidAllocationError):
            pool.resolve(self._ws(tmp_path, "newer"))

    def test_a_key_that_names_no_path_is_never_reclaimed(self, tmp_path):
        pool = self._pool(1)
        uid = pool.resolve("not-a-path")
        with pytest.raises(ui.UidAllocationError):
            pool.resolve(self._ws(tmp_path, "other"))
        pool.release("not-a-path")
        assert pool.resolve(str(tmp_path / "other")) == uid

    def test_a_workspace_that_cannot_be_checked_keeps_its_lease(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root searches a 000 directory")
        locked = tmp_path / "locked"
        (locked / "ws").mkdir(parents=True)
        pool = self._pool(1)
        uid = pool.resolve(str(locked / "ws"))
        locked.chmod(0)  # lstat of the workspace: EACCES, not ENOENT
        try:
            with pytest.raises(ui.UidAllocationError):
                pool.resolve(self._ws(tmp_path, "other"))
        finally:
            locked.chmod(0o700)
        assert pool.resolve(str(locked / "ws")) == uid

    def test_a_live_session_keeps_its_uid_through_many_stateless_runs(
        self, uid_env, tmp_path
    ):
        # The review's scenario on the default band (256 ephemeral uids): an
        # anonymous session's CLI lives for its whole TTL without resolving
        # again while far more stateless runs come and go.
        from src.workspace_manager import WorkspaceManager

        wm = WorkspaceManager(base_path=tmp_path / "workspaces")
        session_ws = wm.resolve(None)
        session_uid = self._spawn_uid(session_ws)
        for _ in range(ui._ephemeral_count() + 44):
            run_ws = wm.resolve(None)
            assert self._spawn_uid(run_ws) != session_uid
            wm.cleanup_temp_workspace(run_ws)  # the run is over
        assert self._spawn_uid(session_ws) == session_uid

    def test_workspace_cleanup_ends_the_lease_a_spawn_took(
        self, uid_env, monkeypatch, tmp_path
    ):
        from src.workspace_manager import WorkspaceManager

        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20009")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "1")
        # A relative base, as USER_WORKSPACES_DIR may be: the spawn keys on the
        # absolute path, and the release must find that same key.
        monkeypatch.chdir(tmp_path)
        wm = WorkspaceManager(base_path=Path("workspaces"))
        first = wm.resolve(None)
        uid = self._spawn_uid(first)
        with pytest.raises(ui.UidAllocationError):  # its only uid is leased
            self._spawn_uid(wm.resolve(None))
        wm.cleanup_temp_workspace(first)
        assert not first.exists()
        assert self._spawn_uid(wm.resolve(None)) == uid

    def test_cleanup_of_a_vanished_workspace_still_ends_its_lease(
        self, uid_env, tmp_path
    ):
        from src.workspace_manager import WorkspaceManager

        wm = WorkspaceManager(base_path=tmp_path / "workspaces")
        ws = wm.resolve(None)
        uid = self._spawn_uid(ws)
        ws.rmdir()
        wm.cleanup_temp_workspace(ws)
        assert ui.get_ephemeral_pool().release(os.path.abspath(ws)) is None
        assert ui.get_ephemeral_pool().is_free(uid)

    def test_a_workspace_cleanup_cannot_remove_keeps_its_lease(
        self, uid_env, monkeypatch, tmp_path
    ):
        # Files the session uid wrote that the gateway's uid cannot delete:
        # handing that uid to another run would give it the leftovers' owner.
        import src.workspace_manager as wm_mod
        from src.workspace_manager import WorkspaceManager

        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20009")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "1")
        wm = WorkspaceManager(base_path=tmp_path / "workspaces")
        ws = wm.resolve(None)
        uid = self._spawn_uid(ws)
        with monkeypatch.context() as m:
            m.setattr(wm_mod.shutil, "rmtree", lambda *a, **k: None)
            wm.cleanup_temp_workspace(ws)
        assert ws.is_dir()
        with pytest.raises(ui.UidAllocationError):  # still leased: fail closed
            self._spawn_uid(wm.resolve(None))
        shutil.rmtree(ws)  # removed later (broker cleanup): reclaimed when full
        assert self._spawn_uid(wm.resolve(None)) == uid

    def test_named_workspaces_are_not_released(self, uid_env, tmp_path):
        from src.workspace_manager import WorkspaceManager

        wm = WorkspaceManager(base_path=tmp_path / "workspaces")
        named = wm.resolve("alice")
        # Never a pool key in practice; pinned anyway: only _tmp_ runs end here.
        uid = ui.resolve_uid(os.path.abspath(named), persist=False)
        wm.cleanup_temp_workspace(named)
        assert named.is_dir()
        assert ui.resolve_uid(os.path.abspath(named), persist=False) == uid

    def test_release_without_a_pool_is_a_noop(self):
        ui.release_ephemeral("/nowhere")
        assert ui._ephemeral_pool is None  # the other modes never build it

    def test_a_failing_release_never_breaks_workspace_cleanup(
        self, monkeypatch, tmp_path, caplog
    ):
        from src.workspace_manager import WorkspaceManager

        def boom(_key):
            raise RuntimeError("boom")

        monkeypatch.setattr(ui, "release_ephemeral", boom)
        wm = WorkspaceManager(base_path=tmp_path / "workspaces")
        ws = wm.resolve(None)
        with caplog.at_level(logging.WARNING):
            wm.cleanup_temp_workspace(ws)
        assert not ws.exists()
        assert "uid lease" in caplog.text


class TestBandsReview2:
    def test_default_broker_uid_is_outside_the_default_band(self):
        lo, hi = ui.uid_range()
        assert not (lo <= ui._DEFAULT_BROKER_UID <= hi)
        assert ui._DEFAULT_BROKER_UID in ui.reserved_uids()

    def test_zero_ephemeral_uids_refuses(self, monkeypatch):
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "0")
        assert any("ephemeral" in e for e in ui.validate_bands())

    def test_fully_reserved_ephemeral_band_refuses(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20003")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "1")
        monkeypatch.setenv(ui.BROKER_UID_ENV, "20003")  # the only ephemeral uid
        assert any("ephemeral" in e for e in ui.validate_bands())


class TestBandsWithoutMaterializing:
    """PR #244 follow-up: no band is ever listed (a wide one ran the gateway out
    of memory), and no session can be mapped to (uid_t)-1."""

    # Loads uid_isolation by path (stdlib only) under a 1 GiB address-space
    # cap: listing the band would need tens of GiB and fail with MemoryError
    # there instead of taking the test run down.
    _CHILD = (
        "import importlib.util, os, resource, sys\n"
        "from pathlib import Path\n"
        "resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))\n"
        "spec = importlib.util.spec_from_file_location('ui_child', sys.argv[1])\n"
        "ui = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(ui)\n"
        "os.environ[ui.UID_MIN_ENV] = '1'\n"
        "os.environ[ui.UID_MAX_ENV] = str(ui._MAX_ID)\n"
        "print(ui.validate_bands())\n"  # a huge stable band
        "reg = ui.UidRegistry(\n"
        "    Path(sys.argv[2]), *ui._stable_band(), ui.reserved_uids()\n"
        ")\n"
        "print(reg.resolve('x'))\n"
        "os.environ[ui.EPHEMERAL_UIDS_ENV] = str(ui._MAX_ID - 10)\n"
        "print(ui.validate_bands(), ui._ephemeral_count())\n"  # capped
        "pool = ui._EphemeralPool(11, ui._MAX_ID, ui.reserved_uids())\n"  # huge
        "print(pool.resolve('a'), pool.resolve('b'), pool._capacity)\n"
    )

    def test_a_huge_band_validates_and_leases_in_bounded_memory(self, tmp_path):
        out = subprocess.run(
            [sys.executable, "-c", self._CHILD, ui.__file__, str(tmp_path / "u")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        # Ephemeral band [11, _MAX_ID]: the app and broker uids inside it are
        # skipped, and the capacity is counted, not listed.
        assert out.stdout.split("\n")[:4] == [
            "[]",
            "1",
            f"[] {ui._MAX_EPHEMERAL}",
            f"11 12 {ui._MAX_ID - 12}",
        ]

    def test_the_ephemeral_slice_is_capped(self, monkeypatch, caplog):
        # Released uids queue until the never-used ones run out, so the pool's
        # memory follows its size: CLAUDE_ISOLATION_EPHEMERAL_UIDS is capped.
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, str(20000 + 10 * ui._MAX_EPHEMERAL))
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, str(5 * ui._MAX_EPHEMERAL))
        assert ui._ephemeral_count() == ui._MAX_EPHEMERAL
        lo, hi = ui._ephemeral_band()
        assert hi - lo + 1 == ui._MAX_EPHEMERAL
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            assert ui.validate_bands() == []
        assert f"is above the maximum {ui._MAX_EPHEMERAL}" in caplog.text

    def test_a_band_reaching_uid_t_minus_one_refuses(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, str(ui._MAX_ID + 1))
        errs = ui.validate_bands()
        assert len(errs) == 1 and f"above {ui._MAX_ID}" in errs[0]
        monkeypatch.setenv(ui.UID_MAX_ENV, str(ui._MAX_ID))
        assert ui.validate_bands() == []

    def test_the_pool_skips_excluded_uids_in_order(self, tmp_path):
        pool = ui._EphemeralPool(20000, 20004, {20001, 20003, 5})
        a, b, c, d = (TestEphemeralLeases._ws(tmp_path, n) for n in "abcd")
        assert [pool.resolve(k) for k in (a, b, c)] == [20000, 20002, 20004]
        with pytest.raises(ui.UidAllocationError, match="all 3 ephemeral uids"):
            pool.resolve(d)
        assert not pool.is_free(20001)  # excluded, never a pool uid
        assert not pool.is_free(20002)  # leased
        pool.release(b)
        assert pool.is_free(20002)
        assert pool.resolve(d) == 20002

    def test_an_all_excluded_pool_refuses(self):
        pool = ui._EphemeralPool(20000, 20001, {20000, 20001})
        with pytest.raises(ui.UidAllocationError, match="no ephemeral uids"):
            pool.resolve("a")


class TestReservedGids:
    """PR #244 follow-up: the broker refuses a uid or gid equal to APP_GID or its
    own gid, and a session's gid is its uid, so the allocator must never hand
    one of those ids out (it used to, and persisted it)."""

    def test_app_and_broker_ids_are_reserved(self, monkeypatch):
        monkeypatch.setenv("APP_UID", "1000")
        monkeypatch.setenv("APP_GID", "20000")
        monkeypatch.setenv(ui.BROKER_GID_ENV, "20002")
        assert ui.reserved_uids() == {0, 1000, 20000, 20002, ui._DEFAULT_BROKER_UID}

    def test_the_broker_gid_defaults_to_the_app_gid(self, monkeypatch):
        monkeypatch.setenv("APP_GID", "20003")
        assert ui.reserved_uids() == {0, 1000, 20003, ui._DEFAULT_BROKER_UID}

    def test_gids_inside_the_band_are_never_allocated(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20010")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "3")  # ephemeral [20008, 20010]
        monkeypatch.setenv("APP_GID", "20000")
        monkeypatch.setenv(ui.BROKER_GID_ENV, "20008")
        assert ui.resolve_uid("alice") == 20001
        anon = {ui.resolve_uid(key, persist=False) for key in ("a", "b")}
        assert anon == {20009, 20010}
        with pytest.raises(ui.UidAllocationError):
            ui.resolve_uid("c", persist=False)

    def test_a_persisted_uid_on_the_app_gid_is_held_and_named(
        self, monkeypatch, tmp_path
    ):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 20000, "bob": 20001}))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20010")
        monkeypatch.setenv("APP_GID", "20000")
        errs = ui.registry_errors()
        assert len(errs) == 1 and "cannot honor (1 on a reserved id" in errs[0]
        assert "APP_GID" in errs[0] and "cannot work" in errs[0]
        assert "(uid 20000)" in errs[0]
        assert "alice" not in errs[0]  # the uid, never the user key
        with pytest.raises(ui.UidAllocationError, match="on a reserved id"):
            ui.resolve_uid("alice")
        assert ui.resolve_uid("bob") == 20001

    def test_held_entries_are_counted_per_reason(self, tmp_path):
        path = tmp_path / "uids.json"
        held = {"r": 20002, "o1": 31000, "o2": 0, "s1": 20003, "s2": 20003, "m": "x"}
        path.write_text(json.dumps(held))
        reg = ui.UidRegistry(path, 20000, 20005, reserved={0, 20002})
        assert reg.describe_held() == (
            "2 on a reserved id (root, the gateway's uid/gid or the broker's "
            "uid/gid: [0, 20002]), which the broker refuses (uids 0, 20002); "
            "1 outside the stable band [20000, 20005] (uid 31000); "
            "2 sharing a uid with another user (uid 20003); 1 not an integer uid"
        )
        assert reg.held_reasons() == ["reserved", "outside", "shared", "malformed"]

    def test_describe_held_names_ten_uids_per_reason_and_no_keys(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({f"user{i}@corp": 40000 + i for i in range(12)}))
        reg = ui.UidRegistry(path, 20000, 20005)
        text = reg.describe_held()
        assert text.startswith(
            "12 outside the stable band [20000, 20005] (uids 40000, 40001, "
        )
        assert text.endswith("40009 and 2 more)")
        assert "@corp" not in text

    @pytest.mark.parametrize(
        "held, remedy",
        [
            ({"a": 31000}, "move the entry into the stable band"),
            ({"a": 20001, "b": 20001}, "a uid of its own"),
            ({"a": "x"}, "fix or remove the entry"),
        ],
        ids=["outside", "shared", "malformed"],
    )
    def test_the_startup_error_names_the_fix_for_each_reason(
        self, monkeypatch, tmp_path, held, remedy
    ):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps(held))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20010")
        errs = ui.registry_errors()
        assert len(errs) == 1 and remedy in errs[0]
        assert "cannot work" not in errs[0]  # only for a reserved id

    def test_a_gid_inside_the_band_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("APP_GID", "20005")
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            assert ui.validate_bands() == []
        assert "broker gid/gateway app gid 20005 falls inside" in caplog.text


class TestRegistrySingleProcess:
    """PR #244 follow-up: each gateway process allocates from its own copy of
    the registry, so a second process on the same one (uvicorn --workers,
    replicas sharing the data volume) refuses uid mode instead of handing two
    users one uid."""

    # Another gateway process: uid_isolation loaded by path, holding the
    # registry until its stdin closes.
    _OTHER = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('ui_other', sys.argv[1])\n"
        "ui = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(ui)\n"
        "ui.resolve_uid('alice')\n"
        "print('holding', flush=True)\n"
        "sys.stdin.read()\n"
    )
    _PROBE = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    print('held')\n"
        "else:\n"
        "    print('free')\n"
    )

    @pytest.fixture
    def other_gateway(self, monkeypatch, tmp_path):
        path = tmp_path / "data" / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        proc = subprocess.Popen(
            [sys.executable, "-c", self._OTHER, ui.__file__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "holding"
            yield path, proc
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
            proc.stdout.close()

    def _probe(self, lock: Path) -> str:
        out = subprocess.run(
            [sys.executable, "-c", self._PROBE, str(lock)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return out.stdout.strip()

    def test_a_second_process_fails_every_path_closed(self, other_gateway):
        path, _proc = other_gateway
        errs = ui.registry_errors()
        assert len(errs) == 1
        assert "another gateway process holds the uid registry" in errs[0]
        assert f"{path}.lock" in errs[0] and "--workers" in errs[0]
        for spawn in (
            lambda: ui.resolve_uid("bob"),
            lambda: ui.existing_uid("alice"),
            lambda: ui.resolve_uid("/ws/anon", persist=False),
        ):
            with pytest.raises(ui.UidAllocationError, match="another gateway"):
                spawn()
        assert json.loads(path.read_text()) == {"alice": 20000}  # untouched

    def test_the_startup_check_refuses(self, other_gateway, monkeypatch):
        from src import config_check
        from src.backends.claude import landlock

        monkeypatch.setattr(landlock, "isolation_mode", lambda: "uid")
        monkeypatch.setattr(landlock, "effective_mode", lambda: "uid")
        issues = config_check._check_uid_registry()
        assert len(issues) == 1 and issues[0].severity == "error"
        assert "another gateway process" in issues[0].message

    def test_the_lock_is_taken_once_the_other_process_exits(self, other_gateway):
        _path, proc = other_gateway
        assert ui.registry_errors()
        proc.stdin.close()
        proc.wait(timeout=30)
        assert ui.registry_errors() == []
        assert ui.resolve_uid("alice") == 20000  # its grant, read from disk
        assert ui.resolve_uid("bob") == 20001

    def test_this_process_holds_it_until_reset(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        lock = tmp_path / "uids.json.lock"
        assert ui.resolve_uid("alice") == 20000
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert self._probe(lock) == "held"
        assert ui.existing_uid("alice") == 20000  # the same process: no conflict
        ui.reset_registry_cache()
        assert self._probe(lock) == "free"
        assert ui.resolve_uid("alice") == 20000
        assert self._probe(lock) == "held"

    def test_the_lock_is_opened_read_write(self, monkeypatch, tmp_path):
        # NFS emulates flock with a POSIX write lock, which needs a descriptor
        # open for writing: read-only is only the fallback.
        import fcntl

        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        assert ui.registry_errors() == []
        fd = ui._held_registry_lock[1]
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDWR

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_a_lock_file_the_gateway_can_only_read_still_locks(
        self, monkeypatch, tmp_path
    ):
        # flock needs no write access locally: an existing read-only lock file
        # (say, pre-created by an operator) must not refuse uid mode.
        path = tmp_path / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        lock = tmp_path / "uids.json.lock"
        lock.write_text("")
        lock.chmod(0o400)
        assert ui.registry_errors() == []
        assert self._probe(lock) == "held"

    def test_a_planted_fifo_refuses_without_hanging(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        os.mkfifo(tmp_path / "uids.json.lock")

        def _hung(*_args):
            raise TimeoutError("opening the lock blocked on a FIFO")

        previous = signal.signal(signal.SIGALRM, _hung)
        signal.alarm(10)
        try:
            errs = ui.registry_errors()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        assert len(errs) == 1 and "is not a regular file" in errs[0]

    def test_a_lock_refused_by_nfs_names_the_cause(self, monkeypatch, tmp_path):
        import errno
        import fcntl

        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))

        def _ebadf(_fd, _op):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(fcntl, "flock", _ebadf)
        errs = ui.registry_errors()
        assert len(errs) == 1 and "cannot lock the uid registry" in errs[0]
        assert "over NFS" in errs[0]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
    def test_an_unwritable_directory_without_a_lock_names_the_fix(
        self, monkeypatch, tmp_path
    ):
        data = tmp_path / "data"
        data.mkdir()
        path = data / "uids.json"
        path.write_text(json.dumps({"alice": 20000}))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        data.chmod(0o500)
        try:
            errs = ui.registry_errors()
        finally:
            data.chmod(0o700)
        assert len(errs) == 1 and "cannot open the uid registry lock" in errs[0]
        assert "create the lock file once" in errs[0]

    def test_a_planted_lock_symlink_refuses(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (tmp_path / "uids.json.lock").symlink_to(victim)
        errs = ui.registry_errors()
        assert len(errs) == 1 and "cannot open the uid registry lock" in errs[0]
        assert victim.read_text() == "keep"


class TestTicketSecretCapture:
    """PR #244 review 2 (#5): the secret must not stay in os.environ."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        ui.reset_captured_secret()
        yield
        ui.reset_captured_secret()

    def test_capture_moves_the_secret_out_of_the_environment(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        ui.capture_ticket_secret()
        assert ui.TICKET_SECRET_ENV not in os.environ
        assert ui.ticket_secret() == b"s3cret"
        # minting and verifying keep working from the captured copy
        t = ui.mint_ticket(uid=20001, workspace="/ws", ro=[], rw=[], env_allow=[])
        assert ui.verify_ticket(t)["uid"] == 20001

    def test_child_copying_os_environ_does_not_inherit_it(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        ui.capture_ticket_secret()
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os; print(os.environ.get({ui.TICKET_SECRET_ENV!r}, ''))",
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.stdout.strip() == ""

    def test_capture_is_idempotent_and_blank_is_ignored(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        ui.capture_ticket_secret()
        ui.capture_ticket_secret()  # env already gone: keeps the captured one
        assert ui.ticket_secret() == b"s3cret"
        ui.reset_captured_secret()
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "   ")
        ui.capture_ticket_secret()
        assert ui.ticket_secret() is None

    def test_env_value_wins_over_a_captured_one(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "old")
        ui.capture_ticket_secret()
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "new")
        assert ui.ticket_secret() == b"new"


class TestTicket:
    def test_mint_requires_secret(self):
        with pytest.raises(RuntimeError):
            ui.mint_ticket(uid=20000, workspace="/ws", ro=[], rw=[], env_allow=[])

    def test_roundtrip(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(
            uid=20001, workspace="/ws", ro=["/usr"], rw=["/ws"], env_allow=["PATH"]
        )
        payload = ui.verify_ticket(t)
        assert payload["uid"] == 20001
        assert payload["workspace"] == "/ws"
        assert payload["rw"] == ["/ws"]
        assert payload["env_allow"] == ["PATH"]

    def test_bad_signature_rejected(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[])
        sig, _, body = t.partition(".")
        tampered = sig + "." + body.replace('"uid":1', '"uid":0')
        with pytest.raises(ValueError):
            ui.verify_ticket(tampered)

    def test_wrong_secret_rejected(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[])
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "different")
        with pytest.raises(ValueError):
            ui.verify_ticket(t)

    def test_expiry_rejected(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[], ttl=1)
        real = time.time()
        monkeypatch.setattr(ui.time, "time", lambda: real + 100)
        with pytest.raises(ValueError):
            ui.verify_ticket(t)

    def test_single_use(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[])
        seen: set = set()
        ui.verify_ticket(t, seen_nonces=seen)  # first use OK
        with pytest.raises(ValueError):
            ui.verify_ticket(t, seen_nonces=seen)  # replay rejected
