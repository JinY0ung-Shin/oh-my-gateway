#!/usr/bin/env python3
"""Isolation launcher for Claude CLI children (issue #241).

The gateway points ``cli_path`` at this fixed, root-owned stub instead of at the
real CLI. The SDK execs it as ``[launcher, *cli_args]``. What it does depends on
``CLAUDE_ISOLATION_MODE`` (passed through the environment):

* **landlock** — apply a Landlock ruleset to itself and ``execv`` the real CLI.
  Landlock is inherited across ``execve``, so the CLI, its Bash tool, a script
  the agent wrote and stdio MCP are all confined at ``open()``. No privilege.
* **uid** — hand the spawn to the resident broker: present the signed ticket
  (``GATEWAY_ISOLATION_TICKET``) and pass fds 0/1/2 over ``SCM_RIGHTS`` to the
  broker socket, then stay as the SDK's child, mirroring the CLI's exit status
  and relaying SIGTERM/SIGINT. The broker forks the CLI as the session's uid and
  applies the same Landlock ruleset after the switch. See ``isolation_broker.py``.

Both the written-script read path (F1) and — in uid mode — cross-user DAC/signal
isolation are closed structurally, not by prompt text. Fail-closed: a missing
real CLI, an unusable Landlock, or (uid mode) an unreachable broker aborts the
spawn with a non-zero exit; the gateway must never fall through to an unconfined
CLI once it has decided to confine.

stdlib only. The launcher starts in the agent-controlled workspace cwd, so it
re-execs itself under ``python3 -I`` before importing anything beyond the two
builtins used for that guard — a planted module in the workspace must never be
importable.
"""

import os
import sys

if not sys.flags.isolated:
    os.execv(
        sys.executable,
        [sys.executable, "-I", os.path.realpath(__file__), *sys.argv[1:]],
    )

import array
import socket
import struct

# Sibling under docker/ (fixed, root-owned); -I keeps this import off cwd/env.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _landlock_rules as ll  # noqa: E402


def _die(msg):
    sys.stderr.write("landlock_launcher: %s\n" % msg)
    raise SystemExit(70)


# --- real CLI + path lists ---------------------------------------------------


def _real_cli():
    real = os.environ.get("GATEWAY_REAL_CLI", "").strip()
    if not real:
        _die("GATEWAY_REAL_CLI is unset; refusing to spawn an unconfined CLI")
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        _die("GATEWAY_REAL_CLI=%r is not an executable file" % real)
    return real


def _cwd():
    return os.environ.get("PWD") or os.getcwd()


def _parse_paths(var):
    return [p for p in os.environ.get(var, "").split(os.pathsep) if p]


def _session_tmpdir(cwd):
    """A workspace-local tmpdir, honored over the shared /tmp.

    An operator ``CLAUDE_CODE_TMPDIR`` already under the cwd is kept; otherwise
    point TMPDIR/CLAUDE_CODE_TMPDIR at ``<cwd>/.cli-tmp`` so the CLI's temp
    writes stay inside the granted workspace, not the shared /tmp (a cross-
    session channel when sessions share a uid). Exported for the real CLI.
    """
    real_cwd = os.path.realpath(cwd)
    configured = os.environ.get("CLAUDE_CODE_TMPDIR", "").strip()
    if configured and os.path.realpath(configured).startswith(real_cwd + os.sep):
        tmp = configured
        try:
            os.makedirs(tmp, exist_ok=True)
        except OSError:
            pass
    else:
        tmp = ll.safe_tmpdir(real_cwd)
    os.environ["TMPDIR"] = tmp
    os.environ["CLAUDE_CODE_TMPDIR"] = tmp
    return tmp


def _ro_rw(real_cli, cwd):
    """The read-only and read-write path lists for a confinement at *cwd*."""
    ro = list(ll.SYSTEM_RO)
    ro.append(os.path.dirname(os.path.realpath(real_cli)))
    ro.extend(_parse_paths("GATEWAY_LANDLOCK_RO"))
    # TMPDIR is set inside the workspace but is NOT a separate rule: it lies
    # beneath the granted cwd, so a rule would be redundant and (because it is
    # agent-controlled) a symlink-escape vector — see ll.safe_tmpdir.
    _session_tmpdir(cwd)
    rw = [cwd, "/dev/shm"]
    rw.extend(_parse_paths("GATEWAY_LANDLOCK_RW"))
    return ro, rw


# --- landlock mode: confine in place, then exec ------------------------------


def _run_landlock(real_cli):
    cwd = _cwd()
    ro, rw = _ro_rw(real_cli, cwd)
    try:
        ll.build_and_restrict(ro, rw)
    except ll.LandlockError as exc:
        _die(str(exc))
    os.execv(real_cli, [real_cli, *sys.argv[1:]])


# --- uid mode: hand the spawn (fds + ticket) to the broker -------------------


def _run_uid():
    ticket = os.environ.get("GATEWAY_ISOLATION_TICKET", "").strip()
    sock_path = os.environ.get("GATEWAY_ISOLATION_BROKER_SOCKET", "").strip()
    if not ticket:
        _die("GATEWAY_ISOLATION_TICKET is unset; refusing an unconfined uid spawn")
    if not sock_path:
        _die("GATEWAY_ISOLATION_BROKER_SOCKET is unset; no broker to hand off to")

    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(sock_path)
    except OSError as exc:
        _die("cannot reach the isolation broker at %s: %s" % (sock_path, exc))

    # Hand the CLI argv + ticket as a length-prefixed JSON header, with fds
    # 0/1/2 as SCM_RIGHTS ancillary data. The broker forks the CLI onto them.
    import json

    # Pass our own environment too: it is the env the SDK built for this child
    # (ANTHROPIC_*, PATH, HOME, …), already scrubbed of gateway secrets by the
    # gateway's _configure_sdk_env. The broker uses it as the CLI's env (the
    # broker's own env would otherwise be inherited, losing the SDK's values),
    # filtered by the ticket's env allowlist. The ticket/socket vars are dropped
    # by the broker so they never reach the CLI.
    header = json.dumps(
        {"ticket": ticket, "argv": sys.argv[1:], "env": dict(os.environ)}
    ).encode("utf-8")
    fds = array.array("i", [0, 1, 2])
    try:
        conn.sendmsg(
            [struct.pack("=I", len(header)), header],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds.tobytes())],
        )
    except OSError as exc:
        _die("failed to hand fds to the broker: %s" % exc)

    # Stay as the SDK's child: block on the broker's status reply and mirror it
    # so the SDK's wait()/returncode track the real CLI. A relay of SIGTERM/
    # SIGINT to the broker keeps teardown working.
    import signal

    def _relay(signum, _frame):
        try:
            conn.sendall(struct.pack("=i", -signum))
        except OSError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _relay)
        except (OSError, ValueError):
            pass

    status = conn.recv(4)
    if len(status) < 4:
        _die("broker closed without a status (spawn refused?)")
    (code,) = struct.unpack("=i", status)
    if code < 0:
        # terminated by signal: re-raise it on ourselves so the SDK sees -signal
        try:
            signal.signal(-code, signal.SIG_DFL)
            os.kill(os.getpid(), -code)
        except (OSError, ValueError):
            pass
        raise SystemExit(128 - code)
    raise SystemExit(code)


def main():
    mode = os.environ.get("CLAUDE_ISOLATION_MODE", "").strip().lower()
    if mode == "uid":
        _run_uid()
    else:
        # landlock (default when the gateway routed here); fail closed on the
        # real CLI first so a misconfig never execs unconfined.
        _run_landlock(_real_cli())


if __name__ == "__main__":
    main()
