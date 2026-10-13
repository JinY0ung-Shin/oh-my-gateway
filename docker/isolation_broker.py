#!/usr/bin/env python3
"""Resident spawn broker for uid-per-user isolation (issue #241, PR-C).

This is the one privileged process in the design. The gateway body holds no
capability; the broker holds only ``CAP_SETUID``/``CAP_SETGID`` (locked down by
the entrypoint — non-root uid ideally, else NNP + securebits + a seccomp arg
filter) and does exactly one thing: fork each Claude CLI as its session's uid
before the Landlock step, so a session gets DAC + signal isolation on top of the
filesystem confinement that ``landlock`` mode already gives.

Flow per spawn (see docs/security-process-isolation.md §3 B/A/C/D/O):

1. The launcher stub (``landlock_launcher.py`` uid branch), running as the
   gateway's uid, connects to this broker's socket, sends a JSON header
   ``{ticket, argv}`` and passes the SDK's fds 0/1/2 over ``SCM_RIGHTS``.
2. The broker verifies the ticket — HMAC signature (shared secret), expiry and
   single-use nonce — then forks. The child switches to the ticket's uid
   (setgroups→setgid→setuid), stays as a ``CHILD_SUBREAPER`` **watcher**, and
   forks the CLI; the CLI sets ``PDEATHSIG``, ``chdir``s into the workspace,
   applies the **same** Landlock ruleset (``_landlock_rules``) *after* the
   switch, dups the passed fds onto 0/1/2 and ``execv``s the real CLI.
3. The watcher waits for the CLI, writes its exit status back to the launcher
   (which mirrors it to the SDK), reaps any ``setsid`` escapees, and exits.

Security notes:

* **Never imports the gateway tree** (``/app/src``): a session can write there
  today, and this runs privileged. It is self-contained stdlib plus the sibling
  ``_landlock_rules`` under ``docker/`` (a fixed, root-owned location). The
  ticket-verify logic is duplicated from ``uid_isolation`` on purpose and kept
  in lockstep by a cross-check test.
* **Fixed real CLI**: the exec target is fixed at broker startup
  (``GATEWAY_REAL_CLI``); a request never names a binary.
* **uid switch is guarded**: skipped when the target already equals the current
  euid, so the protocol (fd passing, fork tree, Landlock, exit mirroring) is
  exercisable same-uid without privilege in tests; the privileged cross-uid
  switch is container-only.
"""

import array
import hashlib
import hmac
import json
import os
import signal
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _landlock_rules as ll  # noqa: E402

_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_PDEATHSIG = 1

_SOCKET_ENV = "GATEWAY_ISOLATION_BROKER_SOCKET"
_SECRET_ENV = "GATEWAY_ISOLATION_TICKET_SECRET"
_REAL_CLI_ENV = "GATEWAY_REAL_CLI"

_seen_nonces: set = set()


def _log(msg):
    sys.stderr.write("isolation_broker: %s\n" % msg)
    sys.stderr.flush()


# --- ticket verify (kept in lockstep with src/.../uid_isolation.py) ----------


def verify_ticket(ticket, secret, seen_nonces):
    sig, _, body = ticket.partition(".")
    if not sig or not body:
        raise ValueError("malformed ticket")
    expected = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad ticket signature")
    payload = json.loads(body)
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("ticket expired")
    nonce = payload.get("nonce")
    if nonce in seen_nonces:
        raise ValueError("ticket already used")
    seen_nonces.add(nonce)
    return payload


# --- fd passing --------------------------------------------------------------


def _recv_spawn(conn):
    """Receive the JSON header and the three passed fds from the launcher."""
    fds = array.array("i")
    # Header is length-prefixed (4 bytes) + JSON; the fds ride the first recvmsg.
    msg, ancdata, _flags, _addr = conn.recvmsg(4096, socket.CMSG_LEN(3 * fds.itemsize))
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            fds.frombytes(cdata[: len(cdata) - (len(cdata) % fds.itemsize)])
    if len(msg) < 4:
        raise ValueError("short spawn message")
    (hlen,) = struct.unpack("=I", msg[:4])
    body = msg[4 : 4 + hlen]
    while len(body) < hlen:
        more = conn.recv(hlen - len(body))
        if not more:
            raise ValueError("truncated spawn header")
        body += more
    header = json.loads(body.decode("utf-8"))
    return header, list(fds)


# --- the privileged spawn ----------------------------------------------------


def _switch_uid(uid, gid):
    """Drop to (gid, uid) if not already there. No-op when already the target
    (lets the protocol run same-uid in tests); the real cross-uid switch needs
    CAP_SETUID/SETGID in the broker."""
    if os.geteuid() == uid and os.getegid() == gid:
        return
    os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)


def _child_ro_rw(real_cli, workspace, ticket_ro, ticket_rw):
    ro = list(ll.SYSTEM_RO)
    ro.append(os.path.dirname(os.path.realpath(real_cli)))
    ro.extend(ticket_ro)
    tmp = os.path.join(workspace, ".cli-tmp")
    os.environ["TMPDIR"] = tmp
    os.environ["CLAUDE_CODE_TMPDIR"] = tmp
    rw = [workspace, tmp, "/dev/shm"]
    rw.extend(ticket_rw)
    return ro, rw


def _spawn_watcher(payload, fds, real_cli):
    """Fork the session-uid watcher → CLI tree. Returns the watcher pid.

    Called in the handler (gateway uid), which owns ``conn`` and relays the
    status; the watcher switches uid and owns the CLI subtree.
    """
    uid = int(payload["uid"])
    gid = int(payload.get("gid", uid))
    workspace = payload["workspace"]
    argv = payload.get("argv", [])
    ro = payload.get("ro", [])
    rw = payload.get("rw", [])

    wpid = os.fork()
    if wpid > 0:
        return wpid  # handler: the watcher's pid

    # --- watcher: session uid, subreaper, owns the CLI ----------------------
    try:
        _switch_uid(uid, gid)
        _prctl(_PR_SET_CHILD_SUBREAPER, 1)
        cpid = os.fork()
        if cpid == 0:
            _exec_cli(real_cli, argv, workspace, ro, rw, fds)  # never returns
        for fd in fds:
            _close(fd)
        status = _waitpid_status(cpid)
        _reap_escapees()
        os._exit(_encode_wait(status))
    except Exception as exc:  # pragma: no cover - defensive in the child
        _log("watcher failed: %s" % exc)
        os._exit(111)


def _exec_cli(real_cli, argv, workspace, ro, rw, fds):
    # Re-parent death to the watcher, enter the workspace, confine, exec.
    _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    try:
        os.chdir(workspace)
    except OSError:
        os._exit(113)
    child_ro, child_rw = _child_ro_rw(real_cli, workspace, ro, rw)
    for target, src in enumerate(fds[:3]):
        os.dup2(src, target)
    for fd in fds:
        if fd > 2:
            _close(fd)
    try:
        ll.build_and_restrict(child_ro, child_rw)
    except ll.LandlockError as exc:
        sys.stderr.write("isolation_broker: %s\n" % exc)
        os._exit(114)
    os.execv(real_cli, [real_cli, *argv])


# --- small libc / wait helpers ----------------------------------------------

import ctypes  # noqa: E402

_libc = ctypes.CDLL(None, use_errno=True)


def _prctl(option, arg2):
    if _libc.prctl(option, arg2, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(%d) failed" % option)


def _close(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _waitpid_status(pid):
    while True:
        try:
            _wpid, status = os.waitpid(pid, 0)
            return status
        except InterruptedError:
            continue


def _encode_wait(status):
    # Fit a waitstatus into the exit code we pass up (0..255); negative is
    # mapped by the launcher to -signal.
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 125


def _reap_escapees():
    # setsid children reparented to this subreaper watcher.
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _status_to_wire(exit_code):
    # exit_code came from _encode_wait (128+sig for signals); turn it into the
    # launcher's convention: >=0 exit code, <0 for -signal.
    if exit_code > 128:
        return -(exit_code - 128)
    return exit_code


# --- connection handling -----------------------------------------------------


def _handle(conn, real_cli, secret):
    """Handler (gateway uid): verify, spawn the watcher, relay status on conn."""
    fds = []
    try:
        header, fds = _recv_spawn(conn)
        payload = verify_ticket(header["ticket"], secret, _seen_nonces)
        payload["argv"] = header.get("argv", [])
    except (ValueError, KeyError, OSError) as exc:
        _log("rejecting spawn: %s" % exc)
        for fd in fds:
            _close(fd)
        try:
            conn.sendall(struct.pack("=i", 120))  # refusal exit code
        except OSError:
            pass
        return

    watcher = _spawn_watcher(payload, fds, real_cli)
    for fd in fds:
        _close(fd)  # the handler keeps no copy of the session's pipes
    exit_code = _encode_wait(_waitpid_status(watcher))
    try:
        conn.sendall(struct.pack("=i", _status_to_wire(exit_code)))
    except OSError:
        pass


def main():
    sock_path = os.environ.get(_SOCKET_ENV, "").strip()
    secret = os.environ.get(_SECRET_ENV, "").strip().encode("utf-8")
    real_cli = os.environ.get(_REAL_CLI_ENV, "").strip()
    if not sock_path or not secret or not real_cli:
        _log(
            "need %s, %s and %s; refusing to start"
            % (_SOCKET_ENV, _SECRET_ENV, _REAL_CLI_ENV)
        )
        raise SystemExit(78)
    if not (os.path.isfile(real_cli) and os.access(real_cli, os.X_OK)):
        _log("%s=%r is not executable" % (_REAL_CLI_ENV, real_cli))
        raise SystemExit(78)

    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen_sock.bind(sock_path)
    os.chmod(sock_path, 0o600)
    listen_sock.listen(64)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    _log("listening on %s (real CLI: %s)" % (sock_path, real_cli))

    while True:
        try:
            conn, _ = listen_sock.accept()
        except InterruptedError:
            continue
        except OSError as exc:
            _log("accept failed: %s" % exc)
            continue
        pid = os.fork()
        if pid == 0:
            listen_sock.close()
            try:
                _handle(conn, real_cli, secret)
            finally:
                os._exit(0)
        conn.close()
        _reap_escapees()


if __name__ == "__main__":
    main()
