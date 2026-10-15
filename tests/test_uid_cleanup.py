"""uid_cleanup: removals and transcript reads that need the session uid.

With per-uid config dirs (uid mode + ``CLAUDE_ISOLATION_CONFIG_BASE``) the
broker removes and reads, as the session uid, what that uid locked away from
the gateway (``docs/isolation-uid-ownership-design.md`` §3.3, §3.6, §4.5).
These pin, with ``broker_ops`` replaced by a fake that acts as the broker
would:

* the :class:`Reclaimer` (retries, backoff, dedupe, drain, stop);
* an anonymous run's uid lease ends only after its workspace, per-uid config
  state and ``/dev/shm`` objects are gone — kept on any failure, retried;
* per-session dirs, stateless transcripts, transcript reads and the retention
  sweep in uid mode;
* that off, landlock and uid-without-a-config-base behave as before and never
  call the broker.

A session uid's 0600/0700 files are simulated with modes the gateway's own uid
cannot get past (skipped as root, which gets past them).
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.session_manager as sm
from src import claude_config_paths as cp
from src.backends.claude import broker_ops, landlock, transcript_turns, uid_cleanup
from src.backends.claude import uid_isolation as ui
from src.backends.claude import uid_layout
from src.backends.claude.transcript_turns import (
    TRANSCRIPT_CORRUPT,
    TRANSCRIPT_MISMATCH,
    TRANSCRIPT_MISSING,
    TRANSCRIPT_UNREADABLE,
)
from src.concurrency import SessionLimitExceeded
from src.session_manager import (
    Session,
    SessionManager,
    TranscriptTemporarilyUnavailable,
    TranscriptUnreadable,
)
from src.workspace_manager import WorkspaceManager

SID = "6f1c8f9e-2b1d-4c3e-9a55-0d6f2f0b7c11"
OTHER_SID = "0b3e7a52-9d4c-4f1e-8c2a-5e6f7a8b9c0d"
FAST = (0.01,) * 6

_not_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root gets past the modes that stand in for uid U"
)


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def _restore_modes(path: Path) -> None:
    """Owner rwx back on every directory under *path* (as the broker does for
    the session uid's own dirs before removing them)."""
    if path.is_symlink() or not path.is_dir():
        return
    path.chmod(0o700)
    for child in path.iterdir():
        _restore_modes(child)


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    _restore_modes(path)
    shutil.rmtree(path)


def _act_as_broker(_uid: int, kwargs: dict) -> None:
    """What the broker's cleanup does to *kwargs*' paths (the shm step aside)."""
    for target in kwargs.get("clear", ()):
        target = Path(target)
        if target.is_dir() and not target.is_symlink():
            target.chmod(0o700)
            for child in list(target.iterdir()):
                _remove(child)
    for target in kwargs.get("remove", ()):
        # The broker gives the uid's own dir (the parent here) its owner rwx
        # back when a removal needs it — also when the gateway cannot look in.
        parent = Path(target).parent
        if parent.is_dir():
            parent.chmod(parent.stat().st_mode & 0o777 | 0o700)
        if os.path.lexists(target):
            _remove(Path(target))


class FakeOps:
    """Stands in for ``broker_ops``: records calls, fails on demand."""

    def __init__(self) -> None:
        self.calls: list = []
        self.fail = 0  # the next N calls raise BrokerOpError
        self.transient = False  # ... with this transient flag
        self.result = True  # what a cleanup that ran answers
        self.effect = _act_as_broker
        self.turns = 0
        self.gate: threading.Event | None = None

    def cleanup(self, uid, **kwargs):
        self.calls.append((uid, kwargs))
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            self.fail -= 1
            raise broker_ops.BrokerOpError(
                "the broker refused the cleanup op (test)", transient=self.transient
            )
        if self.effect is not None:
            self.effect(uid, kwargs)
        return self.result

    def read_transcript(self, uid, path, cwd, *, timeout=30.0):
        self.calls.append((uid, {"read": str(path), "cwd": str(cwd)}))
        if self.fail:
            self.fail -= 1
            raise broker_ops.BrokerOpError(
                "the broker refused the transcript op (test)", transient=self.transient
            )
        return self.turns


def _mode(monkeypatch, mode: str) -> None:
    if mode == "off":
        monkeypatch.delenv(landlock.MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(landlock.MODE_ENV, mode)
    monkeypatch.setattr(landlock, "_effective_mode_cache", None)


@pytest.fixture
def reclaimer(monkeypatch):
    """A fresh reclaimer with short delays, installed as the process's."""
    instance = uid_cleanup.Reclaimer(backoff=FAST)
    monkeypatch.setattr(uid_cleanup, "_reclaimer", instance)
    yield instance
    instance.stop()


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """The gateway's own (shared) config dir."""
    path = tmp_path / "cfg"
    path.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    return path


@pytest.fixture
def ops(monkeypatch):
    fake = FakeOps()
    monkeypatch.setattr(broker_ops, "cleanup", fake.cleanup)
    monkeypatch.setattr(broker_ops, "read_transcript", fake.read_transcript)
    monkeypatch.setattr(broker_ops, "available", lambda: True)
    return fake


@pytest.fixture
def pool(monkeypatch, tmp_path):
    """A temp uid registry and a fresh ephemeral pool."""
    monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
    monkeypatch.setattr(ui, "_registry", None)
    monkeypatch.setattr(ui, "_ephemeral_pool", None)


@pytest.fixture
def shm(monkeypatch, tmp_path):
    """A fake /dev/shm, and no SysV IPC tables or mqueue mount to look at."""
    path = tmp_path / "shm"
    path.mkdir()
    monkeypatch.setattr(uid_cleanup, "_SHM_DIR", str(path))
    monkeypatch.setattr(uid_cleanup, "_SYSVIPC_DIR", str(tmp_path / "no-sysvipc"))
    monkeypatch.setattr(uid_cleanup, "_MQUEUE_DIR", str(tmp_path / "no-mqueue"))
    return path


@pytest.fixture
def per_uid(monkeypatch, tmp_path, cfg, ops, reclaimer, pool, shm):
    """uid mode with per-uid config dirs (``uid_layout.enabled()``)."""
    _mode(monkeypatch, "uid")
    monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
    base = tmp_path / "per-uid"
    base.mkdir()
    monkeypatch.setenv(cp.CONFIG_BASE_ENV, str(base))
    assert uid_layout.enabled()
    return SimpleNamespace(base=base, cfg=cfg, ops=ops, reclaimer=reclaimer)


@pytest.fixture
def wm(tmp_path):
    return WorkspaceManager(base_path=tmp_path / "workspaces")


def _writable_dirs(uid: int) -> Path:
    """``P(uid)`` with its writable dirs, each holding a previous run's state."""
    config = cp.user_config_dir(uid)
    for rel in uid_layout.WRITABLE_DIRS:
        (config / rel).mkdir(parents=True, exist_ok=True)
    (config / "projects" / "-ws" / "old.jsonl").parent.mkdir(exist_ok=True)
    (config / "projects" / "-ws" / "old.jsonl").write_text("{}\n")
    (config / "state" / ".claude.json").write_text("{}")
    return config


def _contents(config: Path) -> list:
    return sorted(
        str(p.relative_to(config))
        for rel in uid_layout.WRITABLE_DIRS
        for p in (config / rel).rglob("*")
    )


def _leased_workspace(wm: WorkspaceManager):
    workspace = wm.resolve(None)
    key = os.path.abspath(workspace)
    uid = ui.resolve_uid(key, persist=False)
    (workspace / ".cli-tmp" / f"claude-{uid}").mkdir(parents=True)
    (workspace / ".cli-tmp" / f"claude-{uid}" / "tmp.out").write_text("out")
    (workspace / "notes.md").write_text("notes")
    return workspace, key, uid


# ---------------------------------------------------------------------------
# Reclaimer
# ---------------------------------------------------------------------------


class TestReclaimer:
    def test_a_job_runs_and_drain_returns(self, reclaimer):
        ran = []
        assert reclaimer.submit("k", lambda: ran.append(1) or True)
        assert reclaimer.drain(5)
        assert ran == [1] and reclaimer.pending() == 0

    def test_a_pending_key_is_not_queued_twice(self, reclaimer):
        gate = threading.Event()
        calls = []

        def first():
            calls.append("first")
            return gate.wait(5)

        assert reclaimer.submit("k", first)
        assert not reclaimer.submit("k", lambda: calls.append("second") or True)
        assert reclaimer.is_pending("k")
        gate.set()
        assert reclaimer.drain(5)
        assert calls == ["first"]
        # Done, the key can be queued again.
        assert reclaimer.submit("k", lambda: calls.append("third") or True)
        assert reclaimer.drain(5)
        assert calls == ["first", "third"]

    def test_a_failed_job_is_retried_until_it_is_done(self, reclaimer):
        results = iter([False, False, True])
        calls = []

        def job():
            calls.append(time.monotonic())
            return next(results)

        reclaimer.submit("k", job)
        assert reclaimer.drain(5)
        assert len(calls) == 3

    def test_an_exception_counts_as_a_failure(self, reclaimer):
        calls = []

        def job():
            calls.append(1)
            if len(calls) < 2:
                raise OSError(errno.EIO, "boom")
            return True

        reclaimer.submit("k", job)
        assert reclaimer.drain(5)
        assert len(calls) == 2

    def test_it_gives_up_after_the_last_delay(self, reclaimer, caplog):
        calls = []
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            reclaimer.submit(("ws", "/x"), lambda: calls.append(1) and False)
            assert reclaimer.drain(5)
        assert len(calls) == len(FAST) + 1
        assert not reclaimer.is_pending(("ws", "/x"))
        assert "Gave up on uid cleanup job ('ws', '/x')" in caplog.text

    def test_the_delays_back_off(self):
        stamps = []
        instance = uid_cleanup.Reclaimer(backoff=(0.05, 0.2))
        try:
            instance.submit("k", lambda: stamps.append(time.monotonic()) and False)
            assert instance.drain(5)
        finally:
            instance.stop()
        assert len(stamps) == 3
        assert stamps[1] - stamps[0] >= 0.05
        assert stamps[2] - stamps[1] >= 0.2

    def test_drain_times_out_while_a_job_runs(self, reclaimer):
        gate = threading.Event()
        reclaimer.submit("k", lambda: gate.wait(5))
        assert not reclaimer.drain(0.05)
        gate.set()
        assert reclaimer.drain(5)

    def test_stop(self, reclaimer):
        gate = threading.Event()
        reclaimer.submit("k", lambda: gate.wait(5) and False)
        gate.set()
        reclaimer.stop()
        assert not reclaimer.submit("k2", lambda: True)
        assert not any(thread.is_alive() for thread in reclaimer._threads)

    def test_the_module_helpers_without_a_reclaimer(self, monkeypatch):
        monkeypatch.setattr(uid_cleanup, "_reclaimer", None)
        assert uid_cleanup.pending() == 0
        assert uid_cleanup.drain(0) is True
        assert uid_cleanup._reclaimer is None  # neither creates one

    def test_the_singleton(self, monkeypatch):
        monkeypatch.setattr(uid_cleanup, "_reclaimer", None)
        first = uid_cleanup.reclaimer()
        assert uid_cleanup.reclaimer() is first
        assert first._threads == []  # the threads start with the first job
        assert first._workers == uid_cleanup.RECLAIMER_WORKERS


# ---------------------------------------------------------------------------
# Anonymous workspaces
# ---------------------------------------------------------------------------


class TestOtherModesAreUnchanged:
    """Off, landlock and uid without a config base: the gateway's own rmtree,
    the lease released once the workspace is gone, no broker, no thread."""

    @pytest.mark.parametrize("mode", ["off", "landlock", "uid"])
    def test_cleanup(self, monkeypatch, wm, cfg, ops, reclaimer, pool, mode):
        _mode(monkeypatch, mode)
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        monkeypatch.delenv(cp.CONFIG_BASE_ENV, raising=False)
        assert not uid_layout.enabled()
        workspace, key, uid = _leased_workspace(wm)

        wm.cleanup_temp_workspace(workspace)

        assert not workspace.exists()
        assert ui.leased_uid(key) is None
        assert ops.calls == []
        assert reclaimer._threads == []

    @_not_root
    def test_what_the_gateway_cannot_remove_keeps_the_lease(
        self, monkeypatch, wm, cfg, ops, reclaimer, pool
    ):
        _mode(monkeypatch, "uid")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        workspace, key, uid = _leased_workspace(wm)
        locked = workspace / ".cli-tmp"
        locked.chmod(0o500)
        try:
            wm.cleanup_temp_workspace(workspace)
            assert workspace.exists()
            assert ui.leased_uid(key) == uid
            assert ops.calls == []
        finally:
            locked.chmod(0o700)


class TestReclaimTempWorkspace:
    def test_the_broker_wipes_then_the_lease_ends(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        config = _writable_dirs(uid)

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        assert not os.path.lexists(workspace)
        assert ui.leased_uid(key) is None
        assert _contents(config) == []
        assert all((config / rel).is_dir() for rel in uid_layout.WRITABLE_DIRS)
        ((called_uid, kwargs),) = per_uid.ops.calls
        assert called_uid == uid
        assert kwargs["workspace"] == key
        assert kwargs["wait"] == 10
        assert kwargs["shm"] is True
        assert kwargs["clear"] == [key] + [
            str(config / rel) for rel in uid_layout.WRITABLE_DIRS
        ]

    def test_only_existing_config_dirs_are_named(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        config = cp.user_config_dir(uid)
        (config / "projects").mkdir(parents=True)

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        assert per_uid.ops.calls[0][1]["clear"] == [key, str(config / "projects")]
        assert ui.leased_uid(key) is None

    def test_it_never_blocks_the_caller(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        per_uid.ops.gate = threading.Event()

        wm.cleanup_temp_workspace(workspace)
        wm.cleanup_temp_workspace(workspace)  # a second teardown: one job

        assert per_uid.reclaimer.is_pending(("ws", key))
        assert workspace.exists() and ui.leased_uid(key) == uid
        per_uid.ops.gate.set()
        assert per_uid.reclaimer.drain(5)
        assert len(per_uid.ops.calls) == 1
        assert ui.leased_uid(key) is None

    def test_a_refused_wipe_keeps_the_lease_and_is_retried(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        per_uid.ops.fail = 2

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        assert len(per_uid.ops.calls) == 3
        assert not workspace.exists()
        assert ui.leased_uid(key) is None

    def test_a_wipe_that_keeps_failing_keeps_the_lease(self, per_uid, wm, caplog):
        workspace, key, uid = _leased_workspace(wm)
        per_uid.ops.fail = 1000

        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            wm.cleanup_temp_workspace(workspace)
            assert per_uid.reclaimer.drain(5)  # a parked job is not waited for

        assert workspace.is_dir()
        assert ui.leased_uid(key) == uid
        # Parked, not dropped: its workspace and lease are still there.
        assert per_uid.reclaimer.is_pending(("ws", key))
        assert per_uid.reclaimer.parked() == 1
        assert "still fails after 7 attempts" in caplog.text
        assert "Gave up" not in caplog.text
        # A later cleanup (the broker is back) runs the parked job at once.
        per_uid.ops.fail = 0
        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)
        assert not workspace.exists() and ui.leased_uid(key) is None
        assert not per_uid.reclaimer.is_pending(("ws", key))

    def test_a_parked_wipe_is_tried_again_on_its_own(self, per_uid, wm, monkeypatch):
        retry = uid_cleanup.Reclaimer(backoff=FAST, parked_retry=0.05)
        monkeypatch.setattr(uid_cleanup, "_reclaimer", retry)
        try:
            workspace, key, uid = _leased_workspace(wm)
            per_uid.ops.fail = len(FAST) + 3  # the backoff, then two parked tries

            wm.cleanup_temp_workspace(workspace)
            deadline = time.monotonic() + 5
            while retry.is_pending(("ws", key)) and time.monotonic() < deadline:
                time.sleep(0.02)

            assert not workspace.exists() and ui.leased_uid(key) is None
            assert len(per_uid.ops.calls) == len(FAST) + 4
        finally:
            retry.stop()

    def test_a_wipe_whose_lease_went_elsewhere_is_given_up(
        self, per_uid, wm, caplog, monkeypatch
    ):
        workspace, key, uid = _leased_workspace(wm)

        def refused_and_released(_uid, **_kwargs):
            ui.release_ephemeral(key)  # say the pool reclaimed it meanwhile
            raise broker_ops.BrokerOpError("refused (test)")

        monkeypatch.setattr(broker_ops, "cleanup", refused_and_released)
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            wm.cleanup_temp_workspace(workspace)
            assert per_uid.reclaimer.drain(5)

        assert not per_uid.reclaimer.is_pending(("ws", key))
        assert "Gave up on uid cleanup job" in caplog.text

    def test_a_failed_step_with_leftovers_keeps_the_root_and_the_lease(
        self, per_uid, wm
    ):
        workspace, key, uid = _leased_workspace(wm)
        config = _writable_dirs(uid)
        per_uid.ops.result, per_uid.ops.effect = False, None  # nothing removed

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        # Never handed out with the previous run's state still in P(uid); the
        # root stays as the lease's anchor (a lease whose workspace is gone
        # may be reclaimed by the pool).
        assert ui.leased_uid(key) == uid
        assert workspace.is_dir()
        assert "projects/-ws/old.jsonl" in _contents(config)

    def test_a_failed_step_that_left_nothing_is_done(self, per_uid, wm):
        # The broker reports a step it could not finish (say, an entry of the
        # gateway's own it may not remove), but the uid's state is gone and
        # the gateway removes the rest.
        workspace, key, uid = _leased_workspace(wm)
        config = _writable_dirs(uid)
        per_uid.ops.result = False

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        assert _contents(config) == []
        assert not workspace.exists()
        assert ui.leased_uid(key) is None

    def test_a_shm_object_of_the_uid_counts_as_a_leftover(self, monkeypatch, shm):
        # /dev/shm entries are checked by owner; the test owns its own files.
        uid = os.getuid()
        (shm / "sem.mine").write_text("")
        assert uid_cleanup._leftovers(uid, []) == [str(shm / "sem.mine")]
        assert uid_cleanup._leftovers(uid + 1, []) == []

    def test_an_already_removed_workspace_still_wipes_the_config_dir(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        config = _writable_dirs(uid)
        shutil.rmtree(workspace)

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        ((_, kwargs),) = per_uid.ops.calls
        assert key not in kwargs["clear"] and kwargs["shm"] is True
        assert _contents(config) == []
        assert ui.leased_uid(key) is None

    def test_a_named_workspace_is_never_touched(self, per_uid, wm):
        workspace = wm.resolve("alice")
        wm.cleanup_temp_workspace(workspace)
        assert workspace.is_dir() and per_uid.ops.calls == []

    def test_sync_runs_here_with_retries(self, per_uid, wm, monkeypatch):
        monkeypatch.setattr(uid_cleanup, "BACKOFF_SECONDS", FAST)
        workspace, key, uid = _leased_workspace(wm)
        per_uid.ops.fail = 2

        wm.cleanup_temp_workspace(workspace, sync=True)

        assert not workspace.exists() and ui.leased_uid(key) is None
        assert len(per_uid.ops.calls) == 3
        assert per_uid.reclaimer._threads == []  # nothing went to a thread

    def test_sync_gives_up_within_its_budget(self, per_uid, wm, monkeypatch, caplog):
        monkeypatch.setattr(uid_cleanup, "BACKOFF_SECONDS", (0.05,) * 6)
        monkeypatch.setattr(uid_cleanup, "_SYNC_BUDGET_SECONDS", 0.2)
        workspace, key, uid = _leased_workspace(wm)
        per_uid.ops.fail = 1000

        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            wm.cleanup_temp_workspace(workspace, sync=True)

        assert time.monotonic() - started < 2
        assert workspace.is_dir() and ui.leased_uid(key) == uid
        assert "Could not finish uid cleanup job" in caplog.text
        # The op timeout stays inside the budget too.
        assert all(kw["timeout"] <= 1.0 for _uid, kw in per_uid.ops.calls)

    def test_sync_ops_get_the_budget_as_their_timeout(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        wm.cleanup_temp_workspace(workspace, sync=True)
        ((_, kwargs),) = per_uid.ops.calls
        assert 1.0 <= kwargs["timeout"] <= 30.0 - 10

    def test_async_ops_get_the_full_timeout(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)
        assert per_uid.ops.calls[0][1]["timeout"] == uid_cleanup._OP_TIMEOUT_SECONDS

    def test_bases_reached_through_links_are_resolved_for_the_broker(
        self, per_uid, tmp_path, monkeypatch
    ):
        # The broker walks every target without following a link: a symlinked
        # workspace base or config base would fail every attempt, and the
        # lease would never end.
        real_ws, real_cfg = tmp_path / "real-ws", tmp_path / "real-cfg"
        real_ws.mkdir()
        real_cfg.mkdir()
        (tmp_path / "ws-link").symlink_to(real_ws)
        (tmp_path / "cfg-link").symlink_to(real_cfg)
        monkeypatch.setenv(cp.CONFIG_BASE_ENV, str(tmp_path / "cfg-link"))
        wm = WorkspaceManager(base_path=tmp_path / "ws-link")
        workspace, key, uid = _leased_workspace(wm)
        assert key.startswith(str(tmp_path / "ws-link"))
        _writable_dirs(uid)

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)

        ((_, kwargs),) = per_uid.ops.calls
        resolved = str(real_ws / workspace.name)
        assert kwargs["workspace"] == resolved
        assert kwargs["clear"][0] == resolved
        assert all(t.startswith(str(real_cfg)) for t in kwargs["clear"][1:])
        assert not os.path.lexists(workspace) and ui.leased_uid(key) is None

    def test_the_last_component_is_never_followed(self, tmp_path):
        (tmp_path / "real").mkdir()
        (tmp_path / "base").symlink_to(tmp_path / "real")
        (tmp_path / "real" / "planted").symlink_to("/etc")
        assert uid_cleanup._broker_path(tmp_path / "base" / "planted") == str(
            tmp_path / "real" / "planted"
        )


class TestOrphanedWorkspaces:
    """A workspace a previous gateway process left: no lease, so the uid comes
    from the root's ACL — only an ephemeral uid nothing holds."""

    UID = 29750  # in the default ephemeral band (29744..29999)

    @pytest.fixture
    def orphan(self, per_uid, wm, monkeypatch):
        workspace = wm.base_path / "_tmp_orphan"
        (workspace / ".cli-tmp").mkdir(parents=True)
        (workspace / ".cli-tmp" / "f").write_text("x")
        names = {"acl": [self.UID]}

        def named_users(path):
            assert path == os.path.abspath(workspace)
            if isinstance(names["acl"], Exception):
                raise names["acl"]
            return list(names["acl"])

        monkeypatch.setattr(uid_cleanup, "_acl_named_users", named_users)
        return SimpleNamespace(workspace=workspace, names=names, **vars(per_uid))

    def test_the_acl_uid_is_wiped_as(self, orphan, wm):
        config = _writable_dirs(self.UID)

        wm.cleanup_temp_workspace(orphan.workspace)
        assert orphan.reclaimer.drain(5)

        ((uid, kwargs),) = orphan.ops.calls
        assert uid == self.UID and kwargs["shm"] is True
        assert kwargs["workspace"] == str(orphan.workspace)
        assert not orphan.workspace.exists()
        assert _contents(config) == []

    def test_the_startup_sweep_takes_that_path(self, orphan, wm):
        old = time.time() - 7200
        os.utime(orphan.workspace, (old, old))
        assert wm.sweep_orphan_temp_workspaces(max_age_seconds=3600) == 1
        assert orphan.reclaimer.drain(5)
        assert orphan.ops.calls[0][0] == self.UID
        assert not orphan.workspace.exists()

    def test_the_first_sweep_takes_them_at_any_age(self, orphan, wm):
        # One gateway process per uid registry: at startup every _tmp_ dir is
        # a previous process's, also one a shutdown drain did not finish.
        assert wm.sweep_orphan_temp_workspaces(max_age_seconds=3600) == 1
        assert orphan.reclaimer.drain(5)
        assert not orphan.workspace.exists()

    def test_later_sweeps_keep_the_age_filter(self, orphan, wm):
        wm.sweep_orphan_temp_workspaces(max_age_seconds=3600)
        assert orphan.reclaimer.drain(5)
        fresh = wm.base_path / "_tmp_fresh"
        fresh.mkdir()
        assert wm.sweep_orphan_temp_workspaces(max_age_seconds=3600) == 0
        assert fresh.is_dir()

    @pytest.mark.parametrize("mode", ["off", "landlock", "uid"])
    def test_other_modes_keep_it_on_the_first_sweep(
        self, monkeypatch, tmp_path, cfg, mode
    ):
        _mode(monkeypatch, mode)  # uid without a config base: no per-uid dirs
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        monkeypatch.delenv(cp.CONFIG_BASE_ENV, raising=False)
        manager = WorkspaceManager(base_path=tmp_path / "w")
        (manager.base_path / "_tmp_fresh").mkdir(parents=True)
        assert manager.sweep_orphan_temp_workspaces(max_age_seconds=3600) == 0
        assert (manager.base_path / "_tmp_fresh").is_dir()

    @pytest.mark.parametrize(
        "acl",
        [[], [20000], [29750, 29751], OSError(errno.ENOTSUP, "no ACLs")],
        ids=["none", "stable-band", "ambiguous", "unreadable"],
    )
    def test_without_one_clear_uid_the_gateway_removes_what_it_can(
        self, orphan, wm, acl
    ):
        orphan.names["acl"] = acl
        wm.cleanup_temp_workspace(orphan.workspace)
        assert orphan.ops.calls == []
        assert not orphan.workspace.exists()  # here all the gateway's own

    def test_a_plain_unleased_workspace_needs_no_acl_module(self, per_uid, wm):
        # No lease and no ACL (a run whose CLI never started): the gateway's
        # own removal, with or without posix_acl importable.
        workspace = wm.resolve(None)
        (workspace / "notes.md").write_text("x")
        wm.cleanup_temp_workspace(workspace)
        assert not workspace.exists() and per_uid.ops.calls == []

    def test_a_uid_the_registry_holds_is_never_wiped_as(self, orphan, wm, monkeypatch):
        monkeypatch.setattr(ui, "registry_holds", lambda uid: uid == self.UID)
        wm.cleanup_temp_workspace(orphan.workspace)
        assert orphan.ops.calls == []

    def test_a_uid_another_run_holds_is_never_wiped_as(self, orphan, wm):
        live = wm.resolve(None)
        uid = ui.resolve_uid(os.path.abspath(live), persist=False)
        orphan.names["acl"] = [uid]
        wm.cleanup_temp_workspace(orphan.workspace)
        assert orphan.ops.calls == []

    def test_leased_again_meanwhile_only_the_workspace_is_emptied(self, orphan, wm):
        # The job found the uid free, but by the time it runs a new run holds
        # it: P(uid) and /dev/shm are that run's now.
        live = wm.resolve(None)
        uid = ui.resolve_uid(os.path.abspath(live), persist=False)
        config = _writable_dirs(uid)
        key = os.path.abspath(orphan.workspace)

        assert uid_cleanup._wipe_workspace(key, uid)

        ((called, kwargs),) = orphan.ops.calls
        assert called == uid
        assert kwargs["clear"] == [key]
        assert not kwargs.get("shm") and not kwargs.get("workspace")
        assert "projects/-ws/old.jsonl" in _contents(config)
        assert not orphan.workspace.exists()
        assert ui.leased_uid(os.path.abspath(live)) == uid


# ---------------------------------------------------------------------------
# Session dirs
# ---------------------------------------------------------------------------


def _session_dir(config: Path, kind: str, sid: str = SID) -> Path:
    path = config / kind / sid
    path.mkdir(parents=True, exist_ok=True)
    (path / "hook-0.sh").write_text("export X=1\n")
    return path


class TestSessionDirsOtherModes:
    """``landlock.remove_session_dirs`` as it was."""

    def test_off_removes_only_session_env(self, monkeypatch, cfg, ops):
        _mode(monkeypatch, "off")
        env, tasks = _session_dir(cfg, "session-env"), _session_dir(cfg, "tasks")
        other = _session_dir(cfg, "session-env", OTHER_SID)
        uid_cleanup.remove_session_dirs(SID)
        assert not env.exists() and tasks.is_dir() and other.is_dir()
        assert ops.calls == []

    @pytest.mark.parametrize("mode", ["landlock", "uid"])
    def test_confined_removes_both(self, monkeypatch, cfg, ops, mode):
        _mode(monkeypatch, mode)
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        monkeypatch.delenv(cp.CONFIG_BASE_ENV, raising=False)
        env, tasks = _session_dir(cfg, "session-env"), _session_dir(cfg, "tasks")
        uid_cleanup.remove_session_dirs(SID, user="alice", workspace="/ws/alice")
        assert not env.exists() and not tasks.exists()
        assert ops.calls == []

    @pytest.mark.parametrize("sid", ["../escape", "a/b", ".hidden", "", None])
    def test_unsafe_ids_are_ignored(self, monkeypatch, cfg, sid):
        _mode(monkeypatch, "landlock")
        (cfg / "session-env" / "keep").mkdir(parents=True)
        (cfg / "escape").mkdir()
        (cfg / "session-env" / "a").mkdir()
        uid_cleanup.remove_session_dirs(sid)
        assert (cfg / "session-env" / "keep").is_dir()
        assert (cfg / "escape").is_dir() and (cfg / "session-env" / "a").is_dir()

    def test_a_symlink_in_place_or_above_is_left_alone(
        self, monkeypatch, cfg, tmp_path
    ):
        _mode(monkeypatch, "landlock")
        target = tmp_path / "target"
        (target / SID).mkdir(parents=True)
        (target / SID / "keep.txt").write_text("keep")
        (cfg / "session-env").mkdir()
        (cfg / "session-env" / SID).symlink_to(target / SID)
        (cfg / "tasks").symlink_to(target)
        uid_cleanup.remove_session_dirs(SID)
        assert (cfg / "session-env" / SID).is_symlink()
        assert (target / SID / "keep.txt").read_text() == "keep"

    def test_links_inside_are_removed_not_followed(self, monkeypatch, cfg, tmp_path):
        _mode(monkeypatch, "landlock")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        env = _session_dir(cfg, "session-env")
        (env / "link").symlink_to(outside)
        uid_cleanup.remove_session_dirs(SID)
        assert not env.exists()
        assert (outside / "keep.txt").read_text() == "keep"

    def test_no_home_no_override_removes_nothing(self, monkeypatch, tmp_path):
        # The strict rule the gateway always used for the shared dir.
        _mode(monkeypatch, "off")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        uid_cleanup.remove_session_dirs(SID)  # no crash, nothing to resolve

    def test_the_id_rule_matches_the_creation_side(self):
        assert uid_cleanup._SESSION_ID_RE.pattern == landlock._SESSION_ID_RE.pattern


class TestSessionDirsPerUid:
    @pytest.fixture
    def alice(self, per_uid):
        uid = ui.resolve_uid("alice", persist=True)
        return SimpleNamespace(uid=uid, config=cp.user_config_dir(uid), **vars(per_uid))

    def test_an_anonymous_sessions_dirs_go_with_its_lease(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        config = cp.user_config_dir(uid)
        env = _session_dir(config, "session-env")
        shared = _session_dir(per_uid.cfg, "session-env")
        uid_cleanup.remove_session_dirs(SID, user=None, workspace=workspace)
        assert env.is_dir() and shared.is_dir()
        assert per_uid.ops.calls == []

    def test_a_named_sessions_dirs_in_its_config_dir(self, alice):
        env = _session_dir(alice.config, "session-env")
        tasks = _session_dir(alice.config, "tasks")
        shared = _session_dir(alice.cfg, "session-env")
        uid_cleanup.remove_session_dirs(SID, user="alice", workspace="/ws/alice/claude")
        assert not env.exists() and not tasks.exists()
        assert shared.is_dir()  # the gateway's own config dir is not the session's
        assert alice.ops.calls == []  # the gateway could remove it all

    @_not_root
    def test_what_the_gateway_cannot_remove_goes_to_the_broker(self, alice):
        env = _session_dir(alice.config, "session-env")
        env.chmod(0o500)  # stands in for the uid's own 0700 dir
        try:
            uid_cleanup.remove_session_dirs(SID, user="alice", workspace="/ws/a")
            assert alice.reclaimer.drain(5)
        finally:
            if env.exists():
                env.chmod(0o700)
        assert not env.exists()
        ((uid, kwargs),) = alice.ops.calls
        assert uid == alice.uid and kwargs == {"remove": [str(env)]}

    @_not_root
    def test_the_guard_stops_it_for_a_live_session(self, alice):
        env = _session_dir(alice.config, "session-env")
        env.chmod(0o500)
        try:
            uid_cleanup.remove_session_dirs(
                SID, user="alice", workspace="/ws/a", guard=lambda: False
            )
            assert alice.reclaimer.drain(5)
            assert env.is_dir() and alice.ops.calls == []
        finally:
            env.chmod(0o700)

    @_not_root
    def test_a_failing_broker_is_retried(self, alice):
        env = _session_dir(alice.config, "session-env")
        env.chmod(0o500)
        alice.ops.fail = 2
        try:
            uid_cleanup.remove_session_dirs(SID, user="alice", workspace="/ws/a")
            assert alice.reclaimer.drain(5)
        finally:
            if env.exists():
                env.chmod(0o700)
        assert not env.exists() and len(alice.ops.calls) == 3


class TestSessionManagerWiring:
    def test_the_session_and_a_guard_are_passed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            uid_cleanup,
            "remove_session_dirs",
            lambda sid, **kwargs: calls.append((sid, kwargs)),
        )
        manager = SessionManager()
        session = manager.get_or_create_session(SID)
        session.user, session.workspace = "alice", "/ws/alice/claude"
        assert manager.delete_session(SID)

        ((sid, kwargs),) = calls
        assert sid == SID
        assert kwargs["user"] == "alice" and kwargs["workspace"] == "/ws/alice/claude"
        guard = kwargs["guard"]
        assert guard() is True
        manager.get_or_create_session(SID)  # a live session holds the id again
        assert guard() is False

    async def test_shutdown_waits_a_bounded_time_for_queued_removals(self, monkeypatch):
        drained = []
        monkeypatch.setattr(uid_cleanup, "pending", lambda: 1)
        monkeypatch.setattr(
            uid_cleanup, "drain", lambda timeout: drained.append(timeout)
        )
        await SessionManager().async_shutdown()
        assert drained == [sm._SHUTDOWN_DRAIN_SECONDS]

    async def test_shutdown_skips_the_drain_when_nothing_is_queued(self, monkeypatch):
        monkeypatch.setattr(uid_cleanup, "pending", lambda: 0)
        monkeypatch.setattr(
            uid_cleanup, "drain", lambda timeout: pytest.fail("drained for nothing")
        )
        await SessionManager().async_shutdown()


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, cwd, turns: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "cwd": str(cwd), "message": {"content": f"turn {n}"}}
        for n in range(turns)
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


class TestRemoveTranscript:
    def test_off_mode(self, monkeypatch, tmp_path, ops):
        _mode(monkeypatch, "off")
        projects = tmp_path / "projects"
        monkeypatch.setattr(sm, "_PROJECTS_ROOT", projects)
        workspace = tmp_path / "_tmp_run"
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace), workspace
        )
        (transcript.parent / SID / "tool-results").mkdir(parents=True)

        uid_cleanup.remove_transcript(SID, workspace=str(workspace))

        assert not transcript.parent.exists() and ops.calls == []

    def test_a_given_path_wins(self, monkeypatch, tmp_path, ops):
        _mode(monkeypatch, "off")
        transcript = _write_transcript(tmp_path / "elsewhere" / f"{SID}.jsonl", "/ws")
        uid_cleanup.remove_transcript(SID, workspace="/ws", path=transcript)
        assert not transcript.exists()

    def test_nothing_without_a_workspace_or_path(self, monkeypatch, ops):
        _mode(monkeypatch, "off")
        uid_cleanup.remove_transcript(SID)
        uid_cleanup.remove_transcript(None, workspace="/ws")
        assert ops.calls == []

    def test_an_anonymous_runs_transcript_goes_with_its_lease(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace), workspace
        )
        assert str(transcript).startswith(str(cp.user_config_dir(uid)))
        uid_cleanup.remove_transcript(SID, workspace=str(workspace), path=transcript)
        assert transcript.is_file() and per_uid.ops.calls == []

    @_not_root
    def test_a_named_sessions_locked_transcript_goes_to_the_broker(self, per_uid):
        uid = ui.resolve_uid("alice", persist=True)
        workspace = "/ws/alice/claude"
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace, user="alice"), workspace
        )
        sidecar = transcript.parent / SID / "subagents"
        sidecar.mkdir(parents=True)
        project = transcript.parent
        project.chmod(0o500)  # the uid's own 0700 project dir
        try:
            uid_cleanup.remove_transcript(SID, user="alice", workspace=workspace)
            assert per_uid.reclaimer.drain(5)
        finally:
            if project.exists():
                project.chmod(0o700)
        ((called, kwargs),) = per_uid.ops.calls
        assert called == uid
        assert kwargs == {"remove": [str(transcript), str(transcript.parent / SID)]}
        assert not transcript.exists() and not project.exists()


class TestTranscriptReads:
    @pytest.fixture
    def alice(self, per_uid):
        uid = ui.resolve_uid("alice", persist=True)
        workspace = "/ws/alice/claude"
        path = sm._session_jsonl_path(SID, workspace, user="alice")
        assert path.is_relative_to(cp.user_config_dir(uid) / "projects")
        return SimpleNamespace(uid=uid, workspace=workspace, path=path, **vars(per_uid))

    def test_the_gateway_reads_what_it_may(self, alice):
        _write_transcript(alice.path, alice.workspace, turns=3)
        assert (
            uid_cleanup.read_transcript_turns(
                SID, user="alice", workspace=alice.workspace
            )
            == 3
        )
        assert uid_cleanup.transcript_exists(
            SID, user="alice", workspace=alice.workspace
        )
        assert alice.ops.calls == []

    @_not_root
    def test_a_locked_transcript_is_counted_by_the_broker(self, alice):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.turns = 5
        assert (
            uid_cleanup.read_transcript_turns(
                SID, user="alice", workspace=alice.workspace
            )
            == 5
        )
        ((uid, kwargs),) = alice.ops.calls
        assert uid == alice.uid
        assert kwargs == {"read": str(alice.path), "cwd": alice.workspace}

    @_not_root
    def test_rehydrate_uses_the_brokers_count(self, alice):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.turns = 4
        session = sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=alice.workspace)
        assert session is not None and session.turn_counter == 4

    @_not_root
    def test_rehydrate_refuses_another_workspaces_transcript(self, alice, caplog):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.turns = TRANSCRIPT_MISMATCH
        with caplog.at_level(logging.WARNING, logger=sm.__name__):
            assert (
                sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=alice.workspace)
                is None
            )
        assert "Refusing to rehydrate session" in caplog.text

    @_not_root
    @pytest.mark.parametrize("code", [TRANSCRIPT_MISSING, TRANSCRIPT_CORRUPT])
    def test_rehydrate_misses_on_the_other_codes(self, alice, code):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.turns = code
        assert (
            sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=alice.workspace) is None
        )

    @_not_root
    def test_a_broker_failure_is_transcript_unreadable(
        self, alice, monkeypatch, caplog
    ):
        monkeypatch.setattr(sm, "_UNREADABLE_WARNED", set())
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.fail = 2
        with caplog.at_level(logging.WARNING, logger=sm.__name__):
            for _ in range(2):
                with pytest.raises(TranscriptUnreadable) as excinfo:
                    sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=alice.workspace)
                assert excinfo.value.path == alice.path
                assert isinstance(excinfo.value.__cause__, broker_ops.BrokerOpError)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "broker refused" in warnings[0].getMessage()

    @_not_root
    def test_no_broker_is_transcript_unreadable(self, alice, monkeypatch):
        monkeypatch.setattr(broker_ops, "available", lambda: False)
        _write_transcript(alice.path, alice.workspace).chmod(0)
        with pytest.raises(TranscriptUnreadable):
            uid_cleanup.read_transcript_turns(
                SID, user="alice", workspace=alice.workspace
            )
        assert alice.ops.calls == []

    @_not_root
    @pytest.mark.parametrize("code, exists", [(TRANSCRIPT_MISSING, False), (2, True)])
    def test_exists_asks_the_broker_when_the_dir_is_locked(self, alice, code, exists):
        _write_transcript(alice.path, alice.workspace)
        alice.path.parent.chmod(0)
        alice.ops.turns = code
        try:
            assert (
                uid_cleanup.transcript_exists(
                    SID, user="alice", workspace=alice.workspace
                )
                is exists
            )
        finally:
            alice.path.parent.chmod(0o700)

    @_not_root
    def test_exists_without_a_broker_is_transcript_unreadable(self, alice, monkeypatch):
        monkeypatch.setattr(broker_ops, "available", lambda: False)
        _write_transcript(alice.path, alice.workspace)
        alice.path.parent.chmod(0)
        try:
            with pytest.raises(TranscriptUnreadable):
                uid_cleanup.transcript_exists(
                    SID, user="alice", workspace=alice.workspace
                )
        finally:
            alice.path.parent.chmod(0o700)

    def test_an_anonymous_run_without_a_lease_has_none(self, per_uid, wm):
        workspace = wm.resolve(None)
        assert sm._session_jsonl_path(SID, workspace) is None
        assert not uid_cleanup.transcript_exists(SID, user=None, workspace=workspace)
        assert (
            uid_cleanup.read_transcript_turns(SID, user=None, workspace=workspace)
            == TRANSCRIPT_MISSING
        )

    def test_create_clients_check_uses_the_sessions_layout(self, per_uid, wm):
        workspace, key, uid = _leased_workspace(wm)
        session = Session(session_id=SID, workspace=str(workspace))
        assert not sm._session_jsonl_exists(session)
        _write_transcript(sm._session_jsonl_path(SID, workspace), workspace)
        assert sm._session_jsonl_exists(session)


class TestTranscriptReadsOtherModes:
    @pytest.fixture
    def projects(self, monkeypatch, tmp_path, ops):
        _mode(monkeypatch, "off")
        root = tmp_path / "projects"
        monkeypatch.setattr(sm, "_PROJECTS_ROOT", root)
        return root

    def test_counts(self, projects, tmp_path):
        workspace = tmp_path / "ws"
        _write_transcript(sm._session_jsonl_path(SID, workspace), workspace, turns=2)
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=workspace) == 2
        )
        assert uid_cleanup.transcript_exists(SID, user="u", workspace=workspace)

    def test_missing(self, projects, tmp_path):
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=tmp_path)
            == TRANSCRIPT_MISSING
        )
        assert not uid_cleanup.transcript_exists(SID, user="u", workspace=tmp_path)

    def test_not_a_regular_file_is_missing(self, projects, tmp_path):
        workspace = tmp_path / "ws"
        path = sm._session_jsonl_path(SID, workspace)
        path.parent.mkdir(parents=True)
        os.mkfifo(path)  # never opened: it would block
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=workspace)
            == TRANSCRIPT_MISSING
        )
        assert not uid_cleanup.transcript_exists(SID, user="u", workspace=workspace)

    def test_invalid_utf8_is_corrupt(self, projects, tmp_path):
        workspace = tmp_path / "ws"
        path = sm._session_jsonl_path(SID, workspace)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"type": "user", "message": {"content": "\xff"}}\n')
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=workspace)
            == TRANSCRIPT_CORRUPT
        )

    def test_crlf_lines_are_read_as_lines(self, projects, tmp_path):
        workspace = tmp_path / "ws"
        path = sm._session_jsonl_path(SID, workspace)
        path.parent.mkdir(parents=True)
        line = json.dumps({"type": "user", "message": {"content": "hi"}})
        path.write_bytes((line + "\r\n" + line + "\r\n").encode())
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=workspace) == 2
        )

    @_not_root
    def test_permission_denied_is_transcript_unreadable_without_the_broker(
        self, projects, tmp_path, ops
    ):
        workspace = tmp_path / "ws"
        path = _write_transcript(sm._session_jsonl_path(SID, workspace), workspace)
        path.chmod(0)
        with pytest.raises(TranscriptUnreadable) as excinfo:
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=workspace)
        assert isinstance(excinfo.value.__cause__, PermissionError)
        path.parent.chmod(0)
        try:
            with pytest.raises(TranscriptUnreadable):
                uid_cleanup.transcript_exists(SID, user="u", workspace=workspace)
        finally:
            path.parent.chmod(0o700)
        assert ops.calls == []

    def test_an_io_error_is_unreadable(self, projects, tmp_path, monkeypatch):
        workspace = tmp_path / "ws"
        _write_transcript(sm._session_jsonl_path(SID, workspace), workspace)

        def broken_open(*_args, **_kwargs):
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(uid_cleanup, "_open_transcript", broken_open)
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=workspace)
            == TRANSCRIPT_UNREADABLE
        )


class TestStatelessTeardown:
    """/v1/agents/messages and the prompt try-run tear down in this order:
    transcript and session dirs, then the workspace."""

    def test_per_uid_everything_goes_with_the_lease_wipe(self, per_uid, wm):
        from src.routes.agent_messages import _cleanup_sdk_transcript

        workspace, key, uid = _leased_workspace(wm)
        config = cp.user_config_dir(uid)
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace), workspace
        )
        assert transcript.is_relative_to(config)
        env = _session_dir(config, "session-env")

        _cleanup_sdk_transcript(Session(session_id=SID, workspace=str(workspace)))
        assert transcript.is_file() and env.is_dir()  # the wipe's, not the app's
        assert per_uid.ops.calls == []

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)
        assert not transcript.exists() and not env.exists()
        assert not workspace.exists() and ui.leased_uid(key) is None

    def test_off_mode_removes_them_itself(self, monkeypatch, tmp_path, cfg, ops, wm):
        from src.routes.agent_messages import _cleanup_sdk_transcript

        _mode(monkeypatch, "off")
        workspace = wm.resolve(None)
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace), workspace
        )
        env = _session_dir(cfg, "session-env")

        _cleanup_sdk_transcript(Session(session_id=SID, workspace=str(workspace)))
        wm.cleanup_temp_workspace(workspace)

        assert not transcript.parent.exists() and not env.exists()
        assert not workspace.exists() and ops.calls == []

    def test_a_reclaim_never_raises(self, per_uid, wm, monkeypatch, caplog):
        workspace, key, uid = _leased_workspace(wm)

        def broken(_key):
            raise RuntimeError("pool unavailable")

        monkeypatch.setattr(ui, "leased_uid", broken)
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            wm.cleanup_temp_workspace(workspace)
        assert "Could not clean up temporary workspace" in caplog.text
        assert workspace.is_dir()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetentionSweep:
    @pytest.fixture
    def users(self, per_uid, monkeypatch):
        # The CLI expires its own state only with the user setting source.
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")
        alice = ui.resolve_uid("alice", persist=True)
        bob = ui.resolve_uid("bob", persist=True)
        config = cp.user_config_dir(alice)
        for rel in ("session-env", "tasks", "projects"):
            (config / rel).mkdir(parents=True)
        return SimpleNamespace(alice=alice, bob=bob, config=config, **vars(per_uid))

    def test_off_sweeps_nothing(self, monkeypatch, ops):
        _mode(monkeypatch, "off")
        assert uid_cleanup.retention_sweep() == 0 and ops.calls == []

    def test_each_named_config_dir_is_expired_as_its_uid(self, users):
        assert uid_cleanup.retention_sweep() == 1  # bob has no config dir yet
        ((uid, kwargs),) = users.ops.calls
        assert uid == users.alice
        assert kwargs == {
            "expire": {
                "older_than": 30 * 86400,
                "units": [
                    str(users.config / "session-env"),
                    str(users.config / "tasks"),
                ],
                "nested": [str(users.config / "projects")],
            }
        }

    def test_only_existing_dirs_are_named(self, users):
        shutil.rmtree(users.config / "tasks")
        shutil.rmtree(users.config / "projects")
        uid_cleanup.retention_sweep()
        expire = users.ops.calls[0][1]["expire"]
        assert expire["units"] == [str(users.config / "session-env")]
        assert expire["nested"] == []

    @pytest.mark.parametrize(
        "settings, days",
        [
            ({"cleanupPeriodDays": 7}, 7),
            ({"cleanupPeriodDays": 0}, 30),
            ({"cleanupPeriodDays": "7"}, 30),
            ({"cleanupPeriodDays": True}, 30),
            ({}, 30),
            ([], 30),
            (None, 30),
        ],
    )
    def test_the_period_comes_from_the_shared_settings(self, users, settings, days):
        if settings is not None:
            (users.cfg / "settings.json").write_text(json.dumps(settings))
        uid_cleanup.retention_sweep()
        assert users.ops.calls[0][1]["expire"]["older_than"] == days * 86400

    def test_failures_are_logged_and_not_counted(self, users, caplog):
        users.ops.fail = 1
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            assert uid_cleanup.retention_sweep() == 0
        assert "Retention sweep of uid" in caplog.text
        users.ops.result = False
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            assert uid_cleanup.retention_sweep() == 0
        assert "left expired entries" in caplog.text


class TestRetentionNeedsTheUserSettingSource:
    """The CLI runs its own cleanupPeriodDays expiry only with the ``user``
    setting source; without it nothing ever expired these, so the gateway does
    not either (it would start deleting transcripts nothing deleted before)."""

    @pytest.fixture
    def alice(self, per_uid):
        uid = ui.resolve_uid("alice", persist=True)
        (cp.user_config_dir(uid) / "projects").mkdir(parents=True)
        return per_uid

    @pytest.mark.parametrize("sources", [None, "project,local", "local"])
    def test_without_it_nothing_is_swept(self, alice, monkeypatch, sources):
        if sources is None:
            monkeypatch.delenv("CLAUDE_SETTING_SOURCES", raising=False)
        else:
            monkeypatch.setenv("CLAUDE_SETTING_SOURCES", sources)
        assert uid_cleanup.retention_sweep() == 0
        assert alice.ops.calls == []

    def test_with_it_the_sweep_runs(self, alice, monkeypatch):
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user")
        assert uid_cleanup.retention_sweep() == 1


# ---------------------------------------------------------------------------
# The reclaimer pool and parked jobs
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestReclaimerPool:
    def test_one_blocked_job_does_not_hold_the_others(self, reclaimer):
        gate, ran = threading.Event(), threading.Event()
        reclaimer.submit("slow", lambda: gate.wait(5))
        reclaimer.submit("fast", lambda: ran.set() or True)
        assert ran.wait(5)  # while "slow" still blocks
        assert reclaimer.is_pending("slow")
        gate.set()
        assert reclaimer.drain(5)

    def test_at_most_workers_jobs_run_at_once(self):
        instance = uid_cleanup.Reclaimer(backoff=FAST, workers=2)
        gate, lock, started = threading.Event(), threading.Lock(), []

        def job(n):
            with lock:
                started.append(n)
            return gate.wait(5)

        try:
            for n in range(4):
                instance.submit(n, lambda n=n: job(n))
            assert _wait_for(lambda: len(started) == 2)
            time.sleep(0.1)
            assert len(started) == 2
            gate.set()
            assert instance.drain(5)
            assert sorted(started) == [0, 1, 2, 3]
        finally:
            instance.stop()

    def test_a_key_never_runs_twice_at_once(self, reclaimer):
        gate, runs = threading.Event(), []

        def job():
            runs.append(1)
            return gate.wait(5)

        reclaimer.submit("k", job)
        assert _wait_for(lambda: runs == [1])
        assert not reclaimer.submit("k", job)  # running: nothing queued
        gate.set()
        assert reclaimer.drain(5)
        assert runs == [1]

    def test_stop_joins_every_worker(self):
        instance = uid_cleanup.Reclaimer(backoff=FAST, workers=3)
        instance.submit("k", lambda: True)
        assert instance.drain(5)
        instance.stop()
        assert len(instance._threads) == 3
        assert not any(thread.is_alive() for thread in instance._threads)


class TestParkedJobs:
    @pytest.fixture
    def parked(self):
        instance = uid_cleanup.Reclaimer(backoff=(0.01,), parked_retry=60)
        yield instance
        instance.stop()

    def test_past_its_backoff_a_kept_job_is_parked_not_dropped(self, parked):
        calls = []
        parked.submit("k", lambda: calls.append(1) and False, keep=lambda: True)
        assert parked.drain(5)  # a parked job is not waited for
        assert len(calls) == 2  # the first try and one backoff retry
        assert parked.is_pending("k") and parked.parked() == 1

    def test_submitting_it_again_runs_it_now(self, parked):
        results, calls = iter([False, False, True]), []
        parked.submit("k", lambda: calls.append(1) or next(results), keep=lambda: True)
        assert parked.drain(5) and parked.parked() == 1
        assert not parked.submit("k", lambda: pytest.fail("a second job"))
        assert _wait_for(lambda: not parked.is_pending("k"))
        assert len(calls) == 3

    @pytest.mark.parametrize("keep", [lambda: False, lambda: 1 / 0])
    def test_it_is_given_up_once_keep_says_no(self, parked, keep, caplog):
        with caplog.at_level(logging.WARNING, logger=uid_cleanup.__name__):
            parked.submit("k", lambda: False, keep=keep)
            assert parked.drain(5)
        assert not parked.is_pending("k")
        assert "Gave up on uid cleanup job k after" in caplog.text


# ---------------------------------------------------------------------------
# SysV IPC and POSIX message queue leftovers
# ---------------------------------------------------------------------------

_SHM_HEADER = (
    "       key      shmid perms                  size  cpid  lpid nattch   uid"
    "   gid  cuid  cgid      atime      dtime      ctime                   rss"
    "                  swap\n"
)
_SEM_HEADER = (
    "       key      semid perms      nsems   uid   gid  cuid  cgid      otime"
    "      ctime\n"
)
_MSG_HEADER = (
    "       key      msqid perms      cbytes       qnum lspid lrpid   uid   gid"
    "  cuid  cgid      stime      rtime      ctime\n"
)


def _shm_row(ident: int, uid: int, cuid: int, perms: str = "600") -> str:
    return (
        f"  0 {ident} {perms} 4096 1 1 0 {uid} {uid} {cuid} {cuid} 0 0 0 4096 0\n"
    )


def _sem_row(ident: int, uid: int, cuid: int) -> str:
    return f"  0 {ident} 600 1 {uid} {uid} {cuid} {cuid} 0 0\n"


def _msg_row(ident: int, uid: int, cuid: int) -> str:
    return f"  0 {ident} 600 0 0 0 0 {uid} {uid} {cuid} {cuid} 0 0 0\n"


class TestIpcLeftovers:
    UID = 29750

    @pytest.fixture
    def tables(self, monkeypatch, tmp_path):
        sysv, mqueue = tmp_path / "sysvipc", tmp_path / "mqueue"
        sysv.mkdir()
        mqueue.mkdir()
        monkeypatch.setattr(uid_cleanup, "_SYSVIPC_DIR", str(sysv))
        monkeypatch.setattr(uid_cleanup, "_MQUEUE_DIR", str(mqueue))
        return SimpleNamespace(sysv=sysv, mqueue=mqueue)

    def test_objects_the_uid_created(self, tables):
        uid, other = self.UID, 1000
        (tables.sysv / "shm").write_text(
            _SHM_HEADER
            + _shm_row(11, uid, uid)
            + _shm_row(12, other, uid)  # made by the uid, handed to another
            + _shm_row(13, other, other)
            + _shm_row(14, uid, uid, perms="2600")  # SHM_LOCKED: still there
        )
        (tables.sysv / "sem").write_text(
            _SEM_HEADER + _sem_row(21, uid, uid) + _sem_row(22, other, other)
        )
        (tables.sysv / "msg").write_text(_MSG_HEADER + _msg_row(31, other, uid))
        assert uid_cleanup.ipc_leftovers(uid) == [
            "SysV shm 11",
            "SysV shm 12",
            "SysV shm 14",
            "SysV sem 21",
            "SysV msg 31",
        ]
        assert uid_cleanup.ipc_leftovers(uid + 1) == []

    def test_an_object_handed_to_the_uid_is_not_its_leftover(self, tables):
        # IPC_SET lets an object's creator make any uid its owner: another
        # session could otherwise keep the uid's lease from being released.
        uid, other = self.UID, 29751
        (tables.sysv / "shm").write_text(_SHM_HEADER + _shm_row(11, uid, other))
        (tables.sysv / "sem").write_text(_SEM_HEADER + _sem_row(21, uid, other))
        (tables.sysv / "msg").write_text(_MSG_HEADER + _msg_row(31, uid, other))
        assert uid_cleanup.ipc_leftovers(uid) == []
        assert uid_cleanup.ipc_leftovers(other) == [
            "SysV shm 11",
            "SysV sem 21",
            "SysV msg 31",
        ]

    def test_a_destroyed_segment_is_gone(self, tables):
        # IPC_RMID on a segment another process still has attached marks it
        # SHM_DEST (perms 01000) and takes its key; the row stays until the
        # last detach, which another uid may never do.
        uid = self.UID
        (tables.sysv / "shm").write_text(
            _SHM_HEADER
            + _shm_row(11, uid, uid, perms="1600")
            + _shm_row(12, 29751, uid, perms="3666")
        )
        assert uid_cleanup.ipc_leftovers(uid) == []

    def test_a_live_segment_reads_as_destroyed_after_rmid(self):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.shmat.restype = ctypes.c_void_p
        if not os.path.isfile("/proc/sysvipc/shm"):
            pytest.skip("no SysV IPC here")
        ident = libc.shmget(0, ctypes.c_size_t(4096), 0o1000 | 0o600)  # private
        if ident < 0:
            pytest.skip("cannot make a SysV segment here")
        name = f"SysV shm {ident}"
        attached, removed = None, False
        try:
            assert name in uid_cleanup.ipc_leftovers(os.getuid())
            attached = libc.shmat(ident, None, 0)
            assert attached not in (None, ctypes.c_void_p(-1).value)
            removed = libc.shmctl(ident, 0, None) == 0  # IPC_RMID, still attached
            assert removed
            assert name not in uid_cleanup.ipc_leftovers(os.getuid())
        finally:
            if attached not in (None, ctypes.c_void_p(-1).value):
                libc.shmdt(ctypes.c_void_p(attached))
            if not removed:
                libc.shmctl(ident, 0, None)

    def test_empty_tables_and_no_mount_are_nothing(self, tables, monkeypatch):
        (tables.sysv / "shm").write_text(_SHM_HEADER)
        monkeypatch.setattr(uid_cleanup, "_MQUEUE_DIR", str(tables.mqueue / "none"))
        assert uid_cleanup.ipc_leftovers(self.UID) == []

    def test_no_tables_at_all_are_nothing(self, tables, monkeypatch, tmp_path):
        monkeypatch.setattr(uid_cleanup, "_SYSVIPC_DIR", str(tmp_path / "none"))
        assert uid_cleanup.ipc_leftovers(self.UID) == []

    def test_a_header_without_the_columns_fails_closed(self, tables):
        (tables.sysv / "sem").write_text("key semid perms\n0 21 600\n")
        assert uid_cleanup.ipc_leftovers(self.UID) == [
            str(tables.sysv / "sem") + " (unrecognized header)"
        ]

    def test_an_unparsable_line_fails_closed(self, tables):
        (tables.sysv / "msg").write_text(_MSG_HEADER + "  0 31 600 x\n")
        assert uid_cleanup.ipc_leftovers(self.UID) == [
            str(tables.sysv / "msg") + " (unparsable line)"
        ]

    @_not_root
    def test_an_unreadable_table_fails_closed(self, tables):
        table = tables.sysv / "shm"
        table.write_text(_SHM_HEADER)
        table.chmod(0)
        try:
            assert uid_cleanup.ipc_leftovers(self.UID) == [str(table)]
        finally:
            table.chmod(0o600)

    def test_message_queues_the_uid_owns(self, tables):
        (tables.mqueue / "q1").write_text("")
        assert uid_cleanup.ipc_leftovers(os.getuid()) == [str(tables.mqueue / "q1")]
        assert uid_cleanup.ipc_leftovers(os.getuid() + 1) == []

    def test_this_kernels_tables_parse(self):
        # The real /proc/sysvipc (when there is one) and /dev/mqueue: a uid
        # with no objects must come back empty, not as a parse failure.
        assert uid_cleanup.ipc_leftovers(29999) == []

    def test_they_keep_the_lease(self, per_uid, wm, monkeypatch, tmp_path):
        workspace, key, uid = _leased_workspace(wm)
        sysv = tmp_path / "sysvipc-live"
        sysv.mkdir()
        (sysv / "shm").write_text(_SHM_HEADER + _shm_row(11, uid, uid))
        monkeypatch.setattr(uid_cleanup, "_SYSVIPC_DIR", str(sysv))
        per_uid.ops.result = False  # the broker could not remove the segment

        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)
        assert ui.leased_uid(key) == uid and workspace.is_dir()

        (sysv / "shm").write_text(_SHM_HEADER)  # gone now
        wm.cleanup_temp_workspace(workspace)
        assert per_uid.reclaimer.drain(5)
        assert ui.leased_uid(key) is None and not workspace.exists()


# ---------------------------------------------------------------------------
# Bounded transcript reads, and transient broker failures
# ---------------------------------------------------------------------------


class TestBoundedTranscriptReads:
    """A session uid may write its own transcript: the gateway never reads one
    into memory unbounded."""

    @pytest.fixture
    def path(self, monkeypatch, tmp_path, ops):
        _mode(monkeypatch, "off")
        monkeypatch.setattr(sm, "_PROJECTS_ROOT", tmp_path / "projects")
        path = sm._session_jsonl_path(SID, tmp_path / "ws")
        path.parent.mkdir(parents=True)
        return path

    def test_a_file_over_the_size_cap_is_unreadable_not_read(
        self, path, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(transcript_turns, "MAX_TRANSCRIPT_BYTES", 1024)
        with open(path, "wb") as sparse:  # one endless "line", no newline
            sparse.truncate(1 << 20)
        with pytest.raises(TranscriptUnreadable) as excinfo:
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=tmp_path / "ws")
        assert "more than the 1024" in str(excinfo.value.__cause__)
        # The route's 500, not a 404 "not found or expired".
        with pytest.raises(TranscriptUnreadable):
            sm._try_rehydrate_from_jsonl(SID, user="u", cwd=str(tmp_path / "ws"))

    def test_a_line_over_the_line_cap_is_corrupt(self, path, tmp_path, monkeypatch):
        monkeypatch.setattr(transcript_turns, "MAX_LINE_CHARS", 64)
        line = json.dumps({"type": "user", "message": {"content": "x" * 200}})
        path.write_text(line + "\n")
        read = []
        real = transcript_turns.bounded_lines

        def spy(fh, max_line):
            read.append(max_line)
            return real(fh, max_line)

        monkeypatch.setattr(transcript_turns, "bounded_lines", spy)
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=tmp_path / "ws")
            == TRANSCRIPT_CORRUPT
        )
        assert read == [64]

    def test_lines_within_the_caps_count(self, path, tmp_path, monkeypatch):
        monkeypatch.setattr(transcript_turns, "MAX_LINE_CHARS", 4096)
        monkeypatch.setattr(transcript_turns, "MAX_TRANSCRIPT_BYTES", 4096)
        line = json.dumps({"type": "user", "message": {"content": "hi"}})
        path.write_text(line + "\n" + line + "\n")
        assert (
            uid_cleanup.read_transcript_turns(SID, user="u", workspace=tmp_path / "ws")
            == 2
        )


class TestTransientBrokerFailures:
    @pytest.fixture
    def alice(self, per_uid):
        ui.resolve_uid("alice", persist=True)
        workspace = "/ws/alice/claude"
        path = sm._session_jsonl_path(SID, workspace, user="alice")
        return SimpleNamespace(workspace=workspace, path=path, **vars(per_uid))

    @_not_root
    def test_a_transient_failure_is_temporarily_unavailable(self, alice, caplog):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.fail, alice.ops.transient = 1, True
        manager = SessionManager()
        with caplog.at_level(logging.WARNING, logger=sm.__name__):
            with pytest.raises(TranscriptTemporarilyUnavailable) as excinfo:
                manager.get_session(SID, user="alice", cwd=alice.workspace)
        assert excinfo.value.path == alice.path
        assert manager.stats()["rehydrate_misses"] == 1
        assert "a retry may succeed" in caplog.text
        # It cleared: the next attempt rehydrates.
        alice.ops.turns = 1
        assert manager.get_session(SID, user="alice", cwd=alice.workspace)

    @_not_root
    def test_a_refusal_stays_unreadable(self, alice):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.fail = 1
        with pytest.raises(TranscriptUnreadable):
            uid_cleanup.read_transcript_turns(
                SID, user="alice", workspace=alice.workspace
            )

    @_not_root
    def test_the_brokers_unreadable_is_unreadable_not_missing(self, alice):
        _write_transcript(alice.path, alice.workspace).chmod(0)
        alice.ops.turns = TRANSCRIPT_UNREADABLE
        with pytest.raises(TranscriptUnreadable) as excinfo:
            sm._try_rehydrate_from_jsonl(SID, user="alice", cwd=alice.workspace)
        assert "either" in str(excinfo.value.__cause__)

    @_not_root
    def test_the_existence_check_tells_them_apart(self, alice):
        _write_transcript(alice.path, alice.workspace)
        alice.path.parent.chmod(0)
        try:
            alice.ops.fail, alice.ops.transient = 1, True
            with pytest.raises(TranscriptTemporarilyUnavailable):
                uid_cleanup.transcript_exists(
                    SID, user="alice", workspace=alice.workspace
                )
            alice.ops.fail, alice.ops.transient = 1, False
            with pytest.raises(TranscriptUnreadable):
                uid_cleanup.transcript_exists(
                    SID, user="alice", workspace=alice.workspace
                )
        finally:
            alice.path.parent.chmod(0o700)


class TestRouteAnswers:
    def test_temporarily_unavailable_is_a_retryable_503(
        self, monkeypatch, tmp_path, isolated_session_manager
    ):
        from src.constants import DEFAULT_MODEL
        from tests.test_session_dirs_lifecycle import _responses_client

        def unavailable(session_id, *, user, workspace):
            raise TranscriptTemporarilyUnavailable(tmp_path / f"{session_id}.jsonl")

        monkeypatch.setattr(uid_cleanup, "read_transcript_turns", unavailable)
        workspace = tmp_path / "workspaces" / "alice" / "claude"
        workspace.mkdir(parents=True)
        with _responses_client(workspace) as client:
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "and then?",
                    "user": "alice",
                    "previous_response_id": f"resp_{SID}_1",
                },
            )
        assert resp.status_code == 503
        assert "x-should-retry" not in resp.headers
        assert "retry shortly" in resp.json()["error"]["message"]
        assert SID not in isolated_session_manager.sessions

    def test_unreadable_while_starting_a_client_is_the_same_500(self):
        from src.routes import responses

        exc = responses._backend_unavailable_error(
            "claude", TranscriptUnreadable(Path("/cfg/projects/-ws/s.jsonl"))
        )
        assert exc.status_code == 500
        assert exc.headers["x-should-retry"] == "false"
        assert exc.detail["error"]["code"] == "transcript_unreadable"
        assert "/cfg" not in exc.detail["error"]["message"]

    def test_temporarily_unavailable_while_starting_a_client_is_a_503(self):
        from src.routes import responses

        exc = responses._backend_unavailable_error(
            "claude", TranscriptTemporarilyUnavailable(Path("/x.jsonl"))
        )
        assert exc.status_code == 503 and not exc.headers


# ---------------------------------------------------------------------------
# Rehydrate outside the manager's lock and off the event loop
# ---------------------------------------------------------------------------


class TestRehydrateOutsideTheLock:
    """A rehydrate's transcript read can be a broker round trip (uid mode):
    it used to run under ``SessionManager.lock`` and on the event loop."""

    @staticmethod
    def _install(monkeypatch, read):
        monkeypatch.setattr(uid_cleanup, "read_transcript_turns", read)

    def test_the_sync_read_does_not_hold_the_lock(self, monkeypatch):
        manager, free = SessionManager(), []

        def read(session_id, *, user, workspace):
            free.append(manager.lock.acquire(blocking=False))
            if free[-1]:
                manager.lock.release()
            return 2

        self._install(monkeypatch, read)
        session = manager.get_session(SID, user="alice", cwd="/ws/a")
        assert free == [True]
        assert session.turn_counter == 2 and manager.sessions[SID] is session
        assert manager.stats()["rehydrate_hits"] == 1

    async def test_the_async_read_runs_off_the_event_loop(self, monkeypatch):
        manager = SessionManager()
        started, release = threading.Event(), threading.Event()
        threads = []

        def read(session_id, *, user, workspace):
            threads.append(threading.current_thread())
            started.set()
            release.wait(5)
            return 1

        self._install(monkeypatch, read)
        task = asyncio.create_task(
            manager.get_session_async(SID, user="alice", cwd="/ws/a")
        )
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(3):  # the loop keeps running meanwhile
            await asyncio.sleep(0.01)
        assert not task.done()
        assert manager.lock.acquire(blocking=False)  # and the lock is free
        manager.lock.release()
        manager.get_or_create_session(OTHER_SID)  # other sessions work
        release.set()
        session = await asyncio.wait_for(task, 5)
        assert session.turn_counter == 1 and manager.sessions[SID] is session
        assert threads and threads[0] is not threading.main_thread()

    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_a_live_session_that_appeared_meanwhile_wins(
        self, monkeypatch, asynchronous
    ):
        manager, live = SessionManager(), []

        def read(session_id, *, user, workspace):
            live.append(manager.get_or_create_session(SID))  # a concurrent one
            return 3

        self._install(monkeypatch, read)
        if asynchronous:
            found = await manager.get_session_async(SID, user="alice", cwd="/ws/a")
        else:
            found = manager.get_session(SID, user="alice", cwd="/ws/a")
        assert found is live[0] and manager.sessions[SID] is live[0]
        assert found.turn_counter == 0  # the live one, not the read
        assert manager.stats()["rehydrate_hits"] == 0

    @pytest.mark.parametrize(
        "error", [TranscriptUnreadable, TranscriptTemporarilyUnavailable]
    )
    async def test_a_failed_read_counts_a_miss(self, monkeypatch, error):
        manager = SessionManager()

        def read(session_id, *, user, workspace):
            raise error(Path("/x.jsonl"))

        self._install(monkeypatch, read)
        with pytest.raises(error):
            await manager.get_session_async(SID, user="alice", cwd="/ws/a")
        assert manager.stats()["rehydrate_misses"] == 1
        assert SID not in manager.sessions

    async def test_admission_is_checked_after_the_read(self, monkeypatch):
        manager = SessionManager()
        manager.get_or_create_session(OTHER_SID)
        self._install(monkeypatch, lambda session_id, *, user, workspace: 1)
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            with pytest.raises(SessionLimitExceeded):
                await manager.get_session_async(SID, user="alice", cwd="/ws/a")
        assert SID not in manager.sessions

    async def test_a_turn_the_transcript_cannot_serve_is_a_miss(self, monkeypatch):
        manager = SessionManager()
        self._install(monkeypatch, lambda session_id, *, user, workspace: 1)
        assert (
            await manager.get_session_async(SID, user="alice", cwd="/ws/a", max_turn=2)
            is None
        )
        assert manager.stats()["rehydrate_misses"] == 1

    async def test_no_context_is_a_plain_cache_lookup(self, monkeypatch):
        manager = SessionManager()
        self._install(monkeypatch, lambda *a, **k: pytest.fail("read without context"))
        assert await manager.get_session_async(SID) is None
        assert await manager.get_session_async(SID, user="alice") is None
        live = manager.get_or_create_session(SID)
        assert await manager.get_session_async(SID, user="alice", cwd="/w") is live
        assert manager.stats()["rehydrate_misses"] == 0


# ---------------------------------------------------------------------------
# A transcript under a project dir the gateway cannot enter
# ---------------------------------------------------------------------------


class TestLockedProjectDir:
    @_not_root
    def test_removal_goes_to_the_broker(self, per_uid):
        uid = ui.resolve_uid("alice", persist=True)
        workspace = "/ws/alice/claude"
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace, user="alice"), workspace
        )
        project = transcript.parent
        project.chmod(0)  # the uid's own 0700 dir: the gateway cannot even look
        try:
            uid_cleanup.remove_transcript(SID, user="alice", workspace=workspace)
            assert per_uid.reclaimer.drain(5)
        finally:
            if project.exists():
                project.chmod(0o700)
        ((called, kwargs),) = per_uid.ops.calls
        assert called == uid
        assert kwargs == {"remove": [str(transcript), str(project / SID)]}
        assert not transcript.exists()

    @_not_root
    def test_the_brokers_word_is_taken_where_the_gateway_cannot_check(self, per_uid):
        ui.resolve_uid("alice", persist=True)
        workspace = "/ws/alice/claude"
        transcript = _write_transcript(
            sm._session_jsonl_path(SID, workspace, user="alice"), workspace
        )
        project = transcript.parent
        per_uid.ops.effect = None  # it "removed" them; the gateway cannot see
        project.chmod(0)
        try:
            uid_cleanup.remove_transcript(SID, user="alice", workspace=workspace)
            assert per_uid.reclaimer.drain(5)
        finally:
            project.chmod(0o700)
        assert len(per_uid.ops.calls) == 1  # done, not retried
