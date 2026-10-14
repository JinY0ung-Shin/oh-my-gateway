"""Landlock confinement launcher (issue #241, PR-A).

Two layers, neither needing the bundled Claude CLI:

* the gateway wiring (``src/backends/claude/landlock.py``) — the opt-in flag,
  fail-closed ``check_config``, the per-workspace path lists, and the
  ``cli_path``/sandbox overrides in ``client.py``;
* the launcher itself (``docker/landlock_launcher.py``) — a child it confines
  can read its workspace and the system roots but not a sibling path or the
  gateway code, proved by running a probe under it with a plain system python
  as the "real CLI".

The real bundled CLI is exercised by ``tests/test_cli_read_boundary.py``'s
``TestLandlockConfinement``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backends.claude import landlock

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO / "docker" / "landlock_launcher.py"
_SYSTEM_PYTHON = "/usr/bin/python3"


def _landlock_skip() -> str:
    if landlock.landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI {landlock.landlock_abi()} < {landlock.MIN_LANDLOCK_ABI}"
    return ""


@pytest.fixture(autouse=True)
def _reset_isolation_env(monkeypatch, tmp_path_factory):
    # effective_mode is memoized and several vars steer it; start each test from
    # a clean slate so cross-test state never leaks.
    from src.backends.claude import uid_isolation as ui

    for var in (
        landlock.FALLBACK_ENV,
        landlock.BROKER_SOCKET_ENV,
        landlock.BROKER_WAIT_ENV,
        "GATEWAY_ISOLATION_TICKET_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    # Building the path lists pre-creates plugin data and session-env dirs in
    # the config dir: keep that out of the developer's real ~/.claude.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-cfg")))
    landlock.reset_mode_cache()
    landlock.reset_uid_probe()
    ui.reset_captured_secret()
    ui.reset_registry_cache()
    yield
    landlock.reset_mode_cache()
    landlock.reset_uid_probe()
    ui.reset_captured_secret()
    ui.reset_registry_cache()


@pytest.fixture
def confine_on(monkeypatch):
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()


@pytest.fixture
def confine_off(monkeypatch):
    monkeypatch.delenv(landlock.MODE_ENV, raising=False)
    landlock.reset_mode_cache()


# ---------------------------------------------------------------------------
# Gateway wiring
# ---------------------------------------------------------------------------


class TestGatewayWiring:
    def test_off_is_a_noop(self, confine_off):
        assert landlock.isolation_mode() == "off"
        assert landlock.confine_enabled() is False
        assert landlock.check_config() == []
        assert landlock.options_env(Path("/tmp/ws")) == {}

    def test_explicit_off_mode(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "off")
        assert landlock.confine_enabled() is False
        assert landlock.check_config() == []

    def test_landlock_mode_confines(self, confine_on):
        assert landlock.isolation_mode() == "landlock"
        assert landlock.confine_enabled() is True

    def test_on_resolves_launcher_and_real_cli(self, confine_on):
        assert landlock.confine_enabled() is True
        assert landlock.launcher_path() == str(_LAUNCHER)
        # The SDK bundled CLI resolves in this repo's venv.
        assert landlock.resolve_real_cli()

    def test_on_builds_ro_rw_env(self, confine_on, tmp_path):
        env = landlock.options_env(tmp_path)
        assert env[landlock.RO_ENV] is not None
        rw = env[landlock.RW_ENV].split(os.pathsep)
        assert str(tmp_path) in rw  # the workspace is always read-write
        assert env[landlock.REAL_CLI_ENV]

    def test_operator_cli_path_override_wins_for_real_cli(
        self, confine_on, monkeypatch, tmp_path
    ):
        fake = tmp_path / "claude"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        import src.backends.claude.constants as c

        monkeypatch.setattr(c, "CLAUDE_CLI_PATH", str(fake), raising=False)
        assert landlock.resolve_real_cli() == str(fake)

    def test_install_process_env_sets_real_cli_and_skips_version_check(
        self, confine_on, monkeypatch
    ):
        monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
        landlock.install_process_env()
        assert os.environ[landlock.REAL_CLI_ENV]
        assert os.environ["CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"] == "1"

    def test_install_process_env_noop_when_off(self, confine_off, monkeypatch):
        monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)
        landlock.install_process_env()
        assert landlock.REAL_CLI_ENV not in os.environ


class TestUidMode:
    """Gateway-side uid wiring (no privilege): mode resolution, fallback, the
    ticket in options.env, and fail-closed when uid is unavailable."""

    def test_available_uid_stays_uid(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        landlock.reset_mode_cache()
        assert landlock.effective_mode() == "uid"
        assert landlock.confine_enabled() is True

    def test_unavailable_uid_without_fallback_stays_uid(self, monkeypatch):
        # Stays uid (not silently downgraded); check_config makes it a hard error.
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: False)
        landlock.reset_mode_cache()
        assert landlock.effective_mode() == "uid"

    def test_unavailable_uid_with_fallback_downgrades(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv(landlock.FALLBACK_ENV, "landlock")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: False)
        landlock.reset_mode_cache()
        assert landlock.effective_mode() == "landlock"

    def test_options_env_uid_mints_ticket(self, monkeypatch, tmp_path):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv("GATEWAY_ISOLATION_TICKET_SECRET", "s3cret")
        monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/run/x.sock")
        monkeypatch.setenv("CLAUDE_ISOLATION_UID_REGISTRY", str(tmp_path / "u.json"))
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        landlock.reset_mode_cache()
        env = landlock.options_env(tmp_path, user="alice")
        assert env[landlock.MODE_ENV] == "uid"
        assert env[landlock.BROKER_SOCKET_ENV] == "/run/x.sock"
        # the ticket verifies and carries the workspace + a uid in range
        from src.backends.claude import uid_isolation as ui

        payload = ui.verify_ticket(env[landlock.TICKET_ENV])
        assert payload["workspace"] == str(tmp_path)
        lo, hi = ui.uid_range()
        assert lo <= payload["uid"] <= hi
        # path lists are NOT in the clear — only inside the signed ticket
        assert landlock.RO_ENV not in env and landlock.RW_ENV not in env

    def test_check_config_uid_unavailable_is_error(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: False)
        landlock.reset_mode_cache()
        errors = landlock.check_config()
        assert any("uid isolation is unavailable" in e for e in errors)

    def test_check_config_uid_without_secret_is_error(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        landlock.reset_mode_cache()
        errors = landlock.check_config()
        assert any("TICKET_SECRET" in e for e in errors)

    def test_check_config_uid_accepts_a_captured_secret(self, monkeypatch):
        # Startup moves the secret out of os.environ before check_config runs;
        # the check must read the captured copy, not report it missing.
        from src.backends.claude import uid_isolation as ui

        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        landlock.reset_mode_cache()
        ui.capture_ticket_secret()
        assert ui.TICKET_SECRET_ENV not in os.environ
        errors = landlock.check_config()
        assert not any("TICKET_SECRET" in e for e in errors), errors


class TestFailClosed:
    def test_invalid_mode_is_rejected(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "bogus")
        errors = landlock.check_config()
        assert errors and "not a valid isolation mode" in errors[0]

    def test_bad_launcher_is_an_error(self, confine_on, monkeypatch):
        monkeypatch.setenv(landlock.LAUNCHER_ENV, "/nonexistent/launcher.py")
        errors = landlock.check_config()
        assert errors and "launcher" in errors[0]

    def test_unresolved_real_cli_is_an_error(self, confine_on, monkeypatch):
        monkeypatch.setattr(landlock, "resolve_real_cli", lambda: None)
        errors = landlock.check_config()
        assert any("no real CLI" in e for e in errors)

    def test_unusable_landlock_is_an_error(self, confine_on, monkeypatch):
        monkeypatch.setattr(landlock, "landlock_abi", lambda: 1)
        errors = landlock.check_config()
        assert any("Landlock cannot be enforced" in e for e in errors)

    def test_abi_2_is_refused(self, confine_on, monkeypatch):
        # #244 review 4: ABI 2 has no TRUNCATE right, so a confined session
        # could still truncate DAC-writable files outside the ruleset.
        monkeypatch.setattr(landlock, "landlock_abi", lambda: 2)
        errors = landlock.check_config()
        assert any(
            "Landlock cannot be enforced" in e and "ABI >= 3" in e for e in errors
        )

    def test_a_probe_that_cannot_run_fails_closed(self, monkeypatch):
        # #244 review 4: an enforcement probe that raised used to count as a
        # pass.
        monkeypatch.setattr(landlock, "landlock_abi", lambda: 7)

        def boom(*_a, **_k):
            raise OSError("no interpreter")

        monkeypatch.setattr(subprocess, "run", boom)
        ok, why = landlock.can_enforce_landlock()
        assert not ok
        assert "could not run" in why

    @staticmethod
    def _launcher_tree(tmp_path, *, mode=0o755):
        d = tmp_path / "bin"
        d.mkdir()
        launcher = d / "landlock_launcher.py"
        launcher.write_text("#!/usr/bin/env python3\n")
        launcher.chmod(mode)
        (d / "_landlock_rules.py").write_text("")
        return launcher

    def test_a_replaceable_launcher_is_an_error(
        self, confine_on, monkeypatch, tmp_path
    ):
        # #244 review 4: an agent-writable launcher was only a warning.
        monkeypatch.delenv(landlock.ALLOW_UNSAFE_LAUNCHER_ENV, raising=False)
        launcher = self._launcher_tree(tmp_path, mode=0o777)
        monkeypatch.setenv(landlock.LAUNCHER_ENV, str(launcher))
        errors = landlock.check_config()
        assert any(
            "could be replaced by a session" in e and str(launcher) in e
            for e in errors
        )

    def test_the_dev_opt_out_only_warns(
        self, confine_on, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv(landlock.ALLOW_UNSAFE_LAUNCHER_ENV, "true")
        launcher = self._launcher_tree(tmp_path, mode=0o777)
        monkeypatch.setenv(landlock.LAUNCHER_ENV, str(launcher))
        with caplog.at_level("WARNING", logger=landlock.__name__):
            errors = landlock.check_config()
        assert not any("could be replaced" in e for e in errors)
        assert "could be replaced by a session" in caplog.text

    def test_write_problems_cover_file_sibling_and_ancestors(
        self, monkeypatch, tmp_path
    ):
        launcher = self._launcher_tree(tmp_path)
        real_stat = Path.stat

        def fake_stat(self, *a, **k):
            st = real_stat(self, *a, **k)
            values = list(st)
            if self == launcher.parent / "_landlock_rules.py":
                values[stat.ST_MODE] = stat.S_IFREG | 0o666
            elif self == launcher.parent:
                values[stat.ST_UID] = 1000
            else:
                values[stat.ST_UID] = 0  # everything else root-owned
                values[stat.ST_MODE] &= ~0o022 | stat.S_ISVTX
            return os.stat_result(values)

        monkeypatch.setattr(Path, "stat", fake_stat)
        problems = landlock.launcher_write_problems(launcher)
        assert any("_landlock_rules.py" in p and "0666" in p for p in problems)
        assert any(f"directory {launcher.parent} " in p for p in problems)
        assert not any(p.startswith(f"{launcher} ") for p in problems)

    def test_a_sticky_world_writable_ancestor_is_allowed(
        self, monkeypatch, tmp_path
    ):
        launcher = self._launcher_tree(tmp_path)
        real_stat = Path.stat

        def fake_stat(self, *a, **k):
            values = list(real_stat(self, *a, **k))
            values[stat.ST_UID] = 0
            if self == launcher.parent.parent:
                values[stat.ST_MODE] = stat.S_IFDIR | stat.S_ISVTX | 0o777
            else:
                values[stat.ST_MODE] &= ~0o022
            return os.stat_result(values)

        monkeypatch.setattr(Path, "stat", fake_stat)
        assert landlock.launcher_write_problems(launcher) == []

    def test_the_shipped_layout_passes(self):
        # The image's /usr/local/bin copies; skipped where they are absent.
        launcher = Path("/usr/local/bin/landlock_launcher.py")
        if not launcher.exists():
            pytest.skip("no image layout here")
        assert landlock.launcher_write_problems(launcher) == []

    def test_config_check_surfaces_errors_as_config_issues(
        self, confine_on, monkeypatch
    ):
        monkeypatch.setenv(landlock.LAUNCHER_ENV, "/nonexistent/launcher.py")
        from src.config_check import _check_landlock_confinement

        issues = _check_landlock_confinement()
        assert issues and all(i.severity == "error" for i in issues)


class TestClientOverrides:
    """``_get_cli_path`` and ``_configure_sandbox`` change under confinement."""

    def _cli(self):
        from unittest.mock import patch

        with (
            patch("src.auth.validate_claude_code_auth") as v,
            patch("src.auth.auth_manager") as a,
        ):
            v.return_value = (True, {"method": "anthropic"})
            a.get_claude_code_env_vars.return_value = {"ANTHROPIC_AUTH_TOKEN": "x"}
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd="/tmp")

    def test_cli_path_is_the_launcher_when_on(self, confine_on):
        from src.backends.claude.client import _get_cli_path

        assert _get_cli_path() == str(_LAUNCHER)

    def test_sandbox_forced_off_when_on(self, confine_on):
        options = self._cli()._build_sdk_options()
        sandbox = options.sandbox
        # SandboxSettings is a TypedDict in the SDK; be tolerant of either shape.
        enabled = sandbox["enabled"] if isinstance(sandbox, dict) else sandbox.enabled
        assert sandbox is not None and enabled is False

    def test_options_carry_confinement_env_when_on(self, confine_on):
        options = self._cli()._build_sdk_options(cwd=Path("/tmp/ws-x"))
        assert options.env.get(landlock.RW_ENV)
        assert "/tmp/ws-x" in options.env[landlock.RW_ENV].split(os.pathsep)

    def test_no_confinement_env_when_off(self, confine_off):
        options = self._cli()._build_sdk_options(cwd=Path("/tmp/ws-x"))
        assert landlock.RW_ENV not in (options.env or {})


# ---------------------------------------------------------------------------
# The launcher binary (no bundled CLI; a system python stands in for it)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(bool(_landlock_skip()), reason=_landlock_skip() or "")
@pytest.mark.skipif(
    not os.access(_SYSTEM_PYTHON, os.X_OK), reason="no /usr/bin/python3"
)
class TestLauncherConfines:
    # argv: workspace, outside dir, a gateway-code file. No %-formatting here so
    # the probe body's own errno formatting stays intact.
    _PROBE = (
        "import json, sys\n"
        "def r(p):\n"
        "    try:\n"
        "        open(p).read(); return 'OK'\n"
        "    except OSError as e: return 'DENY:' + str(e.errno)\n"
        "def w(p):\n"
        "    try:\n"
        "        open(p, 'w').write('x'); return 'OK'\n"
        "    except OSError as e: return 'DENY:' + str(e.errno)\n"
        "ws, out, repo_file = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "print(json.dumps({\n"
        "  'in_read': r(ws + '/in.txt'),\n"
        "  'out_read': r(out + '/secret.txt'),\n"
        "  'repo_read': r(repo_file),\n"
        "  'in_write': w(ws + '/new.txt'),\n"
        "  'out_write': w(out + '/evil.txt'),\n"
        "  'sys_read': r('/etc/hostname'),\n"
        "}))\n"
    )

    def _run(self, tmp_path, ro="", rw=None):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "in.txt").write_text("INSIDE\n")
        (ws / "probe.py").write_text(self._PROBE)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET\n")
        repo_file = str(_REPO / "pyproject.toml")
        env = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            landlock.REAL_CLI_ENV: _SYSTEM_PYTHON,
            landlock.RW_ENV: os.pathsep.join(rw or [str(ws)]),
            landlock.RO_ENV: ro,
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(_LAUNCHER),
                str(ws / "probe.py"),
                str(ws),
                str(outside),
                repo_file,
            ],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_confines_reads_and_writes(self, tmp_path):
        r = self._run(tmp_path)
        assert r["in_read"] == "OK"  # workspace read allowed
        assert r["in_write"] == "OK"  # workspace write allowed
        assert r["sys_read"] == "OK"  # system path readable (needed; by design)
        assert r["out_read"].startswith("DENY")  # sibling dir denied
        assert r["out_write"].startswith("DENY")
        assert r["repo_read"].startswith("DENY")  # gateway code (/app) denied

    _TRUNCATE_PROBE = (
        "import json, os, sys\n"
        "def t(p):\n"
        "    try:\n"
        "        os.truncate(p, 0); return 'OK'\n"
        "    except OSError as e: return 'DENY:' + str(e.errno)\n"
        "print(json.dumps({'in': t(sys.argv[1]), 'ro': t(sys.argv[2]),"
        " 'out': t(sys.argv[3])}))\n"
    )

    def test_truncate_outside_the_writable_set_is_denied(self, tmp_path):
        # #244 review 4: DAC allows these truncates (same uid, 0644 files), so
        # only Landlock's TRUNCATE right (ABI >= 3) can refuse them.
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "probe.py").write_text(self._TRUNCATE_PROBE)
        mine = ws / "mine.txt"
        ro_dir = tmp_path / "ro"
        ro_dir.mkdir()
        shared = ro_dir / "shared.txt"
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        for f in (mine, shared, secret):
            f.write_text("KEEP\n")
        env = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            landlock.REAL_CLI_ENV: _SYSTEM_PYTHON,
            landlock.RW_ENV: str(ws),
            landlock.RO_ENV: str(ro_dir),
        }
        proc = subprocess.run(
            [
                sys.executable,
                str(_LAUNCHER),
                str(ws / "probe.py"),
                str(mine),
                str(shared),
                str(secret),
            ],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        r = json.loads(proc.stdout)
        assert r["in"] == "OK"
        assert r["ro"].startswith("DENY")
        assert r["out"].startswith("DENY")
        assert shared.read_text() == "KEEP\n"
        assert secret.read_text() == "KEEP\n"

    def test_fail_closed_without_real_cli(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), "-v"],
            cwd=str(ws),
            env={"PATH": "/usr/bin:/bin", "PWD": str(ws)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode != 0
        assert "GATEWAY_REAL_CLI" in proc.stderr

    def test_regular_file_in_ro_list_is_readable(self, tmp_path):
        # #4: a regular file (e.g. the user CLAUDE.md) in the ro list must not
        # abort the ruleset build with EINVAL (READ_DIR on a non-dir) and must
        # be readable by the confined child.
        ws = tmp_path / "ws"
        ws.mkdir()
        cfg = tmp_path / "CLAUDE.md"
        cfg.write_text("HELLO\n")
        (ws / "p.py").write_text(
            "import sys\n"
            "try: print('READ:' + open(sys.argv[1]).read().strip())\n"
            "except OSError as e: print('DENY:' + str(e.errno))\n"
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            landlock.REAL_CLI_ENV: _SYSTEM_PYTHON,
            landlock.RW_ENV: str(ws),
            landlock.RO_ENV: str(cfg),
        }
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), str(ws / "p.py"), str(cfg)],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "READ:HELLO"

    def test_cli_tmp_symlink_cannot_escape(self, tmp_path):
        # #1: a hostile .cli-tmp -> / symlink must not widen the sandbox; it is
        # no longer a rule, and the launcher replaces it with a real directory.
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET\n")
        os.symlink("/", ws / ".cli-tmp")
        (ws / "p.py").write_text(
            "import os, sys, json\n"
            "tmp = os.environ.get('TMPDIR', '')\n"
            "def w(p):\n"
            "    try:\n"
            "        open(p, 'w').write('x'); return 'OK'\n"
            "    except OSError as e: return 'DENY:' + str(e.errno)\n"
            "print(json.dumps({\n"
            "  'tmp_is_real_dir': bool(tmp) and not os.path.islink(tmp),\n"
            "  'escape_write': w(sys.argv[1]),\n"
            "}))\n"
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            landlock.REAL_CLI_ENV: _SYSTEM_PYTHON,
            landlock.RW_ENV: str(ws),
            landlock.RO_ENV: "",
        }
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), str(ws / "p.py"), str(outside / "evil")],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        r = json.loads(proc.stdout)
        assert r["tmp_is_real_dir"] is True  # symlink replaced with a real dir
        assert r["escape_write"].startswith("DENY")  # cannot write outside the ws


def _cfg(monkeypatch, tmp_path) -> Path:
    cfg = tmp_path / ".claude"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    return cfg


def _entries(env: dict, var: str) -> list:
    return env[var].split(os.pathsep)


class TestReviewRoRwLists:
    """#5/#6/#14 and the second review: the confined CLI can read its own
    config, no shared cache is writable, and CLAUDE_ADD_DIRS rides the #240
    filter (not the raw strings)."""

    _cfg = staticmethod(_cfg)

    def test_ro_includes_cli_config_files(self, confine_on, monkeypatch, tmp_path):
        # With CLAUDE_CONFIG_DIR set, CLI 2.1.283 reads <dir>/.claude.json —
        # inside the dir, not beside it — and the legacy <dir>/.config.json
        # first when that exists.
        cfg = self._cfg(monkeypatch, tmp_path)
        ro = _entries(landlock.options_env(tmp_path / "ws"), landlock.RO_ENV)
        assert str(cfg / "settings.json") in ro
        assert str(cfg / ".claude.json") in ro
        assert str(cfg / ".config.json") in ro
        assert str(cfg.parent / ".claude.json") not in ro

    def test_global_config_of_a_symlinked_config_dir(
        self, confine_on, monkeypatch, tmp_path
    ):
        # Without CLAUDE_CONFIG_DIR the CLI reads $HOME/.claude.json. The guard
        # resolves a symlinked ~/.claude, whose parent is not $HOME.
        home = tmp_path / "home"
        home.mkdir()
        real = tmp_path / "volume" / "cfg"
        real.mkdir(parents=True)
        (home / ".claude").symlink_to(real)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(home))
        ro = _entries(landlock.options_env(tmp_path / "ws"), landlock.RO_ENV)
        assert str(home / ".claude.json") in ro
        assert str(real.parent / ".claude.json") not in ro
        assert str(real / "settings.json") in ro

    def test_rw_holds_no_shared_cache(self, confine_on, monkeypatch, tmp_path):
        # Every landlock-mode session shares one uid, so a writable ~/.npm, uv
        # cache or plugins/data would let one session plant what another's
        # `npx -y`, `uv run` or plugin hook runs next. RW is the session's own.
        from src.backends.claude.workspace_sandbox import _project_dir_name

        cfg = self._cfg(monkeypatch, tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uvc"))
        ws = tmp_path / "ws"
        env = landlock.options_env(ws)
        assert _entries(env, landlock.RW_ENV) == [
            str(ws),
            str(cfg / "projects" / _project_dir_name(str(ws))),
        ]
        # plugins/data stays readable: the whole plugins tree is a ro asset.
        assert str(cfg / "plugins") in _entries(env, landlock.RO_ENV)

    def test_add_dirs_are_filtered_not_raw(self, confine_on, monkeypatch, tmp_path):
        self._cfg(monkeypatch, tmp_path)
        extra = tmp_path / "extra"
        extra.mkdir()  # a writable mount → the #240 filter must drop it
        monkeypatch.setenv("CLAUDE_ADD_DIRS", str(extra))
        env = landlock.options_env(tmp_path / "ws")
        ro = env[landlock.RO_ENV].split(os.pathsep)
        assert str(extra) not in ro


@pytest.fixture
def uid_on(monkeypatch, tmp_path):
    """uid mode with a reachable broker faked, a secret and a temp registry."""
    monkeypatch.setenv(landlock.MODE_ENV, "uid")
    monkeypatch.setenv("GATEWAY_ISOLATION_TICKET_SECRET", "s3cret")
    monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/run/x.sock")
    monkeypatch.setenv("CLAUDE_ISOLATION_UID_REGISTRY", str(tmp_path / "uids.json"))
    monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
    landlock.reset_mode_cache()


def _ticket(env: dict) -> dict:
    from src.backends.claude import uid_isolation as ui

    return ui.verify_ticket(env[landlock.TICKET_ENV])


class TestPreflightConfinement:
    """#8 and the second review (G1): the slash/catalog preflight is confined,
    runs as the session's uid in a session workspace, and runs no hook and no
    MCP server."""

    def test_session_preflight_runs_as_the_session_uid(self, uid_on, tmp_path):
        # The workspace's settings, skills and commands load in the preflight
        # too, so in uid mode it must not run as the gateway uid.
        ws = tmp_path / "ws"
        pre = landlock.preflight_options_env(ws, user="alice")
        assert pre[landlock.MODE_ENV] == "uid"
        assert landlock.RO_ENV not in pre and landlock.RW_ENV not in pre
        session = _ticket(landlock.options_env(ws, user="alice"))
        assert _ticket(pre)["uid"] == session["uid"]
        assert _ticket(pre)["workspace"] == str(ws)

    def test_anonymous_preflight_shares_the_session_uid(
        self, uid_on, monkeypatch, tmp_path
    ):
        # Keyed on the absolute workspace however the cwd was spelled: the
        # preflight's cwd is absolute, the session's may be relative.
        monkeypatch.chdir(tmp_path)
        pre = landlock.preflight_options_env(tmp_path / "ws")
        session = landlock.options_env(Path("ws"))
        assert _ticket(pre)["uid"] == _ticket(session)["uid"]

    def test_gateway_owned_preflight_is_landlock_even_in_uid_mode(
        self, uid_on, monkeypatch, tmp_path
    ):
        # No session uid to run a no-workspace preflight as.
        monkeypatch.setenv(landlock.REAL_CLI_ENV, _SYSTEM_PYTHON)
        env = landlock.preflight_options_env(tmp_path, gateway_owned=True)
        assert env[landlock.MODE_ENV] == "landlock"  # never uid (no ticket/broker)
        assert landlock.RO_ENV in env and landlock.RW_ENV in env
        assert env[landlock.REAL_CLI_ENV]

    def test_preflight_env_empty_when_off(self, confine_off, tmp_path):
        assert landlock.preflight_options_env(tmp_path) == {}
        assert landlock.preflight_options_env(tmp_path, gateway_owned=True) == {}

    def test_build_preflight_options_gives_real_cwd_and_env(
        self, confine_on, monkeypatch
    ):
        from src.backends.claude import slash_commands as sc

        monkeypatch.setenv(landlock.REAL_CLI_ENV, _SYSTEM_PYTHON)
        opts = sc._build_preflight_options(None)  # no cwd supplied
        assert opts.cwd is not None  # a real dir, never None (would grab /app)
        assert (opts.env or {}).get(landlock.MODE_ENV) == "landlock"
        assert (opts.env or {}).get("GATEWAY_ISOLATION_TICKET_SECRET") == ""

    def test_build_preflight_options_in_a_workspace_uses_the_ticket(
        self, uid_on, tmp_path
    ):
        from src.backends.claude import slash_commands as sc

        opts = sc._build_preflight_options(tmp_path / "ws", user="alice")
        assert opts.env[landlock.MODE_ENV] == "uid"
        assert _ticket(opts.env)["workspace"] == str(tmp_path / "ws")
        assert opts.env["GATEWAY_ISOLATION_TICKET_SECRET"] == ""

    @pytest.mark.parametrize("mode", ["off", "landlock"])
    def test_preflight_runs_no_hook_no_mcp_server_and_no_helper(
        self, monkeypatch, tmp_path, mode
    ):
        from src.backends.claude import slash_commands as sc

        monkeypatch.setenv(landlock.MODE_ENV, mode)
        landlock.reset_mode_cache()
        opts = sc._build_preflight_options(tmp_path)
        assert opts.strict_mcp_config is True
        assert not opts.mcp_servers
        settings = json.loads(opts.settings)
        assert settings.pop("disableAllHooks") is True
        # Every startup command the CLI 2.1.283 schema has, blanked; the four
        # observed to run in a preflight among them.
        assert {
            "apiKeyHelper",
            "awsAuthRefresh",
            "awsCredentialExport",
            "gcpAuthRefresh",
        } <= set(settings)
        assert settings == dict.fromkeys(sc._PREFLIGHT_HELPERS, "")

    @pytest.mark.parametrize("mode", ["off", "landlock", "uid"])
    def test_preflight_env_blanks_the_session_secrets(
        self, monkeypatch, tmp_path, mode
    ):
        # Whatever still runs in a preflight (a workspace apiKeyHelper did)
        # must see none of the gateway's secrets.
        from src.backends.claude import slash_commands as sc

        monkeypatch.setenv("SYSINFO_REDACTION", "true")
        monkeypatch.setenv("CLAUDE_AUTH_METHOD", "api_key")
        secrets = (
            "ADMIN_API_KEY",
            "API_KEY",
            "OPENAI_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
        for name in secrets:
            monkeypatch.setenv(name, f"{name}-value")
        monkeypatch.setenv(landlock.MODE_ENV, mode)
        monkeypatch.setenv("GATEWAY_ISOLATION_TICKET_SECRET", "s3cret")
        if mode == "uid":
            monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, "/run/x.sock")
            monkeypatch.setenv(
                "CLAUDE_ISOLATION_UID_REGISTRY", str(tmp_path / "uids.json")
            )
            monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        landlock.reset_mode_cache()
        env = sc._build_preflight_options(tmp_path / "ws", user="alice").env
        for name in (*secrets, "GATEWAY_ISOLATION_TICKET_SECRET"):
            assert env[name] == "", name
        if mode == "uid":
            assert env[landlock.MODE_ENV] == "uid"  # the confinement env survives

    def test_preflight_and_session_share_the_secret_blanks(self, monkeypatch, tmp_path):
        from src.backends.claude import slash_commands as sc
        from src.backends.claude.client import child_secret_env

        monkeypatch.setenv("SYSINFO_REDACTION", "true")
        monkeypatch.setenv("SYSINFO_CHILD_ENV_MASK", "USAGE_LOG_DB_URL")
        blanks = child_secret_env()
        expected = {"ADMIN_API_KEY", "API_KEY", "USAGE_LOG_DB_URL", "OPENAI_API_KEY"}
        assert expected <= set(blanks)
        preflight = sc._build_preflight_options(tmp_path).env
        session = TestClientOverrides()._cli()._build_sdk_options(cwd=tmp_path).env
        assert blanks.items() <= preflight.items()
        assert blanks.items() <= session.items()

    def test_preflight_cwd_is_private_per_process(self, monkeypatch, tmp_path):
        # A fixed shared name could be pre-created by another uid with a
        # planted .claude/settings.json; the dir must be fresh and 0700.
        import stat
        import tempfile

        from src.backends.claude import slash_commands as sc

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(sc, "_preflight_dir", None)
        planted = tmp_path / "gateway-slash-preflight"
        (planted / ".claude").mkdir(parents=True)
        (planted / ".claude" / "settings.json").write_text("{}")
        d = sc._preflight_cwd()
        assert d != planted and d.parent == tmp_path
        assert d.name.startswith("gateway-slash-preflight-")
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        assert list(d.iterdir()) == []
        assert sc._preflight_cwd() == d  # one per process


class TestPreflightUserThreading:
    """The user reaches the preflight from every caller that knows it."""

    async def test_validate_prompt_passes_the_user(self, monkeypatch, tmp_path):
        from src.backends.claude import slash_commands as sc

        seen = []

        async def _fake_fetch(cwd, user=None):
            seen.append(user)
            return {"review"}

        sc._cache.reset()
        monkeypatch.setattr(sc, "_fetch_commands", _fake_fetch)
        await sc.validate_prompt("/review", cwd=tmp_path, user="alice")
        assert seen == ["alice"]
        sc._cache.reset()

    async def test_skills_allowlist_passes_the_user(self, monkeypatch):
        from claude_agent_sdk import ClaudeAgentOptions

        from src.backends.claude import slash_commands as sc

        seen = []

        async def _fake_available(cwd=None, force=False, user=None):
            seen.append(user)
            return {"review"}

        monkeypatch.setattr(sc, "get_available_commands", _fake_available)
        options = ClaudeAgentOptions(cwd="/tmp/ws", skills=["review"])
        await TestClientOverrides()._cli()._apply_skills_allowlist(options, "alice")
        assert seen == ["alice"]

    async def test_responses_validation_passes_the_user(self, monkeypatch):
        import src.routes.responses as responses_module

        seen = {}

        async def _fake_validate(prompt, cwd=None, user=None):
            seen.update(prompt=prompt, cwd=cwd, user=user)

        monkeypatch.setattr(responses_module, "validate_slash_prompt", _fake_validate)
        await responses_module._validate_backend_prompt(
            SimpleNamespace(backend="claude"), "/review", "/tmp/ws", "alice"
        )
        assert seen == {"prompt": "/review", "cwd": Path("/tmp/ws"), "user": "alice"}


class TestPluginDataDirs:
    """G3: plugins/data is read-only to sessions, so the gateway pre-creates
    each installed plugin's data dir or the CLI fails the plugin's hooks."""

    @pytest.mark.parametrize(
        ("plugin_id", "names"),
        [
            # Both observed as CLI-created dirs on CLI 2.1.283.
            ("hookplug@testmkt", ["hookplug-testmkt"]),
            (
                "defect-agent-frontend@defect-agent-plugins",
                ["defect-agent-frontend-defect-agent-plugins"],
            ),
            ("a.b c@m_k", ["a-b-c-m_k"]),
            ("@scope/Pkg@npm", ["-scope-Pkg-npm", "_40scope_2f_50kg-npm"]),
            ("../..", []),
        ],
    )
    def test_data_dir_names_follow_the_cli(self, plugin_id, names):
        assert landlock._plugin_data_names(plugin_id) == names

    def test_installed_plugins_get_their_data_dir(
        self, confine_on, monkeypatch, tmp_path
    ):
        cfg = _cfg(monkeypatch, tmp_path)
        (cfg / "plugins").mkdir()
        (cfg / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {"hookplug@testmkt": [], "other@mkt": []},
                }
            )
        )
        env = landlock.options_env(tmp_path / "ws")
        data = cfg / "plugins" / "data"
        assert sorted(p.name for p in data.iterdir()) == [
            "hookplug-testmkt",
            "other-mkt",
        ]
        assert not any(e.startswith(str(data)) for e in _entries(env, landlock.RW_ENV))

    def test_no_registry_creates_nothing(self, confine_on, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        landlock.options_env(tmp_path / "ws")
        assert not (cfg / "plugins" / "data").exists()


class TestSessionEnvDir:
    """G10: the session's own session-env dir is created 0700 and granted; the
    directory of every session's env scripts is not."""

    def test_own_dir_is_created_and_granted(self, confine_on, monkeypatch, tmp_path):
        import stat

        cfg = _cfg(monkeypatch, tmp_path)
        sid = str(uuid.uuid4())
        env = landlock.options_env(tmp_path / "ws", session_id=sid)
        own = cfg / "session-env" / sid
        assert str(own) in _entries(env, landlock.RW_ENV)
        assert stat.S_IMODE(own.stat().st_mode) == 0o700
        everything = _entries(env, landlock.RW_ENV) + _entries(env, landlock.RO_ENV)
        assert str(cfg / "session-env") not in everything

    def test_unknown_session_id_grants_nothing(self, confine_on, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        env = landlock.options_env(tmp_path / "ws")
        assert not any("session-env" in e for e in _entries(env, landlock.RW_ENV))
        assert not (cfg / "session-env").exists()

    @pytest.mark.parametrize("sid", ["../escape", "a/b", ".hidden", "-x", "a b"])
    def test_unsafe_session_id_grants_nothing(
        self, confine_on, monkeypatch, tmp_path, sid
    ):
        _cfg(monkeypatch, tmp_path)
        env = landlock.options_env(tmp_path / "ws", session_id=sid)
        assert not any("session-env" in e for e in _entries(env, landlock.RW_ENV))

    def test_planted_symlink_is_not_granted(self, confine_on, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        sid = str(uuid.uuid4())
        (cfg / "session-env").mkdir()
        (cfg / "session-env" / sid).symlink_to(tmp_path)
        env = landlock.options_env(tmp_path / "ws", session_id=sid)
        assert not any("session-env" in e for e in _entries(env, landlock.RW_ENV))

    def test_removal_takes_exactly_the_sessions_dir(
        self, confine_on, monkeypatch, tmp_path
    ):
        cfg = _cfg(monkeypatch, tmp_path)
        sid, other = str(uuid.uuid4()), str(uuid.uuid4())
        landlock.options_env(tmp_path / "ws", session_id=sid)
        landlock.options_env(tmp_path / "ws", session_id=other)
        own = cfg / "session-env" / sid
        (own / "sessionstart-hook-0.sh").write_text("export X=1\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        (own / "link").symlink_to(outside)  # removed, never followed
        landlock.remove_session_env_dir(sid)
        assert not own.exists()
        assert (cfg / "session-env" / other).is_dir()
        assert (outside / "keep.txt").read_text() == "keep"

    @pytest.mark.parametrize("sid", ["../escape", "a/b", ".hidden", "", None])
    def test_removal_ignores_an_unsafe_session_id(self, monkeypatch, tmp_path, sid):
        cfg = _cfg(monkeypatch, tmp_path)
        (cfg / "session-env" / "keep").mkdir(parents=True)
        (cfg / "escape").mkdir()  # what session-env/../escape would name
        (cfg / "session-env" / "a").mkdir()  # and a/b's parent
        landlock.remove_session_env_dir(sid)
        assert (cfg / "session-env" / "keep").is_dir()
        assert (cfg / "escape").is_dir()
        assert (cfg / "session-env" / "a").is_dir()

    def test_removal_leaves_a_symlink_alone(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        sid = str(uuid.uuid4())
        target = tmp_path / "target"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        (cfg / "session-env").mkdir()
        (cfg / "session-env" / sid).symlink_to(target)
        landlock.remove_session_env_dir(sid)
        assert (cfg / "session-env" / sid).is_symlink()
        assert (target / "keep.txt").read_text() == "keep"
        landlock.remove_session_env_dir(str(uuid.uuid4()))  # missing: a no-op

    def test_stateless_cleanup_removes_it(self, confine_on, monkeypatch, tmp_path):
        # /v1/agents/messages and the admin prompt try-run share this cleanup.
        from src.routes.agent_messages import _cleanup_sdk_transcript
        from src.session_manager import Session

        cfg = _cfg(monkeypatch, tmp_path)
        sid = str(uuid.uuid4())
        ws = tmp_path / "ws"
        ws.mkdir()
        landlock.options_env(ws, session_id=sid)
        assert (cfg / "session-env" / sid).is_dir()
        _cleanup_sdk_transcript(Session(session_id=sid, workspace=str(ws)))
        assert not (cfg / "session-env" / sid).exists()

    @pytest.mark.parametrize("kind", ["session_id", "resume"])
    def test_session_options_grant_the_cli_session_id(
        self, confine_on, monkeypatch, tmp_path, kind
    ):
        # A resumed session keeps its id, so it uses the same dir.
        cfg = _cfg(monkeypatch, tmp_path)
        sid = str(uuid.uuid4())
        options = (
            TestClientOverrides()
            ._cli()
            ._build_sdk_options(cwd=tmp_path / "ws", **{kind: sid})
        )
        assert str(cfg / "session-env" / sid) in _entries(options.env, landlock.RW_ENV)


class TestAbsolutePaths:
    """G13: a relative workspace (USER_WORKSPACES_DIR=./workspaces) leaves the
    gateway absolute — the launcher would resolve it against the workspace."""

    def test_landlock_lists_are_absolute(self, confine_on, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        env = landlock.options_env(Path("workspaces") / "alice", session_id="s1")
        entries = _entries(env, landlock.RO_ENV) + _entries(env, landlock.RW_ENV)
        assert entries and all(os.path.isabs(e) for e in entries), entries
        assert str(tmp_path / "workspaces" / "alice") in _entries(env, landlock.RW_ENV)

    def test_lists_name_the_project_dir_of_the_absolute_cwd(
        self, confine_on, monkeypatch, tmp_path
    ):
        # The CLI names its project dir after its absolute cwd; a name encoded
        # from the relative spelling would be an extra, wrong grant.
        from src.backends.claude.workspace_sandbox import _project_dir_name

        cfg = _cfg(monkeypatch, tmp_path)
        monkeypatch.chdir(tmp_path)
        ws = tmp_path / "workspaces" / "alice"
        _ro, rw = landlock._ro_rw_roots(Path("workspaces") / "alice")
        assert rw == [str(ws), str(cfg / "projects" / _project_dir_name(str(ws)))]

    def test_relative_config_dir_entries_are_absolute(
        self, confine_on, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cfg").mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "cfg")
        env = landlock.options_env(tmp_path / "ws")
        assert str(tmp_path / "cfg" / ".claude.json") in _entries(env, landlock.RO_ENV)
        assert all(os.path.isabs(e) for e in _entries(env, landlock.RO_ENV))

    def test_uid_ticket_is_absolute(self, uid_on, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        payload = _ticket(landlock.options_env(Path("workspaces") / "alice"))
        assert payload["workspace"] == str(tmp_path / "workspaces" / "alice")
        assert all(os.path.isabs(e) for e in payload["ro"] + payload["rw"])

    def test_session_cwd_is_absolute(self, confine_on, monkeypatch, tmp_path):
        # The SDK copies options.cwd into PWD, which the launcher grants.
        monkeypatch.chdir(tmp_path)
        options = (
            TestClientOverrides()
            ._cli()
            ._build_sdk_options(cwd=Path("workspaces") / "alice")
        )
        assert Path(options.cwd) == tmp_path / "workspaces" / "alice"

    def test_preflight_cwd_is_absolute(self, confine_on, monkeypatch, tmp_path):
        from src.backends.claude import slash_commands as sc

        monkeypatch.chdir(tmp_path)
        (tmp_path / "workspaces" / "alice").mkdir(parents=True)
        opts = sc._build_preflight_options(Path("workspaces") / "alice")
        assert Path(opts.cwd) == tmp_path / "workspaces" / "alice"

    def test_off_leaves_the_cwd_alone(self, confine_off):
        options = (
            TestClientOverrides()
            ._cli()
            ._build_sdk_options(cwd=Path("workspaces") / "alice")
        )
        assert str(options.cwd) == str(Path("workspaces") / "alice")


class TestBrokerWait:
    """G6: startup waits (bounded) for a broker the entrypoint started a moment
    earlier; effective_mode() itself never blocks."""

    @pytest.fixture
    def uid_requested(self, monkeypatch, tmp_path):
        sock = tmp_path / "broker.sock"
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, str(sock))
        monkeypatch.setattr(landlock, "landlock_abi", lambda: 5)
        # install_process_env writes these into os.environ; restore them.
        monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
        # A bare listening socket, not a broker: the session spawn probe that
        # follows a successful wait is tested in test_uid_startup_probe.py.
        monkeypatch.setattr(landlock, "_probe_uid_sessions", lambda: None)
        landlock.reset_mode_cache()
        return sock

    @staticmethod
    def _listen_after(sock: Path, delay: float):
        import socket
        import threading

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        def bind_later():
            time.sleep(delay)
            server.bind(str(sock))
            server.listen(64)

        thread = threading.Thread(target=bind_later, daemon=True)
        thread.start()
        return server, thread

    def test_startup_waits_for_a_late_broker(self, uid_requested, monkeypatch):
        monkeypatch.setenv(landlock.BROKER_WAIT_ENV, "10")
        server, thread = self._listen_after(uid_requested, 0.5)
        try:
            started = time.monotonic()
            landlock.install_process_env()
            elapsed = time.monotonic() - started
            assert landlock.effective_mode() == "uid"
            assert 0.4 <= elapsed < 10
        finally:
            thread.join()
            server.close()

    def test_a_miss_cached_before_startup_is_resolved_again(
        self, uid_requested, monkeypatch
    ):
        monkeypatch.setenv(landlock.FALLBACK_ENV, "landlock")
        monkeypatch.setenv(landlock.BROKER_WAIT_ENV, "10")
        assert landlock.effective_mode() == "landlock"  # broker not up yet
        server, thread = self._listen_after(uid_requested, 0.2)
        try:
            landlock.install_process_env()
            assert landlock.effective_mode() == "uid"
        finally:
            thread.join()
            server.close()

    def test_the_wait_is_bounded(self, uid_requested, monkeypatch):
        monkeypatch.setenv(landlock.FALLBACK_ENV, "landlock")
        monkeypatch.setenv(landlock.BROKER_WAIT_ENV, "0.4")
        started = time.monotonic()
        landlock.install_process_env()
        elapsed = time.monotonic() - started
        assert 0.4 <= elapsed < 5
        assert landlock.effective_mode() == "landlock"

    def test_effective_mode_never_waits(self, uid_requested, monkeypatch):
        monkeypatch.setenv(landlock.FALLBACK_ENV, "landlock")
        monkeypatch.setenv(landlock.BROKER_WAIT_ENV, "30")
        started = time.monotonic()
        assert landlock.effective_mode() == "landlock"
        assert time.monotonic() - started < 2

    def test_no_wait_without_a_socket_or_landlock(self, monkeypatch):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setenv(landlock.FALLBACK_ENV, "landlock")
        monkeypatch.setenv(landlock.BROKER_WAIT_ENV, "30")
        monkeypatch.delenv(landlock.REAL_CLI_ENV, raising=False)
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
        started = time.monotonic()
        landlock.install_process_env()  # no broker socket configured
        assert time.monotonic() - started < 2

    @pytest.mark.parametrize(
        ("raw", "seconds"),
        [
            ("", 10.0),
            ("2.5", 2.5),
            ("0", 0.0),
            ("-3", 0.0),
            ("x", 10.0),
            # inf would wait forever and nan compare as no wait: the default.
            ("inf", 10.0),
            ("-inf", 10.0),
            ("nan", 10.0),
            ("1e400", 10.0),
            ("299.5", 299.5),
            ("300", 300.0),
            ("1000", 300.0),
        ],
    )
    def test_wait_knob(self, monkeypatch, raw, seconds):
        monkeypatch.setenv(landlock.BROKER_WAIT_ENV, raw)
        assert landlock._broker_wait_seconds() == seconds


class TestTicketFreshAtConnect:
    """G5: the single-use ticket is minted again right before connect(), so a
    create_client whose awaits outlast the TTL still spawns."""

    async def test_slow_preflight_still_connects_with_a_fresh_ticket(
        self, uid_on, monkeypatch, tmp_path
    ):
        from src.backends.claude import client as client_module
        from src.backends.claude import uid_isolation as ui
        from src.session_manager import Session

        monkeypatch.setenv("MCP_PROGRESS_RELAY", "false")
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr(ui, "time", SimpleNamespace(time=lambda: clock["now"]))
        minted = []
        real_mint = ui.mint_ticket

        def _recording_mint(**kwargs):
            minted.append(real_mint(**kwargs))
            return minted[-1]

        monkeypatch.setattr(ui, "mint_ticket", _recording_mint)
        cli = TestClientOverrides()._cli()

        async def _slow_allowlist(options, user=None):
            clock["now"] += 10 * 60  # a queued preflight outlasts the 120 s TTL

        monkeypatch.setattr(cli, "_apply_skills_allowlist", _slow_allowlist)
        presented = []

        class _FakeClient:
            def __init__(self, options=None, **_kwargs):
                self.options = options

            async def connect(self, prompt=None):
                presented.append(self.options.env[landlock.TICKET_ENV])

        monkeypatch.setattr(client_module, "ClaudeSDKClient", _FakeClient)
        session = Session(session_id=str(uuid.uuid4()))
        (tmp_path / "ws").mkdir()  # a real spawn's cwd exists (argv files go there)
        await cli.create_client(session, cwd=str(tmp_path / "ws"), user="alice")

        assert len(minted) == 2 and presented == [minted[-1]]
        with pytest.raises(ValueError, match="expired"):
            ui.verify_ticket(minted[0])  # the build-time ticket is stale by now
        assert ui.verify_ticket(presented[0])["workspace"] == str(tmp_path / "ws")


class TestSkipConfigCheckGuard:
    """A mistyped CLAUDE_ISOLATION_MODE is not bypassable by SKIP_CONFIG_CHECK."""

    def test_skip_does_not_bypass_invalid_mode(self, monkeypatch):
        monkeypatch.setenv("SKIP_CONFIG_CHECK", "true")
        monkeypatch.setenv(landlock.MODE_ENV, "landblock")  # typo
        landlock.reset_mode_cache()
        from src.config_check import run_startup_config_check

        with pytest.raises(RuntimeError):
            run_startup_config_check()

    def test_skip_bypasses_when_mode_valid(self, monkeypatch):
        monkeypatch.setenv("SKIP_CONFIG_CHECK", "true")
        monkeypatch.setenv(landlock.MODE_ENV, "off")
        landlock.reset_mode_cache()
        from src.config_check import run_startup_config_check

        assert run_startup_config_check() == []
