"""The uid-mode startup probe (PR #244 review 3).

Run as the shipped image, uid mode served no turn at all — the session uid
cannot write the app-owned workspace, so the CLI died on its tmpdir — while
startup passed and every request got a 503. Startup now spawns one CLI the way
a session would and refuses uid mode (or falls back, when the operator allowed
it) when that spawn cannot work; a ``.cli-tmp`` that cannot be made fails with
its own status; ``verify()`` names a launcher/broker refusal instead of the npm
install hints.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backends.claude import landlock
from src.backends.claude import uid_isolation as ui

_REPO = Path(__file__).resolve().parents[1]
_BROKER = _REPO / "docker" / "isolation_broker.py"
_SYS_PY = "/usr/bin/python3"


def _unset(monkeypatch, name: str) -> None:
    # setenv first so the undo restores the original state even when the
    # variable was absent: install_process_env writes os.environ directly.
    monkeypatch.setenv(name, "x")
    monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for name in (
        landlock.MODE_ENV,
        landlock.FALLBACK_ENV,
        landlock.BROKER_SOCKET_ENV,
        landlock.BROKER_WAIT_ENV,
        landlock.REAL_CLI_ENV,
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK",
        ui.TICKET_SECRET_ENV,
    ):
        _unset(monkeypatch, name)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
    from src.workspace_manager import workspace_manager

    monkeypatch.setattr(workspace_manager, "base_path", tmp_path / "workspaces")
    _reset()
    yield
    _reset()


def _reset() -> None:
    landlock.reset_mode_cache()
    landlock.reset_uid_probe()
    ui.reset_captured_secret()
    ui.reset_registry_cache()


def _uid_settings(monkeypatch, uid: int) -> None:
    """Gateway uid-mode settings whose ephemeral slice is exactly *uid*."""
    for key, value in {
        landlock.MODE_ENV: "uid",
        ui.TICKET_SECRET_ENV: secrets.token_hex(16),
        ui.UID_MIN_ENV: str(uid - 1),
        ui.UID_MAX_ENV: str(uid),
        ui.EPHEMERAL_UIDS_ENV: "1",
        "APP_UID": str(uid - 3),
        ui.BROKER_UID_ENV: str(uid - 4),
    }.items():
        monkeypatch.setenv(key, value)


class TestDescribeLauncherExit:
    @pytest.mark.parametrize("code", [70, 113, 114, 115, 116, 117, 118, 127])
    def test_refusals_are_named(self, code):
        assert landlock.describe_launcher_exit(code)

    def test_tmpdir_failure_points_at_the_workspace(self):
        assert ".cli-tmp" in landlock.describe_launcher_exit(118)

    @pytest.mark.parametrize("code", [None, 0, 1, 2, 137])
    def test_other_statuses_are_the_clis_own(self, code):
        assert landlock.describe_launcher_exit(code) is None


class TestProbeSpawn:
    """The probe's own spawn, with the launcher run mocked."""

    @staticmethod
    def _probe(monkeypatch, *, result=None, exc=None):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if exc is not None:
                raise exc
            return result

        monkeypatch.setattr(subprocess, "run", fake_run)
        return landlock._uid_session_problems(), calls

    @staticmethod
    def _done(code: int, stderr: bytes = b"") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], code, b"", stderr)

    def test_spawns_the_launcher_like_a_session(self, monkeypatch, tmp_path):
        _uid_settings(monkeypatch, os.getuid())
        problems, calls = self._probe(monkeypatch, result=self._done(0))
        assert problems == []
        ((argv, kwargs),) = calls
        assert argv == [landlock.launcher_path(), "--version"]
        assert kwargs["stdin"] is subprocess.DEVNULL
        env = kwargs["env"]
        assert env[landlock.MODE_ENV] == "uid"
        assert env[landlock.TICKET_ENV]
        assert env[ui.TICKET_SECRET_ENV] == ""  # blanked like a session child's
        payload = ui.verify_ticket(env[landlock.TICKET_ENV])
        assert payload["workspace"] == kwargs["cwd"]
        assert Path(kwargs["cwd"]).name.startswith("_tmp_")
        # The probe workspace is removed again.
        assert not list((tmp_path / "workspaces").glob("_tmp_*"))

    def test_an_unwritable_workspace_is_reported(self, monkeypatch):
        _uid_settings(monkeypatch, os.getuid())
        problems, _ = self._probe(monkeypatch, result=self._done(118))
        assert len(problems) == 1
        assert "exited 118" in problems[0]
        assert ".cli-tmp" in problems[0]

    def test_the_launchers_last_stderr_line_is_quoted(self, monkeypatch):
        _uid_settings(monkeypatch, os.getuid())
        stderr = b"noise\nlandlock_launcher: cannot reach the isolation broker\n"
        problems, _ = self._probe(monkeypatch, result=self._done(70, stderr))
        assert "(landlock_launcher: cannot reach the isolation broker)" in problems[0]

    def test_a_hung_spawn_is_reported(self, monkeypatch):
        _uid_settings(monkeypatch, os.getuid())
        problems, _ = self._probe(
            monkeypatch, exc=subprocess.TimeoutExpired(cmd="launcher", timeout=1)
        )
        assert "did not finish `--version`" in problems[0]


@pytest.fixture
def open_tree():
    """A config dir every uid can search (unlike pytest's 0700 tmp tree)."""
    base = Path(tempfile.mkdtemp(prefix="omg-probe-"))
    base.chmod(0o755)
    cfg = base / ".claude"
    (cfg / "projects").mkdir(parents=True)
    cfg.chmod(0o755)
    (cfg / "projects").chmod(0o755)
    try:
        yield cfg
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TestSessionDirs:
    """Whether a session uid can reach the config dir and keep transcripts."""

    _OTHER = os.getuid() + 54321

    def test_an_unsearchable_parent_blocks_the_config_dir(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        home.chmod(0o700)  # like the image's /home/app
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
        problems = landlock._session_dir_problems(self._OTHER, self._OTHER)
        assert len(problems) == 1
        assert "cannot reach the Claude config dir" in problems[0]
        assert "0700" in problems[0]
        # Its owner is fine.
        assert landlock._session_dir_problems(os.getuid(), os.getgid()) == []

    def test_an_unwritable_projects_dir_blocks_transcripts(
        self, monkeypatch, open_tree
    ):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(open_tree))
        problems = landlock._session_dir_problems(self._OTHER, self._OTHER)
        assert len(problems) == 1
        assert "cannot create its transcript dir" in problems[0]

    def test_a_layout_a_session_uid_can_use_passes(self, monkeypatch, open_tree):
        (open_tree / "projects").chmod(0o1777)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(open_tree))
        assert landlock._session_dir_problems(self._OTHER, self._OTHER) == []


class TestStartupDecision:
    @staticmethod
    def _broker_up(monkeypatch, problems):
        monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/nonexistent/broker.sock")
        monkeypatch.setattr(landlock, "landlock_abi", lambda: 5)
        monkeypatch.setattr(landlock, "_wait_for_broker", lambda timeout: True)
        monkeypatch.setattr(landlock, "_broker_reachable", lambda: True)
        monkeypatch.setattr(landlock, "_uid_session_problems", lambda: list(problems))

    def test_a_failing_probe_refuses_uid_mode(self, monkeypatch):
        _uid_settings(monkeypatch, os.getuid())
        self._broker_up(monkeypatch, ["a CLI spawned as a session uid exited 118"])
        landlock.install_process_env()
        assert landlock.effective_mode() == "uid"  # never a silent drop
        assert not landlock.uid_mode_available()
        monkeypatch.setattr(landlock, "can_enforce_landlock", lambda: (True, ""))
        errors = landlock.check_config()
        assert len(errors) == 1
        assert "a session uid cannot run the CLI here" in errors[0]
        assert "exited 118" in errors[0]
        assert f"{landlock.FALLBACK_ENV}=landlock" in errors[0]

    def test_the_fallback_runs_landlock_and_says_why(self, monkeypatch, caplog):
        _uid_settings(monkeypatch, os.getuid())
        monkeypatch.setenv(landlock.FALLBACK_ENV, "landlock")
        self._broker_up(monkeypatch, ["the workspace is not writable"])
        with caplog.at_level(logging.WARNING):
            landlock.install_process_env()
        assert landlock.effective_mode() == "landlock"
        assert "the workspace is not writable" in caplog.text

    def test_a_working_probe_keeps_uid_mode(self, monkeypatch):
        _uid_settings(monkeypatch, os.getuid())
        self._broker_up(monkeypatch, [])
        landlock.install_process_env()
        assert landlock.effective_mode() == "uid"
        assert landlock.uid_mode_available()

    @pytest.mark.parametrize("broken", ["secret", "band", "registry"])
    def test_left_to_the_clearer_errors(self, monkeypatch, tmp_path, broken):
        _uid_settings(monkeypatch, os.getuid())
        self._broker_up(monkeypatch, ["must not run"])
        if broken == "secret":
            monkeypatch.delenv(ui.TICKET_SECRET_ENV)
        elif broken == "band":
            monkeypatch.setenv(ui.UID_MIN_ENV, "0")
        else:
            (tmp_path / "uids.json").write_text("{nope")
        landlock.install_process_env()
        assert landlock._uid_probe_problems is None
        assert landlock.effective_mode() == "uid"


_needs_same_uid_broker = pytest.mark.skipif(
    landlock.landlock_abi() < 2
    or not os.access(_SYS_PY, os.X_OK)
    or os.geteuid() == 0
    or os.getgid() != os.getuid(),
    reason="needs Landlock, /usr/bin/python3, a non-root uid equal to its gid",
)


@pytest.fixture
def broker_log(tmp_path, monkeypatch):
    """A same-uid test broker whose 'real CLI' is the system python, with the
    gateway configured to hand the probe to it."""
    sock = tmp_path / "broker.sock"
    log = tmp_path / "broker.log"
    secret = secrets.token_hex(16)
    env = {
        **os.environ,
        landlock.BROKER_SOCKET_ENV: str(sock),
        ui.TICKET_SECRET_ENV: secret,
        landlock.REAL_CLI_ENV: _SYS_PY,
        "GATEWAY_ISOLATION_PEER_UID": str(os.getuid()),
    }
    with open(log, "wb") as err:
        proc = subprocess.Popen(
            [sys.executable, str(_BROKER), "--test-same-uid"], env=env, stderr=err
        )
    try:
        for _ in range(250):
            if sock.exists() or proc.poll() is not None:
                break
            time.sleep(0.02)
        assert sock.exists(), log.read_text(errors="replace")
        _uid_settings(monkeypatch, os.getuid())
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, secret)
        monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, str(sock))
        yield log
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.integration
@_needs_same_uid_broker
class TestProbeThroughTheBroker:
    def test_passes_when_a_session_uid_can_work(self, broker_log):
        landlock.install_process_env()
        assert landlock._uid_probe_problems == [], broker_log.read_text()
        assert landlock.effective_mode() == "uid"

    def test_an_unwritable_workspace_refuses_uid_mode(
        self, broker_log, monkeypatch, tmp_path
    ):
        from src.workspace_manager import workspace_manager

        workspace = tmp_path / "workspaces" / "_tmp_readonly"
        workspace.mkdir(parents=True)
        workspace.chmod(0o555)
        monkeypatch.setattr(workspace_manager, "resolve", lambda *a, **k: workspace)
        try:
            landlock.install_process_env()
        finally:
            if workspace.exists():  # the probe removes its _tmp_ workspace
                workspace.chmod(0o755)
        problems = landlock._uid_probe_problems
        assert problems and "exited 118" in problems[0], problems
        assert "cannot create the session tmp dir" in broker_log.read_text()


class TestVerify:
    @staticmethod
    def _cli(tmp_path):
        from src.backends.claude.client import ClaudeCodeCLI

        with (
            patch("src.auth.validate_claude_code_auth", return_value=(True, {})),
            patch("src.auth.auth_manager") as mock_auth,
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}
            return ClaudeCodeCLI(cwd=str(tmp_path))

    async def test_uid_mode_verifies_in_a_fresh_workspace(self, monkeypatch, tmp_path):
        cli = self._cli(tmp_path)
        monkeypatch.setattr(landlock, "effective_mode", lambda: "uid")
        seen = {}

        def build(**kwargs):
            seen["cwd"] = kwargs.get("cwd")
            raise RuntimeError("stop after the options build")

        monkeypatch.setattr(cli, "_build_sdk_options", build)
        assert await cli.verify() is False
        workspace = seen["cwd"]
        assert workspace is not None and workspace.name.startswith("_tmp_")
        assert not workspace.exists()  # cleaned up

    async def test_a_launcher_refusal_is_named_not_an_install_hint(
        self, monkeypatch, tmp_path, caplog
    ):
        cli = self._cli(tmp_path)
        monkeypatch.setattr(landlock, "effective_mode", lambda: "landlock")

        class Refused(Exception):
            exit_code = 118

        def build(**kwargs):
            raise Refused("Command failed with exit code 118")

        monkeypatch.setattr(cli, "_build_sdk_options", build)
        with caplog.at_level(logging.WARNING):
            assert await cli.verify() is False
        assert ".cli-tmp" in caplog.text
        assert "npm install" not in caplog.text
