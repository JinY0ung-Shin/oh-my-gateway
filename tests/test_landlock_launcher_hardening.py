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
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from src.backends.claude import landlock

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO / "docker" / "landlock_launcher.py"
_SYS_PY = "/usr/bin/python3"
_REC = struct.Struct("=Ii")  # docker/_landlock_rules.py REC
_ST_EXITED, _ST_SIGNALED, _ST_REFUSED = 1, 2, 3


def _skip_reason() -> str:
    if landlock.landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return "Landlock ABI < 2"
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
        landlock.RW_ENV: str(ws),
        landlock.RO_ENV: "",
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
            "os.environ['GATEWAY_LANDLOCK_RW'] = '/'\n" % str(markers / "pth")
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
            ws, "print('ran')\n", env=_env(ws, **{var: str(ws) + os.pathsep + "data"})
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
