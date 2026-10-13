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
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.backends.claude import uid_isolation as ui

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
    if _landlock_abi() < 2:
        return "Landlock ABI < 2"
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
            **(extra_env or {}),
        }
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
    U only as its effective uid; the CLI child completes (U, U, U) on its own
    and refuses to exec when it did not take."""

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

    def test_backstop_helper_switches_fully(self, broker_module, monkeypatch):
        u, g = self.U, self.G
        calls = self._patch(broker_module, monkeypatch, (u, u, u), (g, g, g))
        broker_module._switch_fully(u, g, False)
        assert calls == [
            ("setgroups", []),
            ("setresgid", g, g, g),
            ("setresuid", u, u, u),
            ("drop_all_caps",),
        ]

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
