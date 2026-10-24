#!/usr/bin/env -S python3 -I
"""``cli_path`` stub for per-user sandbox containers (issue #248 spike).

The SDK execs this as ``[stub, *cli_args]`` with the session workspace as cwd
and its NDJSON pipes as fds 0/1/2. The stub hands those fds, its argv, cwd and
environ to the spawner sidecar (``sandbox_spawner.py``), which runs the real
CLI inside the user's gVisor container on them. The stub then stays as the
SDK's child: it relays SIGTERM/SIGINT and mirrors the CLI's exit status, so the
SDK's ``wait()``/``returncode``/``terminate()`` keep their meaning.

Inputs (from ``ClaudeAgentOptions.env``):
  OMG_SANDBOX_SPAWNER  spawner socket path
  OMG_SANDBOX_USER     the gateway user whose container runs this CLI

Fail-closed: no spawner, no user or a refusal exits 70; the stub never falls
back to running the CLI itself.
"""

import array
import json
import os
import signal
import socket
import struct
import sys

HDR = struct.Struct("=I")
REC = struct.Struct("=Ii")
REC_SIGNAL = 1
EXITED, SIGNALED, REFUSED = 1, 2, 3


def _die(msg):
    sys.stderr.write("sandbox_launcher: %s\n" % msg)
    raise SystemExit(70)


def main():
    path = os.environ.get("OMG_SANDBOX_SPAWNER", "")
    user = os.environ.get("OMG_SANDBOX_USER", "")
    if not path or not user:
        _die("OMG_SANDBOX_SPAWNER and OMG_SANDBOX_USER are required")
    header = json.dumps(
        {
            "user": user,
            "argv": sys.argv[1:],
            "env": dict(os.environ),
            "cwd": os.getcwd(),
        }
    ).encode()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
        fds = array.array("i", [0, 1, 2])
        conn.sendmsg(
            [HDR.pack(len(header))], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)]
        )
        conn.sendall(header)
    except OSError as exc:
        _die("spawner %s: %s" % (path, exc))

    # Our copies of the pipes must not keep them open: the SDK's EOF has to
    # follow the CLI alone.
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    os.close(null)

    def relay(signum, _frame):
        try:
            conn.sendall(REC.pack(REC_SIGNAL, signum))
        except OSError:
            pass

    signal.signal(signal.SIGTERM, relay)
    signal.signal(signal.SIGINT, relay)

    buf = b""
    while len(buf) < REC.size:
        try:
            chunk = conn.recv(REC.size - len(buf))
        except InterruptedError:
            continue
        if not chunk:
            _die("spawner closed without a status")
        buf += chunk
    kind, value = REC.unpack(buf)
    if kind == EXITED:
        raise SystemExit(value)
    if kind == SIGNALED:
        signal.signal(value, signal.SIG_DFL)
        os.kill(os.getpid(), value)
        raise SystemExit(128 + value)
    _die("spawner refused the spawn")


if __name__ == "__main__":
    main()
