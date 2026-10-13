"""The uid-isolation broker protocol (issue #241, PR-C), same-uid.

Validates the design's crux (docs/security-process-isolation.md §3 B): the SDK
owns the CLI's pipes, but a same-uid launcher stub hands fds 0/1/2 to the broker
over SCM_RIGHTS with a signed ticket, and the broker forks the CLI onto them,
confined with Landlock, mirroring the exit status back.

The ticket uid is this test's own uid, so the broker's uid switch is a no-op —
everything *except* the privileged cross-uid switch runs here. The real
cross-uid switch is container-only.
"""

from __future__ import annotations

import json
import os
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


def _landlock_skip() -> str:
    from src.backends.claude import landlock

    if landlock.landlock_abi() < 2:
        return "Landlock ABI < 2"
    if not os.access(_SYS_PY, os.X_OK):
        return "no /usr/bin/python3"
    return ""


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(_landlock_skip()), reason=_landlock_skip() or ""),
]

_PROBE = (
    "import json, sys\n"
    "def r(p):\n"
    "    try:\n"
    "        open(p).read(); return 'OK'\n"
    "    except OSError as e: return 'DENY:' + str(e.errno)\n"
    "ws, out = sys.argv[1], sys.argv[2]\n"
    "print(json.dumps({'in': r(ws + '/in.txt'), 'out': r(out + '/secret.txt')}))\n"
)


@pytest.fixture
def broker(tmp_path):
    sock = tmp_path / "broker.sock"
    secret = "test-secret-123"
    env = {
        **os.environ,
        ui.BROKER_SOCKET_ENV: str(sock),
        ui.TICKET_SECRET_ENV: secret,
        "GATEWAY_REAL_CLI": _SYS_PY,
    }
    proc = subprocess.Popen(
        [sys.executable, str(_BROKER)],
        env=env,
        stderr=subprocess.PIPE,
    )
    # wait for the socket to appear
    for _ in range(100):
        if sock.exists():
            break
        if proc.poll() is not None:
            raise RuntimeError(
                "broker exited early: %s" % proc.stderr.read().decode()
            )
        time.sleep(0.02)
    else:
        proc.kill()
        raise RuntimeError("broker never created its socket")
    yield sock, secret
    proc.kill()
    proc.wait(timeout=5)


def _run_launcher(sock, secret, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "in.txt").write_text("INSIDE\n")
    (ws / "probe.py").write_text(_PROBE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n")

    ticket = ui.mint_ticket(
        uid=os.getuid(),
        workspace=str(ws),
        ro=[],
        rw=[str(ws)],
        env_allow=[],
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "PWD": str(ws),
        "CLAUDE_ISOLATION_MODE": "uid",
        "GATEWAY_ISOLATION_TICKET": ticket,
        ui.BROKER_SOCKET_ENV: str(sock),
        ui.TICKET_SECRET_ENV: secret,
    }
    return subprocess.run(
        [sys.executable, str(_LAUNCHER), str(ws / "probe.py"), str(ws), str(outside)],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestBrokerProtocol:
    def test_fd_passing_and_confinement(self, broker, tmp_path, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, broker[1])
        proc = _run_launcher(broker[0], broker[1], tmp_path)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["in"] == "OK"  # workspace read reached the CLI's pipe
        assert result["out"].startswith("DENY")  # confined outside the workspace

    def test_cli_exit_code_is_mirrored(self, broker, tmp_path, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, broker[1])
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "boom.py").write_text("import sys; sys.exit(7)\n")
        ticket = ui.mint_ticket(
            uid=os.getuid(), workspace=str(ws), ro=[], rw=[str(ws)], env_allow=[]
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            "CLAUDE_ISOLATION_MODE": "uid",
            "GATEWAY_ISOLATION_TICKET": ticket,
            ui.BROKER_SOCKET_ENV: str(broker[0]),
            ui.TICKET_SECRET_ENV: broker[1],
        }
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), str(ws / "boom.py")],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 7, proc.stderr

    def test_forged_ticket_refused(self, broker, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "x.py").write_text("print('ran')\n")
        # a ticket signed with the WRONG secret must be refused by the broker
        import src.backends.claude.uid_isolation as ui_mod

        os.environ[ui.TICKET_SECRET_ENV] = "attacker-secret"
        try:
            forged = ui_mod.mint_ticket(
                uid=os.getuid(), workspace=str(ws), ro=[], rw=[str(ws)], env_allow=[]
            )
        finally:
            os.environ[ui.TICKET_SECRET_ENV] = broker[1]
        env = {
            "PATH": "/usr/bin:/bin",
            "PWD": str(ws),
            "CLAUDE_ISOLATION_MODE": "uid",
            "GATEWAY_ISOLATION_TICKET": forged,
            ui.BROKER_SOCKET_ENV: str(broker[0]),
            ui.TICKET_SECRET_ENV: broker[1],
        }
        proc = subprocess.run(
            [sys.executable, str(_LAUNCHER), str(ws / "x.py")],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # broker refuses → launcher exits non-zero, CLI never ran
        assert proc.returncode != 0
        assert "ran" not in proc.stdout
