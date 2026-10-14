#!/usr/bin/env -S python3 -I
"""Resident spawn broker for uid-per-user isolation (issue #241, PR-C).

This is the one privileged process in the design. The gateway body holds no
capability; the broker holds only ``CAP_SETUID``/``CAP_SETGID`` (locked down by
the entrypoint — non-root uid ideally, else NNP + securebits + a seccomp arg
filter) and does exactly one thing: fork each Claude CLI as its session's uid
before the Landlock step, so a session gets DAC + signal isolation on top of the
filesystem confinement that ``landlock`` mode already gives.

Flow per spawn (see docs/security-process-isolation.md §3 B/A/C/D/O):

1. The launcher stub (``landlock_launcher.py`` uid branch), running as the
   gateway's uid, connects to this broker's socket, sends a length-prefixed JSON
   header ``{ticket, argv, env}`` and passes the SDK's fds 0/1/2 over
   ``SCM_RIGHTS``.
2. The resident broker pins the peer to the gateway uid (``SO_PEERCRED``, before
   reading anything), reads the bounded header without letting a stalled peer
   hold up other connections, verifies the ticket — HMAC signature, expiry and
   single-use nonce — and the request (absolute paths; uid/gid inside the band
   and not reserved), then forks one **supervisor** per spawn.
3. The supervisor takes the session uid as its *effective* uid only (see
   :func:`_become_supervisor`), drops every capability, becomes a
   ``CHILD_SUBREAPER`` and forks the CLI. The CLI child completes the switch to
   ``(U, U, U)``, sets ``PDEATHSIG``, ``chdir``s into the workspace, applies the
   **same** Landlock ruleset (``_landlock_rules``) *after* the switch, dups the
   passed fds onto 0/1/2 and ``execve``s the real CLI.
4. The supervisor owns the launcher link: it relays SIGTERM/SIGINT records to
   the CLI's process group and tears the tree down when the link drops. Once the
   CLI has exited it kills and reaps every remaining descendant, then writes the
   CLI's exit status back (the launcher mirrors it to the SDK) and exits.

Security notes:

* **Never imports the gateway tree** (``/app/src``): a session can write there
  today, and this runs privileged. It is self-contained stdlib plus the sibling
  ``_landlock_rules`` under ``docker/`` (a fixed, root-owned location). The
  ticket-verify logic is duplicated from ``uid_isolation`` on purpose and kept
  in lockstep by a cross-check test (``tests/test_isolation_broker.py``).
* **Fixed real CLI**: the exec target is fixed at broker startup
  (``GATEWAY_REAL_CLI``); a request never names a binary.
* **No same-uid shortcut in production**: the broker's own uid/gid are reserved
  like root's and the gateway's. Only the test-only ``--test-same-uid`` flag
  (never passed by the entrypoint) lets a ticket name the broker's own uid, so
  the protocol (fd passing, fork tree, Landlock, exit mirroring) is exercisable
  same-uid without privilege; the privileged cross-uid switch is container-only.
"""

import array
import ctypes
import fcntl
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

# Test-only (tests/test_isolation_broker.py): lets a ticket naming exactly the
# broker's own uid and gid run with no switch. The entrypoint never passes it
# and a session cannot reach the broker's argv.
_TEST_SAME_UID_FLAG = "--test-same-uid"

_MAX_PENDING = 32  # handshakes in flight (the peer is pinned to the gateway uid)
_HANDSHAKE_TIMEOUT = 10.0  # total, from accept to a complete spawn header
_TEARDOWN_GRACE = 5.0  # the CLI's first SIGTERM -> SIGKILL, once the link drops
_RELAY_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT})
_ANC_SPACE = socket.CMSG_SPACE(3 * array.array("i").itemsize)  # fds 0/1/2
_HEADER_KEYS = frozenset({"ticket", "argv", "env"})

# Exit codes of a CLI child that failed closed before exec.
_EXIT_CHDIR = 113
_EXIT_LANDLOCK = 114
_EXIT_CREDS = 115
_EXIT_ORPHANED = 116
_EXIT_SETUP = 117
_EXIT_TMPDIR = 118  # cannot make <workspace>/.cli-tmp (workspace not writable)

_libc = ctypes.CDLL(None, use_errno=True)


def _log(msg):
    try:
        sys.stderr.write("isolation_broker: %s\n" % msg)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# --- ticket verify (kept in lockstep with src/.../uid_isolation.py) ----------


def verify_ticket(ticket, secret, seen, now=None):
    """Verify a ticket's HMAC signature, expiry and single use.

    Returns the payload dict or raises ``ValueError``. ``seen`` maps each spent
    nonce to its ticket's ``exp``. ``now`` lets the caller pass a clock that
    never runs backwards (the resident loop keeps a high-water mark).
    """
    # Tickets are minted ASCII (json.dumps escapes the rest); refuse anything
    # else here instead of letting compare_digest raise TypeError on it.
    if not isinstance(ticket, str) or not ticket.isascii():
        raise ValueError("malformed ticket")
    sig, _, body = ticket.partition(".")
    if not sig or not body:
        raise ValueError("malformed ticket")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
        raise ValueError("bad ticket signature")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("ticket payload is not an object")
    exp, nonce = payload.get("exp"), payload.get("nonce")
    if type(exp) is not int or not isinstance(nonce, str) or not nonce:
        raise ValueError("ticket lacks an integer exp or a nonce")
    now = int(time.time() if now is None else now)
    if exp < now:
        raise ValueError("ticket expired")
    # A spent nonce is kept until its ticket expires, then pruned: a replay
    # after that is refused by the expiry check above, so the store stays
    # bounded by the spawn rate times the ticket TTL.
    for spent in [n for n, e in seen.items() if e < now]:
        del seen[spent]
    if nonce in seen:
        raise ValueError("ticket already used")
    seen[nonce] = exp
    return payload


# --- request validation ------------------------------------------------------


def _str_list(value, what):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("%s must be a list of strings" % what)
    return value


def _abs_path(value, what):
    # The CLI child chdirs into the workspace before the ruleset is built, so a
    # relative entry would resolve against the wrong directory: refuse it.
    if not isinstance(value, str) or "\0" in value or not os.path.isabs(value):
        raise ValueError("%s %r is not an absolute path" % (what, value))
    return value


def _parse_header(body):
    """Decode the spawn header against its strict schema."""
    header = json.loads(body.decode("utf-8"))
    if not isinstance(header, dict) or set(header) != _HEADER_KEYS:
        raise ValueError("spawn header must hold exactly ticket, argv and env")
    if not isinstance(header["ticket"], str):
        raise ValueError("spawn header ticket must be a string")
    argv = _str_list(header["argv"], "spawn argv")
    env = header["env"]
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError("spawn env must map strings to strings")
    if any("\0" in a for a in argv) or any(
        not k or "=" in k or "\0" in k or "\0" in v for k, v in env.items()
    ):
        raise ValueError("spawn argv/env holds a NUL byte or an invalid name")
    return header


def _spawn_request(header, payload):
    """The validated spawn: the verified ticket payload plus the header."""
    uid = payload.get("uid")
    gid = payload.get("gid", uid)
    if type(uid) is not int or type(gid) is not int:
        raise ValueError("ticket uid/gid must be integers")
    return {
        "uid": uid,
        "gid": gid,
        "workspace": _abs_path(payload.get("workspace"), "ticket workspace"),
        "ro": [
            _abs_path(p, "ticket ro entry")
            for p in _str_list(payload.get("ro", []), "ticket ro")
        ],
        "rw": [
            _abs_path(p, "ticket rw entry")
            for p in _str_list(payload.get("rw", []), "ticket rw")
        ],
        "env_allow": _str_list(payload.get("env_allow", []), "ticket env_allow"),
        "argv": header["argv"],
        "env": header["env"],
    }


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _authorize(uid, gid, test_same_uid):
    """Refuse a target uid/gid that is reserved or outside the band.

    Defense in depth behind the HMAC ticket: even a validly signed ticket (or
    the gateway itself) may never land a session on root, the gateway's uid/gid
    or the broker's own uid/gid, nor outside ``[UID_MIN, UID_MAX]``. The reserved
    set is duplicated from ``uid_isolation.reserved_uids`` (the broker must not
    import the gateway tree). The one exception is the test-only same-uid mode,
    where a ticket naming exactly the broker's own uid and gid runs unswitched.
    """
    if test_same_uid and (uid, gid) == (os.getuid(), os.getgid()):
        return
    lo = _int_env("CLAUDE_ISOLATION_UID_MIN", 20000)
    hi = _int_env("CLAUDE_ISOLATION_UID_MAX", 29999)
    reserved = {
        0,
        _int_env("APP_UID", 1000),
        _int_env("APP_GID", 1000),
        os.getuid(),
        os.geteuid(),
        os.getgid(),
        os.getegid(),
    }
    for label, val in (("uid", uid), ("gid", gid)):
        if val in reserved:
            raise ValueError("%s %d is reserved" % (label, val))
        if not lo <= val <= hi:
            raise ValueError(
                "%s %d is outside the band [%d, %d]" % (label, val, lo, hi)
            )


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


# --- small libc / process helpers --------------------------------------------


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


def _drain(fd):
    try:
        while os.read(fd, 4096):
            pass
    except OSError:  # BlockingIOError once empty
        pass


def _wait_readable(fd, timeout):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    poller.poll(timeout * 1000)


def _close_fds_except(keep):
    """Close every fd but *keep* and 0/1/2 (fork hygiene: no other spawn's
    socket or pipes may reach this one's CLI)."""
    keep = set(keep) | {0, 1, 2}
    try:
        fds = [int(name) for name in os.listdir("/proc/self/fd")]
    except OSError:
        fds = range(os.sysconf("SC_OPEN_MAX"))
    for fd in fds:
        if fd not in keep:
            _close(fd)


def _children():
    """The pids of this process's direct children, live or zombie."""
    pids = set()
    try:
        for tid in os.listdir("/proc/self/task"):
            with open("/proc/self/task/%s/children" % tid) as f:
                pids.update(int(p) for p in f.read().split())
        return pids
    except OSError:
        pass  # no CONFIG_PROC_CHILDREN: scan every process's parent instead
    me = str(os.getpid())
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as f:
                stat = f.read()
        except OSError:
            continue
        # "pid (comm) state ppid ...": comm may itself hold spaces and ')'.
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 1 and fields[1] == me:
            pids.add(int(name))
    return pids


def _proc_ids(pid):
    """A process's real (uid, gid), or ``None`` if it cannot be read."""
    ids = {}
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith(("Uid:", "Gid:")):
                    ids[line[:3]] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    if "Uid" not in ids or "Gid" not in ids:
        return None
    return ids["Uid"], ids["Gid"]


def _describe(status):
    if os.WIFSIGNALED(status):
        return "signal %d" % os.WTERMSIG(status)
    return "exit %d" % os.WEXITSTATUS(status)


def _send(sock, kind, value):
    try:
        sock.settimeout(1.0)
        sock.sendall(ll.REC.pack(kind, value))
    except OSError:
        pass


# --- the per-spawn supervisor ------------------------------------------------


def _become_supervisor(uid, gid, test_same_uid):
    """Take the session uid for signalling it, but not for being signalled.

    The supervisor must signal the session's processes, and the session must
    not be able to signal — so kill, to escape teardown — its supervisor, even
    on kernels without Landlock signal scoping. kill(2) lets a sender through
    when its real or effective uid matches the target's real or saved uid. So
    the effective uid becomes U while the real and saved uids stay the broker's:
    the supervisor can signal U's processes, and U's processes can neither
    signal nor ptrace it (the credential change also makes it non-dumpable).
    Nothing else ever signals it either: the launcher link is its control
    channel (relay records, EOF = teardown), so no teardown step depends on a
    cross-uid kill() the broker would need CAP_KILL for. The CLI child finishes
    the switch to ``(U, U, U)`` itself, unprivileged, since U is already its
    effective uid.
    """
    broker = os.getuid()
    if not (test_same_uid and (uid, gid) == (broker, os.getgid())):
        os.setgroups([])
        os.setresgid(gid, gid, gid)
        os.setresuid(broker, uid, broker)
        if os.getresuid() != (broker, uid, broker):
            raise OSError("the supervisor uid switch did not take")
    ll.drop_all_caps()  # the session CLI must carry no CAP_SETUID/SETGID (#241)


class _Tree:
    """The CLI process tree one supervisor owns."""

    def __init__(self, cpid):
        self.cpid = cpid
        self.status = None  # the CLI's raw wait status, once reaped

    def reap(self):
        """Reap every exited child; return whether any child remains."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return False
            if pid == 0:
                return True
            if pid == self.cpid:
                self.status = status

    def signal_cli_group(self, sig):
        # Only while the CLI is unreaped: its pid (the group id, setsid) is
        # still allocated then, so the group cannot be a recycled stranger's.
        if self.status is None:
            try:
                os.killpg(self.cpid, sig)
            except ProcessLookupError:  # not yet past its setsid(): no group
                _signal_pid(self.cpid, sig)
            except OSError:
                pass


def _take_relays(buf):
    """Pop the complete relay records off *buf*; return the signals to relay."""
    size = ll.REC.size
    end = len(buf) - len(buf) % size
    sigs = []
    for off in range(0, end, size):
        kind, value = ll.REC.unpack_from(buf, off)
        if kind == ll.REC_RELAY and value in _RELAY_SIGNALS:
            sigs.append(value)
    del buf[:end]
    return sigs


def _supervise(link, tree, wake_r, leftover):
    """Run the session until the CLI has exited, owning the launcher link.

    Relay records forward SIGTERM/SIGINT to the CLI's group. EOF (the launcher
    died, e.g. the SDK's close() escalated to SIGKILL) starts the teardown:
    SIGTERM, then SIGKILL once ``_TEARDOWN_GRACE`` has passed since the CLI's
    first SIGTERM (a relayed one counts — the SDK kills the launcher only after
    its own grace). After EOF the link leaves the poll set, so waiting for the
    CLI blocks on the SIGCHLD wakeup instead of spinning on a readable EOF.
    """
    poller = select.poll()
    poller.register(link.fileno(), select.POLLIN)
    poller.register(wake_r, select.POLLIN)
    pending = bytearray(leftover)
    first_term = None
    kill_at = None
    linked = True

    def relay(now):
        nonlocal first_term
        for sig in _take_relays(pending):
            tree.signal_cli_group(sig)
            if sig == signal.SIGTERM and first_term is None:
                first_term = now

    relay(time.monotonic())
    while True:
        tree.reap()
        if tree.status is not None:
            return
        now = time.monotonic()
        if kill_at is not None and now >= kill_at:
            tree.signal_cli_group(signal.SIGKILL)
            kill_at = None
        timeout = 1.0 if kill_at is None else max(0.0, kill_at - now)
        for fd, _events in poller.poll(timeout * 1000):
            if fd == wake_r:
                _drain(wake_r)
                continue
            try:
                data = link.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                data = b""
            if data:
                pending += data
                relay(now)
            elif linked:  # the launcher is gone: tear the tree down
                linked = False
                poller.unregister(link.fileno())
                if first_term is None:
                    tree.signal_cli_group(signal.SIGTERM)
                    first_term = now
                kill_at = max(now, first_term + _TEARDOWN_GRACE)


def _kill_member(pid):
    """SIGKILL one of our own (unreaped) children and the group it leads."""
    try:
        leads = os.getpgid(pid) == pid
    except OSError:
        leads = False
    _signal_pid(pid, signal.SIGKILL)
    if leads:
        _killpg(pid, signal.SIGKILL)


def _sweep(tree, wake_r):
    """Kill and reap every descendant still alive once the CLI has exited.

    Killing the CLI's group is not enough: CLI 2.1.283 starts each Bash command
    in its own session, and ``cmd &``/``nohup``/``setsid``/double-forked jobs
    re-parent to this subreaper. So: SIGKILL every direct child (and the group
    it leads), reap, and repeat until no child is left — a killed child's own
    children re-parent here and are caught on the next pass. Only our own
    unreaped children are signalled, so no pid can have been recycled; never
    ``kill(-1)``: a named user's other sessions share the uid.
    """
    delay = 0.01
    while tree.reap():
        for pid in _children():
            _kill_member(pid)
        _wait_readable(wake_r, delay)
        _drain(wake_r)
        delay = min(delay * 2, 0.25)


def _wire_status(status):
    if status is not None and os.WIFEXITED(status):
        return ll.ST_EXITED, os.WEXITSTATUS(status)
    if status is not None and os.WIFSIGNALED(status):
        return ll.ST_SIGNALED, os.WTERMSIG(status)
    return ll.ST_SIGNALED, signal.SIGKILL  # never reaped normally: we killed it


def _run_supervisor(link, fds, req, real_cli, leftover, test_same_uid):
    """The supervisor's whole life. Returns its exit code: 0 once the session's
    process tree is gone (or never started), 1 if processes may remain for the
    resident broker's orphan backstop."""
    uid = req["uid"]
    try:
        _become_supervisor(uid, req["gid"], test_same_uid)
        _prctl(_PR_SET_CHILD_SUBREAPER, 1)
        wake_r, wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        signal.signal(signal.SIGCHLD, lambda *_: None)
        signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
        supervisor = os.getpid()
        cpid = os.fork()
    except Exception as exc:
        _log("refusing the spawn for uid %d: %s" % (uid, exc))
        _send(link, ll.ST_REFUSED, 0)
        return 0
    if cpid == 0:
        _exec_cli(req, fds, real_cli, supervisor, test_same_uid)  # never returns
    for fd in fds:
        _close(fd)  # the CLI holds the session's pipes now
    tree = _Tree(cpid)
    try:
        _supervise(link, tree, wake_r, leftover)
    except Exception as exc:
        _log("supervisor error for uid %d: %s; killing the session" % (uid, exc))
        tree.signal_cli_group(signal.SIGKILL)
    try:
        _sweep(tree, wake_r)
    except Exception as exc:
        _log("sweep failed for uid %d: %s" % (uid, exc))
        return 1
    _send(link, *_wire_status(tree.status))
    return 0


# --- the CLI child -----------------------------------------------------------

# Env names the broker always strips before handing the env to the CLI: the
# ticket/socket/secret must never leak into a session.
_ENV_STRIP = (
    "GATEWAY_ISOLATION_TICKET",
    "GATEWAY_ISOLATION_TICKET_SECRET",
    "GATEWAY_ISOLATION_BROKER_SOCKET",
)


def _child_ro_rw(real_cli, workspace, ticket_ro, ticket_rw):
    ro = list(ll.SYSTEM_RO)
    ro.append(os.path.dirname(os.path.realpath(real_cli)))
    ro.extend(ticket_ro)
    # <workspace>/.cli-tmp is NOT a separate rule: it rides beneath the granted
    # workspace, and a rule on that agent-controlled path is a symlink-escape
    # vector (see ll.safe_tmpdir). TMPDIR is set in _child_env to match.
    # /dev/shm stays rw for POSIX semaphores (multiprocessing, joblib); landlock
    # mode shares it across same-uid sessions, here DAC separates it.
    rw = [workspace, "/dev/shm"]
    rw.extend(ticket_rw)
    return ro, rw


def _child_env(env, env_allow, workspace, tmp):
    """The CLI's environment: the launcher-passed env (already secret-scrubbed
    by the gateway), narrowed to the ticket allowlist when one is given, with
    the isolation plumbing stripped, a workspace-local TMPDIR and tool caches,
    and PWD naming the directory the CLI actually runs in."""
    if env_allow:
        allow = set(env_allow)
        out = {k: v for k, v in env.items() if k in allow}
    else:
        out = dict(env)
    for name in _ENV_STRIP:
        out.pop(name, None)
    out.update(ll.session_tmp_env(tmp))
    out["PWD"] = workspace
    return out


def _dup_onto_stdio(fds):
    # Lift the passed fds clear of 0..2 first, so no dup2 can clobber one that
    # has not been placed yet.
    high = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in fds]
    for target, fd in enumerate(high):
        os.dup2(fd, target)
    for fd in high:
        _close(fd)
    for fd in fds:
        if fd > 2:
            _close(fd)


def _finish_switch(uid, gid, test_same_uid):
    """In the CLI child: complete the switch to ``(U, U, U)`` / ``(G, G, G)``.

    Unprivileged: U is already the effective uid (:func:`_become_supervisor`)
    and no capability is left. Returns whether all three ids — and an empty
    group list — are now the session's; the caller must not exec otherwise.
    """
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    return (
        os.getresuid() == (uid, uid, uid)
        and os.getresgid() == (gid, gid, gid)
        and (test_same_uid or not os.getgroups())
    )


def _exec_cli(req, fds, real_cli, supervisor, test_same_uid):
    """The CLI child: finish the uid switch, confine, exec. Never returns."""
    uid, gid, workspace = req["uid"], req["gid"], req["workspace"]
    try:
        signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        if not _finish_switch(uid, gid, test_same_uid):
            _log("the CLI uid switch did not take; refusing to exec")
            os._exit(_EXIT_CREDS)
        os.setsid()  # own session/group: the supervisor signals the whole group
        # After the switch (a credential change can clear it), then checked: if
        # the supervisor is already gone, the death signal would never fire.
        _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != supervisor:
            os._exit(_EXIT_ORPHANED)
    except Exception as exc:
        _log("CLI child setup failed: %s" % exc)
        os._exit(_EXIT_CREDS)
    try:
        os.chdir(workspace)
    except OSError as exc:
        _log("cannot enter the workspace %r: %s" % (workspace, exc))
        os._exit(_EXIT_CHDIR)
    try:
        tmp = ll.safe_tmpdir(workspace)  # a real dir, replacing any symlink
    except OSError as exc:
        _log("cannot create the session tmp dir in %r: %s" % (workspace, exc))
        os._exit(_EXIT_TMPDIR)
    try:
        ro, rw = _child_ro_rw(real_cli, workspace, req["ro"], req["rw"])
        env = _child_env(req["env"], req["env_allow"], workspace, tmp)
        _dup_onto_stdio(fds)
        ll.restore_exec_signals()
    except Exception as exc:
        _log("CLI child setup failed: %s" % exc)
        os._exit(_EXIT_SETUP)
    try:
        ll.build_and_restrict(ro, rw)
    except ll.LandlockError as exc:
        _log(str(exc))
        os._exit(_EXIT_LANDLOCK)
    try:
        os.execve(real_cli, [real_cli, *req["argv"]], env)
    except OSError as exc:
        _log("cannot exec %s: %s" % (real_cli, exc))
    os._exit(127)


# --- the resident broker -----------------------------------------------------


def _switch_fully(uid, gid, test_same_uid):
    if not (test_same_uid and (uid, gid) == (os.getuid(), os.getgid())):
        os.setgroups([])
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)
    ll.drop_all_caps()


def _kill_as(uid, gid, pids, test_same_uid):
    """SIGKILL *pids* from a short-lived child switched to their own uid (the
    broker holds no CAP_KILL); return whether it managed to. Waited for
    synchronously, so none of *pids* can be reaped — and its pid recycled —
    before the signal lands."""
    try:
        helper = os.fork()
    except OSError as exc:
        _log("cannot fork an orphan killer: %s" % exc)
        return False
    if helper == 0:
        code = 1
        try:
            _switch_fully(uid, gid, test_same_uid)
            for pid in pids:
                _signal_pid(pid, signal.SIGKILL)
            code = 0
        finally:
            os._exit(code)
    while True:
        try:
            _pid, status = os.waitpid(helper, 0)
            return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        except InterruptedError:
            continue
        except ChildProcessError:
            return False


class _Pending:
    """One handshake in flight: what the peer has sent so far."""

    __slots__ = ("sock", "fd", "buf", "fds", "deadline")

    def __init__(self, sock, deadline):
        self.sock = sock
        self.fd = sock.fileno()
        self.buf = bytearray()
        self.fds = []
        self.deadline = deadline

    def close(self):
        for fd in self.fds:
            _close(fd)
        self.fds = []
        try:
            self.sock.close()
        except OSError:
            pass

    def forget(self):
        """In a forked child: close our copies without touching the parent's
        objects' state (``detach`` first, so no finalizer closes a reused fd)."""
        for fd in self.fds:
            _close(fd)
        _close(self.sock.detach())


class _Broker:
    """The resident accept loop. Single-threaded (it forks), and it never lets
    one connection — stalled, malformed or hostile — stop it serving others."""

    def __init__(self, listen, secret, real_cli, peers, test_same_uid):
        self.listen = listen
        self.secret = secret
        self.real_cli = real_cli
        self.peers = peers
        self.test_same_uid = test_same_uid
        self.pending = {}  # fd -> _Pending
        self.supervisors = {}  # pid -> (session uid, workspace)
        self.seen = {}  # spent nonce -> its ticket's exp
        self.clock = 0.0  # high-water wall clock for ticket expiry
        self.orphan_check = False
        self.untargetable = set()

    def serve(self):
        while True:
            self._tick()

    def _tick(self):
        poller = select.poll()
        if len(self.pending) < _MAX_PENDING:
            poller.register(self.listen.fileno(), select.POLLIN)
        for fd in self.pending:
            poller.register(fd, select.POLLIN)
        timeout = 1.0
        if self.pending:
            soonest = min(p.deadline for p in self.pending.values())
            timeout = min(timeout, max(0.0, soonest - time.monotonic()))
        for fd, _events in poller.poll(timeout * 1000):
            if fd == self.listen.fileno():
                self._accept()
            elif fd in self.pending:
                self._service(self.pending[fd])
        self._expire()
        self._reap()
        if self.orphan_check:
            self._sweep_orphans()

    def _accept(self):
        try:
            sock, _addr = self.listen.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:  # e.g. EMFILE: back off instead of spinning
            _log("accept failed: %s" % exc)
            time.sleep(0.1)
            return
        # Pin the peer before reading a byte. Root is the entrypoint's
        # readiness probe, not a spawn attempt, so it is not logged.
        peer = _peer_uid(sock)
        if peer not in self.peers:
            if peer != 0:
                _log("refusing a connection from uid %r" % peer)
            sock.close()
            return
        try:
            sock.setblocking(False)
        except OSError:
            sock.close()
            return
        pending = _Pending(sock, time.monotonic() + _HANDSHAKE_TIMEOUT)
        self.pending[pending.fd] = pending

    def _service(self, pending):
        try:
            self._advance(pending)
        except Exception as exc:  # one bad connection never stops the broker
            self._refuse(pending, exc)

    def _advance(self, pending):
        try:
            data, ancdata, flags, _addr = pending.sock.recvmsg(
                65536, _ANC_SPACE, socket.MSG_CMSG_CLOEXEC
            )
        except (BlockingIOError, InterruptedError):
            return
        # Track every received fd before any check can raise, so each refusal
        # path closes them (none may leak into the resident broker).
        for level, kind, cdata in ancdata:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(cdata[: len(cdata) - len(cdata) % fds.itemsize])
                pending.fds.extend(fds)
        if flags & socket.MSG_CTRUNC or len(pending.fds) > 3:
            raise ValueError("more fds than 0/1/2 were passed")
        if not data:
            if not pending.buf and not pending.fds:
                # A connect-then-close probe (the gateway's reachability check).
                self.pending.pop(pending.fd, None)
                pending.close()
                return
            raise ValueError("link closed mid-header")
        pending.buf += data
        if len(pending.buf) < 4:
            return
        (hlen,) = struct.unpack_from("=I", pending.buf)
        if hlen > ll.MAX_HEADER:
            raise ValueError("a %d-byte spawn header exceeds the limit" % hlen)
        if len(pending.buf) < 4 + hlen:
            return
        header = _parse_header(bytes(pending.buf[4 : 4 + hlen]))
        if len(pending.fds) != 3:
            raise ValueError("expected fds 0/1/2, got %d" % len(pending.fds))
        # Verify in the RESIDENT PARENT: the single-use nonce store must be
        # authoritative, and a per-connection fork would each keep its own copy
        # and never see a replay (#241 review).
        self.clock = max(self.clock, time.time())
        payload = verify_ticket(header["ticket"], self.secret, self.seen, self.clock)
        req = _spawn_request(header, payload)
        _authorize(req["uid"], req["gid"], self.test_same_uid)
        self._check_uid_free(req["uid"], req["workspace"])
        self._spawn(pending, req, bytes(pending.buf[4 + hlen :]))

    def _check_uid_free(self, uid, workspace):
        """Refuse a uid still live for a different workspace.

        A uid belongs to one key, and a key to one workspace (a named user's
        dir, or one anonymous ``_tmp_`` run), so a live supervisor holding
        *uid* elsewhere means the gateway reassigned it while that run was
        still alive — the two would share DAC access and ``/dev/shm``
        (#244 review 4). The gateway's lease pool should never ask; this is
        the authoritative backstop, and it also covers an anonymous workspace
        removed before its CLI finished dying. An unswept orphan of a dead
        supervisor holds its uid the same way until the sweep kills it.
        """
        for held_uid, held_ws in self.supervisors.values():
            if held_uid == uid and held_ws != workspace:
                raise ValueError(
                    "uid %d is still live for another workspace" % uid
                )
        if self.orphan_check:
            for pid in _children() - set(self.supervisors):
                ids = _proc_ids(pid)
                if ids is not None and ids[0] == uid:
                    raise ValueError(
                        "uid %d still has an unswept orphan (pid %d)" % (uid, pid)
                    )

    def _spawn(self, pending, req, leftover):
        self.pending.pop(pending.fd, None)
        try:
            pid = os.fork()
        except OSError as exc:
            self._refuse(pending, "fork failed: %s" % exc)
            return
        if pid == 0:
            code = 1
            try:
                for other in self.pending.values():
                    other.forget()
                _close(self.listen.detach())
                _close_fds_except([pending.fd, *pending.fds])
                code = _run_supervisor(
                    pending.sock,
                    pending.fds,
                    req,
                    self.real_cli,
                    leftover,
                    self.test_same_uid,
                )
            except BaseException as exc:
                _log("supervisor crashed: %r" % exc)
            finally:
                os._exit(code)
        self.supervisors[pid] = (req["uid"], req["workspace"])
        pending.close()

    def _refuse(self, pending, reason):
        self.pending.pop(pending.fd, None)
        _log("rejecting spawn: %s" % (reason,))
        _send(pending.sock, ll.ST_REFUSED, 0)
        pending.close()

    def _expire(self):
        now = time.monotonic()
        for pending in [p for p in self.pending.values() if p.deadline <= now]:
            self._refuse(pending, "handshake timed out")

    def _reap(self):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            held = self.supervisors.pop(pid, None)
            uid = held[0] if held is not None else None
            if uid is not None and not (
                os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            ):
                _log(
                    "the supervisor for uid %d ended abnormally (%s); "
                    "sweeping what it left" % (uid, _describe(status))
                )
                self.orphan_check = True

    def _sweep_orphans(self):
        """Backstop for a supervisor that died before its sweep finished: the
        session's survivors re-parent to this subreaper, so kill them as their
        own in-band uid (one short-lived child per uid). Their children then
        re-parent here in turn and are caught on a later tick."""
        targets = {}
        children = _children()
        self.untargetable &= children  # forget reaped pids: they may be reused
        for pid in children - set(self.supervisors) - self.untargetable:
            ids = _proc_ids(pid)
            try:
                if ids is None:
                    raise ValueError("unreadable credentials")
                _authorize(ids[0], ids[1], self.test_same_uid)
            except ValueError as exc:
                _log("cannot sweep orphan pid %d: %s" % (pid, exc))
                self.untargetable.add(pid)
                continue
            targets.setdefault(ids, []).append(pid)
        if not targets:
            self.orphan_check = False
            return
        for (uid, gid), pids in targets.items():
            if not _kill_as(uid, gid, pids, self.test_same_uid):
                _log("could not sweep orphans %r as uid %d" % (pids, uid))
                self.untargetable.update(pids)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    unknown = [a for a in args if a != _TEST_SAME_UID_FLAG]
    if unknown:
        _log("unknown arguments %r; refusing to start" % (unknown,))
        raise SystemExit(64)
    test_same_uid = _TEST_SAME_UID_FLAG in args

    sock_path = os.environ.get(_SOCKET_ENV, "").strip()
    secret = os.environ.get(_SECRET_ENV, "").strip().encode("utf-8")
    real_cli = os.environ.get(_REAL_CLI_ENV, "").strip()
    if not sock_path or not secret or not real_cli:
        _log(
            "need %s, %s and %s; refusing to start"
            % (_SOCKET_ENV, _SECRET_ENV, _REAL_CLI_ENV)
        )
        raise SystemExit(78)
    if not os.path.isabs(sock_path):
        _log("%s=%r is not an absolute path" % (_SOCKET_ENV, sock_path))
        raise SystemExit(78)
    if not (os.path.isfile(real_cli) and os.access(real_cli, os.X_OK)):
        _log("%s=%r is not executable" % (_REAL_CLI_ENV, real_cli))
        raise SystemExit(78)

    # The only legitimate client is a launcher stub running as the gateway's
    # uid (the entrypoint exports it as GATEWAY_ISOLATION_PEER_UID).
    peers = {_int_env("GATEWAY_ISOLATION_PEER_UID", _int_env("APP_UID", 1000))}
    if test_same_uid:
        peers.add(os.geteuid())
        _log("TEST MODE: a ticket naming the broker's own uid runs unswitched")

    _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen.bind(sock_path)
    # Owner + group (the broker runs with the gateway's gid so the gateway-uid
    # launcher can connect); never world — and sessions cannot reach the path
    # anyway (it is outside their Landlock ruleset). (#241 review)
    os.chmod(sock_path, 0o660)
    listen.listen(64)
    listen.setblocking(False)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    _log("listening on %s (real CLI: %s)" % (sock_path, real_cli))
    _Broker(listen, secret, real_cli, peers, test_same_uid).serve()


if __name__ == "__main__":
    main()
