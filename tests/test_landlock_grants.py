"""The Landlock grant filter and the per-session dirs (PR #244 review 4).

``_ro_rw_roots`` used to copy shared-asset and plugin-registry roots into the
Landlock lists after ``workspace_sandbox._add_root`` had resolved them, and the
CLI's config files unresolved (the launcher's ``O_PATH`` open follows a link and
a directory target keeps its directory rights). A symlinked asset, a symlinked
config file or a broad registry/env path therefore widened every confined
session's read + execute grant to the config dir, ``$HOME``, ``/app`` or ``/``
(review findings #1 and #14). Every gateway-built entry now passes one filter
(``landlock._GrantFilter``) and goes in resolved.

Also here: ``rules/``, ``hooks/`` and ``workflows/`` are readable under
confinement (#6), the CLI's task list ``tasks/<session id>`` is created and
granted like ``session-env/<session id>`` (#4), and uid mode leaves both to the
broker's child, which runs as the session uid.

Every protected root lives under ``tmp_path`` here: ``$HOME`` and its config
dir, the workspace base, a stand-in for the gateway app root (``/app``) and
the temp dir (pointed away from pytest's own tmp dir, which lives in the real
one). The real-kernel tests run a system python as the "real CLI" under the
launcher (Landlock ABI >= ``landlock.MIN_LANDLOCK_ABI`` only).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backends.claude import landlock
from src.backends.claude import workspace_sandbox as ws_mod
from src.workspace_manager import workspace_manager

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO / "docker" / "landlock_launcher.py"
_SYSTEM_PYTHON = "/usr/bin/python3"


@pytest.fixture(autouse=True)
def _reset_isolation_env(monkeypatch):
    from src.backends.claude import uid_isolation as ui

    for var in (
        landlock.MODE_ENV,
        landlock.FALLBACK_ENV,
        landlock.BROKER_SOCKET_ENV,
        "GATEWAY_ISOLATION_TICKET_SECRET",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_PLUGIN_CLONE_ROOT",
        "CLAUDE_ADD_DIRS",
    ):
        monkeypatch.delenv(var, raising=False)
    # raising=False: the same tests also run against a pre-fix landlock.py.
    monkeypatch.setattr(landlock, "_warned", set(), raising=False)
    landlock.reset_mode_cache()
    ui.reset_captured_secret()
    ui.reset_registry_cache()
    yield
    landlock.reset_mode_cache()
    ui.reset_captured_secret()
    ui.reset_registry_cache()


@pytest.fixture
def lay(monkeypatch, tmp_path):
    """A gateway layout whose every protected root sits under *tmp_path*."""
    home = tmp_path / "home"
    cfg = home / ".claude"
    for d in ("skills", "plugins", "agents", "projects/-other-user-ws"):
        (cfg / d).mkdir(parents=True)
    (cfg / "projects" / "-other-user-ws" / "t.jsonl").write_text("OTHER-TRANSCRIPT\n")
    base = tmp_path / "data" / "workspaces"
    ws = base / "alice" / "claude"
    ws.mkdir(parents=True)
    (base / "bob" / "claude").mkdir(parents=True)
    (base / "bob" / "claude" / "secret.txt").write_text("BOB-SECRET\n")
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "creds.txt").write_text("APP-DATA-SECRET\n")
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    safe = tmp_path / "opt-shared"
    safe.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(workspace_manager, "base_path", base)
    monkeypatch.setattr(landlock, "_APP_ROOT", app, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    return SimpleNamespace(
        root=tmp_path,
        home=home,
        cfg=cfg,
        base=base,
        ws=ws,
        app=app,
        tmp=systmp,
        safe=safe,
    )


@pytest.fixture
def confine_on(monkeypatch):
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()


@pytest.fixture
def uid_on(monkeypatch, tmp_path):
    """uid mode with a reachable broker faked, a secret and a temp registry."""
    monkeypatch.setenv(landlock.MODE_ENV, "uid")
    monkeypatch.setenv("GATEWAY_ISOLATION_TICKET_SECRET", "s3cret")
    monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/run/x.sock")
    monkeypatch.setenv("CLAUDE_ISOLATION_UID_REGISTRY", str(tmp_path / "uids.json"))
    monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
    landlock.reset_mode_cache()


def _lists(ws: Path, session_id=None, **kwargs):
    return landlock._ro_rw_roots(ws, session_id, **kwargs)


def _under(entries, root: Path) -> list:
    """The entries that are *root* or lie beneath it."""
    r = str(root)
    return [e for e in entries if e == r or e.startswith(r + os.sep)]


def _reach(entries) -> list:
    """What the launcher opens for *entries*: its O_PATH open follows links."""
    return [os.path.realpath(e) for e in entries]


def _grants(entries, path: Path) -> bool:
    """Whether one of *entries* is *path* or an ancestor of it."""
    p = str(path)
    return any(p == e or p.startswith(e.rstrip(os.sep) + os.sep) for e in entries)


def _refusals(caplog) -> list:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == landlock.logger.name and "Not granting" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# #1 (k18): planted symlinks and registry entries
# ---------------------------------------------------------------------------


class TestPlantedLinks:
    def test_baseline_assets_are_granted_resolved(self, lay):
        ro, rw = _lists(lay.ws)
        for name in ("skills", "plugins", "agents", "commands", "CLAUDE.md"):
            assert str(lay.cfg / name) in ro
        assert all(e == os.path.realpath(e) for e in ro + rw), ro + rw
        assert str(lay.cfg) not in ro and str(lay.home) not in ro

    def test_s1_agents_link_to_the_app_root(self, lay, caplog):
        (lay.cfg / "agents").rmdir()
        (lay.cfg / "agents").symlink_to(lay.app)
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.app), ro
        (msg,) = [m for m in _refusals(caplog) if "agents" in m]
        assert str(lay.app) in msg and "gateway app root" in msg

    @pytest.mark.parametrize(
        "target",
        [
            ".",  # the whole config dir
            "..",  # $HOME
            "/",
            "projects",  # every user's transcripts
            "../.ssh",  # $HOME outside the config dir
            "@base",  # the workspace base
            "@bob",  # another user's workspace
            "@tmp",  # the temp dir's contents
            "@data",  # above the workspace base
        ],
    )
    def test_s2_agents_link_to_protected_state(self, lay, target):
        (lay.home / ".ssh").mkdir()
        named = {
            "@base": lay.base,
            "@bob": lay.base / "bob" / "claude",
            "@tmp": lay.tmp / "x",
            "@data": lay.base.parent,
        }
        (lay.tmp / "x").mkdir()
        (lay.cfg / "agents").rmdir()
        (lay.cfg / "agents").symlink_to(named.get(target, target))
        ro, rw = _lists(lay.ws)
        resolved = os.path.realpath(lay.cfg / "agents")
        assert resolved not in _reach(ro)
        # Nothing reaches other users' transcripts, workspaces or the app.
        for secret in (
            lay.cfg / "projects" / "-other-user-ws" / "t.jsonl",
            lay.base / "bob" / "claude" / "secret.txt",
            lay.app / "data" / "creds.txt",
        ):
            assert not _grants(_reach(ro + rw), secret), secret

    def test_s3_settings_local_json_link_to_the_app_root(self, lay):
        (lay.cfg / "settings.local.json").symlink_to(lay.app)
        ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.app), ro
        assert str(lay.cfg / "settings.json") in ro  # its neighbour still reads

    def test_s4_marketplace_install_location_of_the_app_root(self, lay):
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"evil": {"installLocation": str(lay.app)}})
        )
        ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.app), ro

    @pytest.mark.parametrize("install_path", ["/", "@home", "@cfg"])
    def test_installed_plugin_path_that_opens_shared_state(self, lay, install_path):
        path = {"@home": lay.home, "@cfg": lay.cfg}.get(install_path, install_path)
        (lay.cfg / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"p@m": [{"installPath": str(path)}]}})
        )
        ro, _ = _lists(lay.ws)
        assert os.path.realpath(path) not in _reach(ro)

    def test_s7_control_a_link_inside_a_granted_tree_is_no_rule(self, lay):
        # Landlock checks the resolved path at open(); only rule *targets*
        # widen the grant, so a link inside skills/ grants nothing new.
        (lay.cfg / "skills" / "inner").symlink_to(lay.app)
        ro, _ = _lists(lay.ws)
        assert str(lay.cfg / "skills") in ro
        assert not _under(_reach(ro), lay.app), ro

    def test_a_link_to_a_safe_shared_tree_keeps_working(self, lay):
        # docs/security-process-isolation.md: a symlinked asset's target must
        # stay reachable — the resolved target is what is granted.
        (lay.cfg / "skills").rmdir()
        (lay.cfg / "skills").symlink_to(lay.safe)
        ro, _ = _lists(lay.ws)
        assert str(lay.safe) in ro
        assert str(lay.cfg / "skills") not in ro  # the link itself is no rule

    def test_global_config_file_and_its_links(self, lay):
        ro, _ = _lists(lay.ws)
        assert str(lay.home / ".claude.json") in ro
        # A ~/.claude.json kept in the config volume through a link reads.
        (lay.cfg / ".claude.json").write_text("{}")
        (lay.home / ".claude.json").symlink_to(lay.cfg / ".claude.json")
        ro, _ = _lists(lay.ws)
        assert str(lay.cfg / ".claude.json") in ro

    def test_global_config_link_into_transcripts_is_refused(self, lay):
        target = lay.cfg / "projects" / "-other-user-ws" / "t.jsonl"
        (lay.home / ".claude.json").symlink_to(target)
        ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.cfg / "projects"), ro

    def test_refusals_are_logged_once_per_process(self, lay, caplog):
        (lay.cfg / "agents").rmdir()
        (lay.cfg / "agents").symlink_to(".")
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            _lists(lay.ws)
            _lists(lay.ws)
            _lists(lay.ws)
        assert len([m for m in _refusals(caplog) if "agents" in m]) == 1


# ---------------------------------------------------------------------------
# #14 (k17): broad plugin roots, held to what CLAUDE_ADD_DIRS is held to
# ---------------------------------------------------------------------------


class TestBroadPluginRoots:
    @pytest.mark.parametrize("which", ["data", "app-data", "home", "tmp"])
    def test_clone_root_over_shared_state_is_refused(self, lay, monkeypatch, which):
        root = {
            "data": lay.base.parent,  # contains the workspace base
            "app-data": lay.app / "data",
            "home": lay.home,
            "tmp": lay.tmp,
        }[which]
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(root))
        ro, _ = _lists(lay.ws)
        assert str(root) not in _reach(ro)
        assert not _under(_reach(ro), lay.base), ro
        assert not _under(_reach(ro), lay.app), ro

    def test_install_location_over_the_workspace_base_is_refused(self, lay):
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps(
                {
                    "team-mkt": {
                        "source": {"source": "directory", "path": str(lay.base)},
                        "installLocation": str(lay.base.parent),
                    }
                }
            )
        )
        ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.base.parent), ro

    def test_a_safe_clone_root_and_its_clones_are_granted(self, lay, monkeypatch):
        clones = lay.safe / "clones"
        clone = clones / "repo-1234"
        clone.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(clones))
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(clone)}})
        )
        ro, _ = _lists(lay.ws)
        assert str(clones) in ro and str(clone) in ro

    def test_a_clone_root_in_home_exempts_only_itself(self, lay, monkeypatch):
        # Inside $HOME only the global config and the clone root are granted.
        clones = lay.home / "clones"
        (clones / "repo-1").mkdir(parents=True)
        (lay.home / ".ssh").mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(clones))
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps(
                {
                    "ok": {"installLocation": str(clones / "repo-1")},
                    "bad": {"installLocation": str(lay.home / ".ssh")},
                }
            )
        )
        ro, _ = _lists(lay.ws)
        assert str(clones) in ro and str(clones / "repo-1") in ro
        assert str(lay.home / ".ssh") not in ro

    def test_default_clone_root_and_plugin_cache_are_granted(self, lay):
        clone = lay.cfg / "plugin-marketplaces" / "repo-abcd"
        cache = lay.cfg / "plugins" / "cache" / "m" / "p" / "1.0.0"
        clone.mkdir(parents=True)
        cache.mkdir(parents=True)
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(clone)}})
        )
        (lay.cfg / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {"version": 2, "plugins": {"p@m": [{"installPath": str(cache)}]}}
            )
        )
        ro, _ = _lists(lay.ws)
        assert str(clone) in ro and str(cache) in ro

    @pytest.mark.parametrize("where", ["app", "tmp", "home"])
    def test_add_dirs_pass_the_same_filter(self, lay, monkeypatch, where):
        # Even a read-only CLAUDE_ADD_DIRS entry that clears the #240 overlap
        # check is not granted inside the app root, the temp dir or $HOME.
        extra = {"app": lay.app, "tmp": lay.tmp, "home": lay.home}[where] / "docs"
        extra.mkdir()
        monkeypatch.setenv("CLAUDE_ADD_DIRS", str(extra))
        monkeypatch.setattr(ws_mod, "_read_only_tree", lambda _p: True)
        ro, _ = _lists(lay.ws)
        assert str(extra) not in ro

    def test_a_safe_read_only_add_dir_is_granted(self, lay, monkeypatch):
        monkeypatch.setenv("CLAUDE_ADD_DIRS", str(lay.safe))
        monkeypatch.setattr(ws_mod, "_read_only_tree", lambda _p: True)
        ro, _ = _lists(lay.ws)
        assert str(lay.safe) in ro


# ---------------------------------------------------------------------------
# Read-write entries: exactly the per-session paths, never through a link
# ---------------------------------------------------------------------------


class TestReadWriteEntries:
    def _own(self, lay) -> Path:
        return lay.cfg / "projects" / ws_mod._project_dir_name(str(lay.ws))

    def test_own_project_dir_is_granted(self, lay):
        _ro, rw = _lists(lay.ws)
        assert rw == [str(lay.ws), str(self._own(lay))]

    @pytest.mark.parametrize("target", ["/", "@safe"])
    def test_a_planted_project_dir_link_is_refused(self, lay, target, caplog):
        self._own(lay).symlink_to(lay.safe if target == "@safe" else target)
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            _ro, rw = _lists(lay.ws)
        assert rw == [str(lay.ws)]
        assert any("read-write" in m for m in _refusals(caplog))

    def test_a_linked_projects_dir_is_refused(self, lay):
        moved = lay.safe / "projects"
        (lay.cfg / "projects").rename(moved)
        (lay.cfg / "projects").symlink_to(moved)
        _ro, rw = _lists(lay.ws)
        assert rw == [str(lay.ws)]

    @pytest.mark.parametrize("kind", ["session-env", "tasks"])
    def test_a_linked_session_parent_creates_nothing_through_it(
        self, lay, kind, caplog
    ):
        (lay.cfg / kind).symlink_to(lay.safe)
        sid = str(uuid.uuid4())
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            _ro, rw = _lists(lay.ws, sid)
        assert list(lay.safe.iterdir()) == []  # no mkdir through the link
        assert not _under(rw, lay.cfg / kind), rw
        assert any(str(lay.cfg / kind) in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("kind", ["session-env", "tasks"])
    def test_a_planted_session_dir_link_is_refused(self, lay, kind):
        sid = str(uuid.uuid4())
        (lay.cfg / kind).mkdir()
        (lay.cfg / kind / sid).symlink_to(lay.safe)
        _ro, rw = _lists(lay.ws, sid)
        assert not _under(rw, lay.cfg / kind), rw


class TestPluginDataDirsNoFollow:
    def _register(self, plugins_dir: Path) -> None:
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"hookplug@testmkt": []}})
        )

    def test_a_linked_plugins_data_dir_gets_nothing_created(self, lay, caplog):
        self._register(lay.cfg / "plugins")
        (lay.cfg / "plugins" / "data").symlink_to(lay.safe)
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            _lists(lay.ws)
            _lists(lay.ws)
        assert list(lay.safe.iterdir()) == []
        warned = [r for r in caplog.records if "hookplug@testmkt" in r.getMessage()]
        assert len(warned) == 1  # once per process, not per spawn

    def test_a_linked_plugins_dir_gets_nothing_created(self, lay):
        target = lay.safe / "plugins"
        (lay.cfg / "plugins").rename(target)
        (lay.cfg / "plugins").symlink_to(target)
        self._register(target)
        _lists(lay.ws)
        assert not (target / "data").exists()

    def test_a_real_plugins_dir_still_gets_its_data_dirs(self, lay):
        self._register(lay.cfg / "plugins")
        _lists(lay.ws)
        assert (lay.cfg / "plugins" / "data" / "hookplug-testmkt").is_dir()


# ---------------------------------------------------------------------------
# #6 (k14): rules, hook scripts and workflows
# ---------------------------------------------------------------------------


class TestLandlockOnlyAssets:
    def test_rules_hooks_and_workflows_are_read_only(self, lay):
        ro, rw = _lists(lay.ws)
        for name in ("rules", "hooks", "workflows"):
            assert str(lay.cfg / name) in ro
            assert not _under(rw, lay.cfg / name)

    def test_they_stay_out_of_the_file_tool_guard(self):
        # The guard's list also scopes the file tools; these are CLI-only.
        assert not {"rules", "hooks", "workflows"} & set(ws_mod._SHARED_CLAUDE_ASSETS)

    def test_a_linked_rules_dir_passes_the_filter(self, lay):
        (lay.cfg / "rules").symlink_to(lay.cfg / "projects")
        ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.cfg / "projects"), ro


# ---------------------------------------------------------------------------
# #4 (k15): the CLI's task list dir
# ---------------------------------------------------------------------------


class TestTaskListDir:
    def test_landlock_creates_and_grants_the_sessions_own(self, lay, confine_on):
        sid = str(uuid.uuid4())
        ro, rw = _lists(lay.ws, sid)
        own = lay.cfg / "tasks" / sid
        assert own.is_dir() and stat.S_IMODE(own.stat().st_mode) == 0o700
        assert str(own) in rw
        assert str(lay.cfg / "tasks") not in ro + rw

    @pytest.mark.parametrize("sid", [None, "", "../escape", "a/b", ".hidden", "-x"])
    def test_no_or_unsafe_session_id_grants_nothing(self, lay, confine_on, sid):
        _ro, rw = _lists(lay.ws, sid)
        assert not _under(rw, lay.cfg / "tasks"), rw
        assert not _under(rw, lay.cfg / "session-env"), rw
        assert not (lay.cfg / "tasks").exists()

    def test_uid_mode_grants_but_does_not_create(self, lay, uid_on):
        # A gateway-owned 0700 dir is unusable by the session uid (review cut
        # item): the broker's child creates both as that uid.
        from src.backends.claude import uid_isolation as ui

        sid = str(uuid.uuid4())
        env = landlock.options_env(lay.ws, user="alice", session_id=sid)
        rw = ui.verify_ticket(env[landlock.TICKET_ENV])["rw"]
        for kind in ("session-env", "tasks"):
            assert str(lay.cfg / kind / sid) in rw
            assert not (lay.cfg / kind).exists()

    def test_uid_mode_still_refuses_a_planted_link(self, lay, uid_on):
        from src.backends.claude import uid_isolation as ui

        sid = str(uuid.uuid4())
        (lay.cfg / "tasks").mkdir()
        (lay.cfg / "tasks" / sid).symlink_to(lay.safe)
        env = landlock.options_env(lay.ws, user="alice", session_id=sid)
        rw = ui.verify_ticket(env[landlock.TICKET_ENV])["rw"]
        assert not _under(rw, lay.cfg / "tasks"), rw

    def test_removal_takes_both_dirs_under_confinement(self, lay, confine_on):
        sid, other = str(uuid.uuid4()), str(uuid.uuid4())
        landlock.options_env(lay.ws, session_id=sid)
        landlock.options_env(lay.ws, session_id=other)
        (lay.cfg / "tasks" / sid / "1.json").write_text("{}")
        (lay.cfg / "tasks" / sid / "link").symlink_to(lay.safe)  # not followed
        (lay.safe / "keep.txt").write_text("keep")
        landlock.remove_session_env_dir(sid)  # the name the cleanups call
        for kind in ("session-env", "tasks"):
            assert not (lay.cfg / kind / sid).exists()
            assert (lay.cfg / kind / other).is_dir()
        assert (lay.safe / "keep.txt").read_text() == "keep"

    def test_removal_never_goes_through_a_linked_parent(self, lay, confine_on):
        sid = str(uuid.uuid4())
        (lay.safe / sid).mkdir()
        (lay.safe / sid / "keep.txt").write_text("keep")
        (lay.cfg / "tasks").symlink_to(lay.safe)
        (lay.cfg / "session-env").symlink_to(lay.safe)
        landlock.remove_session_dirs(sid)
        assert (lay.safe / sid / "keep.txt").read_text() == "keep"

    def test_off_mode_leaves_the_clis_own_task_list(self, lay):
        # Without confinement the gateway never made it: a later resume of
        # the session still finds its tasks.
        sid = str(uuid.uuid4())
        (lay.cfg / "tasks" / sid).mkdir(parents=True)
        (lay.cfg / "session-env" / sid).mkdir(parents=True)
        landlock.remove_session_dirs(sid)
        assert (lay.cfg / "tasks" / sid).is_dir()
        assert not (lay.cfg / "session-env" / sid).exists()


# ---------------------------------------------------------------------------
# Real kernel: the lists above, enforced by the launcher
# ---------------------------------------------------------------------------


def _landlock_skip() -> str:
    if landlock.landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return (
            f"Landlock ABI {landlock.landlock_abi()} < "
            f"{landlock.MIN_LANDLOCK_ABI}"
        )
    if not os.access(_SYSTEM_PYTHON, os.X_OK):
        return f"no {_SYSTEM_PYTHON}"
    return ""


def _confined(lay, script: str, *args: str, session_id=None, ws=None) -> dict:
    """Run *script* (a system-python probe printing JSON) under the launcher
    with the gateway's own landlock-mode env for the workspace (*ws*, else the
    layout's), started in it as the SDK starts the CLI."""
    ws = ws or lay.ws
    probe = ws / "probe.py"
    probe.write_text(script)
    env = landlock.options_env(ws, session_id=session_id)
    assert env[landlock.MODE_ENV] == "landlock", env
    proc = subprocess.run(
        [sys.executable, str(_LAUNCHER), str(probe), *args],
        cwd=str(ws),
        env={
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            **env,
            landlock.REAL_CLI_ENV: _SYSTEM_PYTHON,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


_READ_PROBE = (
    "import json, sys\n"
    "def r(p):\n"
    "    try:\n"
    "        return 'OK:' + open(p).read().strip()\n"
    "    except OSError as e:\n"
    "        return 'DENY:' + str(e.errno)\n"
    "print(json.dumps([r(p) for p in sys.argv[1:]]))\n"
)


@pytest.mark.skipif(bool(_landlock_skip()), reason=_landlock_skip() or "")
class TestEnforcedOnTheKernel:
    def test_a_planted_link_is_denied_and_a_safe_link_reads(self, lay, confine_on):
        # The finding's s2: agents -> . granted the whole config dir.
        (lay.cfg / "agents").rmdir()
        (lay.cfg / "agents").symlink_to(".")
        (lay.safe / "s").mkdir()
        (lay.safe / "s" / "SKILL.md").write_text("SAFE-SKILL")
        (lay.cfg / "skills").rmdir()
        (lay.cfg / "skills").symlink_to(lay.safe)
        through_link = lay.cfg / "agents" / "projects" / "-other-user-ws" / "t.jsonl"
        out = _confined(
            lay,
            _READ_PROBE,
            str(through_link),
            str(lay.cfg / "projects" / "-other-user-ws" / "t.jsonl"),
            str(lay.cfg / "skills" / "s" / "SKILL.md"),
        )
        assert out[0].startswith("DENY") and out[1].startswith("DENY"), out
        assert out[2] == "OK:SAFE-SKILL", out

    def test_rules_read_and_hook_scripts_run(self, lay, confine_on):
        (lay.cfg / "rules").mkdir()
        (lay.cfg / "rules" / "team.md").write_text("RULE")
        hook = lay.cfg / "hooks" / "guard.sh"
        hook.parent.mkdir()
        hook.write_text("#!/bin/sh\nexit 7\n")
        hook.chmod(0o755)
        outside = lay.root / "outside.sh"  # control: an ungranted script
        outside.write_text("#!/bin/sh\nexit 7\n")
        outside.chmod(0o755)
        script = (
            "import json, subprocess, sys\n"
            "def run(argv):\n"
            "    try:\n"
            "        return subprocess.run(argv).returncode\n"
            "    except OSError as e:\n"
            "        return 'DENY:' + str(e.errno)\n"
            "rule, hook, outside = sys.argv[1:4]\n"
            "print(json.dumps({\n"
            "  'rule': open(rule).read(),\n"
            "  'exec': run([hook]),\n"
            "  'sh': run(['/bin/sh', hook]),\n"
            "  'outside': run([outside]),\n"
            "}))\n"
        )
        out = _confined(
            lay, script, str(lay.cfg / "rules" / "team.md"), str(hook), str(outside)
        )
        assert out["rule"] == "RULE"
        assert out["exec"] == 7 and out["sh"] == 7, out  # the script's own code
        assert out["outside"] != 7, out

    def test_the_task_list_works_and_another_sessions_does_not(self, lay, confine_on):
        # What CLI 2.1.283's TaskCreate does: mkdir, an O_EXCL .lock, realpath
        # of it (proper-lockfile), then the task file.
        sid, other = str(uuid.uuid4()), str(uuid.uuid4())
        script = (
            "import json, os, sys\n"
            "def step(fn):\n"
            "    try:\n"
            "        fn(); return 'OK'\n"
            "    except OSError as e:\n"
            "        return 'DENY:' + str(e.errno)\n"
            "own, other = sys.argv[1], sys.argv[2]\n"
            "lock = os.path.join(own, '.lock')\n"
            "def write():\n"
            "    with open(os.path.join(own, '1.json'), 'w') as f:\n"
            "        f.write('{}')\n"
            "print(json.dumps({\n"
            "  'mkdir': step(lambda: os.makedirs(own, exist_ok=True)),\n"
            "  'lock': step(lambda: os.close(os.open(\n"
            "      lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))),\n"
            "  'realpath': step(lambda: os.path.realpath(lock, strict=True)),\n"
            "  'write': step(write),\n"
            "  'list': step(lambda: os.listdir(own)),\n"
            "  'other': step(lambda: os.makedirs(other, exist_ok=True)),\n"
            "}))\n"
        )
        tasks = lay.cfg / "tasks"
        out = _confined(
            lay, script, str(tasks / sid), str(tasks / other), session_id=sid
        )
        assert {k: v for k, v in out.items() if k != "other"} == {
            "mkdir": "OK",
            "lock": "OK",
            "realpath": "OK",
            "write": "OK",
            "list": "OK",
        }, out
        assert out["other"].startswith("DENY"), out
        assert (tasks / sid / "1.json").read_text() == "{}"


# ---------------------------------------------------------------------------
# Follow-up review (#244 r4 verify): the clone-root anchor (A), the workspace
# itself (B), precedence on equal and nested roots (D, C), the real CLI's
# directory (E) and hard links (F)
# ---------------------------------------------------------------------------


def _secret_home(lay) -> None:
    (lay.home / ".ssh").mkdir()
    (lay.home / ".ssh" / "id_rsa").write_text("PRIVATE-KEY\n")
    (lay.home / ".aws").mkdir()
    (lay.home / ".aws" / "credentials").write_text("AWS-SECRET\n")


def _relink(path: Path, target) -> None:
    """Replace the dir *path* with a symlink to *target*."""
    path.rmdir()
    path.symlink_to(target)


class TestCloneRootAnchor:
    """A: only the operator's CLAUDE_PLUGIN_CLONE_ROOT exempts $HOME paths."""

    @pytest.mark.parametrize("secret", [".ssh", ".aws"])
    def test_a_planted_default_clone_root_anchors_nothing(self, lay, secret):
        _secret_home(lay)
        (lay.cfg / "plugin-marketplaces").mkdir()
        _relink(lay.cfg / "plugin-marketplaces", lay.home / secret)
        _relink(lay.cfg / "agents", lay.home / secret)
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(lay.home / secret)}})
        )
        ro, rw = _lists(lay.ws)
        assert not _under(_reach(ro + rw), lay.home / secret), ro

    def test_a_real_default_clone_root_keeps_its_clones(self, lay):
        clone = lay.cfg / "plugin-marketplaces" / "repo-1"
        clone.mkdir(parents=True)
        (lay.cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(clone)}})
        )
        ro, _ = _lists(lay.ws)
        assert str(clone) in ro

    def test_the_default_clone_root_outside_the_config_dir_is_no_anchor(
        self, lay, monkeypatch
    ):
        # With CLAUDE_CONFIG_DIR elsewhere, the default clone root is
        # $HOME/.claude/plugin-marketplaces: a $HOME path like any other.
        cfg = lay.root / "cfg-volume"
        cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        clone = lay.cfg / "plugin-marketplaces" / "repo-1"
        clone.mkdir(parents=True)
        (cfg / "plugins").mkdir()
        (cfg / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(clone)}})
        )
        ro, _ = _lists(lay.ws)
        assert str(clone) not in ro
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(clone.parent))
        ro, _ = _lists(lay.ws)
        assert str(clone) in ro  # the operator names it: granted

    def test_a_symlinked_operator_clone_root_anchors_nothing(self, lay, monkeypatch):
        _secret_home(lay)
        (lay.home / "clones").symlink_to(lay.home / ".ssh")  # swapped in
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(lay.home / "clones"))
        _relink(lay.cfg / "agents", lay.home / ".ssh")
        ro, _ = _lists(lay.ws)
        assert not _under(_reach(ro), lay.home / ".ssh"), ro


class TestWorkspaceIsChecked:
    """B: the workspace is the one rw grant the filter did not judge."""

    @pytest.mark.parametrize("target", ["cfg", "base", "bob", "home", "outside"])
    def test_a_planted_workspace_link_refuses_the_spawn(
        self, lay, confine_on, target, caplog
    ):
        dest = {
            "cfg": lay.cfg,
            "base": lay.base,
            "bob": lay.base / "bob" / "claude",
            "home": lay.home,
            "outside": lay.safe,
        }[target]
        lay.ws.rmdir()
        lay.ws.symlink_to(dest)
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            with pytest.raises(landlock.WorkspaceRefused, match="symlink below"):
                landlock.options_env(lay.ws)
        assert any("Refusing a confined spawn" in m for m in _refusals_any(caplog))

    def test_a_link_above_the_workspace_below_the_base_refuses(self, lay, confine_on):
        alice = lay.base / "alice"
        moved = lay.root / "elsewhere"
        alice.rename(moved)
        alice.symlink_to(moved)
        with pytest.raises(landlock.WorkspaceRefused):
            landlock.options_env(lay.ws)

    @pytest.mark.parametrize("mode", ["uid", "preflight", "preflight-new-user"])
    def test_every_spawn_path_refuses(self, lay, monkeypatch, tmp_path, mode):
        from src.backends.claude import uid_isolation as ui

        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv("GATEWAY_ISOLATION_TICKET_SECRET", "s3cret")
        monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/run/x.sock")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        landlock.reset_mode_cache()
        lay.ws.rmdir()
        lay.ws.symlink_to(lay.cfg)
        with pytest.raises(landlock.WorkspaceRefused):
            if mode == "uid":
                landlock.options_env(lay.ws, user="alice")
            elif mode == "preflight":
                ui.resolve_uid("alice")
                landlock.preflight_options_env(lay.ws, user="alice")
            else:  # landlock-shaped, as the gateway uid
                landlock.preflight_options_env(lay.ws, user="mallory")

    def test_the_workspace_base_itself_is_refused(self, lay, confine_on):
        with pytest.raises(landlock.WorkspaceRefused, match="workspace base"):
            landlock.options_env(lay.base)

    @pytest.mark.parametrize("where", ["cfg", "home", "tmp", "root", "projects"])
    def test_a_cwd_outside_the_base_must_be_harmless(self, lay, confine_on, where):
        cwd = {
            "cfg": lay.cfg,
            "home": lay.home,
            "tmp": lay.tmp,
            "root": Path("/"),
            "projects": lay.cfg / "projects" / "-other-user-ws",
        }[where]
        with pytest.raises(landlock.WorkspaceRefused):
            landlock.options_env(cwd)

    def test_a_cwd_resolving_into_the_base_from_outside_is_refused(
        self, lay, confine_on
    ):
        link = lay.safe / "ws"
        link.symlink_to(lay.base / "bob" / "claude")
        with pytest.raises(landlock.WorkspaceRefused):
            landlock.options_env(link)
        nested = lay.safe / "via"
        nested.symlink_to(lay.base / "bob")
        with pytest.raises(landlock.WorkspaceRefused, match="workspace base"):
            landlock.options_env(nested / "claude")  # the link is a parent

    def test_a_private_temp_dir_outside_the_base_is_fine(self, lay, confine_on):
        # The client's own fallback cwd (mkdtemp), which verify() runs in.
        private = Path(tempfile.mkdtemp(dir=lay.tmp))
        _ro, rw = _lists(private)
        assert rw[0] == str(private)

    def test_the_preflight_dir_must_be_the_gateways_own(
        self, lay, confine_on, monkeypatch
    ):
        private = Path(tempfile.mkdtemp(dir=lay.tmp))
        _ro, rw = _lists(private, gateway_owned=True)
        assert rw[0] == str(private)
        landlock.preflight_options_env(private, gateway_owned=True)  # no raise
        link = lay.tmp / "planted"
        link.symlink_to(private)
        with pytest.raises(landlock.WorkspaceRefused, match="real directory"):
            landlock.preflight_options_env(link, gateway_owned=True)
        with pytest.raises(landlock.WorkspaceRefused, match="cannot be checked"):
            landlock.preflight_options_env(lay.tmp / "gone", gateway_owned=True)
        monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(landlock.WorkspaceRefused, match="owned by uid"):
            landlock.preflight_options_env(private, gateway_owned=True)

    def test_the_resolved_workspace_and_its_one_project_dir(
        self, lay, confine_on, monkeypatch
    ):
        # A base reached through a symlink above it is fine: the kernel cwd —
        # what the CLI names its projects/ entry after — is the resolved path.
        link = lay.root / "base-link"
        link.symlink_to(lay.base)
        monkeypatch.setattr(workspace_manager, "base_path", link)
        lexical = link / "alice" / "claude"
        _ro, rw = _lists(lexical)
        name = ws_mod._project_dir_name(str(lay.ws))
        # One projects/ entry, for the resolved name only.
        assert rw == [str(lay.ws), str(lay.cfg / "projects" / name)]

    def test_the_uid_ticket_carries_the_resolved_workspace(
        self, lay, uid_on, monkeypatch
    ):
        from src.backends.claude import uid_isolation as ui

        link = lay.root / "base-link"
        link.symlink_to(lay.base)
        monkeypatch.setattr(workspace_manager, "base_path", link)
        env = landlock.options_env(link / "alice" / "claude", user="alice")
        payload = ui.verify_ticket(env[landlock.TICKET_ENV])
        assert payload["workspace"] == str(lay.ws)
        assert payload["rw"][0] == str(lay.ws)

    def test_the_startup_probe_reports_a_refused_workspace(
        self, lay, monkeypatch, tmp_path
    ):
        from src.backends.claude import uid_isolation as ui

        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv("GATEWAY_ISOLATION_TICKET_SECRET", "s3cret")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        lay.ws.rmdir()
        lay.ws.symlink_to(lay.cfg)
        monkeypatch.setattr(workspace_manager, "resolve", lambda *_a, **_k: lay.ws)
        monkeypatch.setattr(workspace_manager, "cleanup_temp_workspace", lambda _p: 0)
        landlock._probe_uid_sessions()
        problems = landlock._uid_probe_problems
        landlock.reset_uid_probe()
        assert problems and "refusing a confined spawn" in problems[0], problems

    async def test_verify_refuses_a_linked_client_cwd(
        self, lay, confine_on, caplog, monkeypatch
    ):
        from unittest.mock import patch

        from src.backends.claude import client as client_module

        spawned = []

        def no_spawn(**kwargs):  # never start a CLI from this test
            spawned.append(kwargs)
            raise AssertionError("verify() spawned a CLI")

        monkeypatch.setattr(client_module, "query", no_spawn)
        link = lay.safe / "client-cwd"
        link.symlink_to(lay.cfg)
        with (
            patch("src.auth.validate_claude_code_auth") as validate,
            patch("src.auth.auth_manager") as auth,
        ):
            validate.return_value = (True, {"method": "anthropic"})
            auth.get_claude_code_env_vars.return_value = {"ANTHROPIC_AUTH_TOKEN": "x"}
            cli = client_module.ClaudeCodeCLI(cwd=str(link))
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            assert await cli.verify() is False
        assert spawned == []
        assert any("Refusing a confined spawn" in m for m in _refusals_any(caplog))


def _refusals_any(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.name == landlock.logger.name]


class TestPrecedence:
    """D (and C): equal roots take the strictest rule, exemptions never open
    the workspace base, the app root or a private system root."""

    def test_home_as_the_workspace_base_grants_no_other_workspace(
        self, lay, monkeypatch
    ):
        monkeypatch.setattr(workspace_manager, "base_path", lay.home)
        alice = lay.home / "alice" / "claude"
        alice.mkdir(parents=True)
        (lay.home / "bob" / "claude").mkdir(parents=True)
        (lay.home / "bob" / "claude" / "s.txt").write_text("BOB\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(lay.home / "bob"))
        ro, rw = _lists(alice)
        assert not _grants(_reach(ro + rw), lay.home / "bob" / "claude" / "s.txt")
        # $HOME and the base are one directory: its strictest rule applies, so
        # the global config file is refused too; the config dir nested in it
        # keeps its own rule.
        assert str(lay.home / ".claude.json") not in ro
        assert str(lay.cfg / "skills") in ro

    def test_no_exemption_inside_a_base_that_holds_home(self, lay, monkeypatch):
        monkeypatch.setattr(workspace_manager, "base_path", lay.root)
        clones = lay.home / "clones"
        clones.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(clones))
        ro, _ = _lists(lay.ws)
        assert str(lay.home / ".claude.json") not in ro
        assert str(clones) not in ro
        assert str(lay.cfg / "skills") in ro  # the config dir's own rule

    def test_no_exemption_inside_the_app_root(self, lay, monkeypatch):
        home = lay.app / "home"
        (home / ".claude" / "skills").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        ro, _ = _lists(lay.ws)
        assert str(home / ".claude.json") not in ro
        assert str(home / ".claude" / "skills") in ro

    def test_home_nested_in_the_temp_dir_keeps_its_rule(self, lay, monkeypatch):
        # As in tests and throwaway deployments: HOME below the temp dir. Its
        # own rule decides inside it; the rest of the temp dir stays refused.
        home = lay.tmp / "home"
        cfg = home / ".claude"
        (cfg / "skills").mkdir(parents=True)
        (home / ".ssh").mkdir()
        (lay.tmp / "x").mkdir()
        (cfg / "agents").symlink_to(home / ".ssh")
        (cfg / "commands").symlink_to(lay.tmp / "x")
        monkeypatch.setenv("HOME", str(home))
        clones = home / "clones"
        clones.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(clones))
        ro, _ = _lists(lay.ws)
        assert str(home / ".claude.json") in ro
        assert str(cfg / "skills") in ro
        assert str(clones) in ro
        assert str(home / ".ssh") not in ro
        assert str(lay.tmp / "x") not in ro

    def test_home_at_the_root_fails_closed_with_a_hint(self, lay, monkeypatch, caplog):
        # C: with HOME=/ every path outside the config dir is a $HOME path.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(lay.cfg))
        monkeypatch.setenv("HOME", "/")
        _relink(lay.cfg / "skills", lay.safe)
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            ro, rw = _lists(lay.ws)
        assert str(lay.safe) not in ro
        assert str(lay.cfg / "plugins") in ro  # the config dir still reads
        assert rw[0] == str(lay.ws)
        assert any("$HOME is /" in m for m in _refusals(caplog))


class TestRealCliDir:
    """E: the launcher grants the real CLI's directory read + execute."""

    def _cli(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        return path

    @pytest.fixture
    def cli_path(self, monkeypatch):
        import src.backends.claude.constants as c

        def use(path: Path) -> None:
            monkeypatch.setattr(c, "CLAUDE_CLI_PATH", str(path), raising=False)
            monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)

        return use

    @pytest.mark.parametrize("where", ["home", "app", "cfg", "tmp", "base", "ws"])
    def test_a_cli_dir_that_opens_shared_state_refuses_startup(
        self, lay, confine_on, cli_path, where
    ):
        parent = {
            "home": lay.home,
            "app": lay.app,
            "cfg": lay.cfg,
            "tmp": lay.tmp,
            "base": lay.base,
            "ws": lay.base / "bob" / "claude" / "bin",
        }[where]
        cli = self._cli(parent / "claude")
        cli_path(cli)
        errors = [e for e in landlock.check_config() if "real CLI" in e]
        assert errors and str(cli.parent) in errors[0], errors
        assert "CLAUDE_CLI_PATH" in errors[0]

    def test_the_native_installers_layout_is_fine(self, lay, confine_on, cli_path):
        cli = self._cli(lay.home / ".local/share/claude/versions/2.1.283/claude")
        cli_path(cli)
        assert not [e for e in landlock.check_config() if "real CLI" in e]

    def test_the_bundled_cli_is_fine(self, lay, confine_on, monkeypatch):
        # The SDK's _bundled/ dir holds the CLI and nothing else.
        monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)
        assert not [e for e in landlock.check_config() if "real CLI" in e]

    def test_a_preset_gateway_real_cli_is_checked_too(
        self, lay, confine_on, monkeypatch
    ):
        bad = self._cli(lay.home / "claude")
        monkeypatch.setenv(landlock.REAL_CLI_ENV, str(bad))
        errors = [e for e in landlock.check_config() if "real CLI" in e]
        assert errors and str(lay.home) in errors[0]


class TestSystemRootPlacement:
    """Round-2 N-A: the launcher grants the system roots read-only to every
    session, so gateway state placed under one of them (``USER_WORKSPACES_DIR=
    /opt/ws``, a checkout in ``/opt``) was readable by every session."""

    @pytest.fixture
    def fake_opt(self, lay, monkeypatch):
        opt = lay.root / "opt"
        opt.mkdir()
        monkeypatch.setattr(landlock, "_LAUNCHER_SYSTEM_RO", ("/usr", str(opt)))
        return opt

    @pytest.mark.parametrize("what", ["base", "home", "app"])
    def test_gateway_state_under_a_system_root_refuses_startup(
        self, lay, confine_on, monkeypatch, fake_opt, what
    ):
        moved = fake_opt / what
        moved.mkdir()
        if what == "base":
            monkeypatch.setattr(workspace_manager, "base_path", moved)
        elif what == "home":
            monkeypatch.setenv("HOME", str(moved))
        else:
            monkeypatch.setattr(landlock, "_APP_ROOT", moved)
        errors = [e for e in landlock.check_config() if "lies under" in e]
        assert errors and str(moved) in errors[0], errors

    def test_the_temp_dir_under_a_system_root_refuses_startup(
        self, lay, confine_on, monkeypatch, fake_opt
    ):
        moved = fake_opt / "tmp"
        moved.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(moved))
        errors = [e for e in landlock.check_config() if "lies under" in e]
        assert errors and "temp dir" in errors[0], errors

    def test_the_uid_registry_under_a_system_root_is_refused_in_uid_mode(
        self, lay, monkeypatch, fake_opt
    ):
        from src.backends.claude import uid_isolation as ui

        monkeypatch.setattr(ui, "_registry_path", lambda: fake_opt / "uids.json")
        errors = landlock._system_ro_errors("uid")
        assert errors and "uid registry" in errors[0], errors
        assert not landlock._system_ro_errors("landlock")

    def test_a_system_root_that_resolves_to_slash_refuses_startup(
        self, lay, confine_on, monkeypatch
    ):
        # The launcher's following open would grant all of /.
        link = lay.root / "opt-link"
        link.symlink_to("/")
        monkeypatch.setattr(landlock, "_LAUNCHER_SYSTEM_RO", ("/usr", str(link)))
        errors = [e for e in landlock.check_config() if "resolves to /" in e]
        assert errors and str(link) in errors[0], errors

    def test_the_default_layout_passes(self, lay, confine_on, fake_opt):
        assert not [e for e in landlock.check_config() if "lies under" in e]

    def test_off_mode_never_checks(self, lay, monkeypatch, fake_opt):
        monkeypatch.setattr(workspace_manager, "base_path", fake_opt)
        assert not [e for e in landlock.check_config() if "lies under" in e]

    def test_the_list_matches_the_launchers(self):
        import importlib.util

        rules_path = Path(landlock.__file__).resolve().parents[3] / "docker"
        spec = importlib.util.spec_from_file_location(
            "landlock_rules_sysro", rules_path / "_landlock_rules.py"
        )
        rules = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rules)
        assert tuple(landlock._LAUNCHER_SYSTEM_RO) == tuple(rules.SYSTEM_RO)


class TestFileGrants:
    """F: a file grant is a regular file with one name."""

    def _victim(self, lay) -> Path:
        return lay.cfg / "projects" / "-other-user-ws" / "t.jsonl"

    @pytest.mark.parametrize(
        "name", ["CLAUDE.md", "settings.json", "settings.local.json", ".config.json"]
    )
    def test_a_hard_linked_config_file_is_refused(self, lay, name, caplog):
        os.link(self._victim(lay), lay.cfg / name)
        with caplog.at_level(logging.WARNING, logger=landlock.logger.name):
            ro, _ = _lists(lay.ws)
        assert str(lay.cfg / name) not in ro
        assert any("hard-linked" in m for m in _refusals(caplog))

    def test_a_hard_linked_global_config_is_refused(self, lay):
        os.link(self._victim(lay), lay.home / ".claude.json")
        ro, _ = _lists(lay.ws)
        assert str(lay.home / ".claude.json") not in ro

    def test_plain_files_are_still_granted(self, lay):
        for name in ("CLAUDE.md", "settings.json"):
            (lay.cfg / name).write_text("{}")
        (lay.home / ".claude.json").write_text("{}")
        ro, _ = _lists(lay.ws)
        assert str(lay.cfg / "CLAUDE.md") in ro
        assert str(lay.cfg / "settings.json") in ro
        assert str(lay.home / ".claude.json") in ro

    @pytest.mark.parametrize("kind", ["dir", "link-to-dir", "fifo"])
    def test_a_config_file_that_is_not_a_regular_file_is_refused(self, lay, kind):
        target = lay.cfg / "settings.local.json"
        if kind == "dir":
            target.mkdir()
        elif kind == "link-to-dir":
            target.symlink_to(lay.safe)  # a safe place, but not a file
        else:
            os.mkfifo(target)
        ro, _ = _lists(lay.ws)
        assert not [e for e in ro if e in (str(target), str(lay.safe))], ro

    def test_a_file_where_a_project_dir_belongs_is_refused(self, lay):
        own = lay.cfg / "projects" / ws_mod._project_dir_name(str(lay.ws))
        os.link(self._victim(lay), own)
        _ro, rw = _lists(lay.ws)
        assert rw == [str(lay.ws)]


@pytest.mark.skipif(bool(_landlock_skip()), reason=_landlock_skip() or "")
class TestFollowUpOnTheKernel:
    def test_a_planted_default_clone_root_reads_nothing(self, lay, confine_on):
        # e4: plugin-marketplaces -> ~/.ssh and agents -> ~/.ssh.
        _secret_home(lay)
        (lay.cfg / "plugin-marketplaces").mkdir()
        _relink(lay.cfg / "plugin-marketplaces", lay.home / ".ssh")
        _relink(lay.cfg / "agents", lay.home / ".ssh")
        out = _confined(
            lay,
            _READ_PROBE,
            str(lay.home / ".ssh" / "id_rsa"),
            str(lay.cfg / "agents" / "id_rsa"),
        )
        assert all(r.startswith("DENY") for r in out), out

    def test_home_as_the_base_reads_no_other_workspace(
        self, lay, confine_on, monkeypatch
    ):
        # e1 D, enforced: alice's session cannot read bob's workspace.
        monkeypatch.setattr(workspace_manager, "base_path", lay.home)
        alice = lay.home / "alice" / "claude"
        alice.mkdir(parents=True)
        (lay.home / "bob" / "claude").mkdir(parents=True)
        (lay.home / "bob" / "claude" / "s.txt").write_text("BOB\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(lay.home / "bob"))
        out = _confined(lay, _READ_PROBE, str(lay.home / "bob/claude/s.txt"), ws=alice)
        assert out[0].startswith("DENY"), out

    def test_a_symlinked_base_runs_in_the_resolved_workspace(
        self, lay, confine_on, monkeypatch
    ):
        # The resolved workspace is the first rw entry, and a CLI started in
        # the lexical spelling works there (its kernel cwd is that entry).
        link = lay.root / "base-link"
        link.symlink_to(lay.base)
        monkeypatch.setattr(workspace_manager, "base_path", link)
        script = (
            "import json, os\n"
            "open('own.txt', 'w').write('x')\n"
            "print(json.dumps([os.getcwd()]))\n"
        )
        out = _confined(lay, script, ws=link / "alice" / "claude")
        assert out == [str(lay.ws)], out
        assert (lay.ws / "own.txt").read_text() == "x"

    def test_a_hard_linked_claude_md_reads_nothing(self, lay, confine_on):
        # e2 F2: CLAUDE.md hard-linked to another user's transcript.
        victim = lay.cfg / "projects" / "-other-user-ws" / "t.jsonl"
        os.link(victim, lay.cfg / "CLAUDE.md")
        out = _confined(lay, _READ_PROBE, str(lay.cfg / "CLAUDE.md"), str(victim))
        assert all(r.startswith("DENY") for r in out), out
