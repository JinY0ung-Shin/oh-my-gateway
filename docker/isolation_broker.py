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
import select
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


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _reserved_uids():
    """uids the broker must never switch a session to: 0, the gateway app uid,
    and the broker's own uid. Duplicated from ``uid_isolation.reserved_uids``
    (the broker must not import the gateway tree) and kept in lockstep."""
    return {0, _int_env("APP_UID", 1000), os.geteuid()}


def _authorize(payload):
    """Refuse a spawn whose target uid/gid is reserved or outside the band.

    Defense in depth behind the HMAC ticket: even a validly signed ticket (or
    the gateway itself) may never land a session on root, the gateway's uid or
    the broker's uid, nor outside ``[UID_MIN, UID_MAX]``. Skipped when the
    target already equals the broker's euid — the no-op same-uid case the
    protocol tests exercise without privilege."""
    uid = int(payload.get("uid", -1))
    gid = int(payload.get("gid", uid))
    if uid == os.geteuid():
        return  # no privileged uid switch happens; nothing to escalate
    lo = _int_env("CLAUDE_ISOLATION_UID_MIN", 20000)
    hi = _int_env("CLAUDE_ISOLATION_UID_MAX", 29999)
    reserved = _reserved_uids()
    for label, val in (("uid", uid), ("gid", gid)):
        if val in reserved:
            raise ValueError("%s %d is reserved" % (label, val))
        if not (lo <= val <= hi):
            raise ValueError("%s %d is outside the band [%d, %d]" % (label, val, lo, hi))


def _peer_uid(conn):
    """The connecting peer's uid via SO_PEERCRED, or ``None`` if unavailable."""
    try:
        data = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
        )
        _pid, uid, _gid = struct.unpack("iII", data)
        return uid
    except OSError:
        return None


def _child_ro_rw(real_cli, workspace, ticket_ro, ticket_rw):
    ro = list(ll.SYSTEM_RO)
    ro.append(os.path.dirname(os.path.realpath(real_cli)))
    ro.extend(ticket_ro)
    # <workspace>/.cli-tmp is NOT a separate rule: it rides beneath the granted
    # workspace, and a rule on that agent-controlled path is a symlink-escape
    # vector (see ll.safe_tmpdir). TMPDIR is set in _child_env to match.
    rw = [workspace, "/dev/shm"]
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
    env = payload.get("env", {})
    env_allow = payload.get("env_allow", [])

    wpid = os.fork()
    if wpid > 0:
        return wpid  # handler: the watcher's pid

    # --- watcher: session uid, subreaper, owns the CLI process group --------
    cpid = 0
    try:
        _switch_uid(uid, gid)
        ll.drop_all_caps()  # the session CLI must carry no CAP_SETUID/SETGID (#241)
        _prctl(_PR_SET_CHILD_SUBREAPER, 1)
        cpid = os.fork()
        if cpid == 0:
            _exec_cli(real_cli, argv, workspace, ro, rw, fds, env, env_allow)
        for fd in fds:
            _close(fd)
        # The CLI is its own session/process-group leader (setsid in _exec_cli),
        # so its whole subtree shares pgid == cpid. Relay a teardown signal to
        # that group, and sweep it once the CLI exits, so no background process
        # a session started outlives the session.
        _install_group_relay(cpid)
        status = _waitpid_status(cpid)
        _killpg(cpid, signal.SIGKILL)
        _reap_escapees()
        os._exit(_encode_wait(status))
    except Exception as exc:  # pragma: no cover - defensive in the child
        _log("watcher failed: %s" % exc)
        _killpg(cpid, signal.SIGKILL)
        os._exit(111)


# Env names the broker always strips before handing the env to the CLI: the
# ticket/socket/secret must never leak into a session.
_ENV_STRIP = (
    "GATEWAY_ISOLATION_TICKET",
    "GATEWAY_ISOLATION_TICKET_SECRET",
    "GATEWAY_ISOLATION_BROKER_SOCKET",
)


def _child_env(env, env_allow, workspace):
    """The CLI's environment: the launcher-passed env (already secret-scrubbed
    by the gateway), narrowed to the ticket allowlist when one is given, with
    the isolation plumbing stripped and a workspace-local TMPDIR set."""
    if env_allow:
        out = {k: v for k, v in env.items() if k in set(env_allow)}
    else:
        out = dict(env)
    for name in _ENV_STRIP:
        out.pop(name, None)
    tmp = os.path.join(workspace, ".cli-tmp")
    out["TMPDIR"] = tmp
    out["CLAUDE_CODE_TMPDIR"] = tmp
    return out


def _exec_cli(real_cli, argv, workspace, ro, rw, fds, env, env_allow):
    # Own session/process group so the watcher can tear down the whole subtree.
    try:
        os.setsid()
    except OSError:
        pass
    # Re-parent death to the watcher, enter the workspace, confine, exec.
    _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    try:
        os.chdir(workspace)
    except OSError:
        os._exit(113)
    ll.safe_tmpdir(workspace)  # real <workspace>/.cli-tmp, replacing any symlink
    child_ro, child_rw = _child_ro_rw(real_cli, workspace, ro, rw)
    child_env = _child_env(env, env_allow, workspace)
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
    os.execve(real_cli, [real_cli, *argv], child_env)


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


def _killpg(pgid, sig):
    if pgid and pgid > 0:
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass


def _signal_pid(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _install_group_relay(pgid):
    """In the watcher: forward SIGTERM/SIGINT to the CLI's process group."""

    def _relay(signum, _frame):
        _killpg(pgid, signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _relay)
        except (OSError, ValueError):
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


def _supervise(conn, watcher):
    """Wait for the watcher while watching the launcher link.

    Relays a teardown signal the launcher forwards (``-signum``) to the watcher
    (which forwards it to the CLI group), and — crucially — on the launcher's
    disconnect (EOF, e.g. the SDK ``close()`` escalating to SIGKILL on the
    launcher) tears the watcher down so the CLI tree cannot keep running (and
    billing) after its session ends. Returns the watcher's raw wait status.
    """
    try:
        conn.setblocking(False)
    except OSError:
        pass
    deadline = None  # set once teardown has been requested
    while True:
        try:
            pid, status = os.waitpid(watcher, os.WNOHANG)
        except ChildProcessError:
            return 0
        if pid == watcher:
            return status
        timeout = 0.2
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _signal_pid(watcher, signal.SIGKILL)
                deadline = time.monotonic() + 5.0
                continue
            timeout = min(timeout, remaining)
        try:
            readable, _, _ = select.select([conn], [], [], timeout)
        except (OSError, ValueError):
            readable = []
        if not readable:
            continue
        try:
            data = conn.recv(4)
        except BlockingIOError:
            continue
        except OSError:
            data = b""
        if not data:  # launcher gone: ask the watcher to tear down, then force
            if deadline is None:
                _signal_pid(watcher, signal.SIGTERM)
                deadline = time.monotonic() + 5.0
            continue
        if len(data) >= 4:
            (code,) = struct.unpack("=i", data[:4])
            if code < 0:
                _signal_pid(watcher, -code)  # relay to the watcher → CLI group


def _handle(conn, payload, fds, real_cli):
    """Per-connection child: spawn the watcher, supervise the launcher link,
    and mirror the exit status back. Verification already happened in the
    resident parent, so a spent nonce cannot be replayed into a fresh fork."""
    watcher = _spawn_watcher(payload, fds, real_cli)
    for fd in fds:
        _close(fd)  # the handler keeps no copy of the session's pipes
    status = _supervise(conn, watcher)
    try:
        conn.sendall(struct.pack("=i", _status_to_wire(_encode_wait(status))))
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

    # The only legitimate client is a launcher stub running as the gateway's
    # uid. Default to APP_UID; the broker's own uid is always allowed so the
    # protocol runs same-uid in tests without privilege.
    allowed_peer = _int_env("GATEWAY_ISOLATION_PEER_UID", _int_env("APP_UID", 1000))

    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen_sock.bind(sock_path)
    # Owner + group (the broker runs with the gateway's gid so the gateway-uid
    # launcher can connect); never world — and sessions cannot reach the path
    # anyway (it is outside their Landlock ruleset). (#241 review)
    os.chmod(sock_path, 0o660)
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
        # Receive + verify the ticket in the RESIDENT PARENT: the single-use
        # nonce set must be authoritative, and a per-connection fork would each
        # keep its own copy and never see a replay (#241 review). Also pin the
        # peer to the gateway uid and bound the target uid/gid.
        fds = []
        try:
            conn.settimeout(10.0)  # a hung client must not wedge the parent
            header, fds = _recv_spawn(conn)
            peer = _peer_uid(conn)
            if peer is not None and peer != allowed_peer and peer != os.geteuid():
                raise ValueError("unexpected peer uid %r" % peer)
            payload = verify_ticket(header["ticket"], secret, _seen_nonces)
            _authorize(payload)
            payload["argv"] = header.get("argv", [])
            payload["env"] = header.get("env", {})
        except (ValueError, KeyError, OSError) as exc:
            _log("rejecting spawn: %s" % exc)
            for fd in fds:
                _close(fd)
            try:
                conn.sendall(struct.pack("=i", 120))  # refusal exit code
            except OSError:
                pass
            conn.close()
            _reap_escapees()
            continue
        pid = os.fork()
        if pid == 0:
            listen_sock.close()
            try:
                _handle(conn, payload, fds, real_cli)
            finally:
                os._exit(0)
        conn.close()
        for fd in fds:
            _close(fd)
        _reap_escapees()


if __name__ == "__main__":
    main()
