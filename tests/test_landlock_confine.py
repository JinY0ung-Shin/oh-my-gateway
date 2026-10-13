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
import subprocess
import sys
from pathlib import Path

import pytest

from src.backends.claude import landlock

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO / "docker" / "landlock_launcher.py"
_SYSTEM_PYTHON = "/usr/bin/python3"


def _landlock_skip() -> str:
    if landlock.landlock_abi() < 2:
        return f"Landlock ABI {landlock.landlock_abi()} < 2"
    return ""


@pytest.fixture(autouse=True)
def _reset_isolation_env(monkeypatch):
    # effective_mode is memoized and several vars steer it; start each test from
    # a clean slate so cross-test state never leaks.
    for var in (
        landlock.FALLBACK_ENV,
        landlock.BROKER_SOCKET_ENV,
        "GATEWAY_ISOLATION_TICKET_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    landlock.reset_mode_cache()
    yield
    landlock.reset_mode_cache()


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

        with patch("src.auth.validate_claude_code_auth") as v, patch(
            "src.auth.auth_manager"
        ) as a:
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


class TestReviewRoRwLists:
    """#5/#6/#14: the confined CLI can read its own config, write its runtime
    caches, and CLAUDE_ADD_DIRS rides the #240 filter (not the raw strings)."""

    def _cfg(self, monkeypatch, tmp_path) -> Path:
        cfg = tmp_path / ".claude"
        cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        return cfg

    def test_ro_includes_cli_config_files(self, confine_on, monkeypatch, tmp_path):
        cfg = self._cfg(monkeypatch, tmp_path)
        env = landlock.options_env(tmp_path / "ws")
        ro = env[landlock.RO_ENV].split(os.pathsep)
        assert str(cfg / "settings.json") in ro
        assert str(cfg.parent / ".claude.json") in ro

    def test_rw_includes_runtime_caches(self, confine_on, monkeypatch, tmp_path):
        cfg = self._cfg(monkeypatch, tmp_path)
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uvc"))
        env = landlock.options_env(tmp_path / "ws")
        rw = env[landlock.RW_ENV].split(os.pathsep)
        assert str(cfg / "plugins" / "data") in rw
        assert str(tmp_path / "uvc") in rw

    def test_add_dirs_are_filtered_not_raw(self, confine_on, monkeypatch, tmp_path):
        self._cfg(monkeypatch, tmp_path)
        extra = tmp_path / "extra"
        extra.mkdir()  # a writable mount → the #240 filter must drop it
        monkeypatch.setenv("CLAUDE_ADD_DIRS", str(extra))
        env = landlock.options_env(tmp_path / "ws")
        ro = env[landlock.RO_ENV].split(os.pathsep)
        assert str(extra) not in ro


class TestPreflightConfinement:
    """#8: the slash/catalog preflight is confined (landlock shape, real cwd)."""

    def test_preflight_env_is_landlock_even_in_uid_mode(self, monkeypatch, tmp_path):
        monkeypatch.setenv(landlock.MODE_ENV, "uid")
        monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
        monkeypatch.setenv(landlock.REAL_CLI_ENV, _SYSTEM_PYTHON)
        landlock.reset_mode_cache()
        env = landlock.preflight_options_env(tmp_path)
        assert env[landlock.MODE_ENV] == "landlock"  # never uid (no ticket/broker)
        assert landlock.RO_ENV in env and landlock.RW_ENV in env
        assert env[landlock.REAL_CLI_ENV]

    def test_preflight_env_empty_when_off(self, confine_off, tmp_path):
        assert landlock.preflight_options_env(tmp_path) == {}

    def test_build_preflight_options_gives_real_cwd_and_env(
        self, confine_on, monkeypatch
    ):
        from src.backends.claude import slash_commands as sc

        monkeypatch.setenv(landlock.REAL_CLI_ENV, _SYSTEM_PYTHON)
        opts = sc._build_preflight_options(None)  # no cwd supplied
        assert opts.cwd is not None  # a real dir, never None (would grab /app)
        assert (opts.env or {}).get(landlock.MODE_ENV) == "landlock"
        assert (opts.env or {}).get("GATEWAY_ISOLATION_TICKET_SECRET") == ""


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
