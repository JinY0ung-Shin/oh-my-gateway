"""Launcher hardening (PR #244 review 2): how ``docker/landlock_launcher.py``
starts, which paths it trusts, the environment it hands the CLI, Landlock IPC
scoping, and how it mirrors the broker's status in uid mode.

No bundled CLI: a system python stands in for the real CLI, and a fake broker
(a thread on a unix socket) drives the uid branch's status mirroring.
"""

from __future__ import annotations

import array
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from src.backends.claude import landlock
from tests.test_landlock_session_env import _TMP_PROBE, _plant

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO / "docker" / "landlock_launcher.py"
_SYS_PY = "/usr/bin/python3"
_REC = struct.Struct("=Ii")  # docker/_landlock_rules.py REC
_ST_EXITED, _ST_SIGNALED, _ST_REFUSED = 1, 2, 3


def _skip_reason() -> str:
    if landlock.landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI < {landlock.MIN_LANDLOCK_ABI}"
    if not os.access(_SYS_PY, os.X_OK):
        return "no /usr/bin/python3"
    return ""


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(_skip_reason()), reason=_skip_reason() or ""),
]

_needs_scope = pytest.mark.skipif(
    landlock.landlock_abi() < 6, reason="Landlock IPC scoping needs ABI 6"
)


def _env(ws: Path, **extra) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "PWD": str(ws),
        landlock.REAL_CLI_ENV: _SYS_PY,
        landlock.RW_ENV: json.dumps([str(ws)]),
        landlock.RO_ENV: json.dumps([]),
        **extra,
    }


def _confined(ws: Path, script: str, *args, cmd=None, env=None, timeout=30):
    """Run *script* as the 'real CLI' under the launcher (landlock mode)."""
    (ws / "probe.py").write_text(script)
    launcher = cmd or [sys.executable, str(_LAUNCHER)]
    return subprocess.run(
        [*launcher, "-I", str(ws / "probe.py"), *map(str, args)],
        cwd=str(ws),
        env=env or _env(ws),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def ws(tmp_path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


class TestStartsIsolated:
    """B4: the shebang starts the interpreter with -I, so planted site hooks
    never run before the ruleset is built."""

    def _plant(self, tmp_path: Path) -> tuple[dict, Path]:
        home = tmp_path / "home"
        markers = tmp_path / "markers"
        markers.mkdir()
        user_site = subprocess.run(
            [_SYS_PY, "-c", "import site; print(site.getusersitepackages())"],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        Path(user_site).mkdir(parents=True)
        # The attack: rewrite the rw list before the launcher reads it.
        (Path(user_site) / "evil.pth").write_text(
            "import os; open(%r, 'w').write('pth'); "
            "os.environ['GATEWAY_LANDLOCK_RW'] = '[\"/\"]'\n" % str(markers / "pth")
        )
        pypath = tmp_path / "pypath"
        pypath.mkdir()
        (pypath / "sitecustomize.py").write_text(
            "open(%r, 'w').write('sitecustomize')\n" % str(markers / "sc")
        )
        startup = tmp_path / "startup.py"
        startup.write_text("open(%r, 'w').write('startup')\n" % str(markers / "st"))
        plant = {
            "HOME": str(home),
            "PYTHONPATH": str(pypath),
            "PYTHONSTARTUP": str(startup),
        }
        return plant, markers

    def test_planted_site_hooks_have_no_effect(self, tmp_path, ws):
        plant, markers = self._plant(tmp_path)
        # Control: the plant is live for a non-isolated interpreter.
        subprocess.run(
            [_SYS_PY, "-c", "pass"],
            env={"PATH": "/usr/bin:/bin", **plant},
            check=True,
        )
        assert {p.name for p in markers.iterdir()} == {"pth", "sc"}
        for marker in markers.iterdir():
            marker.unlink()

        outside = tmp_path / "outside"
        outside.mkdir()
        probe = (
            "import sys\n"
            "try:\n"
            "    open(sys.argv[1], 'w').write('x'); print('WROTE')\n"
            "except OSError as e: print('DENY', e.errno)\n"
        )
        # Exec the launcher by its path, as the SDK does: the shebang decides.
        proc = _confined(
            ws,
            probe,
            outside / "evil.txt",
            cmd=[str(_LAUNCHER)],
            env=_env(ws, **plant),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("DENY"), proc.stdout  # rw was not widened
        assert not list(markers.iterdir())  # no planted hook ran
        assert not (outside / "evil.txt").exists()

    @pytest.mark.parametrize("name", ["landlock_launcher.py", "isolation_broker.py"])
    def test_shebang_is_isolated(self, name):
        script = _REPO / "docker" / name
        assert script.read_text().splitlines()[0] == "#!/usr/bin/env -S python3 -I"
        assert os.access(script, os.X_OK)


class TestTrustedPaths:
    """B5: the kernel's cwd, never $PWD, and only absolute rule paths."""

    def test_relative_pwd_does_not_move_the_grant(self, ws):
        # The SDK sets PWD to the cwd it was handed, which is relative when
        # USER_WORKSPACES_DIR is; the launcher must still grant the real cwd.
        probe = (
            "import os\n"
            "open('new.txt', 'w').write('x')\n"
            "print(os.environ['TMPDIR'])\n"
        )
        proc = _confined(ws, probe, env=_env(ws, PWD="ws"))
        assert proc.returncode == 0, proc.stderr
        assert (ws / "new.txt").read_text() == "x"
        assert proc.stdout.strip() == str(ws / ".cli-tmp")

    @pytest.mark.parametrize("var", [landlock.RW_ENV, landlock.RO_ENV])
    def test_relative_rule_path_is_refused(self, ws, var):
        proc = _confined(
            ws, "print('ran')\n", env=_env(ws, **{var: json.dumps([str(ws), "data"])})
        )
        assert proc.returncode == 70
        assert "ran" not in proc.stdout
        assert "is not an absolute path" in proc.stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_an_unmakeable_tmpdir_is_refused_with_the_reason(self, ws):
        # Used to be swallowed; the CLI then died on its own mkdir (#244 review 3).
        (ws / "probe.py").write_text("print('ran')\n")
        ws.chmod(0o555)
        try:
            proc = subprocess.run(
                [sys.executable, str(_LAUNCHER), "-I", str(ws / "probe.py")],
                cwd=str(ws),
                env=_env(ws),
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            ws.chmod(0o755)
        assert proc.returncode == 70
        assert "ran" not in proc.stdout
        assert "cannot create the session tmp dir" in proc.stderr


@pytest.fixture
def gateway_landlock(monkeypatch, tmp_path):
    """The gateway in landlock mode with a scratch config dir; yields it."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    # The grant filter refuses entries inside the system temp dir, where all
    # of tmp_path lives: give the gateway a temp dir of its own below it.
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    for var in (landlock.FALLBACK_ENV, "CLAUDE_ADD_DIRS", "CLAUDE_PLUGIN_CLONE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    landlock.reset_mode_cache()
    yield cfg
    landlock.reset_mode_cache()


_READ_ARGS = (
    "import json, sys\n"
    "def r(p):\n"
    "    try:\n"
    "        return open(p).read().strip()\n"
    "    except OSError as e: return 'DENY:%d' % e.errno\n"
    "print(json.dumps([r(p) for p in sys.argv[1:]]))\n"
)


class TestPathListEncoding:
    """#244 review 4: the lists are JSON arrays — a ':' in a path used to split
    one grant into others (every spawn refused, or a grant of what follows)."""

    def test_colon_paths_from_the_gateway_are_granted(self, gateway_landlock, tmp_path):
        # An admin marketplace cloned from https://git.internal:8443, in a
        # workspace base whose name has a ':' too.
        clone = tmp_path / "clones" / "git.internal:8443-abcd"
        clone.mkdir(parents=True)
        (clone / "SKILL.md").write_text("SKILL\n")
        (gateway_landlock / "plugins").mkdir()
        (gateway_landlock / "plugins" / "known_marketplaces.json").write_text(
            json.dumps({"m": {"installLocation": str(clone)}})
        )
        ws = tmp_path / "ws:2026" / "alice"
        ws.mkdir(parents=True)
        env = landlock.options_env(ws)
        assert str(clone) in json.loads(env[landlock.RO_ENV])
        assert str(ws) in json.loads(env[landlock.RW_ENV])
        env.update({"PATH": "/usr/bin:/bin", landlock.REAL_CLI_ENV: _SYS_PY})
        probe = _READ_ARGS + "open('w.txt', 'w').write('x')\n"
        proc = _confined(ws, probe, clone / "SKILL.md", env=env)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == ["SKILL"]
        assert (ws / "w.txt").read_text() == "x"

    def test_a_colon_slash_entry_grants_only_itself(self, ws, tmp_path):
        secret = tmp_path / "secret"
        secret.mkdir()
        (secret / "s.txt").write_text("SECRET\n")
        weird = Path(f"{tmp_path}/mk/x:{secret}")  # split on ':' -> grants secret/
        weird.mkdir(parents=True)
        (weird / "in.txt").write_text("IN\n")
        env = _env(ws, **{landlock.RO_ENV: json.dumps([str(weird)])})
        proc = _confined(ws, _READ_ARGS, weird / "in.txt", secret / "s.txt", env=env)
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)
        assert got[0] == "IN"
        assert got[1].startswith("DENY"), got

    @pytest.mark.parametrize("var", [landlock.RW_ENV, landlock.RO_ENV])
    @pytest.mark.parametrize(
        "value",
        [
            "/abs/path",  # the old ':'-joined format
            "not json",
            '"/abs"',
            '{"p": "/abs"}',
            "[1]",
            '["/abs", null]',
            '[""]',
            '["/a\\u0000b"]',
            '["/\\ud800"]',  # not encodable as a file name
        ],
    )
    def test_anything_but_a_list_of_paths_is_refused(self, ws, var, value):
        proc = _confined(ws, "print('ran')\n", env=_env(ws, **{var: value}))
        assert proc.returncode == 70
        assert "ran" not in proc.stdout
        assert "refusing it" in proc.stderr

    @pytest.mark.parametrize("value", [None, "", "  ", "[]"])
    def test_an_unset_or_empty_ro_list_is_no_entry(self, ws, value):
        env = _env(ws)
        env.pop(landlock.RO_ENV)
        if value is not None:
            env[landlock.RO_ENV] = value
        proc = _confined(ws, "open('w.txt', 'w').write('x')\n", env=env)
        assert proc.returncode == 0, proc.stderr
        assert (ws / "w.txt").read_text() == "x"


class TestWorkspaceIsTheGateways:
    """B (#244 review 4): the launcher granted rw on os.getcwd() — after the
    SDK's chdir followed a workspace planted as a link (``alice -> ~/.claude``,
    ``alice -> <base>``), that was the link's target: the whole config dir, or
    every user's workspace. The kernel's cwd must now be exactly the gateway's
    first rw entry — the workspace, which the gateway resolves."""

    @pytest.mark.parametrize("rw", [None, "", "[]", "elsewhere"])
    def test_a_spawn_without_its_workspace_in_rw_is_refused(self, ws, tmp_path, rw):
        env = _env(ws)
        env.pop(landlock.RW_ENV)
        if rw == "elsewhere":
            env[landlock.RW_ENV] = json.dumps([str(tmp_path)])
        elif rw is not None:
            env[landlock.RW_ENV] = rw
        proc = _confined(ws, "print('ran')\n", env=env)
        assert proc.returncode == 70
        assert "ran" not in proc.stdout
        assert "not the gateway's workspace grant" in proc.stderr

    def test_a_cwd_that_is_a_later_rw_entry_is_refused(self, ws, tmp_path):
        # The session's own projects dir is rw too, but it is not the workspace:
        # a cwd there must not pass for one (#244 review 4, round-2 N-B).
        own = tmp_path / "projects-own"
        own.mkdir()
        env = _env(ws)
        env[landlock.RW_ENV] = json.dumps([str(own), str(ws)])
        proc = _confined(ws, "print('ran')\n", env=env)
        assert proc.returncode == 70
        assert "ran" not in proc.stdout
        assert "not the gateway's workspace grant" in proc.stderr

    @pytest.mark.parametrize("target", ["cfg", "base"])
    def test_a_workspace_planted_as_a_link_is_refused(self, tmp_path, target):
        base = tmp_path / "base"
        (base / "bob").mkdir(parents=True)
        (base / "bob" / "secret.txt").write_text("BOB\n")
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        alice = base / "alice"
        os.symlink(cfg if target == "cfg" else base, alice)
        # The SDK chdirs into the gateway's name; the kernel's cwd is the target.
        proc = _confined(alice, _READ_ARGS, base / "bob" / "secret.txt")
        assert proc.returncode == 70, proc.stdout
        assert "not the gateway's workspace grant" in proc.stderr

    def test_a_real_cli_dir_holding_the_workspace_is_refused(self, ws, tmp_path):
        # The launcher grants the real CLI's dir itself, past the gateway's
        # filter: a CLI at <dir>/claude would hand the session all of <dir>.
        shim = tmp_path / "claude"
        shim.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        shim.chmod(0o755)
        env = _env(ws, **{landlock.REAL_CLI_ENV: str(shim)})
        proc = _confined(ws, "print('ran')\n", env=env)
        assert proc.returncode == 70
        assert "ran" not in proc.stdout
        assert "refusing to grant the real CLI's directory" in proc.stderr

    def test_the_resolved_workspace_runs(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        os.symlink(real, tmp_path / "alice")
        env = _env(tmp_path / "alice", **{landlock.RW_ENV: json.dumps([str(real)])})
        proc = _confined(tmp_path / "alice", "open('w.txt', 'w').write('x')\n", env=env)
        assert proc.returncode == 0, proc.stderr
        assert (real / "w.txt").read_text() == "x"


class TestGatewayEntriesFollowNoLink:
    """G (#244 review 4): the gateway's entries were opened following links,
    so a link swapped in after the gateway checked an entry — ``agents ->
    /app``, a planted ``projects/<name>`` into bob's workspace — widened the
    grant. Each is now walked from / following none; a refused one is logged
    and not granted."""

    def test_a_linked_ro_entry_is_not_granted(self, ws, tmp_path):
        secret = tmp_path / "app"
        secret.mkdir()
        (secret / "creds.txt").write_text("APP-SECRET\n")
        agents = tmp_path / "cfg" / "agents"
        agents.parent.mkdir()
        os.symlink(secret, agents)
        env = _env(ws, **{landlock.RO_ENV: json.dumps([str(agents)])})
        proc = _confined(ws, _READ_ARGS, agents / "creds.txt", env=env)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)[0].startswith("DENY")
        assert "not granting" in proc.stderr and "is a symlink" in proc.stderr
        # Control: the same tree, named without the link, is granted.
        env = _env(ws, **{landlock.RO_ENV: json.dumps([str(secret)])})
        proc = _confined(ws, _READ_ARGS, secret / "creds.txt", env=env)
        assert json.loads(proc.stdout) == ["APP-SECRET"]

    def test_a_link_above_the_entry_is_not_followed(self, ws, tmp_path):
        real = tmp_path / "cfg-real"
        (real / "skills").mkdir(parents=True)
        (real / "skills" / "s.md").write_text("S\n")
        os.symlink(real, tmp_path / "cfg")
        entry = tmp_path / "cfg" / "skills"
        env = _env(ws, **{landlock.RO_ENV: json.dumps([str(entry)])})
        proc = _confined(ws, _READ_ARGS, real / "skills" / "s.md", env=env)
        assert json.loads(proc.stdout)[0].startswith("DENY"), proc.stderr

    def test_a_linked_rw_entry_is_not_granted(self, ws, tmp_path):
        bob = tmp_path / "bob"
        bob.mkdir()
        own = tmp_path / "projects" / "alice"
        own.parent.mkdir()
        os.symlink(bob, own)
        env = _env(ws, **{landlock.RW_ENV: json.dumps([str(ws), str(own)])})
        probe = "import sys\nopen(sys.argv[1], 'w').write('x')\n"
        proc = _confined(ws, probe, own / "plant.txt", env=env)
        assert proc.returncode != 0
        assert "PermissionError" in proc.stderr
        assert not (bob / "plant.txt").exists()

    def test_a_hard_linked_file_entry_is_not_granted(self, ws, tmp_path):
        # A file rule grants the inode: a link added after the gateway checked
        # the entry (CLAUDE.md -> another user's transcript) must not count.
        theirs = tmp_path / "projects" / "bob" / "t.jsonl"
        theirs.parent.mkdir(parents=True)
        theirs.write_text("BOB-TRANSCRIPT\n")
        claude_md = tmp_path / "cfg" / "CLAUDE.md"
        claude_md.parent.mkdir()
        os.link(theirs, claude_md)
        env = _env(ws, **{landlock.RO_ENV: json.dumps([str(claude_md)])})
        proc = _confined(ws, _READ_ARGS, claude_md, env=env)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)[0].startswith("DENY")
        assert "a file with 2 links" in proc.stderr
        # Control: once it is a file of its own, it is granted.
        os.unlink(claude_md)
        claude_md.write_text("SHARED\n")
        proc = _confined(ws, _READ_ARGS, claude_md, env=env)
        assert json.loads(proc.stdout) == ["SHARED"]

    def test_a_missing_rw_entry_is_made_following_no_link(self, ws, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        os.symlink(target, tmp_path / "link")
        made = tmp_path / "made" / "dir"
        rw = [str(ws), str(tmp_path / "link" / "new"), str(made)]
        env = _env(ws, **{landlock.RW_ENV: json.dumps(rw)})
        proc = _confined(ws, "open(%r, 'w').write('x')\n" % str(made / "f"), env=env)
        assert proc.returncode == 0, proc.stderr
        assert list(target.iterdir()) == []  # never made through the link
        assert (made / "f").read_text() == "x"


class TestPlantedCliTmp:
    """#244 review 4: a regular file, FIFO or socket at .cli-tmp, or a .cli-tmp
    made unusable (chmod 000), failed every later spawn in the workspace (exit
    70, or the CLI's own exit 1) until an operator stepped in."""

    @pytest.mark.parametrize("kind", ["file", "fifo", "socket"])
    def test_a_planted_entry_is_replaced(self, ws, kind):
        _plant(ws / ".cli-tmp", kind)
        for _ in range(2):
            proc = _confined(ws, _TMP_PROBE)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == str(ws / ".cli-tmp")
        assert stat.S_ISDIR(os.lstat(ws / ".cli-tmp").st_mode)

    @pytest.mark.parametrize(
        "plant",
        [
            # the reported trigger, from inside a confined session
            "import shutil; shutil.rmtree('.cli-tmp'); open('.cli-tmp', 'w')\n",
            # its sibling: the CLI then dies on its own mkdir (exit 1)
            "import os; os.chmod('.cli-tmp', 0)\n",
        ],
    )
    def test_a_session_cannot_lock_its_workspace(self, ws, plant):
        proc = _confined(ws, plant)
        assert proc.returncode == 0, proc.stderr
        proc = _confined(ws, _TMP_PROBE)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(ws / ".cli-tmp")

    def test_a_configured_tmpdir_is_repaired_and_absolute(self, ws):
        # An operator CLAUDE_CODE_TMPDIR naming one entry in the cwd (here
        # relative) is kept, made absolute — the git config path derives from
        # it — and repaired.
        _plant(ws / "optmp", "file")
        proc = _confined(ws, _TMP_PROBE, env=_env(ws, CLAUDE_CODE_TMPDIR="optmp"))
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(ws / "optmp")
        assert (ws / "optmp" / "gitconfig").is_file()

    @pytest.mark.parametrize(
        "configured, used",
        [
            ("optmp", "optmp"),
            ("./optmp", "optmp"),
            ("{ws}/optmp", "optmp"),
            ("a/b", ".cli-tmp"),
            ("{ws}/a/b", ".cli-tmp"),
            ("../optmp", ".cli-tmp"),
            (".", ".cli-tmp"),
            ("/tmp/optmp", ".cli-tmp"),
        ],
    )
    def test_a_configured_tmpdir_must_be_one_entry(self, ws, configured, used):
        env = _env(ws, CLAUDE_CODE_TMPDIR=configured.format(ws=ws))
        proc = _confined(ws, _TMP_PROBE, env=env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(ws / used)

    def test_a_configured_path_through_a_link_stays_inside(self, ws, tmp_path):
        # N2: `a` swapped for a link after the old realpath check steered the
        # unconfined launcher's unlink, mkdir and git config write outside the
        # workspace. A value of more than one entry is not used at all.
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, ws / "a")
        proc = _confined(ws, _TMP_PROBE, env=_env(ws, CLAUDE_CODE_TMPDIR="a/b"))
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(ws / ".cli-tmp")
        assert list(outside.iterdir()) == []


class TestSessionEnv:
    """B10: npm/uv caches go to the session tmp dir, overriding inherited ones."""

    def test_tool_caches_are_per_session(self, ws):
        probe = (
            "import json, os\n"
            "keys = ['TMPDIR', 'CLAUDE_CODE_TMPDIR', 'npm_config_cache',\n"
            "        'NPM_CONFIG_CACHE', 'UV_CACHE_DIR']\n"
            "print(json.dumps({k: os.environ.get(k) for k in keys}))\n"
        )
        env = _env(
            ws,
            npm_config_cache="/home/app/.npm",
            NPM_CONFIG_CACHE="/home/app/.npm",
            UV_CACHE_DIR="/home/app/.cache/uv",
        )
        proc = _confined(ws, probe, env=env)
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)
        tmp = str(ws / ".cli-tmp")
        assert got["TMPDIR"] == got["CLAUDE_CODE_TMPDIR"] == tmp
        assert got["npm_config_cache"] == got["NPM_CONFIG_CACHE"] == tmp + "/npm-cache"
        assert got["UV_CACHE_DIR"] == tmp + "/uv-cache"

    def test_sigpipe_is_back_at_default(self, ws):
        # Python ignores SIGPIPE/SIGXFSZ at startup and ignored dispositions
        # survive execve; the exec'd CLI must start at the default. A python
        # probe would re-ignore them itself, so sh reports the inherited mask.
        env = _env(ws, **{landlock.REAL_CLI_ENV: "/bin/sh"})
        (ws / "probe.sh").write_text("grep SigIgn /proc/self/status\n")
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), str(ws / "probe.sh")],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        ignored = int(proc.stdout.split()[1], 16)
        assert not ignored & (1 << (signal.SIGPIPE - 1))
        assert not ignored & (1 << (signal.SIGXFSZ - 1))


class TestIpcScoping:
    """Landlock ABI 6 scoping: a confined process cannot signal, or reach an
    abstract unix socket of, anything outside its domain — but its own
    processes still signal each other."""

    @_needs_scope
    def test_signals_stay_inside_the_domain(self, ws):
        outsider = subprocess.Popen(["sleep", "30"])
        try:
            probe = (
                "import os, signal, sys, time\n"
                "outsider = int(sys.argv[1])\n"
                "res = {}\n"
                "kid = os.fork()\n"
                "if kid == 0:\n"
                "    os.kill(os.getppid(), 0)\n"  # child -> parent: same domain
                "    time.sleep(30); os._exit(0)\n"
                "os.kill(kid, signal.SIGTERM)\n"  # parent -> child: same domain
                "_, st = os.waitpid(kid, 0)\n"
                "res = [os.WIFSIGNALED(st)]\n"
                "try:\n"
                "    os.kill(outsider, signal.SIGTERM); res.append('KILLED')\n"
                "except PermissionError: res.append('EPERM')\n"
                "try:\n"
                "    os.kill(os.getppid(), 0); res.append('PARENT')\n"
                "except PermissionError: res.append('EPERM')\n"
                "print(res)\n"
            )
            proc = _confined(ws, probe, outsider.pid)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "[True, 'EPERM', 'EPERM']"
            assert outsider.poll() is None  # the outsider was never hit
        finally:
            outsider.kill()
            outsider.wait()

    @_needs_scope
    def test_abstract_unix_sockets_stay_inside_the_domain(self, ws):
        name = "\0gw-scope-test-%d" % os.getpid()
        outside = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        outside.bind(name)
        outside.listen(1)
        try:
            probe = (
                "import socket, sys\n"
                "out = []\n"
                "c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "try:\n"
                "    c.connect(sys.argv[1].replace('@', '\\0')); out.append('OUT')\n"
                "except PermissionError: out.append('EPERM')\n"
                "own = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "own.bind('\\0gw-scope-own'); own.listen(1)\n"
                "c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "c2.connect('\\0gw-scope-own'); out.append('OWN')\n"
                "print(out)\n"
            )
            proc = _confined(ws, probe, name.replace("\0", "@"))
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "['EPERM', 'OWN']"
        finally:
            outside.close()


class _FakeBroker:
    """Accepts one spawn, swallows the header and fds, replies with *reply*."""

    def __init__(self, path: Path, reply: bytes | None):
        self.path = path
        self.reply = reply
        self.header = None
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.listener.accept()
        with conn:
            buf, fds = b"", []
            while len(buf) < 4 or len(buf) < 4 + struct.unpack_from("=I", buf)[0]:
                data, anc, _flags, _addr = conn.recvmsg(1 << 16, 64)
                for _level, _kind, cdata in anc:
                    fds.extend(array.array("i", cdata[: len(cdata) // 4 * 4]))
                if not data:
                    break
                buf += data
            for fd in fds:
                os.close(fd)
            self.header = json.loads(buf[4:])
            if self.reply is not None:
                conn.sendall(self.reply)

    def close(self):
        self.listener.close()


@pytest.mark.parametrize(
    "reply, returncode",
    [
        (_REC.pack(_ST_EXITED, 0), 0),
        (_REC.pack(_ST_EXITED, 3), 3),
        (_REC.pack(_ST_EXITED, 137), 137),
        (_REC.pack(_ST_EXITED, 255), 255),
        (_REC.pack(_ST_SIGNALED, signal.SIGTERM), -signal.SIGTERM),
        (_REC.pack(_ST_SIGNALED, signal.SIGKILL), -signal.SIGKILL),
        (_REC.pack(_ST_SIGNALED, signal.SIGSEGV), -signal.SIGSEGV),
        # stop/ignore-by-default signals are never self-raised: 128+N instead
        (_REC.pack(_ST_SIGNALED, signal.SIGTSTP), 128 + signal.SIGTSTP),
        (_REC.pack(_ST_SIGNALED, signal.SIGCHLD), 128 + signal.SIGCHLD),
        (_REC.pack(_ST_REFUSED, 0), 70),
        (_REC.pack(99, 0), 70),
        (None, 70),  # the link closes without a status
    ],
)
def test_uid_launcher_mirrors_the_broker_status(tmp_path, ws, reply, returncode):
    fake = _FakeBroker(tmp_path / "fake.sock", reply)
    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "CLAUDE_ISOLATION_MODE": "uid",
            "GATEWAY_ISOLATION_TICKET": "sig.body",
            "GATEWAY_ISOLATION_BROKER_SOCKET": str(fake.path),
            "MY_MARKER": "m",
        }
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), "--print"],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,  # a self-stopped launcher would hang here
        )
        assert proc.returncode == returncode, proc.stderr
        if reply == _REC.pack(_ST_REFUSED, 0):
            assert "refused the spawn" in proc.stderr
        fake.thread.join(timeout=5)
        assert fake.header["ticket"] == "sig.body"
        assert fake.header["argv"] == ["--print"]
        assert fake.header["env"]["MY_MARKER"] == "m"
        assert "GATEWAY_ISOLATION_TICKET" not in fake.header["env"]
    finally:
        fake.close()


def test_min_abi_matches_the_gateway():
    """``docker/_landlock_rules.MIN_ABI`` and the gateway's startup floor are one
    number (the broker/launcher cannot import ``src``)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_landlock_rules_parity", _REPO / "docker" / "_landlock_rules.py"
    )
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    assert rules.MIN_ABI == landlock.MIN_LANDLOCK_ABI == 3


def test_the_rules_module_refuses_abi_2(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_landlock_rules_abi2", _REPO / "docker" / "_landlock_rules.py"
    )
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    monkeypatch.setattr(rules, "abi", lambda: 2)
    with pytest.raises(rules.LandlockError, match="ABI 2 < 3"):
        rules.build_and_restrict([], [])
