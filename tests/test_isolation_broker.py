"""The uid-isolation broker protocol (issue #241, PR-C), same-uid.

Validates the design's crux (docs/security-process-isolation.md §3 B): the SDK
owns the CLI's pipes, but a same-uid launcher stub hands fds 0/1/2 to the broker
over SCM_RIGHTS with a signed ticket, and the broker forks the CLI onto them,
confined with Landlock, mirroring the exit status back.

The broker runs with the test-only ``--test-same-uid`` flag and the ticket names
this test's own uid, so no uid switch happens — everything *except* the
privileged cross-uid switch runs here. ``TestProductionMode`` starts it without
the flag to pin that the broker's own uid is then refused. The real cross-uid
switch is container-only.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import pwd
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.backends.claude import uid_isolation as ui
from tests.test_landlock_session_env import (
    _BASH,
    _GIT,
    _GIT_PROBE,
    _SHELL_PROBE,
    _TMP_PROBE,
    _WRAPPER,
    _assert_git_works,
    _git_home,
    _plant,
)

_REPO = Path(__file__).resolve().parents[1]
_BROKER = _REPO / "docker" / "isolation_broker.py"
_LAUNCHER = _REPO / "docker" / "landlock_launcher.py"
_SYS_PY = "/usr/bin/python3"
_REC = struct.Struct("=Ii")  # docker/_landlock_rules.py REC
_ST_REFUSED = 3


def _landlock_abi() -> int:
    from src.backends.claude import landlock

    return landlock.landlock_abi()


def _landlock_skip() -> str:
    from src.backends.claude import landlock

    if _landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI < {landlock.MIN_LANDLOCK_ABI}"
    if not os.access(_SYS_PY, os.X_OK):
        return "no /usr/bin/python3"
    return ""


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(_landlock_skip()), reason=_landlock_skip() or ""),
]

# A broker wrapper for fault injection: patches os.* inside the broker process,
# then runs the real broker script as __main__. argv: mode, broker, flags...
_SEAM = r"""
import errno, os, runpy, sys
mode, broker = sys.argv[1], sys.argv[2]
sys.argv = [broker, *sys.argv[3:]]
resident = os.getpid()
if mode == "deny-resident-signals":
    # Simulates the cross-uid EPERM the resident broker would get signalling a
    # supervisor that switched uid (it holds no CAP_KILL).
    real_kill, real_killpg = os.kill, os.killpg

    def kill(pid, sig):
        if os.getpid() == resident:
            raise PermissionError(errno.EPERM, "seam: no signals from the broker")
        return real_kill(pid, sig)

    def killpg(pgid, sig):
        if os.getpid() == resident:
            raise PermissionError(errno.EPERM, "seam: no signals from the broker")
        return real_killpg(pgid, sig)

    os.kill, os.killpg = kill, killpg
elif mode == "fail-first-fork":
    real_fork, calls = os.fork, [0]

    def fork():
        calls[0] += 1
        if calls[0] == 1:
            raise OSError(errno.EAGAIN, "seam: fork failed")
        return real_fork()

    os.fork = fork
elif mode == "stop-orphan-killer":
    # Stops the orphan killer the resident forks before it kills anything, as
    # a session's orphan could stop a (U, U, U) killer without Landlock signal
    # scoping (same-uid there is no credential split to refuse the signal).
    import signal

    real_fork = os.fork

    def fork():
        caller = sys._getframe(1).f_code.co_name
        pid = real_fork()
        if pid == 0 and caller == "_kill_as":
            os.kill(os.getpid(), signal.SIGSTOP)
        return pid

    os.fork = fork
elif mode == "slow-cleanup":
    # Holds a cleanup helper inside its run: unlinking an entry named
    # slow-marker takes 3 s (only a cleanup ever unlinks one).
    import time

    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if os.path.basename(os.fsdecode(path)) == "slow-marker":
            sys.stderr.write("seam: slow unlink\n")
            sys.stderr.flush()
            time.sleep(3)
        return real_unlink(path, dir_fd=dir_fd)

    os.unlink = unlink
elif mode == "eacces-prefix":
    # An entry named eacces-* cannot be removed from its directory, as when U
    # may not write a parent the gateway owns (same-uid, every dir is ours).
    real_unlink, real_rmdir = os.unlink, os.rmdir

    def denied(path):
        return os.path.basename(os.fsdecode(path)).startswith("eacces-")

    def unlink(path, *, dir_fd=None):
        if denied(path):
            raise PermissionError(errno.EACCES, "seam: parent not writable")
        return real_unlink(path, dir_fd=dir_fd)

    def rmdir(path, *, dir_fd=None):
        if denied(path):
            raise PermissionError(errno.EACCES, "seam: parent not writable")
        return real_rmdir(path, dir_fd=dir_fd)

    os.unlink, os.rmdir = unlink, rmdir
runpy.run_path(broker, run_name="__main__")
"""


def _mint(secret: str, **fields) -> str:
    """A ticket in uid_isolation's wire format with arbitrary payload fields."""
    payload = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "ro": [],
        "rw": [],
        "env_allow": [],
        "nonce": secrets.token_hex(16),
        "exp": int(time.time()) + 120,
    }
    payload.update(fields)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{sig}.{body}"


class _Broker:
    def __init__(self, proc, sock: Path, secret: str, log: Path):
        self.proc = proc
        self.sock = sock
        self.secret = secret
        self.log = log

    def alive(self) -> bool:
        return self.proc.poll() is None

    def log_text(self) -> str:
        return self.log.read_text(errors="replace")

    def ticket(self, ws: Path, **fields) -> str:
        fields.setdefault("workspace", str(ws))
        fields.setdefault("rw", [str(ws)])
        return _mint(self.secret, **fields)

    def op_ticket(self, op, **fields) -> str:
        return _mint_op(self.secret, op, **fields)

    def fd_count(self) -> int:
        return len(os.listdir(f"/proc/{self.proc.pid}/fd"))


@pytest.fixture
def start_broker(tmp_path):
    started = []

    def start(*, test_same_uid=True, seam=None, extra_env=None):
        n = len(started)
        sock = tmp_path / f"broker{n}.sock"
        log = tmp_path / f"broker{n}.log"
        secret = "test-secret-123"
        env = {
            **os.environ,
            ui.BROKER_SOCKET_ENV: str(sock),
            ui.TICKET_SECRET_ENV: secret,
            "GATEWAY_REAL_CLI": _SYS_PY,
            "GATEWAY_ISOLATION_PEER_UID": str(os.getuid()),
        }
        # The same-uid broker's cleanup op sweeps this test dir for shm, or
        # nothing: never one a developer's shell happened to export.
        env.pop(_TEST_SHM_ENV, None)
        env.update(extra_env or {})
        flags = ["--test-same-uid"] if test_same_uid else []
        if seam:
            cmd = [sys.executable, "-c", _SEAM, seam, str(_BROKER), *flags]
        else:
            cmd = [sys.executable, str(_BROKER), *flags]
        with open(log, "wb") as err:
            proc = subprocess.Popen(cmd, env=env, stderr=err)
        broker = _Broker(proc, sock, secret, log)
        started.append(broker)
        for _ in range(200):
            if sock.exists():
                return broker
            if proc.poll() is not None:
                raise RuntimeError("broker exited early: %s" % broker.log_text())
            time.sleep(0.02)
        proc.kill()
        raise RuntimeError("broker never created its socket")

    yield start
    for broker in started:
        broker.proc.kill()
        broker.proc.wait(timeout=5)


@pytest.fixture
def broker(start_broker):
    return start_broker()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_dead(pids, timeout=10.0) -> list:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [p for p in pids if _alive(p)]
        if not alive:
            return []
        time.sleep(0.05)
    return [p for p in pids if _alive(p)]


def _wait_file(path: Path, timeout=10.0, proc=None) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text().strip()
            if text:
                return text
        if proc is not None and proc.poll() is not None:
            raise RuntimeError("launcher exited before %s appeared" % path.name)
        time.sleep(0.02)
    raise RuntimeError("%s never appeared" % path)


def _workspace(tmp_path: Path, name="ws") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    return ws


def _launcher_env(broker: _Broker, ws: Path, ticket: str | None = None, **extra):
    return {
        "PATH": "/usr/bin:/bin",
        "PWD": str(ws),
        "CLAUDE_ISOLATION_MODE": "uid",
        "GATEWAY_ISOLATION_TICKET": ticket or broker.ticket(ws),
        ui.BROKER_SOCKET_ENV: str(broker.sock),
        **extra,
    }


def _run(broker, ws, script: str, *args, ticket=None, timeout=30, **extra):
    (ws / "cli.py").write_text(script)
    return subprocess.run(
        [sys.executable, str(_LAUNCHER), str(ws / "cli.py"), *map(str, args)],
        cwd=str(ws),
        env=_launcher_env(broker, ws, ticket, **extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _popen(broker, ws, script: str, *args, **extra):
    (ws / "cli.py").write_text(script)
    return subprocess.Popen(
        [sys.executable, str(_LAUNCHER), str(ws / "cli.py"), *map(str, args)],
        cwd=str(ws),
        env=_launcher_env(broker, ws, **extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


_PROBE = (
    "import json, sys\n"
    "def r(p):\n"
    "    try:\n"
    "        open(p).read(); return 'OK'\n"
    "    except OSError as e: return 'DENY:' + str(e.errno)\n"
    "ws, out = sys.argv[1], sys.argv[2]\n"
    "print(json.dumps({'in': r(ws + '/in.txt'), 'out': r(out + '/secret.txt')}))\n"
)

# Starts a detached grandchild (own session, like a CLI Bash command) and a
# double-forked one, records their pids, and exits.
_SPAWN_ESCAPEES = (
    "import os, subprocess, sys, time\n"
    "out = sys.argv[1]\n"
    "a = subprocess.Popen(['sleep', '300'], start_new_session=True).pid\n"
    "r, w = os.pipe()\n"
    "if os.fork() == 0:\n"
    "    os.setsid()\n"
    "    if os.fork() == 0:\n"
    "        os.write(w, str(os.getpid()).encode()); time.sleep(300); os._exit(0)\n"
    "    os._exit(0)\n"
    "os.close(w); b = int(os.read(r, 32))\n"
    "open(out, 'w').write('%d %d' % (a, b))\n"
)

# Ignores SIGTERM, starts a detached child that ignores it too, records
# "cli_pid child_pid supervisor_pid", then waits forever.
_STUBBORN = (
    "import os, signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "kid = subprocess.Popen(\n"
    "    [sys.executable, '-c',\n"
    "     'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);'\n"
    "     ' time.sleep(300)'],\n"
    "    start_new_session=True).pid\n"
    "open(sys.argv[1], 'w').write('%d %d %d' % (os.getpid(), kid, os.getppid()))\n"
    "time.sleep(300)\n"
)


class TestBrokerProtocol:
    def test_fd_passing_and_confinement(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        (ws / "in.txt").write_text("INSIDE\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET\n")
        proc = _run(broker, ws, _PROBE, ws, outside)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["in"] == "OK"  # workspace read reached the CLI's pipe
        assert result["out"].startswith("DENY")  # confined outside the workspace

    def test_cli_exit_code_is_mirrored(self, broker, tmp_path):
        proc = _run(broker, _workspace(tmp_path), "import sys; sys.exit(7)\n")
        assert proc.returncode == 7, proc.stderr

    def test_env_propagated_but_ticket_stripped(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        script = (
            "import json, os\n"
            "print(json.dumps({\n"
            "  'marker': os.environ.get('MY_MARKER'),\n"
            "  'ticket': os.environ.get('GATEWAY_ISOLATION_TICKET'),\n"
            "  'secret': os.environ.get('GATEWAY_ISOLATION_TICKET_SECRET'),\n"
            "  'socket': os.environ.get('GATEWAY_ISOLATION_BROKER_SOCKET'),\n"
            "}))\n"
        )
        proc = _run(
            broker,
            ws,
            script,
            MY_MARKER="hello",  # a gateway-set child var must reach the CLI
            GATEWAY_ISOLATION_TICKET_SECRET=broker.secret,
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["marker"] == "hello"  # SDK-built env reached the CLI
        assert result["ticket"] is None  # plumbing stripped
        assert result["secret"] is None
        assert result["socket"] is None

    def test_session_tmp_and_tool_caches_are_per_session(self, broker, tmp_path):
        # B10: npm/uv caches live in the session tmp dir, overriding inherited
        # (shared, app-owned) values; PWD names the workspace the CLI runs in.
        ws = _workspace(tmp_path)
        script = (
            "import json, os\n"
            "keys = ['TMPDIR', 'CLAUDE_CODE_TMPDIR', 'npm_config_cache',\n"
            "        'NPM_CONFIG_CACHE', 'UV_CACHE_DIR', 'PWD']\n"
            "print(json.dumps({k: os.environ.get(k) for k in keys}))\n"
        )
        proc = _run(
            broker,
            ws,
            script,
            NPM_CONFIG_CACHE="/home/app/.npm",
            UV_CACHE_DIR="/home/app/.cache/uv",
            PWD="relative/ws",
        )
        assert proc.returncode == 0, proc.stderr
        env = json.loads(proc.stdout)
        tmp = str(ws / ".cli-tmp")
        assert env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"] == tmp
        assert env["npm_config_cache"] == env["NPM_CONFIG_CACHE"] == tmp + "/npm-cache"
        assert env["UV_CACHE_DIR"] == tmp + "/uv-cache"
        assert env["PWD"] == str(ws)

    def test_ticket_single_use_replay_refused(self, broker, tmp_path):
        """A nonce is spent in the resident parent, so a replay is refused even
        though each connection is served by a fresh fork (#241 review #13)."""
        ws = _workspace(tmp_path)
        ticket = broker.ticket(ws)
        first = _run(broker, ws, "print('ran')\n", ticket=ticket)
        assert first.returncode == 0, first.stderr
        assert "ran" in first.stdout
        second = _run(broker, ws, "print('ran')\n", ticket=ticket)  # same nonce
        assert second.returncode == 70
        assert "refused" in second.stderr
        assert "ran" not in second.stdout

    def test_a_live_uid_is_refused_for_another_workspace(self, broker, tmp_path):
        """#244 review 4: a uid still running for one workspace is never handed
        to a run in another one, whatever the gateway's pool asked for; the same
        workspace (a named user's next session) and a finished run's uid are."""
        ws_a = _workspace(tmp_path, "a")
        ws_b = _workspace(tmp_path, "b")
        pidfile = ws_a / "cli.pid"
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        holder = _popen(broker, ws_a, script, pidfile)
        try:
            _wait_file(pidfile, proc=holder)
            other = _run(broker, ws_b, "print('ran')\n")
            assert other.returncode == 70
            assert "ran" not in other.stdout
            assert "still live for another workspace" in broker.log_text()
            same = _run(broker, ws_a, "print('ran')\n")
            assert same.returncode == 0, same.stderr
        finally:
            holder.kill()
            holder.wait(timeout=5)
        deadline = time.monotonic() + 10
        while True:
            after = _run(broker, ws_b, "print('ran')\n")
            if after.returncode == 0 or time.monotonic() > deadline:
                break
            time.sleep(0.1)  # the supervisor is reaped on the broker's next tick
        assert after.returncode == 0, after.stderr
        assert "ran" in after.stdout

    def test_forged_ticket_refused(self, broker, tmp_path, monkeypatch):
        ws = _workspace(tmp_path)
        # a ticket signed with the WRONG secret must be refused by the broker
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "attacker-secret")
        forged = ui.mint_ticket(
            uid=os.getuid(), workspace=str(ws), ro=[], rw=[str(ws)], env_allow=[]
        )
        proc = _run(broker, ws, "print('ran')\n", ticket=forged)
        assert proc.returncode == 70  # broker refuses; the CLI never ran
        assert "ran" not in proc.stdout
        assert "bad ticket signature" in broker.log_text()

    @pytest.mark.skipif(
        os.getgid() != os.getuid(), reason="gateway tickets carry no gid (gid=uid)"
    )
    def test_gateway_minted_ticket_spawns(self, broker, tmp_path, monkeypatch):
        ws = _workspace(tmp_path)
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, broker.secret)
        ticket = ui.mint_ticket(
            uid=os.getuid(), workspace=str(ws), ro=[], rw=[str(ws)], env_allow=[]
        )
        proc = _run(broker, ws, "print('ran')\n", ticket=ticket)
        assert proc.returncode == 0, proc.stderr
        assert "ran" in proc.stdout


class TestCliChildSessionEnv:
    """#244 review 4, uid mode: the broker's CLI child repairs a planted
    .cli-tmp and sets the session shell and git global config exactly as the
    launcher does in landlock mode (both through docker/_landlock_rules.py)."""

    @pytest.mark.parametrize("kind", ["file", "fifo", "socket"])
    def test_a_planted_cli_tmp_is_replaced(self, broker, tmp_path, kind):
        # Used to be exit 118 on every later spawn in the workspace.
        ws = _workspace(tmp_path)
        _plant(ws / ".cli-tmp", kind)
        for _ in range(2):
            proc = _run(broker, ws, _TMP_PROBE)
            assert proc.returncode == 0, (proc.stderr, broker.log_text())
            assert proc.stdout.strip() == str(ws / ".cli-tmp")
        assert (ws / ".cli-tmp").is_dir() and not (ws / ".cli-tmp").is_symlink()

    @pytest.mark.parametrize(
        "plant",
        [
            "import shutil; shutil.rmtree('.cli-tmp'); open('.cli-tmp', 'w')\n",
            "import os; os.chmod('.cli-tmp', 0)\n",
        ],
    )
    def test_a_session_cannot_lock_its_workspace(self, broker, tmp_path, plant):
        ws = _workspace(tmp_path)
        assert _run(broker, ws, plant).returncode == 0
        proc = _run(broker, ws, _TMP_PROBE)
        assert proc.returncode == 0, (proc.stderr, broker.log_text())
        assert proc.stdout.strip() == str(ws / ".cli-tmp")

    @pytest.mark.skipif(_BASH is None, reason="no system bash")
    def test_bash_calls_run_without_a_login_shell(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        (home / ".profile").write_text("PATH=/profile/reset\n")
        path = f"{tmp_path / 'operator-bin'}:/usr/bin:/bin"
        # SHELL names a bash: the CLI's pick, as in the image (no zsh).
        proc = _run(broker, ws, _SHELL_PROBE, HOME=str(home), PATH=path, SHELL=_BASH)
        assert proc.returncode == 0, (proc.stderr, broker.log_text())
        r = json.loads(proc.stdout)
        assert r["shell"] == str(_WRAPPER)
        assert r["sibling"].startswith("DENY"), r  # the file, not docker/
        assert r["wrapped"] == {"rc": 0, "out": f"NOLOGIN\nP={path}\n", "err": ""}
        assert ".profile: Permission denied" in r["direct"]["err"], r  # control

    @pytest.mark.skipif(_GIT is None, reason="no git")
    def test_git_works_with_a_home_git_config(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        _git_home(home)
        proc = _run(
            broker,
            ws,
            _GIT_PROBE,
            HOME=str(home),
            GIT_CONFIG_GLOBAL=str(home / ".gitconfig"),
        )
        assert proc.returncode == 0, (proc.stderr, broker.log_text())
        _assert_git_works(json.loads(proc.stdout), ws / ".cli-tmp")

    def test_a_workspace_planted_as_a_link_is_refused(self, broker, tmp_path):
        # B (#244 review 4): chdir follows the link, the ruleset grants the
        # name. The kernel's cwd must be exactly the ticket's workspace.
        real = _workspace(tmp_path, "real")
        link = tmp_path / "alice"
        os.symlink(real, link)
        proc = _run(broker, link, "print('ran')\n")
        assert proc.returncode == 113, (proc.stderr, broker.log_text())
        assert "ran" not in proc.stdout
        assert "resolves to" in broker.log_text()

    def test_a_linked_ticket_entry_is_not_granted(self, broker, tmp_path):
        # G (#244 review 4): the ticket's entries, which the gateway resolved,
        # are opened following no link; one swapped in later is refused.
        ws = _workspace(tmp_path)
        (ws / "in.txt").write_text("IN\n")
        secret = tmp_path / "app"
        secret.mkdir()
        (secret / "secret.txt").write_text("SECRET\n")
        agents = tmp_path / "agents"
        os.symlink(secret, agents)
        ticket = broker.ticket(ws, ro=[str(agents)])
        proc = _run(broker, ws, _PROBE, ws, agents, ticket=ticket)
        assert proc.returncode == 0, (proc.stderr, broker.log_text())
        assert json.loads(proc.stdout)["out"].startswith("DENY")
        assert "is a symlink" in proc.stderr
        # Control: the same tree, named without the link, is granted.
        ticket = broker.ticket(ws, ro=[str(secret)])
        proc = _run(broker, ws, _PROBE, ws, secret, ticket=ticket)
        assert json.loads(proc.stdout)["out"] == "OK"

    def test_a_hard_linked_ticket_file_is_not_granted(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        (ws / "in.txt").write_text("IN\n")
        theirs = tmp_path / "bob"
        theirs.mkdir()
        (theirs / "t.jsonl").write_text("BOB\n")
        shared = tmp_path / "shared"
        shared.mkdir()
        os.link(theirs / "t.jsonl", shared / "secret.txt")
        ticket = broker.ticket(ws, ro=[str(shared / "secret.txt")])
        proc = _run(broker, ws, _PROBE, ws, shared, ticket=ticket)
        assert proc.returncode == 0, (proc.stderr, broker.log_text())
        assert json.loads(proc.stdout)["out"].startswith("DENY")
        assert "a file with 2 links" in proc.stderr

    def test_a_real_cli_dir_holding_the_workspace_is_refused(
        self, start_broker, tmp_path
    ):
        # The broker grants its own real CLI's dir, which the gateway's startup
        # check cannot see: one holding the workspace is refused (exit 114).
        shim = tmp_path / "claude"
        shim.write_text('#!/bin/sh\nexec /usr/bin/python3 "$@"\n')
        shim.chmod(0o755)
        broker = start_broker(extra_env={"GATEWAY_REAL_CLI": str(shim)})
        proc = _run(broker, _workspace(tmp_path), "print('ran')\n")
        assert proc.returncode == 114, (proc.stderr, broker.log_text())
        assert "ran" not in proc.stdout
        assert "refusing to grant the real CLI's directory" in broker.log_text()


class TestExitStatusMirroring:
    """B8: the exit code and the terminating signal travel as separate fields,
    and the launcher reproduces them exactly as its own returncode."""

    @pytest.mark.parametrize("code", [0, 1, 130, 137, 148, 255])
    def test_exit_code(self, broker, tmp_path, code):
        proc = _run(broker, _workspace(tmp_path), "import sys; sys.exit(%d)\n" % code)
        assert proc.returncode == code, proc.stderr

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
    def test_death_by_signal(self, broker, tmp_path, sig):
        script = "import os; os.kill(os.getpid(), %d)\n" % sig
        proc = _run(broker, _workspace(tmp_path), script)
        assert proc.returncode == -sig, proc.stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_an_unwritable_workspace_has_its_own_status(self, broker, tmp_path):
        # A session uid that cannot write its workspace used to exec the CLI
        # anyway, which then died on its tmpdir mkdir with a bare exit 1 (#244
        # review 3: every uid-mode turn in the shipped image).
        ws = _workspace(tmp_path)
        (ws / "cli.py").write_text("print('ran')\n")
        ws.chmod(0o555)
        try:
            proc = subprocess.run(
                [sys.executable, str(_LAUNCHER), str(ws / "cli.py")],
                cwd=str(ws),
                env=_launcher_env(broker, ws),
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            ws.chmod(0o755)
        assert proc.returncode == 118, proc.stderr
        assert "ran" not in proc.stdout
        assert "cannot create the session tmp dir" in broker.log_text()


class TestTeardown:
    """B1/B2/B6: the supervisor owns teardown, sweeps every descendant, and
    never depends on another process signalling it."""

    def test_launcher_death_tears_down_cli(self, broker, tmp_path):
        """Killing the launcher (as the SDK's close() does) must tear the CLI
        tree down, not leave it running after the session ends (#241 review #7)."""
        ws = _workspace(tmp_path)
        pidfile = ws / "cli.pid"
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        proc = _popen(broker, ws, script, pidfile)
        try:
            cli_pid = int(_wait_file(pidfile, proc=proc))
            assert _alive(cli_pid), "CLI never started"
            proc.kill()  # the SDK's SIGKILL on the launcher
            proc.wait(timeout=5)
            assert not _wait_dead([cli_pid]), "CLI survived the launcher's death"
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_detached_descendants_die_with_the_session(self, broker, tmp_path):
        # B2 (1): a setsid'd grandchild and a double-forked one re-parent to the
        # supervisor; the status only goes out once they are dead and reaped.
        ws = _workspace(tmp_path)
        proc = _run(broker, ws, _SPAWN_ESCAPEES, ws / "kids")
        assert proc.returncode == 0, proc.stderr
        kids = [int(p) for p in (ws / "kids").read_text().split()]
        assert not _wait_dead(kids, timeout=2.0), "escapees outlived the session"

    def test_sigterm_ignoring_tree_dies_after_launcher_eof(self, broker, tmp_path):
        # B2 (2) + B6: the CLI and a detached child ignore SIGTERM; after the
        # launcher dies the supervisor waits out the grace without spinning,
        # then SIGKILLs the group and sweeps the rest.
        ws = _workspace(tmp_path)
        pids = ws / "pids"
        proc = _popen(broker, ws, _STUBBORN, pids)
        try:
            cli, kid, supervisor = map(int, _wait_file(pids, proc=proc).split())
            proc.kill()
            proc.wait(timeout=5)
            time.sleep(0.3)
            assert _alive(cli) and _alive(kid)  # still within the grace period
            before = _cpu_ticks(supervisor)
            time.sleep(2.5)
            spent = (_cpu_ticks(supervisor) - before) / os.sysconf("SC_CLK_TCK")
            assert spent < 0.25, "supervisor burned %.2fs CPU while waiting" % spent
            assert not _wait_dead([cli, kid, supervisor], timeout=10.0)
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_relayed_sigterm_reaches_the_cli(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        pidfile = ws / "cli.pid"
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        proc = _popen(broker, ws, script, pidfile)
        _wait_file(pidfile, proc=proc)
        proc.send_signal(signal.SIGTERM)  # the SDK's terminate()
        assert proc.wait(timeout=10) == -signal.SIGTERM

    def test_teardown_never_needs_the_broker_to_signal(self, start_broker, tmp_path):
        # B1 acceptance: with every signal from the resident broker failing
        # EPERM (as a cross-uid kill without CAP_KILL would), launcher EOF still
        # tears the whole tree down and a relayed SIGTERM still reaches the CLI.
        broker = start_broker(seam="deny-resident-signals")
        ws = _workspace(tmp_path)
        pids = ws / "pids"
        proc = _popen(broker, ws, _STUBBORN, pids)
        try:
            cli, kid, supervisor = map(int, _wait_file(pids, proc=proc).split())
            proc.kill()
            assert not _wait_dead([cli, kid, supervisor], timeout=12.0)
        finally:
            if proc.poll() is None:
                proc.kill()
        ws2 = _workspace(tmp_path, "ws2")
        pidfile = ws2 / "cli.pid"
        script = (
            "import os, sys, time\n"
            "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        proc = _popen(broker, ws2, script, pidfile)
        _wait_file(pidfile, proc=proc)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == -signal.SIGTERM
        assert broker.alive()

    @pytest.mark.skipif(_landlock_abi() < 6, reason="Landlock signal scoping: ABI 6")
    def test_session_cannot_signal_its_supervisor(self, broker, tmp_path):
        """On ABI >= 6 Landlock scoping alone carries this: the test-only
        same-uid broker skips the uid switch, so the confined tree is refused
        only because it cannot signal out of its Landlock domain — while it can
        still signal its own processes. The credential layer that covers
        ABI < 6 (real/saved uid stay the broker's) is container-verify only;
        ``TestCredentialSplit`` pins its call sequence."""
        script = (
            "import os, signal, subprocess\n"
            "kid = subprocess.Popen(['sleep', '30'])\n"
            "os.kill(kid.pid, signal.SIGTERM); kid.wait()\n"
            "try:\n"
            "    os.kill(os.getppid(), signal.SIGKILL); print('KILLED')\n"
            "except PermissionError: print('EPERM', kid.returncode)\n"
        )
        proc = _run(broker, _workspace(tmp_path), script)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["EPERM", str(-signal.SIGTERM)]

    def test_supervisor_death_backstop_kills_orphans(self, broker, tmp_path):
        # If a supervisor dies anyway, its CLI goes with it (PDEATHSIG) and the
        # detached survivors re-parent to the resident broker, which kills them
        # as their own uid.
        ws = _workspace(tmp_path)
        pids = ws / "pids"
        proc = _popen(broker, ws, _STUBBORN, pids)
        try:
            cli, kid, supervisor = map(int, _wait_file(pids, proc=proc).split())
            os.kill(supervisor, signal.SIGKILL)
            assert not _wait_dead([cli, kid], timeout=10.0)
            assert proc.wait(timeout=10) == 70  # link dropped without a status
            assert broker.alive()
        finally:
            if proc.poll() is None:
                proc.kill()


def _cpu_ticks(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    fields = stat[stat.rfind(")") + 2 :].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def _raw_spawn(broker, payload: bytes, *, fds=True, hlen=None, half_close=True):
    """Speak the spawn handshake by hand; return the broker's reply bytes."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(15)
    conn.connect(str(broker.sock))
    data = struct.pack("=I", len(payload) if hlen is None else hlen) + payload
    anc = []
    if fds:
        r, w = os.pipe()
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r, w, w]))]
    try:
        sent = conn.sendmsg([data], anc)
        conn.sendall(data[sent:])
    finally:
        if fds:
            os.close(r)
            os.close(w)
    if half_close:
        conn.shutdown(socket.SHUT_WR)
    try:
        return conn.recv(64)
    finally:
        conn.close()


class TestResidentRobustness:
    """B3/B12: no single connection can stop the resident broker, and every
    refused connection's fds are closed."""

    def _malformed(self, broker, ws):
        bad_sig = json.dumps({"ticket": "éabc.def", "argv": [], "env": {}})
        typed = json.dumps(
            {"ticket": broker.ticket(ws, uid="x"), "argv": [], "env": {}}
        )
        return [
            ("top-level list", b"[]", {}),
            ("non-string ticket", b'{"ticket": 5}', {}),
            ("non-ASCII signature", bad_sig.encode(), {}),
            ("deep nesting", b"[" * 200000 + b"]" * 200000, {}),
            ("not UTF-8", b'{"ticket": "\xff"}', {}),
            ("wrong env types", b'{"ticket": "a.b", "argv": [], "env": {"A": 1}}', {}),
            ("bad payload types", typed.encode(), {}),
            ("oversized length", b"{}", {"hlen": 0xFFFFFFFF}),
            ("truncated header", b'{"ticket"', {"hlen": 1000}),
            (
                "no fds",
                json.dumps(
                    {"ticket": broker.ticket(ws), "argv": [], "env": {}}
                ).encode(),
                {"fds": False},
            ),
        ]

    def test_malformed_headers_do_not_kill_the_broker(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        assert _run(broker, ws, "print('warm')\n").returncode == 0
        fds_before = broker.fd_count()
        for name, payload, kw in self._malformed(broker, ws):
            reply = _raw_spawn(broker, payload, **kw)
            assert reply == _REC.pack(_ST_REFUSED, 0), name
            assert broker.alive(), "broker died on: %s" % name
        deadline = time.monotonic() + 2.0  # it closes just after replying
        while broker.fd_count() != fds_before and time.monotonic() < deadline:
            time.sleep(0.02)
        assert broker.fd_count() == fds_before  # no received fd leaked (B12)
        proc = _run(broker, ws, "print('still serving')\n")
        assert proc.returncode == 0, proc.stderr
        assert "still serving" in proc.stdout

    def test_too_many_fds_refused(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(15)
        conn.connect(str(broker.sock))
        header = json.dumps({"ticket": broker.ticket(ws), "argv": [], "env": {}})
        data = struct.pack("=I", len(header)) + header.encode()
        r, w = os.pipe()
        conn.sendmsg(
            [data],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r] * 5))],
        )
        os.close(r)
        os.close(w)
        assert conn.recv(64) == _REC.pack(_ST_REFUSED, 0)
        conn.close()
        assert broker.alive()

    def test_stalled_peer_does_not_block_a_spawn(self, broker, tmp_path):
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(str(broker.sock))
        stalled.sendall(b"\x10\x00")  # half a length prefix, then silence
        try:
            start = time.monotonic()
            proc = _run(broker, _workspace(tmp_path), "print('ran')\n", timeout=15)
            assert proc.returncode == 0, proc.stderr
            assert time.monotonic() - start < 5.0  # not held behind the stall
        finally:
            stalled.close()

    def test_reachability_probe_is_not_an_error(self, broker):
        for _ in range(3):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(str(broker.sock))
            probe.close()
        time.sleep(0.3)
        assert broker.alive()
        assert "rejecting" not in broker.log_text()

    def test_fork_failure_refuses_and_keeps_serving(self, start_broker, tmp_path):
        broker = start_broker(seam="fail-first-fork")
        first = _run(broker, _workspace(tmp_path), "print('ran')\n")
        assert first.returncode == 70 and "ran" not in first.stdout
        second = _run(broker, _workspace(tmp_path, "ws2"), "print('ran')\n")
        assert second.returncode == 0, second.stderr
        assert "fork failed" in broker.log_text()


class TestRequestValidation:
    """B5: relative paths are refused fail-closed."""

    def test_relative_workspace_refused(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        ticket = broker.ticket(ws, workspace="ws")
        proc = _run(broker, ws, "print('ran')\n", ticket=ticket)
        assert proc.returncode == 70 and "ran" not in proc.stdout
        assert "not an absolute path" in broker.log_text()

    def test_relative_rw_entry_refused(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        ticket = broker.ticket(ws, rw=[str(ws), "data"])
        proc = _run(broker, ws, "print('ran')\n", ticket=ticket)
        assert proc.returncode == 70 and "ran" not in proc.stdout
        assert "not an absolute path" in broker.log_text()


class TestProductionMode:
    """B7: without --test-same-uid nothing short-circuits the uid checks."""

    def test_broker_own_uid_refused(self, start_broker, tmp_path):
        broker = start_broker(test_same_uid=False)
        proc = _run(broker, _workspace(tmp_path), "print('ran')\n")
        assert proc.returncode == 70 and "ran" not in proc.stdout
        assert "is reserved" in broker.log_text()

    def test_out_of_band_gid_refused(self, start_broker, tmp_path):
        broker = start_broker(test_same_uid=False)
        ws = _workspace(tmp_path)
        ticket = broker.ticket(ws, uid=20005, gid=5)
        proc = _run(broker, ws, "print('ran')\n", ticket=ticket)
        assert proc.returncode == 70 and "ran" not in proc.stdout
        assert "gid 5 is outside the band" in broker.log_text()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root could really switch")
    def test_in_band_switch_without_privilege_fails_closed(
        self, start_broker, tmp_path
    ):
        # The supervisor attempts the real switch, cannot (no CAP_SETUID here),
        # and refuses: the CLI never runs unswitched.
        broker = start_broker(test_same_uid=False)
        ws = _workspace(tmp_path)
        ticket = broker.ticket(ws, uid=20005, gid=20005)
        proc = _run(broker, ws, "print('ran')\n", ticket=ticket)
        assert proc.returncode == 70 and "ran" not in proc.stdout
        assert "refusing the spawn for uid 20005" in broker.log_text()

    def test_unknown_flag_refuses_to_start(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, str(_BROKER), "--same-uid"],
            env={**os.environ, ui.BROKER_SOCKET_ENV: str(tmp_path / "s.sock")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 64


@pytest.fixture
def broker_module(monkeypatch):
    # The broker inserts docker/ into sys.path at import; keep that local.
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("isolation_broker_ut", _BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAuthorizeIdRange:
    """PR #244 follow-up: (uid_t)-1 = 4294967295 tells set*id to leave an id
    unchanged, so the broker refuses it (and anything below 1) whatever band
    its environment names."""

    def test_the_bound_matches_the_gateway(self, broker_module):
        assert broker_module._MAX_SESSION_ID == ui._MAX_ID

    @pytest.mark.parametrize("bad", [ui._MAX_ID + 1, ui._MAX_ID + 2, -1])
    def test_ids_outside_uid_t_are_refused(self, broker_module, monkeypatch, bad):
        for name in ("APP_UID", "APP_GID"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CLAUDE_ISOLATION_UID_MIN", "-5")
        monkeypatch.setenv("CLAUDE_ISOLATION_UID_MAX", str(ui._MAX_ID + 2))
        with pytest.raises(ValueError, match="not a valid session id"):
            broker_module._authorize(bad, 20000, False)
        with pytest.raises(ValueError, match="not a valid session id"):
            broker_module._authorize(20000, bad, False)
        broker_module._authorize(ui._MAX_ID, ui._MAX_ID, False)  # the top is valid


class TestTicketVerify:
    """B13/B14: the broker's verify_ticket matches the gateway's, and its nonce
    store is bounded without ever accepting a replay."""

    def test_cross_check_with_the_gateway(self, broker_module, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "lockstep-secret")
        secret = ui.ticket_secret()

        def both(ticket, gateway_seen, broker_seen):
            outcomes = []
            for verify in (
                lambda: ui.verify_ticket(ticket, seen_nonces=gateway_seen),
                lambda: broker_module.verify_ticket(ticket, secret, broker_seen),
            ):
                try:
                    outcomes.append(verify())
                except ValueError:
                    outcomes.append(ValueError)
            return outcomes

        def mint(**kw):
            return ui.mint_ticket(
                uid=20001, workspace="/ws", ro=["/ro"], rw=["/ws"], env_allow=[], **kw
            )

        valid = mint()
        gateway, broker = both(valid, set(), {})
        assert gateway == broker and gateway["uid"] == 20001  # valid

        sig, _, body = mint().partition(".")
        tampered = sig + "." + body.replace('"uid":20001', '"uid":0')
        assert both(tampered, set(), {}) == [ValueError, ValueError]

        expired = mint(ttl=1)
        real = time.time()
        monkeypatch.setattr(time, "time", lambda: real + 100)
        assert both(expired, set(), {}) == [ValueError, ValueError]
        monkeypatch.setattr(time, "time", lambda: real)

        gateway_seen, broker_seen = set(), {}
        replayed = mint()
        first = both(replayed, gateway_seen, broker_seen)
        assert first[0] == first[1] and first[0] is not ValueError
        assert both(replayed, gateway_seen, broker_seen) == [ValueError, ValueError]

    def test_refusals_match_the_gateway(self, broker_module, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "lockstep-secret")
        secret = ui.ticket_secret()

        def signed(body):
            sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
            return f"{sig}.{body}"

        cases = {
            "empty": "",
            "no dot": "nodot",
            "non-ASCII": "\u00e9abc.def",
            "bad signature": "abc.def",
            "not a str": 5,
            "None": None,
            "list payload": signed("[1, 2]"),
            "invalid JSON": signed("{"),
            "string exp": _mint("lockstep-secret", exp="soon"),
            "bool exp": _mint("lockstep-secret", exp=True),
            "list nonce": _mint("lockstep-secret", nonce=["x"]),
            "empty nonce": _mint("lockstep-secret", nonce=""),
        }
        for name, ticket in cases.items():
            for side, verify in (
                ("gateway", lambda t=ticket: ui.verify_ticket(t, seen_nonces=set())),
                ("broker", lambda t=ticket: broker_module.verify_ticket(t, secret, {})),
            ):
                try:
                    verify()
                except ValueError:
                    continue
                pytest.fail("%s accepted the %s ticket" % (side, name))

    def test_nonce_store_is_pruned_but_replays_stay_refused(self, broker_module):
        secret = b"s"
        mod = broker_module
        seen: dict = {}
        now = 1_000_000
        ticket = _mint("s", exp=now + 10)
        mod.verify_ticket(ticket, secret, seen, now=now)
        with pytest.raises(ValueError, match="already used"):
            mod.verify_ticket(ticket, secret, seen, now=now + 10)  # until exp
        with pytest.raises(ValueError, match="expired"):
            mod.verify_ticket(ticket, secret, seen, now=now + 11)  # after it
        mod.verify_ticket(_mint("s", exp=now + 100), secret, seen, now=now + 11)
        assert len(seen) == 1  # the expired nonce was pruned

    @pytest.mark.parametrize(
        "ticket",
        ["", "nodot", "éabc.def", 5, None, "abc.def"],
    )
    def test_malformed_tickets_raise_value_error(self, broker_module, ticket):
        with pytest.raises(ValueError):
            broker_module.verify_ticket(ticket, b"s", {})

    def test_signed_but_mistyped_payload_raises_value_error(self, broker_module):
        for fields in ({"exp": "soon"}, {"nonce": ["x"]}, {"exp": True}):
            with pytest.raises(ValueError):
                broker_module.verify_ticket(_mint("s", **fields), b"s", {})


class _Exit(BaseException):
    """What a patched os._exit raises, so a test can read the exit code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Proxy:
    """A module stand-in: the given overrides, everything else delegated."""

    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestCredentialSplit:
    """B1's credential layer as calls. The switch itself needs CAP_SETUID and is
    container-verify only, and the same-uid protocol tests skip it, so this pins
    the sequence: the supervisor keeps the broker's real and saved uid and takes
    U only as its effective uid, and so does the resident's orphan killer; the
    CLI child completes (U, U, U) on its own and refuses to exec when it did not
    take."""

    U, G = 20005, 20006

    def _patch(self, mod, monkeypatch, resuid, resgid, groups=(), **extra):
        calls = []

        def rec(name):
            return lambda *args: calls.append((name, *args))

        fake_os = _Proxy(
            os,
            setgroups=lambda g: calls.append(("setgroups", list(g))),
            setresgid=rec("setresgid"),
            setresuid=rec("setresuid"),
            getresuid=lambda: resuid,
            getresgid=lambda: resgid,
            getgroups=lambda: list(groups),
            **extra,
        )
        monkeypatch.setattr(mod, "os", fake_os)
        monkeypatch.setattr(mod.ll, "drop_all_caps", rec("drop_all_caps"))
        return calls

    def test_supervisor_keeps_real_and_saved_uid(self, broker_module, monkeypatch):
        b, u, g = os.getuid(), self.U, self.G
        calls = self._patch(broker_module, monkeypatch, (b, u, b), (g, g, g))
        broker_module._become_supervisor(u, g, False)
        assert calls == [
            ("setgroups", []),
            ("setresgid", g, g, g),
            ("setresuid", b, u, b),
            ("drop_all_caps",),
        ]

    def test_supervisor_refuses_when_the_split_did_not_take(
        self, broker_module, monkeypatch
    ):
        # A fully switched supervisor would be killable by the session.
        u, g = self.U, self.G
        calls = self._patch(broker_module, monkeypatch, (u, u, u), (g, g, g))
        with pytest.raises(OSError, match="did not take"):
            broker_module._become_supervisor(u, g, False)
        assert ("drop_all_caps",) not in calls

    def test_only_the_test_flag_skips_the_switch(self, broker_module, monkeypatch):
        own = (os.getuid(), os.getgid())
        calls = self._patch(broker_module, monkeypatch, (own[0],) * 3, (own[1],) * 3)
        broker_module._become_supervisor(*own, True)
        assert calls == [("drop_all_caps",)]
        calls.clear()
        # Without the flag there is no shortcut, even for the broker's own ids
        # (which _authorize refuses before a supervisor ever exists).
        broker_module._become_supervisor(*own, False)
        assert calls == [
            ("setgroups", []),
            ("setresgid", own[1], own[1], own[1]),
            ("setresuid", own[0], own[0], own[0]),
            ("drop_all_caps",),
        ]

    def test_cli_child_completes_the_switch(self, broker_module, monkeypatch):
        u, g = self.U, self.G
        calls = self._patch(broker_module, monkeypatch, (u, u, u), (g, g, g))
        assert broker_module._finish_switch(u, g, False) is True
        assert calls == [("setresgid", g, g, g), ("setresuid", u, u, u)]

    @pytest.mark.parametrize(
        "resuid, resgid, groups",
        [
            ((os.getuid(), U, os.getuid()), (G, G, G), ()),  # real/saved not U
            ((U, U, U), (G, G, os.getgid()), ()),  # saved gid not G
            ((U, U, U), (G, G, G), (5,)),  # a supplementary group survived
        ],
    )
    def test_cli_child_detects_an_incomplete_switch(
        self, broker_module, monkeypatch, resuid, resgid, groups
    ):
        self._patch(broker_module, monkeypatch, resuid, resgid, groups)
        assert broker_module._finish_switch(self.U, self.G, False) is False

    def _orphan_killer(self, mod, monkeypatch, resuid, pids):
        """Run _kill_as's forked side in-process; return (exit code, calls)."""
        u, g = self.U, self.G

        def exit_(code):
            raise _Exit(code)

        calls = self._patch(
            mod,
            monkeypatch,
            resuid,
            (g, g, g),
            fork=lambda: 0,
            _exit=exit_,
            kill=lambda pid, sig: calls.append(("kill", pid, sig)),
        )
        with pytest.raises(_Exit) as exited:
            mod._kill_as(u, g, pids, False)
        return exited.value.code, calls

    def test_orphan_killer_keeps_real_and_saved_uid(self, broker_module, monkeypatch):
        # #11 (review 4): a (U, U, U) killer could be SIGSTOPped by the orphans
        # it was sent for (no signal scoping below Landlock ABI 6) while the
        # resident broker waited on it; it takes the supervisor's split.
        b, u, g = os.getuid(), self.U, self.G
        code, calls = self._orphan_killer(
            broker_module, monkeypatch, (b, u, b), [101, 102]
        )
        assert code == 0
        assert calls == [
            ("setgroups", []),
            ("setresgid", g, g, g),
            ("setresuid", b, u, b),
            ("drop_all_caps",),
            ("kill", 101, signal.SIGKILL),
            ("kill", 102, signal.SIGKILL),
        ]

    def test_orphan_killer_kills_nothing_unless_the_split_took(
        self, broker_module, monkeypatch
    ):
        u = self.U
        code, calls = self._orphan_killer(broker_module, monkeypatch, (u, u, u), [101])
        assert code == 1
        assert not [c for c in calls if c[0] in ("kill", "drop_all_caps")]

    def test_cli_child_exits_115_before_anything_else(self, broker_module, monkeypatch):
        u, g, b = self.U, self.G, os.getuid()
        steps = []

        def exit_(code):
            raise _Exit(code)

        self._patch(
            broker_module,
            monkeypatch,
            (b, u, b),  # the switch did not complete
            (g, g, g),
            _exit=exit_,
            setsid=lambda: steps.append("setsid"),
            chdir=lambda path: steps.append("chdir"),
            execve=lambda *args: steps.append("execve"),
        )
        monkeypatch.setattr(
            broker_module,
            "signal",
            _Proxy(signal, set_wakeup_fd=lambda fd, **kw: -1, signal=lambda *a: None),
        )
        req = {
            "uid": u,
            "gid": g,
            "workspace": "/nonexistent",
            "ro": [],
            "rw": [],
            "env": {},
            "env_allow": [],
            "argv": [],
        }
        with pytest.raises(_Exit) as exited:
            broker_module._exec_cli(req, [], "/bin/true", os.getpid(), False)
        assert exited.value.code == 115
        assert steps == []  # no setsid, chdir or exec on a failed switch


# Drives the broker's _kill_as directly: a sleeping child stands in for an
# orphan, and os.fork is wrapped so the killer _kill_as forks first reports its
# pid, then stops itself or hangs when asked. argv: broker, mode (run, stop or
# hang), timeout, uid, gid, same-uid flag. Prints the outcome as JSON.
_KILL_AS_HARNESS = r"""
import importlib.util, json, os, signal, sys, time
spec = importlib.util.spec_from_file_location("broker_harness", sys.argv[1])
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)
mode, timeout = sys.argv[2], float(sys.argv[3])
uid, gid, same = int(sys.argv[4]), int(sys.argv[5]), sys.argv[6] == "1"
broker._KILLER_TIMEOUT = timeout
victim = os.fork()
if victim == 0:
    time.sleep(60)
    os._exit(0)
r, w = os.pipe()
real_fork = os.fork

def fork():
    pid = real_fork()
    if pid == 0:
        os.write(w, b"%d" % os.getpid())
        if mode == "stop":
            os.kill(os.getpid(), signal.SIGSTOP)
        elif mode == "hang":
            time.sleep(60)
    return pid

os.fork = fork
start = time.monotonic()
ok = broker._kill_as(uid, gid, [victim], same)
elapsed = time.monotonic() - start
os.close(w)
killer = int(os.read(r, 32))
try:
    os.waitpid(killer, os.WNOHANG)
    reaped = False
except ChildProcessError:
    reaped = True

def victim_state():
    done, status = os.waitpid(victim, os.WNOHANG)
    if not done:
        return "alive"
    killed = os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    return "killed" if killed else "exited"

state, end = victim_state(), time.monotonic() + 5
while ok and state == "alive" and time.monotonic() < end:
    time.sleep(0.01)
    state = victim_state()
if state == "alive":
    os.kill(victim, signal.SIGKILL)
    os.waitpid(victim, 0)
print(json.dumps({"ok": ok, "elapsed": elapsed, "reaped": reaped, "victim": state}))
"""


class TestOrphanKiller:
    """#11 (PR #244 review 4): the orphan backstop can never wedge the
    single-threaded resident broker. Its killer takes the supervisor's split
    credentials, so a session cannot signal it (``TestCredentialSplit``), and
    is waited for with a bound: a killer that stops or overruns is SIGKILLed and
    reaped, and the sweep is reported failed rather than blocking every user."""

    def _kill_as(self, mode, timeout, uid=None, gid=None, same=True):
        uid = os.getuid() if uid is None else uid
        gid = os.getgid() if gid is None else gid
        argv = [str(_BROKER), mode, str(timeout), str(uid), str(gid), str(int(same))]
        # Own group: a wedged run's stopped killer and victim die with it.
        proc = subprocess.Popen(
            [sys.executable, "-c", _KILL_AS_HARNESS, *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=20)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        assert proc.returncode == 0, err
        return json.loads(out)

    def test_a_stopped_killer_is_killed_at_once(self):
        result = self._kill_as("stop", timeout=30)
        assert result["ok"] is False
        assert result["elapsed"] < 5  # WUNTRACED saw the stop: no 30 s wait
        assert result["reaped"]
        assert result["victim"] == "alive"  # never killed: reported, not claimed

    def test_an_overrunning_killer_is_killed_at_the_timeout(self):
        result = self._kill_as("hang", timeout=0.5)
        assert result["ok"] is False
        assert 0.5 <= result["elapsed"] < 5
        assert result["reaped"]
        assert result["victim"] == "alive"

    def test_a_working_killer_kills_and_is_reaped(self):
        result = self._kill_as("run", timeout=30)
        assert result["ok"] is True
        assert result["reaped"]
        assert result["victim"] == "killed"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root could really switch")
    def test_a_killer_that_cannot_switch_kills_nothing(self):
        # The real set*id calls, unprivileged: they fail and the killer exits
        # without signalling anything (never as the broker's own uid).
        result = self._kill_as("run", timeout=30, uid=20005, gid=20005, same=False)
        assert result["ok"] is False
        assert result["reaped"]
        assert result["victim"] == "alive"

    @staticmethod
    def _wait_log(broker, text, timeout=10.0):
        deadline = time.monotonic() + timeout
        while text not in broker.log_text():
            if time.monotonic() >= deadline:
                pytest.fail("%r never logged:\n%s" % (text, broker.log_text()))
            time.sleep(0.05)

    def test_a_stopped_killer_does_not_wedge_the_broker(self, start_broker, tmp_path):
        import re

        broker = start_broker(seam="stop-orphan-killer")
        ws = _workspace(tmp_path)
        pids = ws / "pids"
        proc = _popen(broker, ws, _STUBBORN, pids)
        cli = kid = None
        try:
            cli, kid, supervisor = map(int, _wait_file(pids, proc=proc).split())
            os.kill(supervisor, signal.SIGKILL)  # survivors re-parent to the broker
            assert proc.wait(timeout=10) == 70
            self._wait_log(broker, "could not sweep orphans")
            # Still serving: it answers at once. Every spawn here has the
            # orphans' uid (same-uid test mode), and a uid stays refused while
            # orphans a sweep gave up on still hold it, so it never runs
            # beside them.
            refused = _run(broker, _workspace(tmp_path, "ws2"), "print('ran')\n")
            assert refused.returncode == 70
            assert "ran" not in refused.stdout
            self._wait_log(broker, "still has an unswept orphan")
            assert broker.alive()
            assert _alive(kid)  # the stopped killer never got to it
            stopped = re.search(r"orphan killer (\d+) stopped", broker.log_text())
            assert stopped, broker.log_text()
            assert not Path("/proc/%s" % stopped.group(1)).exists()  # reaped
            for pid in (cli, kid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # the CLI may already be gone; the kid is not
            ws3 = _workspace(tmp_path, "ws3")
            deadline = time.monotonic() + 10
            while True:  # the broker reaps them on its next tick
                ran = _run(broker, ws3, "print('ran')\n")
                if ran.returncode == 0 or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            assert ran.returncode == 0, ran.stderr
            assert "ran" in ran.stdout  # and spawning again once they are gone
        finally:
            if proc.poll() is None:
                proc.kill()
            leftovers = [p for p in (cli, kid) if p is not None]
            if broker.alive():  # a stopped killer, if the broker wedged on it
                leftovers += [int(pid) for pid in _supervisors(broker)]
            for pid in leftovers:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def _fd_links(pid: int) -> dict:
    links = {}
    for name in os.listdir(f"/proc/{pid}/fd"):
        try:
            links[int(name)] = os.readlink(f"/proc/{pid}/fd/{name}")
        except OSError:
            pass
    return links


class TestFdHygiene:
    """B12: fds parked by a half-sent handshake are close-on-exec in the broker,
    and a supervisor forked meanwhile drops them and every other connection's
    socket instead of carrying them into its session."""

    def test_parked_fds_never_reach_another_spawn(self, broker, tmp_path):
        r, w = os.pipe()
        pipe = "pipe:[%d]" % os.fstat(r).st_ino
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stalled.connect(str(broker.sock))
        stalled.sendmsg(
            [struct.pack("=I", 100) + b"{"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r, w, w]))],
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                parked = [
                    fd
                    for fd, link in _fd_links(broker.proc.pid).items()
                    if link == pipe
                ]
                if len(parked) == 3:
                    break
                time.sleep(0.02)
            assert len(parked) == 3, "the broker never received the fds"
            for fd in parked:
                info = Path(f"/proc/{broker.proc.pid}/fdinfo/{fd}").read_text()
                flags = int(info.split("flags:")[1].split()[0], 8)
                assert flags & os.O_CLOEXEC, "a received fd is inheritable"
            broker_sockets = {
                link
                for fd, link in _fd_links(broker.proc.pid).items()
                if fd > 2 and link.startswith("socket:")
            }  # the listener + the stalled connection

            ws = _workspace(tmp_path)
            script = (
                "import json, os, sys, time\n"
                "links = {}\n"
                "for n in os.listdir('/proc/self/fd'):\n"
                "    try: links[n] = os.readlink('/proc/self/fd/' + n)\n"
                "    except OSError: pass\n"
                "json.dump({'ppid': os.getppid(), 'links': links},"
                " open(sys.argv[1] + '.tmp', 'w'))\n"
                "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                "time.sleep(30)\n"
            )
            proc = _popen(broker, ws, script, ws / "fds.json")
            try:
                seen = json.loads(_wait_file(ws / "fds.json", proc=proc))
                assert pipe not in seen["links"].values()  # not in the CLI
                supervisor = _fd_links(seen["ppid"])
                assert pipe not in supervisor.values()  # nor in its supervisor
                assert not broker_sockets & set(supervisor.values())
            finally:
                proc.kill()
                proc.wait(timeout=10)
        finally:
            stalled.close()
            os.close(r)
            os.close(w)


def _bundled_cli() -> str | None:
    from src.backends.claude import landlock

    return landlock.resolve_real_cli()


@pytest.mark.skipif(not _bundled_cli(), reason="no bundled CLI")
class TestBundledCliThroughBroker:
    """The real CLI 2.1.283 on SDK pipes handed over by the launcher, as the
    broker's grandchild: a Bash tool call runs, the turn completes, and after
    disconnect() nothing of the session — not even a background job the agent
    started — is left. Zero cost: the fake Messages API, dummy credentials."""

    async def test_turn_runs_and_disconnect_leaves_nothing(
        self, start_broker, tmp_path, monkeypatch
    ):
        import re

        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

        for key in list(os.environ):
            if key.startswith("CLAUDE_CODE_") or key in ("CLAUDECODE", "CLAUDE_PID"):
                monkeypatch.delenv(key)
        # The SDK's -v check spawns the launcher without options.env (the
        # gateway skips it the same way, landlock.install_process_env).
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
        broker = start_broker(extra_env={"GATEWAY_REAL_CLI": _bundled_cli()})
        ws = _workspace(tmp_path)
        home = tmp_path / "home"
        command = "echo $((6*7)); sleep 300 >/dev/null 2>&1 & echo PID=$!"

        def plan(body):
            for message in body.get("messages", []):
                content = message.get("content")
                if message["role"] == "user" and isinstance(content, list):
                    if any(b.get("type") == "tool_result" for b in content):
                        return {"text": "done"}
            return {"tool_use": {"name": "Bash", "input": {"command": command}}}

        with FakeAnthropicAPI(plan) as api:
            env = {
                **api.cli_env(home),
                "CLAUDE_CODE_HARBOR_KITE": "0",
                "CLAUDE_ISOLATION_MODE": "uid",
                "GATEWAY_ISOLATION_TICKET": broker.ticket(ws, rw=[str(ws), str(home)]),
                ui.BROKER_SOCKET_ENV: str(broker.sock),
            }
            client = ClaudeSDKClient(
                options=ClaudeAgentOptions(
                    cwd=str(ws),
                    model="claude-sonnet-5",
                    env=env,
                    permission_mode="bypassPermissions",
                    max_turns=4,
                    cli_path=str(_LAUNCHER),
                )
            )
            await asyncio.wait_for(client.connect(), timeout=120)
            try:

                async def turn():
                    await client.query("go")
                    async for _ in client.receive_response():
                        pass

                await asyncio.wait_for(turn(), timeout=120)
            finally:
                await client.disconnect()
            bodies = json.dumps([r["body"] for r in api.requests])
        assert "42" in bodies, "the Bash tool call never ran"
        match = re.search(r"PID=(\d+)", bodies)
        assert match, "the background job's pid never came back"
        assert not _wait_dead([int(match.group(1))], timeout=5.0)
        deadline = time.monotonic() + 5.0
        while _supervisors(broker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _supervisors(broker), "a supervisor outlived disconnect()"


def _supervisors(broker) -> list:
    pids = []
    for tid in os.listdir(f"/proc/{broker.proc.pid}/task"):
        pids += Path(f"/proc/{broker.proc.pid}/task/{tid}/children").read_text().split()
    return pids


# --- broker ops: cleanup and transcript ---------------------------------------
#
# docs/isolation-uid-ownership-design.md §3.3 / §3.6. Same-uid like the rest:
# the helper's real switch to (broker, U, broker) is container-only, and
# TestOpHelperCredentials pins its calls. Every path here is under tmp_path; a
# same-uid broker never sweeps the real /dev/shm (TestShmDir).

_ST_EXITED, _ST_TRANSCRIPT = 1, 4
_TEST_SHM_ENV = "GATEWAY_ISOLATION_TEST_SHM_DIR"
_DROP = object()  # a field _mint_op leaves out


def _mint_op(secret: str, op, **fields) -> str:
    """An op ticket as ``uid_isolation.mint_op_ticket`` signs it, for this
    test's own uid and gid (which the same-uid broker runs unswitched); a
    field given as ``_DROP`` is left out."""
    payload = {
        "op": op,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "nonce": secrets.token_hex(16),
        "exp": int(time.time()) + 120,
    }
    payload.update(fields)
    payload = {k: v for k, v in payload.items() if v is not _DROP}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{sig}.{body}"


def _op_send(broker, ticket, *, fds=False, argv=(), env=None, extra=b""):
    """Send one op request as the gateway does: the header, with no fds
    unless *fds*; return the connected socket for :func:`_op_reply`."""
    header = json.dumps({"ticket": ticket, "argv": list(argv), "env": env or {}})
    data = struct.pack("=I", len(header)) + header.encode() + extra
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(15)
    conn.connect(str(broker.sock))
    if not fds:
        conn.sendall(data)
        return conn
    r, w = os.pipe()
    try:
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r]))]
        sent = conn.sendmsg([data], anc)
    finally:
        os.close(r)
        os.close(w)
    conn.sendall(data[sent:])
    return conn


def _op_reply(conn, timeout=15.0):
    """The one ``(kind, value)`` record the broker answers, or ``None`` if the
    link closes first. Closes the socket."""
    conn.settimeout(timeout)
    buf = b""
    try:
        while len(buf) < _REC.size:
            chunk = conn.recv(_REC.size - len(buf))
            if not chunk:
                return None
            buf += chunk
    finally:
        conn.close()
    return _REC.unpack(buf)


def _op(broker, ticket, **kw):
    return _op_reply(_op_send(broker, ticket, **kw))


def _cleanup_ticket(broker, workspace="", **fields):
    return broker.op_ticket("cleanup", workspace=str(workspace), **fields)


def _cleanup(broker, workspace="", **fields):
    return _op(broker, _cleanup_ticket(broker, workspace, **fields))


def _transcript(broker, path, cwd):
    return _op(broker, broker.op_ticket("transcript", path=str(path), cwd=str(cwd)))


def _wait_log(broker, text, timeout=10.0, count=1):
    deadline = time.monotonic() + timeout
    while broker.log_text().count(text) < count:
        if time.monotonic() >= deadline:
            pytest.fail("%r never logged:\n%s" % (text, broker.log_text()))
        time.sleep(0.05)


def _outside(tmp_path: Path) -> Path:
    """A directory no cleanup is aimed at, with a file links point to."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("v")
    return outside


def _session_tree(root: Path, outside: Path) -> Path:
    """What a session leaves: files, nested dirs, a FIFO, links out of the
    tree, and directories of its own that lost their owner bits."""
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "sub" / "deep" / "f").write_text("f")
    os.mkfifo(root / "fifo")
    os.symlink(outside, root / "dir-link")
    os.symlink(outside / "victim", root / "file-link")
    for name, mode in (("r-x", 0o500), ("none", 0o000), ("w-only", 0o300)):
        (root / name).mkdir()
        (root / name / "x").write_text("x")
        (root / name).chmod(mode)
    return root


def _real_shm_canary():
    """A file of ours in the real /dev/shm, which no test may touch."""
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK | os.X_OK)):
        return None
    canary = shm / ("omg-broker-test-%s" % secrets.token_hex(8))
    canary.write_text("canary")
    return canary


_HOLD = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)

_CWD = "/data/workspaces/alice/claude"
_PROMPT = {"type": "user", "cwd": _CWD, "message": {"role": "user", "content": "hi"}}
_ANSWER = {"type": "assistant", "cwd": _CWD, "message": {"content": []}}
_RESULT = {
    "type": "user",
    "cwd": _CWD,
    "message": {"content": [{"type": "tool_result", "tool_use_id": "t"}]},
}
_META = {"type": "user", "cwd": _CWD, "isMeta": True, "message": {"content": "x"}}


def _jsonl(path: Path, *lines) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


class TestOpValidation:
    """The strict per-op schemas, checked in the resident before anything
    runs (in-process)."""

    def _payload(self, op="cleanup", **fields):
        payload = {"op": op, "uid": 20005, "nonce": "n", "exp": 1}
        if op == "cleanup":
            payload["workspace"] = ""
        payload.update(fields)
        return {k: v for k, v in payload.items() if v is not _DROP}

    def test_cleanup_defaults(self, broker_module):
        req = broker_module._op_request("cleanup", self._payload())
        assert req == {
            "op": "cleanup",
            "uid": 20005,
            "gid": 20005,  # absent: the uid
            "workspace": "",
            "wait": 0,
            "clear": [],
            "remove": [],
            "expire": None,
            "shm": False,
        }
        req = broker_module._op_request(
            "cleanup",
            self._payload(
                gid=20006,
                workspace="/ws/_tmp_x",
                wait=30,
                clear=["/ws/_tmp_x"],
                remove=["/cfg/p/x.jsonl"],
                expire={"older_than": 3600},
                shm=True,
            ),
        )
        assert req["gid"] == 20006 and req["wait"] == 30 and req["shm"] is True
        assert req["expire"] == {"older_than": 3600, "units": [], "nested": []}

    @pytest.mark.parametrize(
        "fields",
        [
            {"workspace": _DROP},
            {"workspace": None},
            {"workspace": "ws"},
            {"workspace": "/"},
            {"rw": ["/ws"]},  # a spawn field: unknown here
            {"env_allow": []},
            {"wait": -1},
            {"wait": 31},
            {"wait": True},
            {"wait": 1.5},
            {"wait": "5"},
            {"shm": 1},
            {"shm": "true"},
            {"clear": "/ws"},
            {"clear": [5]},
            {"remove": [None]},
            {"remove": ["relative/x"]},
            {"expire": None},
            {"expire": []},
            {"expire": {"older_than": 3599}},
            {"expire": {"older_than": True}},
            {"expire": {"units": ["/x"]}},
            {"expire": {"older_than": 3600, "units": "/x"}},
            {"expire": {"older_than": 3600, "nested": ["x"]}},
            {"expire": {"older_than": 3600, "extra": []}},
            {"uid": "20005"},
            {"uid": True},
            {"gid": 1.0},
        ],
    )
    def test_cleanup_refusals(self, broker_module, fields):
        with pytest.raises(ValueError):
            broker_module._op_request("cleanup", self._payload(**fields))

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "relative.jsonl",
            "/",
            "//x/a.jsonl",
            "/a/../b",
            "/a/./b",
            "/a//b",
            "/a/",
            "/a\0b",
            "/a/\ud800",  # not encodable as a path
            5,
            None,
            ["/a"],
        ],
    )
    def test_path_refusals(self, broker_module, path):
        with pytest.raises(ValueError):
            broker_module._op_path(path, "test path")
        with pytest.raises(ValueError):
            broker_module._op_request("cleanup", self._payload(remove=[path]))

    def test_transcript_schema(self, broker_module):
        good = self._payload("transcript", path="/cfg/p/s.jsonl", cwd="/ws")
        req = broker_module._op_request("transcript", good)
        assert (req["path"], req["cwd"], req["gid"]) == ("/cfg/p/s.jsonl", "/ws", 20005)
        for fields in (
            {"path": "/cfg/p/s.json"},  # not a transcript
            {"path": _DROP},
            {"cwd": _DROP},
            {"cwd": "ws"},
            {"cwd": "/ws/"},
            {"workspace": ""},  # a cleanup field: unknown here
        ):
            with pytest.raises(ValueError):
                broker_module._op_request("transcript", {**good, **fields})

    def test_the_op_a_ticket_names(self, broker_module):
        assert broker_module._ticket_op({}) == "spawn"
        for op in ("spawn", "cleanup", "transcript"):
            assert broker_module._ticket_op({"op": op}) == op
        for op in ("frobnicate", "", None, 5, ["cleanup"], {"op": 1}):
            with pytest.raises(ValueError, match="unknown broker op"):
                broker_module._ticket_op({"op": op})

    def test_the_spawn_request_refuses_an_op_ticket(self, broker_module):
        header = {"ticket": "t", "argv": [], "env": {}}
        payload = {"uid": 20005, "workspace": "/ws"}
        broker_module._spawn_request(header, payload)  # a spawn
        broker_module._spawn_request(header, {**payload, "op": "spawn"})
        for op in ("cleanup", "transcript", None):
            with pytest.raises(ValueError, match="cannot spawn"):
                broker_module._spawn_request(header, {**payload, "op": op})


class TestShmDir:
    """With --test-same-uid the session uid IS the developer's, so the shm
    step may never reach the real /dev/shm then (in-process: no sweep)."""

    def test_production_sweeps_dev_shm_whatever_the_env(self, broker_module):
        assert broker_module._shm_dir(False, {}) == "/dev/shm"
        env = {_TEST_SHM_ENV: "/tmp/x"}
        assert broker_module._shm_dir(False, env) == "/dev/shm"

    def test_test_mode_sweeps_only_a_test_dir(self, broker_module, tmp_path):
        shm = str(tmp_path / "shm")
        assert broker_module._shm_dir(True, {_TEST_SHM_ENV: shm}) == shm
        # Unset, blank, relative, or outside the temp dir: skipped.
        for value in (None, "", " ", "relative", "/dev/shm", "/dev/shm/x"):
            env = {} if value is None else {_TEST_SHM_ENV: value}
            assert broker_module._shm_dir(True, env) is None, value


class TestTestModePaths:
    """Under --test-same-uid an op runs as the developer's own uid: whatever a
    test asks, it touches nothing outside the temp dir."""

    def test_paths_outside_the_temp_dir_are_refused(self, broker, tmp_path):
        outside = "/nonexistent-omg-broker-test/x.jsonl"
        for fields in (
            {"clear": [outside]},
            {"remove": [str(tmp_path / "ok"), outside]},
            {"expire": {"older_than": 3600, "nested": [outside]}},
        ):
            assert _cleanup(broker, **fields) == (_ST_REFUSED, 0), fields
        assert _transcript(broker, outside, _CWD) == (_ST_REFUSED, 0)
        assert "is outside the temp dir" in broker.log_text()
        assert _cleanup(broker, remove=[str(tmp_path / "ok")]) == (_ST_EXITED, 0)


class TestCleanupOp:
    """The cleanup op's steps against tmp trees, end to end."""

    def test_clear_keeps_the_dir_and_empties_it(self, broker, tmp_path):
        outside = _outside(tmp_path)
        d = _session_tree(tmp_path / "d", outside)
        assert _cleanup(broker, clear=[str(d)]) == (_ST_EXITED, 0), broker.log_text()
        assert d.is_dir() and list(d.iterdir()) == []
        assert (outside / "victim").read_text() == "v"  # links went, unfollowed
        assert _cleanup(broker, clear=[str(tmp_path / "missing")]) == (_ST_EXITED, 0)

    def test_remove_takes_files_dirs_links_and_missing(self, broker, tmp_path):
        outside = _outside(tmp_path)
        f = tmp_path / "s.jsonl"
        f.write_text("{}\n")
        d = _session_tree(tmp_path / "d", outside)
        link = tmp_path / "link"
        os.symlink(outside, link)
        missing = tmp_path / "nope" / "deeper"
        reply = _cleanup(broker, remove=[str(f), str(d), str(link), str(missing)])
        assert reply == (_ST_EXITED, 0), broker.log_text()
        assert not os.path.lexists(f) and not d.exists()
        assert not os.path.lexists(link)  # the link itself, not its target
        assert (outside / "victim").read_text() == "v"

    def test_a_symlinked_component_fails_only_that_target(self, broker, tmp_path):
        outside = _outside(tmp_path)
        (outside / "inner").mkdir()
        (outside / "inner" / "f").write_text("f")
        via = tmp_path / "via"
        via.symlink_to(outside)
        other = tmp_path / "other"
        other.write_text("x")
        reply = _cleanup(broker, remove=[str(via / "victim"), str(other)])
        assert reply == (_ST_EXITED, 1)
        assert not other.exists()  # the other target still went
        reply = _cleanup(broker, clear=[str(via), str(via / "inner")])
        assert reply == (_ST_EXITED, 1)
        assert (outside / "victim").read_text() == "v"
        assert (outside / "inner" / "f").read_text() == "f"
        assert "cannot reach" in broker.log_text()  # via/...: a link component
        assert "cannot open" in broker.log_text()  # via itself: not followed

    def test_our_own_dirs_get_their_owner_bits_back(self, broker, tmp_path):
        parent = tmp_path / "projects" / "p"
        target = parent / "s"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "x").write_text("x")
        (parent / "s.jsonl").write_text("{}\n")
        (target / "sub").chmod(0o000)
        parent.chmod(0o500)  # U's own parent, narrowed by the agent
        try:
            reply = _cleanup(broker, remove=[str(target), str(parent / "s.jsonl")])
            assert reply == (_ST_EXITED, 0), broker.log_text()
            assert os.lstat(parent).st_mode & 0o7777 == 0o700  # rwx back only
        finally:
            parent.chmod(0o755)
        assert list(parent.iterdir()) == []

    def test_an_emptied_dir_whose_parent_is_not_writable_counts(
        self, start_broker, tmp_path
    ):
        # Spec: U cannot remove a workspace root from the gateway's base; it
        # empties it and the gateway rmdirs it. Anything else that stays fails.
        broker = start_broker(seam="eacces-prefix")
        ws = tmp_path / "eacces-ws"
        (ws / "sub").mkdir(parents=True)
        (ws / "sub" / "f").write_text("f")
        assert _cleanup(broker, remove=[str(ws)]) == (_ST_EXITED, 0), broker.log_text()
        assert ws.is_dir() and list(ws.iterdir()) == []
        f = tmp_path / "eacces-file"
        f.write_text("x")
        assert _cleanup(broker, remove=[str(f)]) == (_ST_EXITED, 1)
        assert f.exists()
        outer = tmp_path / "outer"
        (outer / "eacces-inner").mkdir(parents=True)  # not the target itself
        assert _cleanup(broker, remove=[str(outer)]) == (_ST_EXITED, 1)
        assert (outer / "eacces-inner").is_dir()

    def test_a_deep_tree_is_removed(self, broker, tmp_path):
        # Deeper than the helper's fd window: subtrees move up as it goes.
        top = tmp_path / "deep"
        top.mkdir()
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(200):
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.close(os.open("leaf", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
        os.close(fd)
        assert _cleanup(broker, remove=[str(top)]) == (_ST_EXITED, 0), broker.log_text()
        assert not top.exists()

    def test_expire_removes_only_what_is_old(self, broker, tmp_path):
        cfg = tmp_path / "cfg"
        env, proj = cfg / "session-env", cfg / "projects"
        (env / "old-sid").mkdir(parents=True)
        (env / "old-sid" / "f").write_text("x")
        (env / "fresh-sid").mkdir()
        (env / "fresh-sid" / "f").write_text("x")  # an old dir, a fresh file
        (env / "old-file").write_text("x")
        p1, p2, p3 = proj / "p1", proj / "p2", proj / "p3"
        (p1 / "memory").mkdir(parents=True)
        (p1 / "memory" / "m.md").write_text("m")
        (p1 / "old.jsonl").write_text("{}\n")
        (p1 / "old" / "subagents").mkdir(parents=True)
        (p1 / "old" / "subagents" / "a.jsonl").write_text("{}\n")
        (p1 / "fresh.jsonl").write_text("{}\n")
        p2.mkdir()
        (p2 / "s.jsonl").write_text("{}\n")
        p3.mkdir()  # a spawn has just made it: fresh and empty
        (proj / "stray").write_text("x")
        old = time.time() - 10 * 86400
        for path in (  # children before their parents
            env / "old-sid" / "f",
            env / "old-sid",
            env / "fresh-sid",
            env / "old-file",
            p1 / "memory" / "m.md",
            p1 / "memory",
            p1 / "old.jsonl",
            p1 / "old" / "subagents" / "a.jsonl",
            p1 / "old" / "subagents",
            p1 / "old",
            p1,
            p2 / "s.jsonl",
            p2,
            proj / "stray",
        ):
            os.utime(path, (old, old), follow_symlinks=False)
        expire = {
            "older_than": 86400,
            "units": [str(env), str(cfg / "tasks")],  # tasks: missing, skipped
            "nested": [str(proj)],
        }
        assert _cleanup(broker, expire=expire) == (_ST_EXITED, 0), broker.log_text()
        assert not (env / "old-sid").exists() and not (env / "old-file").exists()
        assert (env / "fresh-sid" / "f").exists()
        assert not (p1 / "old.jsonl").exists() and not (p1 / "old").exists()
        assert (p1 / "memory" / "m.md").exists()  # memory is never expired
        assert (p1 / "fresh.jsonl").exists()
        assert not p2.exists()  # emptied, and old itself: gone
        assert p3.is_dir()  # empty but fresh: kept
        assert (proj / "stray").exists()  # not a project directory

    def test_expire_of_a_linked_dir_fails(self, broker, tmp_path):
        outside = _outside(tmp_path)
        old = time.time() - 10 * 86400
        os.utime(outside / "victim", (old, old))
        (tmp_path / "session-env").symlink_to(outside)
        expire = {"older_than": 3600, "units": [str(tmp_path / "session-env")]}
        assert _cleanup(broker, expire=expire) == (_ST_EXITED, 1)
        assert (outside / "victim").exists()

    def test_shm_sweeps_the_configured_dir_only(
        self, start_broker, broker_module, tmp_path
    ):
        shm = tmp_path / "shm"
        shm.mkdir()
        # Guard: if this mode could name the real /dev/shm, stop before a sweep.
        assert broker_module._shm_dir(True, {_TEST_SHM_ENV: str(shm)}) == str(shm)
        broker = start_broker(extra_env={_TEST_SHM_ENV: str(shm)})
        outside = _outside(tmp_path)
        (shm / "sem.mp-x").write_text("x")
        (shm / "dir" / "in").mkdir(parents=True)
        (shm / "dir" / "in" / "f").write_text("f")
        os.symlink(outside, shm / "link")
        canary = _real_shm_canary()
        try:
            assert _cleanup(broker, shm=True) == (_ST_EXITED, 0), broker.log_text()
            assert list(shm.iterdir()) == []
            assert (outside / "victim").read_text() == "v"
            assert canary is None or canary.read_text() == "canary"
        finally:
            if canary is not None:
                canary.unlink(missing_ok=True)

    def test_shm_step_is_skipped_without_a_test_dir(
        self, broker, broker_module, tmp_path
    ):
        assert broker_module._shm_dir(True, {}) is None  # guard, as above
        unconfigured = tmp_path / "shm"
        unconfigured.mkdir()
        marker = unconfigured / "sem.mine"
        marker.write_text("mine")
        assert marker.stat().st_uid == os.getuid()
        canary = _real_shm_canary()
        try:
            assert _cleanup(broker, shm=True) == (_ST_EXITED, 0), broker.log_text()
            assert "shm step skipped in test mode" in broker.log_text()
            assert marker.read_text() == "mine"
            assert canary is None or canary.read_text() == "canary"
        finally:
            if canary is not None:
                canary.unlink(missing_ok=True)

    @pytest.mark.skipif(
        os.getgid() != os.getuid(), reason="gateway tickets carry no gid (gid=uid)"
    )
    def test_gateway_minted_op_tickets(self, broker, tmp_path, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, broker.secret)
        d = tmp_path / "d"
        d.mkdir()
        (d / "f").write_text("x")
        ticket = ui.mint_op_ticket(
            ui.OP_CLEANUP,
            uid=os.getuid(),
            workspace="",
            wait=0,
            clear=[str(d)],
            remove=[],
            shm=False,
        )
        assert _op(broker, ticket) == (_ST_EXITED, 0), broker.log_text()
        assert d.is_dir() and list(d.iterdir()) == []
        t = _jsonl(tmp_path / "s.jsonl", _PROMPT, _ANSWER)
        ticket = ui.mint_op_ticket(
            ui.OP_TRANSCRIPT, uid=os.getuid(), path=str(t), cwd=_CWD
        )
        assert _op(broker, ticket) == (_ST_TRANSCRIPT, 1), broker.log_text()


class TestOpProtocol:
    """B3/B12 for ops: a bad op request is refused by the resident, which keeps
    serving and leaks no fd; an op ticket is single-use and never spawns."""

    def test_bad_op_requests_are_refused(self, broker, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        assert _cleanup(broker, clear=[str(d)]) == (_ST_EXITED, 0)  # warm
        fds_before = broker.fd_count()
        transcript = broker.op_ticket("transcript", path=str(d / "a.jsonl"), cwd=str(d))
        young = {"older_than": 60}
        cases = [
            ("an fd", _cleanup_ticket(broker), {"fds": True}),
            ("argv", _cleanup_ticket(broker), {"argv": ["x"]}),
            ("env", _cleanup_ticket(broker), {"env": {"A": "b"}}),
            ("data after the header", _cleanup_ticket(broker), {"extra": b"\0" * 8}),
            ("unknown op", broker.op_ticket("frobnicate"), {}),
            ("op not a string", broker.op_ticket(5), {}),
            ("op null", broker.op_ticket(None), {}),
            ("unknown key", _cleanup_ticket(broker, rw=[str(d)]), {}),
            ("no workspace", broker.op_ticket("cleanup"), {}),
            ("relative path", _cleanup_ticket(broker, clear=["d"]), {}),
            ("/", _cleanup_ticket(broker, remove=["/"]), {}),
            ("not normalized", _cleanup_ticket(broker, remove=[f"{d}/../d"]), {}),
            ("double slash", _cleanup_ticket(broker, remove=["/" + str(d)]), {}),
            ("wait too long", _cleanup_ticket(broker, wait=31), {}),
            ("expire too young", _cleanup_ticket(broker, expire=young), {}),
            ("reserved uid", _cleanup_ticket(broker, uid=0), {}),
            (
                "not a transcript",
                broker.op_ticket("transcript", path=str(d / "s.json"), cwd=str(d)),
                {},
            ),
            ("transcript fds", transcript, {"fds": True}),
        ]
        for name, ticket, kw in cases:
            assert _op(broker, ticket, **kw) == (_ST_REFUSED, 0), name
            assert broker.alive(), "broker died on: %s" % name
        deadline = time.monotonic() + 2.0
        while broker.fd_count() != fds_before and time.monotonic() < deadline:
            time.sleep(0.02)
        assert broker.fd_count() == fds_before  # no received fd leaked
        assert "rejecting cleanup:" in broker.log_text()
        (d / "f").write_text("x")
        assert _cleanup(broker, clear=[str(d)]) == (_ST_EXITED, 0)  # still serving
        assert not (d / "f").exists()

    def test_an_op_ticket_is_single_use(self, broker, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        ticket = _cleanup_ticket(broker, clear=[str(d)])
        assert _op(broker, ticket) == (_ST_EXITED, 0)
        (d / "f").write_text("x")
        assert _op(broker, ticket) == (_ST_REFUSED, 0)
        assert "ticket already used" in broker.log_text()
        assert (d / "f").exists()

    def test_an_op_ticket_never_spawns(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        tickets = [
            _cleanup_ticket(broker, ws),
            broker.op_ticket("transcript", path=str(ws / "s.jsonl"), cwd=str(ws)),
        ]
        for ticket in tickets:  # with fds 0/1/2, as a launcher sends them
            header = json.dumps({"ticket": ticket, "argv": [], "env": {}}).encode()
            assert _raw_spawn(broker, header) == _REC.pack(_ST_REFUSED, 0)
        proc = _run(broker, ws, "print('ran')\n", ticket=_cleanup_ticket(broker, ws))
        assert proc.returncode == 70 and "ran" not in proc.stdout
        assert broker.alive()

    def test_a_spawn_ticket_is_no_op_ticket(self, broker, tmp_path):
        # No op names a spawn, and a spawn passes fds 0/1/2: refused without.
        ws = _workspace(tmp_path)
        assert _op(broker, broker.ticket(ws)) == (_ST_REFUSED, 0)
        assert "expected fds 0/1/2, got 0" in broker.log_text()


class TestCleanupLiveness:
    """A cleanup never runs while its uid is in use: refused with ``wait=0``,
    otherwise parked until it is free or its wait is over."""

    def test_a_live_session_holds_its_workspace(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        stuff = ws / "stuff"
        stuff.mkdir()
        (stuff / "f").write_text("x")
        holder = _popen(broker, ws, _HOLD, ws / "cli.pid")
        try:
            _wait_file(ws / "cli.pid", proc=holder)
            start = time.monotonic()
            assert _cleanup(broker, ws, clear=[str(stuff)]) == (_ST_REFUSED, 0)
            assert time.monotonic() - start < 2
            assert "uid %d still has a live session" % os.getuid() in broker.log_text()
            start = time.monotonic()
            reply = _cleanup(broker, ws, wait=1, clear=[str(stuff)])
            assert reply == (_ST_REFUSED, 0)  # at its deadline
            assert 0.9 <= time.monotonic() - start < 5
            assert "after 1s" in broker.log_text()
            # Another workspace of the uid is not held up by this session.
            other = tmp_path / "other"
            other.mkdir()
            (other / "g").write_text("g")
            reply = _cleanup(broker, tmp_path / "elsewhere", clear=[str(other)])
            assert reply == (_ST_EXITED, 0), broker.log_text()
            ticket = _cleanup_ticket(broker, ws, wait=10, clear=[str(stuff)])
            conn = _op_send(broker, ticket)
            assert not select.select([conn], [], [], 0.5)[0]  # parked
            assert (stuff / "f").exists()
        finally:
            holder.kill()
            holder.wait(timeout=5)
        assert _op_reply(conn) == (_ST_EXITED, 0), broker.log_text()
        assert stuff.is_dir() and list(stuff.iterdir()) == []

    def test_an_shm_sweep_waits_for_every_session_of_the_uid(
        self, start_broker, tmp_path
    ):
        shm = tmp_path / "shm"
        shm.mkdir()
        broker = start_broker(extra_env={_TEST_SHM_ENV: str(shm)})
        ws = _workspace(tmp_path)
        holder = _popen(broker, ws, _HOLD, ws / "cli.pid")
        try:
            _wait_file(ws / "cli.pid", proc=holder)
            assert _cleanup(broker, shm=True) == (_ST_REFUSED, 0)
            assert _cleanup(broker, tmp_path / "elsewhere", shm=True) == (
                _ST_REFUSED,
                0,
            )
            assert _cleanup(broker) == (_ST_EXITED, 0)  # no shm, no workspace
        finally:
            holder.kill()
            holder.wait(timeout=5)

    def test_a_workspace_cleanup_holds_the_uid_while_it_runs(
        self, start_broker, tmp_path
    ):
        broker = start_broker(seam="slow-cleanup")
        d = tmp_path / "d"
        d.mkdir()
        (d / "slow-marker").write_text("x")
        run_ws = tmp_path / "_tmp_run"
        conn = _op_send(broker, _cleanup_ticket(broker, run_ws, clear=[str(d)]))
        parked = None
        try:
            _wait_log(broker, "seam: slow unlink")
            proc = _run(broker, _workspace(tmp_path), "print('ran')\n")
            assert proc.returncode == 70 and "ran" not in proc.stdout
            assert "uid %d is being cleaned up" % os.getuid() in broker.log_text()
            x = tmp_path / "x"
            x.write_text("x")
            assert _cleanup(broker, remove=[str(x)]) == (_ST_REFUSED, 0)
            assert "another cleanup of uid" in broker.log_text()
            parked = _op_send(broker, _cleanup_ticket(broker, wait=10, remove=[str(x)]))
            # A transcript read has no live check.
            missing = tmp_path / "none.jsonl"
            assert _transcript(broker, missing, _CWD) == (_ST_TRANSCRIPT, -1)
            assert _op_reply(conn) == (_ST_EXITED, 0), broker.log_text()
            assert _op_reply(parked) == (_ST_EXITED, 0), broker.log_text()
            assert not x.exists()
        finally:
            conn.close()
            if parked is not None:
                parked.close()
        deadline = time.monotonic() + 10
        while True:  # the helper is reaped on the broker's next tick
            proc = _run(broker, _workspace(tmp_path, "ws2"), "print('ran')\n")
            if proc.returncode == 0 or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert proc.returncode == 0, proc.stderr
        assert "ran" in proc.stdout

    def test_unswept_orphans_hold_the_uid(self, start_broker, tmp_path):
        broker = start_broker(seam="stop-orphan-killer")
        ws = _workspace(tmp_path)
        proc = _popen(broker, ws, _STUBBORN, ws / "pids")
        cli = kid = None
        try:
            cli, kid, supervisor = map(int, _wait_file(ws / "pids", proc=proc).split())
            os.kill(supervisor, signal.SIGKILL)  # survivors re-parent to the broker
            assert proc.wait(timeout=10) == 70
            _wait_log(broker, "could not sweep orphans")
            assert _cleanup(broker) == (_ST_REFUSED, 0)
            assert "still has an unswept orphan" in broker.log_text()
            for pid in (cli, kid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 10
            while True:  # reaped on a later tick
                reply = _cleanup(broker)
                if reply == (_ST_EXITED, 0) or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            assert reply == (_ST_EXITED, 0), broker.log_text()
        finally:
            if proc.poll() is None:
                proc.kill()
            for pid in [p for p in (cli, kid) if p is not None]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_waiting_cleanups_are_bounded_and_dropped_when_left(
        self, broker, tmp_path
    ):
        ws = _workspace(tmp_path)
        holder = _popen(broker, ws, _HOLD, ws / "cli.pid")
        conns = []
        try:
            _wait_file(ws / "cli.pid", proc=holder)
            fds_before = broker.fd_count()
            for _ in range(65):
                conns.append(_op_send(broker, _cleanup_ticket(broker, ws, wait=30)))
            deadline = time.monotonic() + 5
            ready = []
            while not ready and time.monotonic() < deadline:
                ready = select.select(conns, [], [], 0.2)[0]
            assert len(ready) == 1, "one over the bound must be refused"
            conns.remove(ready[0])
            assert _op_reply(ready[0]) == (_ST_REFUSED, 0)
            assert "64 cleanups are waiting already" in broker.log_text()
            assert not select.select(conns, [], [], 0.5)[0]  # the 64 still wait
            for conn in conns:
                conn.close()
            conns = []
            _wait_log(broker, "its client went away", count=64)
            deadline = time.monotonic() + 2.0
            while broker.fd_count() != fds_before and time.monotonic() < deadline:
                time.sleep(0.02)
            assert broker.fd_count() == fds_before
        finally:
            for conn in conns:
                conn.close()
            holder.kill()
            holder.wait(timeout=5)


class TestTranscriptOp:
    """The transcript op: the user turns of a file read as U, or a code."""

    def test_counts_the_user_turns(self, broker, tmp_path):
        t = _jsonl(
            tmp_path / "s.jsonl", _PROMPT, _ANSWER, _RESULT, _ANSWER, _META, _PROMPT
        )
        assert _transcript(broker, t, _CWD) == (_ST_TRANSCRIPT, 2)

    def test_another_workspace_is_a_mismatch(self, broker, tmp_path):
        t = _jsonl(tmp_path / "s.jsonl", _PROMPT, {**_PROMPT, "cwd": "/data/bob"})
        assert _transcript(broker, t, _CWD) == (_ST_TRANSCRIPT, -2)

    @pytest.mark.parametrize(
        "content",
        [b'{"type": "user"\n', b"\n", b'{"a": "\xff\xfe"}\n', b"[1, 2]\n"],
        ids=["bad-json", "blank-line", "not-utf8", "not-an-object"],
    )
    def test_corrupt(self, broker, tmp_path, content):
        t = tmp_path / "s.jsonl"
        t.write_bytes(json.dumps(_PROMPT).encode() + b"\n" + content)
        assert _transcript(broker, t, _CWD) == (_ST_TRANSCRIPT, -3)

    def test_missing(self, broker, tmp_path):
        assert _transcript(broker, tmp_path / "none.jsonl", _CWD) == (
            _ST_TRANSCRIPT,
            -1,
        )
        nowhere = tmp_path / "no" / "dir" / "s.jsonl"
        assert _transcript(broker, nowhere, _CWD) == (_ST_TRANSCRIPT, -1)
        real = tmp_path / "real"
        real.mkdir()
        _jsonl(real / "s.jsonl", _PROMPT)
        (tmp_path / "link").symlink_to(real)  # the walk follows no link
        linked = tmp_path / "link" / "s.jsonl"
        assert _transcript(broker, linked, _CWD) == (_ST_TRANSCRIPT, -1)

    @pytest.mark.parametrize("kind", ["fifo", "dir", "sparse", "symlink"])
    def test_not_a_regular_file_is_unreadable(self, broker, tmp_path, kind):
        t = tmp_path / "s.jsonl"
        if kind == "fifo":
            os.mkfifo(t)  # never opened for its writer: O_NONBLOCK
        elif kind == "dir":
            t.mkdir()
        elif kind == "sparse":
            with open(t, "wb") as f:
                f.truncate((256 << 20) + 1)  # over the cap: never read
        else:
            t.symlink_to(_jsonl(tmp_path / "real.jsonl", _PROMPT))
        start = time.monotonic()
        assert _transcript(broker, t, _CWD) == (_ST_TRANSCRIPT, -4)
        assert time.monotonic() - start < 5


class TestProductionModeOps:
    """B7 for ops: no shortcut without --test-same-uid, and a helper that
    cannot take the session uid does nothing."""

    def test_the_brokers_own_uid_is_refused(self, start_broker, tmp_path):
        broker = start_broker(test_same_uid=False)
        assert _cleanup(broker) == (_ST_REFUSED, 0)
        assert "is reserved" in broker.log_text()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root could really switch")
    def test_a_helper_that_cannot_switch_does_nothing(self, start_broker, tmp_path):
        broker = start_broker(test_same_uid=False)
        d = tmp_path / "d"
        d.mkdir()
        (d / "f").write_text("x")
        ids = {"uid": 20005, "gid": 20005}
        ticket = broker.op_ticket("cleanup", workspace="", clear=[str(d)], **ids)
        assert _op(broker, ticket) == (_ST_REFUSED, 0)
        assert (d / "f").exists()  # nothing ran as the broker's own uid
        assert "refusing the cleanup for uid 20005" in broker.log_text()
        t = _jsonl(tmp_path / "s.jsonl", _PROMPT)
        ticket = broker.op_ticket("transcript", path=str(t), cwd=_CWD, **ids)
        assert _op(broker, ticket) == (_ST_REFUSED, 0)
        assert "refusing the transcript read for uid 20005" in broker.log_text()


class TestOpHelperCredentials:
    """The helpers' credential layer as calls (the switch itself needs
    CAP_SETUID: container-verify only): the supervisor's split, then the
    alarm, before any file is touched; nothing at all when it did not take."""

    U, G = 20005, 20006

    class _Sock:
        def __init__(self):
            self.sent = b""

        def settimeout(self, _timeout):
            pass

        def sendall(self, data):
            self.sent += data

    def _patch(self, mod, monkeypatch, resuid):
        calls = []

        def rec(name):
            return lambda *args: calls.append((name, *args))

        fake_os = _Proxy(
            os,
            setgroups=lambda g: calls.append(("setgroups", list(g))),
            setresgid=rec("setresgid"),
            setresuid=rec("setresuid"),
            getresuid=lambda: resuid,
        )
        monkeypatch.setattr(mod, "os", fake_os)
        monkeypatch.setattr(mod.ll, "drop_all_caps", rec("drop_all_caps"))
        monkeypatch.setattr(mod, "signal", _Proxy(signal, alarm=rec("alarm")))
        return calls

    def _split(self):
        b, u, g = os.getuid(), self.U, self.G
        return [
            ("setgroups", []),
            ("setresgid", g, g, g),
            ("setresuid", b, u, b),
            ("drop_all_caps",),
        ]

    def _cleanup_req(self, d):
        return {
            "op": "cleanup",
            "uid": self.U,
            "gid": self.G,
            "workspace": "",
            "wait": 0,
            "clear": [str(d)],
            "remove": [],
            "expire": None,
            "shm": False,  # never True here: not test mode, so /dev/shm
        }

    def test_a_cleanup_runs_after_the_split(self, broker_module, monkeypatch, tmp_path):
        b = os.getuid()
        calls = self._patch(broker_module, monkeypatch, (b, self.U, b))
        d = tmp_path / "d"
        d.mkdir()
        (d / "f").write_text("x")
        sock = self._Sock()
        assert broker_module._run_cleanup(sock, self._cleanup_req(d), False) == 0
        assert calls == [*self._split(), ("alarm", 60)]
        assert sock.sent == _REC.pack(_ST_EXITED, 0)
        assert not (d / "f").exists()

    def test_a_cleanup_without_the_split_touches_nothing(
        self, broker_module, monkeypatch, tmp_path
    ):
        u = self.U
        calls = self._patch(broker_module, monkeypatch, (u, u, u))  # did not take
        d = tmp_path / "d"
        d.mkdir()
        (d / "f").write_text("x")
        sock = self._Sock()
        assert broker_module._run_cleanup(sock, self._cleanup_req(d), False) == 0
        assert sock.sent == _REC.pack(_ST_REFUSED, 0)
        assert not [c for c in calls if c[0] in ("alarm", "drop_all_caps")]
        assert (d / "f").exists()

    def test_a_transcript_read_runs_after_the_split(
        self, broker_module, monkeypatch, tmp_path
    ):
        b = os.getuid()
        calls = self._patch(broker_module, monkeypatch, (b, self.U, b))
        req = {
            "op": "transcript",
            "uid": self.U,
            "gid": self.G,
            "path": str(_jsonl(tmp_path / "s.jsonl", _PROMPT)),
            "cwd": _CWD,
        }
        sock = self._Sock()
        assert broker_module._run_transcript(sock, req, False) == 0
        assert calls == [*self._split(), ("alarm", 20)]
        assert sock.sent == _REC.pack(_ST_TRANSCRIPT, 1)


class TestRemovalEngine:
    """The cleanup helper's tree walk, in-process as this test's uid."""

    @staticmethod
    def _chain(top: Path, depth: int, leaf="leaf") -> None:
        top.mkdir()
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        os.close(os.open(leaf, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
        os.close(fd)

    def test_a_deep_chain_is_emptied_in_a_bounded_fd_window(
        self, broker_module, monkeypatch, tmp_path
    ):
        top = tmp_path / "top"
        self._chain(top, 50)
        live, peak = set(), [0]

        def open_(*args, **kw):
            fd = os.open(*args, **kw)
            live.add(fd)
            peak[0] = max(peak[0], len(live))
            return fd

        def close(fd):
            live.discard(fd)
            os.close(fd)

        monkeypatch.setattr(broker_module, "os", _Proxy(os, open=open_, close=close))
        monkeypatch.setattr(broker_module, "_MAX_DEPTH", 4)
        log = broker_module._Failures(os.getuid())
        fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert broker_module._empty(fd, str(top), log) is True
        finally:
            os.close(fd)
        assert log.count == 0 and list(top.iterdir()) == []  # moved ones too
        assert not live  # every fd it opened is closed
        assert 0 < peak[0] <= 4 + 1  # the window, plus a transient open

    def test_the_age_check_is_capped(self, broker_module, monkeypatch, tmp_path):
        monkeypatch.setattr(broker_module, "_EXPIRE_SCAN_CAP", 5)
        e = tmp_path / "e"
        e.mkdir()
        for n in range(4):
            (e / str(n)).write_text("x")
        cutoff = time.time() + 100  # everything here is older
        fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert broker_module._is_old(fd, "e", cutoff) is True  # 5: at the cap
            (e / "4").write_text("x")
            assert broker_module._is_old(fd, "e", cutoff) is False  # 6: unsure
        finally:
            os.close(fd)

    def test_the_age_check_stops_at_the_fd_window(
        self, broker_module, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(broker_module, "_MAX_DEPTH", 3)
        self._chain(tmp_path / "shallow", 2)
        self._chain(tmp_path / "deep", 3)
        cutoff = time.time() + 100
        fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert broker_module._is_old(fd, "shallow", cutoff) is True
            assert broker_module._is_old(fd, "deep", cutoff) is False
        finally:
            os.close(fd)

    def test_the_age_check_follows_no_link(self, broker_module, tmp_path):
        old = time.time() - 10 * 86400
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "new").write_text("x")
        e = tmp_path / "e"
        e.mkdir()
        os.symlink(fresh, e / "link")
        os.utime(e / "link", (old, old), follow_symlinks=False)
        os.utime(e, (old, old))
        fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert broker_module._is_old(fd, "e", time.time() - 86400) is True
        finally:
            os.close(fd)


class TestSessionEnv:
    """The CLI child's umask, user names and git safe.directory (uid mode:
    the workspace root is the gateway's, not the session uid's)."""

    def _env(self, mod, env, uid=20005):
        return mod._child_env(env, [], "/ws", "/ws/.cli-tmp", uid)

    @staticmethod
    def _git_pairs(env, first, last):
        return [
            (env["GIT_CONFIG_KEY_%d" % n], env["GIT_CONFIG_VALUE_%d" % n])
            for n in range(first, last + 1)
        ]

    def test_an_operator_git_config_is_kept(self, broker_module):
        env = self._env(
            broker_module,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.pager",
                "GIT_CONFIG_VALUE_0": "cat",
            },
        )
        assert env["GIT_CONFIG_COUNT"] == "3"
        assert self._git_pairs(env, 0, 2) == [
            ("core.pager", "cat"),
            ("safe.directory", "/ws"),
            ("safe.directory", "/ws/*"),
        ]

    @pytest.mark.parametrize(
        "inherited",
        [
            {},
            {"GIT_CONFIG_COUNT": ""},
            {"GIT_CONFIG_COUNT": "x"},
            {"GIT_CONFIG_COUNT": "-1"},
            {"GIT_CONFIG_COUNT": " 1", "GIT_CONFIG_KEY_0": "a.b"},
            {"GIT_CONFIG_COUNT": "1"},  # its pair is missing: git would die
            {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b"},
            {"GIT_CONFIG_COUNT": "1234567"},
        ],
    )
    def test_without_a_valid_count_ours_come_first(self, broker_module, inherited):
        env = self._env(broker_module, {**inherited, "GIT_CONFIG_VALUE_0": "v"})
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert self._git_pairs(env, 0, 1) == [
            ("safe.directory", "/ws"),
            ("safe.directory", "/ws/*"),
        ]

    def test_user_and_logname_name_the_session_uid(self, broker_module, monkeypatch):
        class _Entry:
            pw_name = "s20005"

        def getpwuid(uid):
            if uid == 20005:
                return _Entry()
            raise KeyError(uid)

        monkeypatch.setattr(broker_module, "pwd", _Proxy(pwd, getpwuid=getpwuid))
        env = self._env(broker_module, {"USER": "app", "LOGNAME": "app"})
        assert env["USER"] == env["LOGNAME"] == "s20005"
        env = self._env(broker_module, {"USER": "app"}, uid=20006)  # no entry
        assert env["USER"] == "app" and "LOGNAME" not in env

    def test_the_cli_runs_with_them(self, broker, tmp_path):
        ws = _workspace(tmp_path)
        script = (
            "import json, os, subprocess, sys\n"
            "mask = os.umask(0)\n"
            "safe = None\n"
            "if sys.argv[1]:\n"
            "    safe = subprocess.run(\n"
            "        [sys.argv[1], 'config', '--get-all', 'safe.directory'],\n"
            "        capture_output=True, text=True).stdout.splitlines()\n"
            "keys = ['USER', 'LOGNAME', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0']\n"
            "print(json.dumps({'umask': mask, 'safe': safe,\n"
            "                  'env': {k: os.environ.get(k) for k in keys}}))\n"
        )
        proc = _run(
            broker,
            ws,
            script,
            _GIT or "",
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.pager",
            GIT_CONFIG_VALUE_0="cat",
        )
        assert proc.returncode == 0, (proc.stderr, broker.log_text())
        result = json.loads(proc.stdout)
        assert result["umask"] == 0o077
        name = pwd.getpwuid(os.getuid()).pw_name
        assert result["env"]["USER"] == result["env"]["LOGNAME"] == name
        assert result["env"]["GIT_CONFIG_COUNT"] == "3"
        assert result["env"]["GIT_CONFIG_KEY_0"] == "core.pager"  # still applies
        if _GIT:
            assert result["safe"][-2:] == [str(ws), str(ws) + "/*"]
