"""Blue/green plugin replay (``docs/zero-downtime-deploy.md`` §5.4 items 1-4).

* the replay-wide ``flock`` (startup replay and admin paths, across processes);
* the replay digest: an unchanged boot skips the replay, every kind of input
  change replays, an admin change invalidates it;
* replaced ``<dest>.old-*`` trees survive while another instance is alive;
* anonymous ``_tmp_`` workspaces carry the instance and the startup sweep is
  scoped to it while another instance is alive.

A "live instance" is a child process holding ``<GATEWAY_INSTANCE_DIR>/<name>.lock``
with ``flock``, exactly as ``instance_registry.register()`` does.
"""

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from src import instance_registry, plugin_local_copy as lc
from src.workspace_manager import WorkspaceManager

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "install_plugins.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_plugins_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Holder:
    """A child process holding ``flock(LOCK_EX)`` on *path* until stopped."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        code = textwrap.dedent("""
            import fcntl, os, sys
            fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
            print("ready", flush=True)
            sys.stdin.read()
            """)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout.readline().strip() == "ready"

    def stop(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)


@pytest.fixture
def instances(tmp_path, monkeypatch):
    """An instances dir of our own; yields ``start(name)`` -> holder."""
    directory = tmp_path / "instances"
    directory.mkdir()
    monkeypatch.setenv("GATEWAY_INSTANCE_DIR", str(directory))
    monkeypatch.delenv("GATEWAY_INSTANCE", raising=False)
    holders = []

    def start(name):
        holder = _Holder(directory / f"{name}.lock")
        holders.append(holder)
        return holder

    yield start
    for holder in holders:
        holder.stop()


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def _fake_claude(bin_dir: Path) -> None:
    """Logs args; ``--version`` prints ``$FAKE_CLAUDE_VERSION``; FAILPLUGIN fails;
    sleeps ``$FAKE_CLAUDE_SLEEP`` and logs start/end with its parent pid."""
    fake = bin_dir / "claude"
    fake.write_text("""#!/bin/sh
printf 'start %s %s\\n' "$PPID" "$*" >> "$FAKE_CLAUDE_LOG"
if [ -n "${FAKE_CLAUDE_SLEEP:-}" ]; then sleep "$FAKE_CLAUDE_SLEEP"; fi
printf 'end %s\\n' "$PPID" >> "$FAKE_CLAUDE_LOG"
case "$*" in
    --version) printf '%s\\n' "${FAKE_CLAUDE_VERSION:-}"; exit 0 ;;
    *FAILPLUGIN*) exit 1 ;;
esac
exit 0
""")
    fake.chmod(0o755)


def _fake_git(bin_dir: Path) -> None:
    """``ls-remote`` prints ``$FAKE_LS_REMOTE`` (fails when empty); ``clone``
    creates the dest."""
    fake = bin_dir / "git"
    fake.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
if [ "${1:-}" = "ls-remote" ]; then
    [ -n "${FAKE_LS_REMOTE:-}" ] || exit 2
    printf '%s\\n' "$FAKE_LS_REMOTE"
    exit 0
fi
if [ "${1:-}" = "clone" ]; then
    for a in "$@"; do dest="$a"; done
    mkdir -p "$dest/.claude-plugin"
fi
exit 0
""")
    fake.chmod(0o755)


def _local_marketplace(repo: Path) -> Path:
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "local", "owner": {"name": "T"}, "plugins": []})
    )
    return repo


class _Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_claude(bin_dir)
        _fake_git(bin_dir)
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.manifest = self.data / "gateway-plugins.json"
        self.home = tmp_path / "home"
        self.claude_log = tmp_path / "claude.log"
        self.git_log = tmp_path / "git.log"
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")},
            "HOME": str(self.home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CLAUDE_PLUGIN_MANIFEST": str(self.manifest),
            "FAKE_CLAUDE_LOG": str(self.claude_log),
            "FAKE_GIT_LOG": str(self.git_log),
            "FAKE_CLAUDE_VERSION": "2.1.283 (Claude Code)",
            "FAKE_LS_REMOTE": "a" * 40 + "\trefs/heads/main",
            "GATEWAY_INSTANCE_DIR": str(tmp_path / "instances"),
        }
        self.env.pop("GATEWAY_INSTANCE", None)

    def write_manifest(self, added, removed=()):
        self.manifest.write_text(
            json.dumps({"version": 1, "added": added, "removed": list(removed)})
        )

    def run(self, **extra):
        for log in (self.claude_log, self.git_log):
            if log.exists():
                log.unlink()
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env={**self.env, **extra},
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return proc

    def claude_calls(self):
        if not self.claude_log.exists():
            return []
        return [
            line.split(" ", 2)[2]
            for line in self.claude_log.read_text().splitlines()
            if line.startswith("start ")
        ]

    def git_calls(self):
        return self.git_log.read_text().splitlines() if self.git_log.exists() else []

    @property
    def digest(self) -> Path:
        return self.data / lc.REPLAY_DIGEST_NAME


@pytest.fixture
def env(tmp_path):
    return _Env(tmp_path)


def _remote(repo="https://example/acme/mkt.git", name="foo", **extra):
    return {"repo": repo, "name": name, "marketplace": "acme", **extra}


# ---------------------------------------------------------------------------
# 1. replay-wide lock
# ---------------------------------------------------------------------------


def test_two_replays_never_interleave_across_processes(env, tmp_path):
    """Two installers on one config dir: every claude call of one replay
    finishes before the other's first starts."""
    repo = _local_marketplace(tmp_path / "repo")
    env.write_manifest(
        [{"repo": str(repo), "name": "p1"}, {"repo": str(repo), "name": "p2"}]
    )
    run_env = {**env.env, "FAKE_CLAUDE_SLEEP": "0.2", "FAKE_CLAUDE_VERSION": ""}
    procs = [
        subprocess.Popen([sys.executable, str(SCRIPT)], env=run_env) for _ in range(2)
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    lines = env.claude_log.read_text().splitlines()
    owners = [line.split()[1] for line in lines]
    assert len(set(owners)) == 2
    # One contiguous block per installer process.
    switches = sum(1 for a, b in zip(owners, owners[1:]) if a != b)
    assert switches == 1, lines
    assert (env.home / ".claude" / lc.REPLAY_LOCK_NAME).exists()


def test_replay_waits_for_a_lock_held_by_another_process(env, tmp_path):
    repo = _local_marketplace(tmp_path / "repo")
    env.write_manifest([{"repo": str(repo), "name": "p1"}])
    holder = _Holder(env.home / ".claude" / lc.REPLAY_LOCK_NAME)
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT)], env=env.env, stderr=subprocess.PIPE
    )
    try:
        time.sleep(1.0)
        assert proc.poll() is None  # blocked on the lock
        assert "plugin install p1" not in "\n".join(env.claude_calls())
    finally:
        holder.stop()
    assert proc.wait(timeout=60) == 0
    assert any(c.startswith("plugin install p1") for c in env.claude_calls())


def test_replay_lock_is_reentrant_and_blocks_other_processes(tmp_path):
    cfg = tmp_path / "cfg"
    with lc.replay_lock(cfg):
        with lc.replay_lock(cfg):  # nested: an admin install adding its marketplace
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import fcntl,os,sys;fd=os.open(sys.argv[1],os.O_RDWR);"
                    "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                    str(cfg / lc.REPLAY_LOCK_NAME),
                ],
                capture_output=True,
            )
            assert probe.returncode != 0  # held
        assert lc._replay_depth == 1
    assert lc._replay_depth == 0 and lc._replay_fd is None
    fd = os.open(str(cfg / lc.REPLAY_LOCK_NAME), os.O_RDWR)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released
    finally:
        os.close(fd)


def test_claude_config_dir_matches_the_app_rule(monkeypatch, tmp_path):
    from src import claude_config_paths

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert lc.claude_config_dir() == claude_config_paths.config_dir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "x"))
    assert lc.claude_config_dir() == claude_config_paths.config_dir()


# ---------------------------------------------------------------------------
# 2. digest skip
# ---------------------------------------------------------------------------


def _assert_skipped(env, proc):
    assert "inputs unchanged" in proc.stderr
    assert [c for c in env.claude_calls() if c != "--version"] == []
    assert all(c.startswith("ls-remote") for c in env.git_calls())


def _assert_replayed(env, proc):
    assert "inputs unchanged" not in proc.stderr
    assert any(c.startswith("plugin install") for c in env.claude_calls())


def test_unchanged_inputs_skip_the_replay(env):
    env.write_manifest([_remote()])
    first = env.run()
    _assert_replayed(env, first)
    assert any(c.startswith("clone") for c in env.git_calls())
    assert env.digest.is_file()
    second = env.run()
    _assert_skipped(env, second)
    assert not any(c.startswith("clone") for c in env.git_calls())


def test_remote_commit_change_replays(env):
    env.write_manifest([_remote()])
    env.run()
    proc = env.run(FAKE_LS_REMOTE="b" * 40 + "\trefs/heads/main")
    _assert_replayed(env, proc)
    _assert_skipped(env, env.run(FAKE_LS_REMOTE="b" * 40 + "\trefs/heads/main"))


def test_manifest_change_replays(env):
    env.write_manifest([_remote()])
    env.run()
    env.write_manifest([_remote(), _remote(name="bar")])
    _assert_replayed(env, env.run())
    env.write_manifest(
        [_remote(), _remote(name="bar")], removed=[{"spec": "x", "scope": "user"}]
    )
    _assert_replayed(env, env.run())


def test_cli_version_change_replays(env):
    env.write_manifest([_remote()])
    env.run()
    _assert_replayed(env, env.run(FAKE_CLAUDE_VERSION="2.1.300 (Claude Code)"))


def test_local_source_change_replays(env, tmp_path):
    repo = _local_marketplace(tmp_path / "repo")
    env.write_manifest([{"repo": str(repo), "name": "p1"}])
    env.run()
    _assert_skipped(env, env.run())
    (repo / "plugins").mkdir()
    (repo / "plugins" / "SKILL.md").write_text("new")
    _assert_replayed(env, env.run())
    _assert_skipped(env, env.run())
    # Same size, new content: the mtime changes.
    target = repo / "plugins" / "SKILL.md"
    target.write_text("NEW")
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    _assert_replayed(env, env.run())


def test_registry_change_replays(env):
    """A wiped/rewritten plugin registry (``docker compose down -v`` on the
    claude_home volume while data/ survives) replays."""
    env.write_manifest([_remote()])
    plugins = env.home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    installed = plugins / "installed_plugins.json"
    installed.write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "foo@acme": [
                        {
                            "scope": "user",
                            "version": "1",
                            "installPath": str(plugins),
                            "lastUpdated": "t1",
                        }
                    ]
                },
            }
        )
    )
    env.run()
    # Timestamps alone do not count.
    data = json.loads(installed.read_text())
    data["plugins"]["foo@acme"][0]["lastUpdated"] = "t2"
    installed.write_text(json.dumps(data))
    _assert_skipped(env, env.run())
    installed.unlink()
    _assert_replayed(env, env.run())


def test_failed_replay_writes_no_digest(env):
    env.write_manifest([_remote(name="FAILPLUGIN")])
    env.run()
    assert not env.digest.exists()
    _assert_replayed(env, env.run())


def test_digest_failures_fail_open(env, tmp_path):
    env.write_manifest([_remote()])
    _assert_replayed(env, env.run(FAKE_LS_REMOTE=""))  # ls-remote fails
    assert not env.digest.exists()
    _assert_replayed(env, env.run(FAKE_CLAUDE_VERSION=""))  # no version
    assert not env.digest.exists()
    env.run()
    assert env.digest.is_file()
    _assert_replayed(env, env.run(FAKE_LS_REMOTE=""))
    assert not env.digest.exists()  # removed before a replay that cannot record


def test_oversized_local_tree_replays(env, tmp_path, monkeypatch):
    repo = _local_marketplace(tmp_path / "repo")
    monkeypatch.setattr(installer, "_TREE_MAX_ENTRIES", 1)
    with pytest.raises(OSError):
        installer._tree_material(str(repo))


def test_tree_digest_skips_git_and_records_links(tmp_path):
    repo = _local_marketplace(tmp_path / "repo")
    before = installer._tree_material(str(repo))
    (repo / ".git").mkdir()
    (repo / ".git" / "index").write_text("x")
    assert installer._tree_material(str(repo)) == before
    os.symlink("/etc", repo / "link")
    with_link = installer._tree_material(str(repo))
    assert with_link != before
    (repo / "link").unlink()
    os.symlink("/usr", repo / "link")
    assert installer._tree_material(str(repo)) != with_link


# ---------------------------------------------------------------------------
# Admin changes take the lock and invalidate the digest
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(tmp_path, monkeypatch):
    from src import plugin_admin_service as svc

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(data / "gateway-plugins.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(svc, "_require_claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(svc, "_with_confinement_warnings", lambda r: r)
    calls = []

    def fake_run(cmd, *, token=""):
        calls.append((list(cmd), lc._replay_depth))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "run", fake_run)
    (data / lc.REPLAY_DIGEST_NAME).write_text("deadbeef\n")
    return svc, data, calls


def test_admin_install_invalidates_digest_under_the_lock(admin):
    svc, data, calls = admin
    svc.install_plugin("demo", marketplace="acme")
    assert not (data / lc.REPLAY_DIGEST_NAME).exists()
    assert calls and all(depth >= 1 for _, depth in calls)
    assert lc._replay_depth == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda svc: svc.uninstall_plugin("demo@acme"),
        lambda svc: svc.remove_marketplace("acme"),
    ],
)
def test_admin_mutations_invalidate_digest(admin, call):
    svc, data, calls = admin
    call(svc)
    assert not (data / lc.REPLAY_DIGEST_NAME).exists()
    assert all(depth >= 1 for _, depth in calls)


def test_admin_install_with_repo_nests_the_lock(admin, tmp_path, monkeypatch):
    """install_plugin(repo=…) calls add_marketplace: the re-entrant lock must
    not deadlock, and the nested call sees it held."""
    svc, data, calls = admin
    repo = _local_marketplace(tmp_path / "mkt")
    monkeypatch.setattr(svc, "_local_needs_copy", lambda p: False)
    done = threading.Event()

    def go():
        svc.install_plugin("demo", marketplace="local", repo=str(repo))
        done.set()

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert done.is_set()
    assert [c[0][1:3] for c in calls][:1] == [["plugin", "marketplace"]]
    assert all(depth >= 1 for _, depth in calls)


def test_admin_waits_for_another_process_replay(admin, tmp_path):
    svc, data, calls = admin
    holder = _Holder(tmp_path / "cfg" / lc.REPLAY_LOCK_NAME)
    done = threading.Event()
    thread = threading.Thread(
        target=lambda: (svc.uninstall_plugin("demo@acme"), done.set()), daemon=True
    )
    thread.start()
    try:
        time.sleep(0.5)
        assert not done.is_set() and calls == []
    finally:
        holder.stop()
    thread.join(timeout=20)
    assert done.is_set() and calls


async def test_autorefresh_refresh_runs_under_the_lock(admin, monkeypatch):
    """The auto-refresh cycle reaches the admin refresh in a worker thread,
    which takes the replay lock (never on the event loop)."""
    from src import plugin_autorefresh

    svc, data, calls = admin
    seen = []

    def refresh(name):
        with svc._replay_locked():
            seen.append((lc._replay_depth, threading.current_thread().name))
        return {"updated_plugins": [], "failed_updates": []}

    monkeypatch.setattr(svc, "refresh_marketplace", refresh)
    monkeypatch.setattr(
        plugin_autorefresh.PluginAutoRefresher,
        "_managed_marketplace_names",
        staticmethod(lambda: {"acme"}),
    )
    result = await plugin_autorefresh.PluginAutoRefresher(poll_seconds=999).run_cycle()
    assert result["results"][0]["status"] == "refreshed"
    assert seen and seen[0][0] == 1
    assert seen[0][1] != threading.main_thread().name
    assert not (data / lc.REPLAY_DIGEST_NAME).exists()


def test_refresh_marketplace_is_wrapped():
    from src import plugin_admin_service as svc

    for name in (
        "add_marketplace",
        "refresh_marketplace",
        "remove_marketplace",
        "install_plugin",
        "uninstall_plugin",
    ):
        assert getattr(svc, name).__wrapped__  # functools.wraps of the lock


# ---------------------------------------------------------------------------
# 3. old tree lifetime
# ---------------------------------------------------------------------------


def test_other_instances_alive_matches_instance_registry(instances, monkeypatch):
    assert lc.other_instances_alive() is False
    assert instance_registry.other_live_instances() == []
    holder = instances("green")
    assert lc.other_instances_alive() is True
    assert instance_registry.other_live_instances() == ["green"]
    monkeypatch.setenv("GATEWAY_INSTANCE", "green")  # ourselves: not "other"
    assert lc.other_instances_alive() is False
    assert instance_registry.other_live_instances() == []
    monkeypatch.delenv("GATEWAY_INSTANCE")
    holder.stop()
    assert lc.other_instances_alive() is False
    assert instance_registry.other_live_instances() == []


def test_other_instances_alive_without_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_INSTANCE_DIR", str(tmp_path / "missing"))
    assert lc.other_instances_alive() is False


def test_default_instances_dir_matches_registry(monkeypatch):
    monkeypatch.delenv("GATEWAY_INSTANCE_DIR", raising=False)
    assert lc._instances_dir(os.environ) == instance_registry.instances_dir()


def _parked(tmp_path):
    root = tmp_path / "clones"
    dest = root / "mkt-1234abcd"
    dest.mkdir(parents=True)
    old = root / ("mkt-1234abcd" + lc._OLD + "0123456789ab")
    old.mkdir()
    (old / "f").write_text("x")
    _age(old, lc.OLD_GRACE_SECONDS + 60)
    return root, dest, old


def test_old_tree_kept_while_another_instance_is_alive(tmp_path, instances):
    root, dest, old = _parked(tmp_path)
    holder = instances("blue")
    lc.replace_tree(dest, lambda staging: (staging / "a").write_text("1"))
    assert old.is_dir()  # past grace, but blue may still be inside it
    parked = [p for p in root.iterdir() if lc._OLD in p.name]
    assert len(parked) == 2  # and the tree just replaced is parked too
    holder.stop()
    for p in parked:
        _age(p, lc.OLD_GRACE_SECONDS + 60)
    lc.replace_tree(dest, lambda staging: (staging / "a").write_text("2"))
    assert not old.exists()
    assert [p for p in root.iterdir() if lc._OLD in p.name] != parked


def test_staging_sweep_unchanged_while_another_instance_is_alive(tmp_path, instances):
    root, dest, _ = _parked(tmp_path)
    stale = root / ("mkt-1234abcd" + lc._NEW + "zz")
    stale.mkdir()
    _age(stale, lc.STALE_STAGING_SECONDS + 60)
    instances("blue")
    lc.replace_tree(dest, lambda staging: None)
    assert not stale.exists()


def test_old_tree_within_grace_kept_with_no_instance(tmp_path, instances):
    root, dest, old = _parked(tmp_path)
    _age(old, 10)
    assert lc.sweep_parked_trees(root) == 1
    assert old.is_dir()


def test_startup_sweep_removes_parked_trees_only_when_alone(tmp_path, instances):
    root, dest, old = _parked(tmp_path)
    holder = instances("green")
    assert lc.sweep_parked_trees(root) == 0
    assert old.is_dir()
    holder.stop()
    assert lc.sweep_parked_trees(root) == 1
    assert not old.exists() and dest.is_dir()


def test_startup_sweep_never_raises(tmp_path, instances):
    assert lc.sweep_parked_trees(tmp_path / "missing") == 0


def test_installer_sweeps_parked_trees_at_startup(env, tmp_path):
    root = env.home / ".claude" / "plugin-marketplaces"
    dest = root / "mkt-1234abcd"
    dest.mkdir(parents=True)
    old = root / ("mkt-1234abcd" + lc._OLD + "0123456789ab")
    old.mkdir()
    _age(old, lc.OLD_GRACE_SECONDS + 60)
    holder = _Holder(tmp_path / "instances" / "blue.lock")
    env.run()  # no manifest: still sweeps, but blue is alive
    assert old.is_dir()
    holder.stop()
    env.run()
    assert not old.exists() and dest.is_dir()


# ---------------------------------------------------------------------------
# 4. _tmp_ workspaces
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    base = tmp_path / "ws"
    base.mkdir()
    return WorkspaceManager(base)


def test_anonymous_workspace_carries_the_instance(manager, monkeypatch, instances):
    assert manager.resolve(None).name.startswith("_tmp_")
    assert len(manager.resolve(None).name) == len("_tmp_") + 32
    monkeypatch.setenv("GATEWAY_INSTANCE", "blue")
    ws = manager.resolve(None)
    assert ws.name.startswith("_tmp_blue_") and ws.is_dir()
    # Still a temp workspace to everything that recognises one.
    manager.cleanup_temp_workspace(ws)
    assert not ws.exists()


def _old_dirs(manager, *names):
    out = []
    for name in names:
        path = manager.base_path / name
        path.mkdir()
        _age(path, 7200)
        out.append(path)
    return out


def test_sweep_scoped_to_own_prefix_while_other_alive(manager, monkeypatch, instances):
    monkeypatch.setenv("GATEWAY_INSTANCE", "blue")
    instances("green")
    mine, theirs, legacy = _old_dirs(
        manager, "_tmp_blue_" + "a" * 32, "_tmp_green_" + "b" * 32, "_tmp_" + "c" * 32
    )
    fresh = manager.base_path / ("_tmp_blue_" + "d" * 32)
    fresh.mkdir()
    assert manager.sweep_orphan_temp_workspaces(3600) == 1
    assert not mine.exists()
    assert theirs.is_dir() and legacy.is_dir() and fresh.is_dir()


def test_sweep_takes_every_prefix_when_alone(manager, monkeypatch, instances):
    monkeypatch.setenv("GATEWAY_INSTANCE", "blue")
    dirs = _old_dirs(
        manager, "_tmp_blue_" + "a" * 32, "_tmp_green_" + "b" * 32, "_tmp_" + "c" * 32
    )
    assert manager.sweep_orphan_temp_workspaces(3600) == 3
    assert not any(d.exists() for d in dirs)


def test_sweep_without_instance_beside_a_live_one_takes_nothing(manager, instances):
    instances("green")
    (legacy,) = _old_dirs(manager, "_tmp_" + "c" * 32)
    assert manager.sweep_orphan_temp_workspaces(3600) == 0
    assert legacy.is_dir()


def test_uid_any_age_only_when_alone(manager, monkeypatch, instances):
    from src import workspace_manager as wm

    monkeypatch.setattr(wm, "_per_uid_layouts", lambda: True)
    taken = []
    monkeypatch.setattr(
        manager, "cleanup_temp_workspace", lambda p: taken.append(p.name)
    )
    monkeypatch.setenv("GATEWAY_INSTANCE", "blue")
    for name in ("_tmp_blue_" + "a" * 32, "_tmp_" + "c" * 32):
        (manager.base_path / name).mkdir()  # fresh
    holder = instances("green")
    assert manager.sweep_orphan_temp_workspaces(3600) == 0  # age filter kept
    holder.stop()
    fresh_manager = WorkspaceManager(manager.base_path)
    monkeypatch.setattr(
        fresh_manager, "cleanup_temp_workspace", lambda p: taken.append(p.name)
    )
    assert fresh_manager.sweep_orphan_temp_workspaces(3600) == 2  # any age


def test_liveness_error_keeps_others(manager, monkeypatch):
    from src import workspace_manager as wm

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(instance_registry, "other_live_instances", boom)
    monkeypatch.delenv("GATEWAY_INSTANCE", raising=False)
    (legacy,) = _old_dirs(manager, "_tmp_" + "c" * 32)
    assert wm._other_instances_alive() is True
    assert manager.sweep_orphan_temp_workspaces(3600) == 0
    assert legacy.is_dir()
