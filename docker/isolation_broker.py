#!/usr/bin/env -S python3 -I
"""Resident spawn broker for uid-per-user isolation (issue #241, PR-C).

This is the one privileged process in the design. The gateway body holds no
capability; the broker holds only ``CAP_SETUID``/``CAP_SETGID`` (locked down by
the entrypoint — non-root uid ideally, else NNP + securebits + a seccomp arg
filter) and does two things as a session's uid. It forks each Claude CLI as
that uid before the Landlock step, so a session gets DAC + signal isolation on
top of the filesystem confinement that ``landlock`` mode already gives. And it
runs the two file operations the gateway's own uid cannot do on what a session
uid owns (its 0600/0700 files): wipe it (op ``cleanup``) and count a
transcript's turns (op ``transcript``; docs/isolation-uid-ownership-design.md
§3.3, §3.6).

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

Flow per op (the gateway itself connects, as its own uid —
``src/backends/claude/broker_ops.py``):

5. The same header, ``{ticket, argv, env}``, with an empty ``argv`` and
   ``env`` and no fds. The resident pins the peer and verifies the ticket
   exactly as for a spawn, then dispatches on the ticket's ``op`` (none or
   ``"spawn"`` is a spawn); each op's fields have a strict schema (an unknown
   one refuses the ticket), every path is absolute and normalized, and the
   uid/gid pass :func:`_authorize`. A cleanup waits while its uid is in use —
   a live session in its workspace, any live session when the uid's IPC
   objects are to be swept or its state expired, an unswept orphan, another
   workspace cleanup of the uid: refused at once, or parked (connection held,
   at most ``wait`` <= 30 seconds and ``_MAX_PARKED`` requests) and run once
   the uid is free. While a workspace cleanup of a uid runs, a spawn of that
   uid is refused.
6. A forked **helper** takes the supervisor's credentials
   (:func:`_become_supervisor`), arms an alarm, does the work and answers with
   one record before exiting: ``(ST_EXITED, 0)`` or ``(ST_EXITED, 1)`` for a
   cleanup (its steps clear, remove, expire and shm — the uid's /dev/shm
   entries, SysV IPC objects and POSIX message queues — in that order; 1 when
   any step failed), ``(ST_TRANSCRIPT, n)`` for a transcript (``n >= 0`` user
   turns, or a negative ``TRANSCRIPT_*`` code), ``(ST_REFUSED, 0)`` when the
   switch failed. The resident tracks and reaps helpers like supervisors.

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
* **An op acts as the session uid, never as the broker.** A helper's file
  access is exactly U's (effective — so filesystem — uid U, real and saved uid
  the broker's, no capability, no supplementary group), so a cleanup removes,
  and a transcript read reads, only what U itself could; U's processes cannot
  signal or ptrace it. The helpers run no session code and are not
  Landlock-confined, so every path is walked from ``/`` one component at a
  time without following a symlink and operated on through directory fds, and
  a directory is only ever chmodded (owner ``rwx`` back) when U owns it. A
  transcript read returns a count, never content. The spawn path refuses any
  ticket whose ``op`` is not ``"spawn"``, so an op ticket can never start a CLI.
* **IPC objects**: the shm step removes only what U owns (the /dev/shm and
  /dev/mqueue entries) or owns or created (SysV objects, through IPC_RMID as
  U). Under the test-only ``--test-same-uid`` flag U is the developer's own
  uid, so then it sweeps ``GATEWAY_ISOLATION_TEST_SHM_DIR`` or nothing — never
  ``/dev/shm``, SysV IPC or ``/dev/mqueue`` — and every path an op touches
  must lie in the temp dir.
"""

import array
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import pwd
import re
import select
import signal
import socket
import stat
import struct
import sys
import tempfile
import time
from json.decoder import scanstring as _scanstring
from pathlib import PurePosixPath
from typing import Iterable, Iterator

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

# The operations a ticket names (``payload["op"]``, as uid_isolation.OP_*); a
# ticket without ``op`` is a spawn. Each op's payload holds exactly these keys.
_OP_SPAWN = "spawn"
_OP_CLEANUP = "cleanup"
_OP_TRANSCRIPT = "transcript"
_TICKET_KEYS = frozenset({"op", "uid", "gid", "nonce", "exp"})
_OP_KEYS = {
    _OP_CLEANUP: _TICKET_KEYS
    | {"workspace", "wait", "clear", "remove", "expire", "shm"},
    _OP_TRANSCRIPT: _TICKET_KEYS | {"path", "cwd"},
}
_EXPIRE_KEYS = frozenset({"older_than", "units", "nested"})
_MAX_WAIT = 30  # seconds a cleanup may wait for its uid to come free
_MIN_EXPIRE_AGE = 3600  # seconds
_MAX_PARKED = 64  # cleanups waiting at once, beside the handshake bound
_PARK_RECHECK = 0.1  # seconds between two looks at a waiting cleanup
_CLEANUP_ALARM = 60  # seconds: a cleanup helper's whole run
_TRANSCRIPT_ALARM = 60  # seconds: a transcript helper's whole run
_EXPIRE_SCAN_CAP = 10000  # entries the age check of one entry looks at
_MAX_DEPTH = 64  # directory fds one walk holds open (deeper ones move up)
_MAX_LOGGED = 20  # failures one cleanup logs one by one
_SHM_DIR = "/dev/shm"
_MQUEUE_DIR = "/dev/mqueue"  # POSIX message queues, when mounted
_SYSVIPC_DIR = "/proc/sysvipc"
# The SysV IPC tables there and each one's id column (their layouts differ).
_SYSVIPC_TABLES = (("shm", "shmid"), ("sem", "semid"), ("msg", "msqid"))
_IPC_RMID = 0
# Test-only: the directory (in the temp dir) the cleanup op's shm step sweeps
# under --test-same-uid, where the session uid is the developer's own (unset:
# the step is skipped). Ignored without the flag.
_TEST_SHM_ENV = "GATEWAY_ISOLATION_TEST_SHM_DIR"

# Exit codes of a CLI child that failed closed before exec.
_EXIT_CHDIR = 113
_EXIT_LANDLOCK = 114
_EXIT_CREDS = 115
_EXIT_ORPHANED = 116
_EXIT_SETUP = 117
_EXIT_TMPDIR = 118  # <workspace>/.cli-tmp cannot be made a dir (unwritable ws)

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


def _ticket_op(payload):
    """The op a verified ticket names: a spawn when it names none."""
    op = payload.get("op", _OP_SPAWN)
    if not isinstance(op, str) or (op != _OP_SPAWN and op not in _OP_KEYS):
        raise ValueError("unknown broker op %r" % (op,))
    return op


def _ticket_ids(payload):
    uid = payload.get("uid")
    gid = payload.get("gid", uid)
    if type(uid) is not int or type(gid) is not int:
        raise ValueError("ticket uid/gid must be integers")
    return uid, gid


def _spawn_request(header, payload):
    """The validated spawn: the verified ticket payload plus the header."""
    if payload.get("op", _OP_SPAWN) != _OP_SPAWN:
        # An op ticket carries the same signature: whatever the connection
        # passed, it must never start a CLI.
        raise ValueError("a %r ticket cannot spawn" % (payload.get("op"),))
    uid, gid = _ticket_ids(payload)
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


def _op_path(value, what):
    """A path an op names: absolute and normalized, so the walk from ``/``
    takes exactly the components the gateway signed — no ``.``/``..``, no
    empty one (``//x``, ``x/``), never ``/`` itself."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or value == "/"
        or "\0" in value
        or os.path.normpath(value) != value
    ):
        raise ValueError("%s %r is not a normalized absolute path" % (what, value))
    try:
        os.fsencode(value)
    except UnicodeError:
        raise ValueError("%s %r is not a path" % (what, value)) from None
    return value


def _op_paths(value, what):
    if not isinstance(value, list):
        raise ValueError("%s must be a list of paths" % what)
    return [_op_path(p, "%s entry" % what) for p in value]


def _expire_request(value):
    if not isinstance(value, dict) or not set(value) <= _EXPIRE_KEYS:
        raise ValueError("cleanup expire holds only older_than, units and nested")
    age = value.get("older_than")
    if type(age) is not int or age < _MIN_EXPIRE_AGE:
        raise ValueError(
            "cleanup expire older_than must be an integer >= %d" % _MIN_EXPIRE_AGE
        )
    return {
        "older_than": age,
        "units": _op_paths(value.get("units", []), "cleanup expire units"),
        "nested": _op_paths(value.get("nested", []), "cleanup expire nested"),
    }


def _op_request(op, payload):
    """The validated cleanup or transcript request: the verified ticket
    payload against its op's strict schema (an unknown key refuses it)."""
    unknown = set(payload) - _OP_KEYS[op]
    if unknown:
        raise ValueError("a %s ticket holds unknown keys %s" % (op, sorted(unknown)))
    uid, gid = _ticket_ids(payload)
    req = {"op": op, "uid": uid, "gid": gid}
    if op == _OP_TRANSCRIPT:
        path = _op_path(payload.get("path"), "transcript path")
        if not path.endswith(".jsonl"):
            raise ValueError("transcript path %r is not a .jsonl file" % path)
        req["path"] = path
        req["cwd"] = _op_path(payload.get("cwd"), "transcript cwd")
        return req
    if "workspace" not in payload:
        raise ValueError('a cleanup ticket names its workspace ("" for none)')
    workspace = payload["workspace"]
    if workspace != "":
        _op_path(workspace, "cleanup workspace")
    wait = payload.get("wait", 0)
    if type(wait) is not int or not 0 <= wait <= _MAX_WAIT:
        raise ValueError("cleanup wait must be an integer in [0, %d]" % _MAX_WAIT)
    shm = payload.get("shm", False)
    if type(shm) is not bool:
        raise ValueError("cleanup shm must be a boolean")
    req.update(
        workspace=workspace,
        wait=wait,
        clear=_op_paths(payload.get("clear", []), "cleanup clear"),
        remove=_op_paths(payload.get("remove", []), "cleanup remove"),
        expire=_expire_request(payload["expire"]) if "expire" in payload else None,
        shm=shm,
    )
    return req


def _in_test_root(path):
    root = os.path.realpath(tempfile.gettempdir()).rstrip("/")
    return path.startswith(root + "/")


def _check_test_paths(req):
    """Under the test-only ``--test-same-uid`` flag an op runs as the
    developer's own uid, so a test's mistake could remove their files: every
    path it touches must lie in the temp dir, where tests keep theirs."""
    if req["op"] == _OP_TRANSCRIPT:
        paths = [req["path"]]
    else:
        paths = req["clear"] + req["remove"]
        if req["expire"] is not None:
            paths += req["expire"]["units"] + req["expire"]["nested"]
    for path in paths:
        if not _in_test_root(path):
            raise ValueError("TEST MODE: %r is outside the temp dir" % path)


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


# The largest id a session may get: (uid_t)-1 = 4294967295 tells setresuid/
# setresgid to leave an id unchanged, so a session "switched" to it would keep
# the broker's. Mirrors uid_isolation._MAX_ID, which refuses such a band at
# gateway startup; this is the check that holds whatever the broker's env says.
_MAX_SESSION_ID = 4294967294


def _authorize(uid, gid, test_same_uid):
    """Refuse a target uid/gid that is reserved or outside the band.

    Defense in depth behind the HMAC ticket: even a validly signed ticket (or
    the gateway itself) may never land a session on root, the gateway's uid/gid
    or the broker's own uid/gid, nor outside ``[UID_MIN, UID_MAX]`` or
    ``[1, _MAX_SESSION_ID]``. The reserved set is mirrored by
    ``uid_isolation.reserved_uids`` (the broker must not import the gateway
    tree). The one exception is the test-only same-uid mode, where a ticket
    naming exactly the broker's own uid and gid runs unswitched.
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
        if not 1 <= val <= _MAX_SESSION_ID:
            raise ValueError(
                "%s %d is not a valid session id [1, %d]"
                % (label, val, _MAX_SESSION_ID)
            )
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
    """``(ro, rw)`` of the broker's own, opened following links, and
    ``(gateway_ro, gateway_rw)``: the ticket's workspace and lists, which the
    gateway resolved, opened following none (``ll.build_and_restrict``)."""
    ro = list(ll.SYSTEM_RO)
    ro.append(os.path.dirname(os.path.realpath(real_cli)))
    # The session shell wrapper the CLI execs for every Bash call (set in
    # _child_env): the file, never its directory.
    ro.append(ll.SESSION_SHELL)
    # <workspace>/.cli-tmp is NOT a separate rule: it rides beneath the granted
    # workspace, and a rule on that agent-controlled path is a symlink-escape
    # vector (see ll.session_tmpdir). TMPDIR is set in _child_env to match.
    # /dev/shm stays rw for POSIX semaphores (multiprocessing, joblib), and
    # every session uid shares it. DAC keeps one session's objects from the
    # others only because the CLI runs with umask 077 (_exec_cli: new objects
    # are 0600) — an agent can still chmod its own open — and what a run left
    # stays until the cleanup op's shm step, before its uid is leased again.
    rw = ["/dev/shm"]
    return ro, rw, list(ticket_ro), [workspace, *ticket_rw]


def _git_config_count(env):
    """The inherited ``GIT_CONFIG_COUNT`` when git would honour it — decimal
    digits with every ``GIT_CONFIG_KEY_<n>``/``GIT_CONFIG_VALUE_<n>`` below it
    set (git dies on a bogus count or a missing pair) — else ``None``."""
    raw = env.get("GIT_CONFIG_COUNT", "")
    if raw == "":
        return 0  # git reads an empty count as none
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 6:
        return None
    count = int(raw)
    for n in range(count):
        if "GIT_CONFIG_KEY_%d" % n not in env or "GIT_CONFIG_VALUE_%d" % n not in env:
            return None
    return count


def _git_safe_directory_env(env, workspace):
    """``GIT_CONFIG_*`` entries that mark the workspace, and every directory
    below it, as ``safe.directory`` (command-line scope, which git honours for
    it). The workspace root belongs to the gateway's uid, not to U, so U's git
    refused a repository there as "dubious ownership". Added after an
    inherited operator count, which still applies; an invalid one is replaced.
    The ``<workspace>/*`` form needs a git that knows leading-path matches
    (2.46+); an older one only matches the workspace itself."""
    count = _git_config_count(env)
    if count is None:
        count = 0
    out = {}
    for n, value in enumerate((workspace, workspace + "/*"), count):
        out["GIT_CONFIG_KEY_%d" % n] = "safe.directory"
        out["GIT_CONFIG_VALUE_%d" % n] = value
    out["GIT_CONFIG_COUNT"] = str(count + 2)
    return out


def _session_user_env(uid):
    """``USER``/``LOGNAME`` naming the session uid when it has a passwd entry
    (getpass and many tools trust them before passwd); none otherwise."""
    try:
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError, ValueError):
        return {}
    return {"USER": name, "LOGNAME": name} if name else {}


def _child_env(env, env_allow, workspace, tmp, uid):
    """The CLI's environment: the launcher-passed env (already secret-scrubbed
    by the gateway), narrowed to the ticket allowlist when one is given, with
    the isolation plumbing stripped, a workspace-local TMPDIR, tool caches and
    git global config, the session shell (as the launcher sets them in
    landlock mode), the workspace as a git ``safe.directory``, ``USER`` and
    ``LOGNAME`` for the session uid, and PWD naming the directory the CLI
    actually runs in."""
    if env_allow:
        allow = set(env_allow)
        out = {k: v for k, v in env.items() if k in allow}
    else:
        out = dict(env)
    for name in _ENV_STRIP:
        out.pop(name, None)
    out.update(ll.session_tmp_env(tmp))
    out.update(ll.session_shell_env(out))
    out.update(_git_safe_directory_env(out, workspace))
    out.update(_session_user_env(uid))
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
        # chdir follows links, the ruleset grants the workspace as named: a
        # workspace planted as a link (to the config dir, the workspace base)
        # would confine the CLI somewhere else than it is granted. The gateway
        # sends it resolved, so the kernel's cwd must be exactly it.
        cwd = os.getcwd()
    except OSError as exc:
        _log("cannot enter the workspace %r: %s" % (workspace, exc))
        os._exit(_EXIT_CHDIR)
    if cwd != workspace:
        _log("the workspace %r resolves to %r; refusing it" % (workspace, cwd))
        os._exit(_EXIT_CHDIR)
    # The ruleset grants the real CLI's dir, which the gateway never sees.
    cli_dir = os.path.dirname(os.path.realpath(real_cli))
    problem = ll.cli_dir_problem(cli_dir, workspace)
    if problem:
        _log("refusing to grant the real CLI's directory %r: %s" % (cli_dir, problem))
        os._exit(_EXIT_LANDLOCK)
    try:
        # Bound to "." — the cwd just checked — and repaired following no link.
        dot = os.open(".", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            tmp = ll.safe_tmpdir(workspace, cwd_fd=dot)
        finally:
            os.close(dot)
    except OSError as exc:
        _log("cannot create the session tmp dir in %r: %s" % (workspace, exc))
        os._exit(_EXIT_TMPDIR)
    try:
        ro, rw, gateway_ro, gateway_rw = _child_ro_rw(
            real_cli, workspace, req["ro"], req["rw"]
        )
        env = _child_env(req["env"], req["env_allow"], workspace, tmp, uid)
        _dup_onto_stdio(fds)
        ll.restore_exec_signals()
    except Exception as exc:
        _log("CLI child setup failed: %s" % exc)
        os._exit(_EXIT_SETUP)
    try:
        ll.build_and_restrict(ro, rw, gateway_ro=gateway_ro, gateway_rw=gateway_rw)
    except ll.LandlockError as exc:
        _log(str(exc))
        os._exit(_EXIT_LANDLOCK)
    # What the CLI and its tools create where no default ACL applies — above
    # all /dev/shm, which every session uid shares — is U's alone (0600/0700
    # instead of 0644/0755). Beneath the workspace's and the config dir's
    # default ACLs the umask is not used, so the gateway's access is unchanged.
    # Last, so the setup above (.cli-tmp, the granted dirs) keeps its modes.
    os.umask(0o077)
    try:
        os.execve(real_cli, [real_cli, *req["argv"]], env)
    except OSError as exc:
        _log("cannot exec %s: %s" % (real_cli, exc))
    os._exit(127)


# --- transcript turns (a copy of src/backends/claude/transcript_turns.py) ----
#
# The broker must not import the gateway tree, so the rule the gateway applies
# to a transcript it may read itself — the counter, the line and value bounds
# and their constants — is copied here verbatim. tests/test_transcript_turns.py keeps the
# two equal (a shared corpus and the functions' source text): change both
# together.

TRANSCRIPT_MISSING = -1
TRANSCRIPT_MISMATCH = -2
TRANSCRIPT_CORRUPT = -3
TRANSCRIPT_UNREADABLE = -4

# Bounds on what a reader takes in. A transcript is session-writable (a session
# uid may replace its own with a sparse file holding one endless "line"), and
# both the gateway and the broker's helper read it into memory: the caller
# refuses a file larger than MAX_TRANSCRIPT_BYTES (UNREADABLE) and iterates
# through :func:`bounded_lines`, which turns a line longer than MAX_LINE_CHARS
# into CORRUPT instead of reading all of it. Parsing a line builds one object
# per JSON value (a dense line of small lists takes some 24 times its size), so
# :func:`count_transcript_turns` refuses a line that may hold more than
# MAX_LINE_VALUES values before parsing it (CORRUPT, :func:`too_many_values`).
# The CLI writes none of these: its long lines are long strings (images, tool
# output), and its tool output is capped well below the value bound.
MAX_TRANSCRIPT_BYTES = 1 << 30
MAX_LINE_CHARS = 64 << 20
MAX_LINE_VALUES = 1 << 20
# What :func:`too_many_values` counts: a string's opening quote, and the
# bracket, brace, comma or colon every other value follows. One character
# class: a regex repeating a group keeps a backtracking record per repetition
# (gigabytes for a line of escapes); strings are skipped by json's own scanner.
_VALUE_CHARS = re.compile(r'["\[{,:]')


def count_transcript_turns(
    lines: Iterable[str], expected_cwd: str, max_values: int = MAX_LINE_VALUES
) -> int:
    """The external user turns in *lines*, or a negative code.

    * :data:`TRANSCRIPT_CORRUPT` — a line that is not a JSON object (a blank
      line included: the CLI writes none), one that may hold more than
      *max_values* JSON values (:func:`too_many_values`, not parsed), or a user
      line whose ``message`` is not an object. Refuse to guess.
    * :data:`TRANSCRIPT_MISMATCH` — a line records a ``cwd`` other than
      *expected_cwd*: the transcript belongs to another workspace that maps to
      the same ``projects/`` name.
    * otherwise the number of ``type == "user"`` lines that are neither
      ``isMeta`` system reminders nor tool results (a non-empty ``content``
      list of only ``tool_result`` blocks): the prompts the gateway sent, one
      per turn.

    *lines* are text lines as a file yields them (trailing newline allowed).
    """
    expected = PurePosixPath(expected_cwd)
    turns = 0
    for raw in lines:
        if too_many_values(raw, max_values):
            return TRANSCRIPT_CORRUPT
        try:
            line = json.loads(raw)
        except (ValueError, RecursionError):  # JSONDecodeError is a ValueError;
            # a line nested deeper than the parser's recursion limit is no
            # transcript line either
            return TRANSCRIPT_CORRUPT
        if not isinstance(line, dict):
            return TRANSCRIPT_CORRUPT
        recorded = line.get("cwd")
        if (
            isinstance(recorded, str)
            and recorded
            and PurePosixPath(recorded) != expected
        ):
            return TRANSCRIPT_MISMATCH
        if line.get("type") != "user" or line.get("isMeta"):
            continue
        message = line.get("message", {})
        if not isinstance(message, dict):
            return TRANSCRIPT_CORRUPT
        content = message.get("content")
        if (
            isinstance(content, list)
            and content
            and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        ):
            continue
        turns += 1
    return turns


def bounded_lines(fh, max_line: int = MAX_LINE_CHARS) -> Iterator[str]:
    """The lines of the text file *fh*, at most *max_line* characters each.

    A longer line is not read past the bound: it is yielded as ``""``, which
    :func:`count_transcript_turns` counts as CORRUPT, and iteration stops.
    Mirrored by ``docker/isolation_broker.py`` like the counter.
    """
    while True:
        line = fh.readline(max_line + 1)
        if not line:
            return
        if len(line) > max_line:
            yield ""
            return
        yield line


def too_many_values(line: str, limit: int = MAX_LINE_VALUES) -> bool:
    """Whether the JSON text *line* may hold more than *limit* values.

    An upper bound, counted without parsing: every value is a string or
    follows a bracket, brace, comma or colon (:data:`_VALUE_CHARS`), and a
    string is skipped with json's own C scanner, so the scan sees the strings
    ``json.loads`` sees, in constant memory. A line holds no more values than
    characters, so only a longer line is scanned, and only until the count
    passes *limit*. Also true for a line with a string ``json.loads`` refuses
    (unterminated, a bad escape, a control character): CORRUPT either way.
    Mirrored by ``docker/isolation_broker.py`` like the counter.
    """
    if len(line) <= limit:
        return False
    count = pos = 0
    while count <= limit:
        match = _VALUE_CHARS.search(line, pos)
        if match is None:
            return False
        count += 1
        pos = match.end()
        if line[pos - 1] == '"':
            try:
                pos = _scanstring(line, pos, True)[1]
            except ValueError:
                return True
    return True


# --- path walking (both ops) -------------------------------------------------
#
# The op helpers run no session code, so they are not Landlock-confined: what
# keeps them on the paths the gateway signed is that every one is reached from
# / one component at a time without following a symlink, and acted on through
# directory fds — a link an agent planted, or swaps in mid-walk, is removed or
# refused, never followed.

_WALK = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
_DIR_RD = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_FILE_RD = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _walk_parent(path):
    """``(fd, name)``: an ``O_PATH`` fd on the parent directory of the
    normalized absolute *path* (:func:`_op_path`) and its last component,
    reached from ``/`` one component at a time without following a symlink.
    Raises ``FileNotFoundError`` for a missing component and
    ``NotADirectoryError`` for one that is a symlink or not a directory."""
    names = path.split("/")[1:]
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in names[:-1]:
            child = os.open(name, _WALK, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd, names[-1]


def _lstat_at(dir_fd, name):
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


# --- the transcript op -------------------------------------------------------


def _read_transcript(path, cwd):
    """The ``transcript`` op's answer for the file *path*, read as U: its user
    turns against the workspace *cwd* (:func:`count_transcript_turns`), or a
    negative code. MISSING: a component or the file is absent, or a directory
    component is not one (a symlink: the walk follows none). UNREADABLE: the
    file is not a regular one (a FIFO, a directory, a symlink), is larger than
    ``MAX_TRANSCRIPT_BYTES``, or cannot be opened or read. CORRUPT also covers
    a file that is not UTF-8 and a line over ``MAX_LINE_CHARS``
    (:func:`bounded_lines`)."""
    try:
        parent, name = _walk_parent(path)
    except (FileNotFoundError, NotADirectoryError):
        return TRANSCRIPT_MISSING
    except OSError:
        return TRANSCRIPT_UNREADABLE
    try:
        fd = os.open(name, _FILE_RD, dir_fd=parent)
    except (FileNotFoundError, NotADirectoryError):
        return TRANSCRIPT_MISSING
    except OSError:
        return TRANSCRIPT_UNREADABLE
    finally:
        os.close(parent)
    lines = None
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_TRANSCRIPT_BYTES:
            lines = os.fdopen(fd, "r", encoding="utf-8", newline=None)
    except (OSError, ValueError):
        pass
    if lines is None:
        _close(fd)
        return TRANSCRIPT_UNREADABLE
    with lines:
        try:
            return count_transcript_turns(bounded_lines(lines), cwd)
        except (UnicodeDecodeError, RecursionError):
            return TRANSCRIPT_CORRUPT
        except (OSError, MemoryError):
            return TRANSCRIPT_UNREADABLE


# --- the cleanup op ----------------------------------------------------------


class _Failures:
    """One cleanup's failures: any one makes its result 1. The first
    ``_MAX_LOGGED`` are logged with their path and OS error (never a file's
    content); the rest are only counted."""

    def __init__(self, uid):
        self.uid = uid
        self.count = 0

    def fail(self, what, exc):
        self.count += 1
        if self.count <= _MAX_LOGGED:
            _log("cleanup for uid %d: %s: %s" % (self.uid, what, exc))

    def summary(self):
        if self.count > _MAX_LOGGED:
            _log(
                "cleanup for uid %d: %d more failures not logged"
                % (self.uid, self.count - _MAX_LOGGED)
            )


def _owned(st):
    return st.st_uid == os.geteuid()  # U, the helper's filesystem uid


def _give_rwx(fd):
    """Give the directory open at *fd* its owner rwx back when U owns it and
    lacks one of them; return whether it changed. Through ``/proc/self/fd``:
    fchmod refuses an ``O_PATH`` fd, and the magic link is this very inode.
    Never anything U does not own."""
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or not _owned(st) or st.st_mode & 0o700 == 0o700:
        return False
    os.chmod("/proc/self/fd/%d" % fd, stat.S_IMODE(st.st_mode) | 0o700)
    return True


def _repaired(dir_fd, op):
    """``op()``, once more after a PermissionError if that gave the directory
    *dir_fd* its owner rwx back (only when U owns it)."""
    try:
        return op()
    except PermissionError:
        if not _give_rwx(dir_fd):
            raise
    return op()


def _unlink_at(dir_fd, name):
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _rmdir_at(dir_fd, name):
    try:
        os.rmdir(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _open_dir(dir_fd, name, st):
    """An ``O_RDONLY`` fd on the directory *name* of *dir_fd*: the very inode
    the ``lstat`` *st* describes. One U owns but may not open first gets its
    owner rwx back, through an ``O_PATH`` fd (which needs no permission)."""
    try:
        fd = os.open(name, _DIR_RD, dir_fd=dir_fd)
    except PermissionError as exc:
        if not _owned(st):
            raise
        path_fd = os.open(name, _WALK, dir_fd=dir_fd)
        try:
            repaired = os.path.samestat(st, os.fstat(path_fd)) and _give_rwx(path_fd)
        finally:
            os.close(path_fd)
        if not repaired:
            raise exc
        fd = os.open(name, _DIR_RD, dir_fd=dir_fd)
    if not os.path.samestat(st, os.fstat(fd)):
        os.close(fd)
        raise OSError(errno.EAGAIN, "it was replaced meanwhile")
    return fd


def _is_empty_dir(dir_fd, name, st):
    try:
        fd = os.open(name, _DIR_RD, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        return os.path.samestat(st, os.fstat(fd)) and not os.listdir(fd)
    except OSError:
        return False
    finally:
        os.close(fd)


class _Frame:
    """A directory being emptied."""

    __slots__ = ("fd", "path", "name", "names")

    def __init__(self, fd, path, name):
        self.fd = fd  # O_RDONLY, on the directory itself
        self.path = path  # for the log only
        self.name = name  # its entry in the frame below (None: the top)
        self.names = None  # its entries still to remove, once listed


def _listed(frame, log):
    try:
        _give_rwx(frame.fd)  # U's own, and about to lose every entry
    except OSError:
        pass  # the listing or the removals below say what that costs
    try:
        return os.listdir(frame.fd)
    except OSError as exc:
        log.fail("cannot list %r" % frame.path, exc)
        return []


def _move_up(top, frame, name, st, serial):
    """Move the directory *name* of *frame* into the *top* directory under a
    fresh name, queued there; False when it cannot move (the caller then
    descends after all, one fd deeper)."""
    try:
        if _owned(st) and st.st_mode & 0o700 != 0o700:
            # Moving a directory rewrites its "..", so U needs write on it.
            path_fd = os.open(name, _WALK, dir_fd=frame.fd)
            try:
                if os.path.samestat(st, os.fstat(path_fd)):
                    _give_rwx(path_fd)
            finally:
                os.close(path_fd)
        while True:
            serial[0] += 1
            fresh = ".isolation-cleanup-%d" % serial[0]
            if _lstat_at(top.fd, fresh) is None:
                break
        os.rename(name, fresh, src_dir_fd=frame.fd, dst_dir_fd=top.fd)
    except OSError:
        return False
    top.names.append(fresh)
    return True


def _empty(top_fd, path, log):
    """Remove everything in the directory open at *top_fd*, keeping it.

    Iterative, following no symlink: an entry is ``lstat``ed, then unlinked,
    or opened as the very directory ``lstat`` saw, emptied and removed in
    turn; U's own directories get their owner rwx back first. At most
    ``_MAX_DEPTH`` directory fds are held at once: a directory below that
    depth moves up into the top one under a fresh name and is emptied from
    there, so a deep chain neither runs out of fds nor fails the cleanup for
    good. Returns whether every entry went."""
    before = log.count
    serial = [0]
    stack = [_Frame(top_fd, path, None)]
    try:
        while stack:
            frame = stack[-1]
            if frame.names is None:
                frame.names = _listed(frame, log)
            if not frame.names:
                stack.pop()
                if frame.name is not None:
                    os.close(frame.fd)
                    try:
                        _rmdir_at(stack[-1].fd, frame.name)
                    except OSError as exc:
                        log.fail("cannot remove %r" % frame.path, exc)
                continue
            name = frame.names.pop()
            child = _Frame(None, "%s/%s" % (frame.path, name), name)
            try:
                st = _lstat_at(frame.fd, name)
                if st is None:
                    continue
                if not stat.S_ISDIR(st.st_mode):
                    _unlink_at(frame.fd, name)
                    continue
                deep = len(stack) > _MAX_DEPTH
                if deep and _move_up(stack[0], frame, name, st, serial):
                    continue
                child.fd = _open_dir(frame.fd, name, st)
            except OSError as exc:
                log.fail("cannot remove %r" % child.path, exc)
                continue
            stack.append(child)
    finally:
        for frame in stack[1:]:
            _close(frame.fd)
    return log.count == before


def _remove_entry(dir_fd, name, path, log, top=False):
    """Remove the entry *name* of *dir_fd* entirely, following no symlink: a
    directory is emptied (:func:`_empty`) and removed, anything else unlinked;
    a missing entry is done. A *dir_fd* that U owns but that lacks the owner
    bits this needs gets them back. *top* (a ``remove`` target): a directory
    left empty whose parent U may not write counts as done — the gateway owns
    that parent and removes it."""
    try:
        st = _repaired(dir_fd, lambda: _lstat_at(dir_fd, name))
        if st is None:
            return
        if not stat.S_ISDIR(st.st_mode):
            _repaired(dir_fd, lambda: _unlink_at(dir_fd, name))
            return
        fd = _open_dir(dir_fd, name, st)
        try:
            emptied = _empty(fd, path, log)
        finally:
            os.close(fd)
        try:
            _repaired(dir_fd, lambda: _rmdir_at(dir_fd, name))
        except PermissionError:
            if not (top and emptied and _is_empty_dir(dir_fd, name, st)):
                raise
    except OSError as exc:
        log.fail("cannot remove %r" % path, exc)


def _target(path, log):
    """``(parent fd, name)`` of a cleanup target (:func:`_walk_parent`), or
    ``None``: a component is missing (nothing to do), or the walk failed — a
    symlink or non-directory component (logged)."""
    try:
        return _walk_parent(path)
    except FileNotFoundError:
        return None
    except NotADirectoryError as exc:
        log.fail("cannot reach %r (a symlink or non-directory on the way)" % path, exc)
        return None
    except OSError as exc:
        log.fail("cannot reach %r" % path, exc)
        return None


def _open_target_dir(path, log):
    """An ``O_RDONLY`` fd on the directory *path* (a ``clear`` or ``expire``
    target), or ``None``: it is missing, or it is not a directory reached
    without a symlink (logged)."""
    found = _target(path, log)
    if found is None:
        return None
    parent, name = found
    try:
        st = _repaired(parent, lambda: _lstat_at(parent, name))
        if st is None:
            return None
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory (or a symlink)")
        return _open_dir(parent, name, st)
    except OSError as exc:
        log.fail("cannot open %r" % path, exc)
        return None
    finally:
        os.close(parent)


def _clear(path, log):
    fd = _open_target_dir(path, log)
    if fd is not None:
        try:
            _empty(fd, path, log)
        finally:
            os.close(fd)


def _remove(path, log):
    found = _target(path, log)
    if found is not None:
        parent, name = found
        try:
            _remove_entry(parent, name, path, log, top=True)
        finally:
            os.close(parent)


def _names(fd, path, log):
    try:
        return os.listdir(fd)
    except OSError as exc:
        log.fail("cannot list %r" % path, exc)
        return []


def _is_old(dir_fd, name, cutoff):
    """Whether the entry *name* and everything below it last changed before
    *cutoff* (``lstat`` mtimes, no symlink followed). Unsure is not old: an
    entry that cannot be read through, or whose walk passes
    ``_EXPIRE_SCAN_CAP`` entries or ``_MAX_DEPTH`` levels, stays."""
    try:
        st = _lstat_at(dir_fd, name)
    except OSError:
        return False
    if st is None or st.st_mtime >= cutoff:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return True
    seen = 1
    fds, todo = [], []
    try:
        fds.append(os.open(name, _DIR_RD, dir_fd=dir_fd))
        todo.append(os.listdir(fds[-1]))
        while todo:
            if not todo[-1]:
                todo.pop()
                os.close(fds.pop())
                continue
            entry = todo[-1].pop()
            seen += 1
            if seen > _EXPIRE_SCAN_CAP:
                return False
            est = os.stat(entry, dir_fd=fds[-1], follow_symlinks=False)
            if est.st_mtime >= cutoff:
                return False
            if stat.S_ISDIR(est.st_mode):
                if len(fds) >= _MAX_DEPTH:
                    return False
                fds.append(os.open(entry, _DIR_RD, dir_fd=fds[-1]))
                todo.append(os.listdir(fds[-1]))
    except OSError:
        return False
    finally:
        for fd in fds:
            _close(fd)
    return True


def _expire_project(dir_fd, name, path, cutoff, log):
    try:
        st = _lstat_at(dir_fd, name)
        if st is None or not stat.S_ISDIR(st.st_mode):
            return  # only directories hold sessions; anything else stays
        fd = _open_dir(dir_fd, name, st)
    except OSError as exc:
        log.fail("cannot open %r" % path, exc)
        return
    try:
        for entry in _names(fd, path, log):
            if entry != "memory" and _is_old(fd, entry, cutoff):
                _remove_entry(fd, entry, "%s/%s" % (path, entry), log)
    finally:
        os.close(fd)
    if st.st_mtime < cutoff:
        try:
            os.rmdir(name, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                log.fail("cannot remove %r" % path, exc)


def _expire_step(spec, log):
    """The ``expire`` step: what has not changed since the cutoff goes.

    ``units`` (``session-env``, ``tasks``): each direct child whose whole
    subtree is older (:func:`_is_old`). ``nested`` (``projects``): in each
    direct child directory, each entry but ``memory`` likewise; then that
    directory itself when it is left empty and was old already (its mtime
    before this sweep), so the empty ``projects/<name>`` a spawn has just made
    — and granted — for a session that has not written yet stays."""
    cutoff = int(time.time()) - spec["older_than"]
    for path in spec["units"]:
        fd = _open_target_dir(path, log)
        if fd is None:
            continue
        try:
            for name in _names(fd, path, log):
                if _is_old(fd, name, cutoff):
                    _remove_entry(fd, name, "%s/%s" % (path, name), log)
        finally:
            os.close(fd)
    for path in spec["nested"]:
        fd = _open_target_dir(path, log)
        if fd is None:
            continue
        try:
            for name in _names(fd, path, log):
                _expire_project(fd, name, "%s/%s" % (path, name), cutoff, log)
        finally:
            os.close(fd)


def _shm_dir(test_same_uid, environ=None):
    """The directory the cleanup op's shm step sweeps, or ``None`` to skip it.

    ``/dev/shm`` — opened following links: a fixed system path no session
    controls — except under the test-only ``--test-same-uid`` flag, where the
    session uid is the developer's own and /dev/shm holds their processes'
    objects: then only ``GATEWAY_ISOLATION_TEST_SHM_DIR``, an absolute path in
    the temp dir, and no sweep at all without one. The variable is ignored
    without the flag."""
    if not test_same_uid:
        return _SHM_DIR
    environ = os.environ if environ is None else environ
    configured = environ.get(_TEST_SHM_ENV, "").strip()
    if os.path.isabs(configured) and _in_test_root(os.path.normpath(configured)):
        return configured
    return None


def _sweep_owned(directory, uid, log, missing_ok=False):
    """Every entry directly in *directory* that U owns goes (a directory with
    all it holds), following no symlink. The directory itself is a fixed
    system path no session controls, opened following links; with
    *missing_ok* an absent one (nothing mounted there) has nothing to sweep."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        if not missing_ok:
            log.fail("cannot open %r" % directory, exc)
        return
    except OSError as exc:
        log.fail("cannot open %r" % directory, exc)
        return
    try:
        for name in _names(fd, directory, log):
            path = "%s/%s" % (directory, name)
            try:
                st = _lstat_at(fd, name)
            except OSError as exc:
                log.fail("cannot stat %r" % path, exc)
                continue
            if st is not None and st.st_uid == uid:
                _remove_entry(fd, name, path, log)
    finally:
        os.close(fd)


def _sweep_shm(shm_dir, uid, log):
    """U's entries directly in *shm_dir* (:func:`_shm_dir`; ``None`` skips)."""
    if shm_dir is None:
        _log(
            "cleanup for uid %d: shm step skipped in test mode (%s is not set)"
            % (uid, _TEST_SHM_ENV)
        )
        return
    _sweep_owned(shm_dir, uid, log)


def _sysvipc_owned(directory, uid):
    """The SysV IPC objects *uid* owns or created, from the ``/proc/sysvipc``
    tables in *directory*: ``(objects, problems)``. *objects* lists ``(kind,
    id)`` — kind ``shm``, ``sem`` or ``msg`` — and *problems* ``(kind,
    error)`` for a table that cannot be read or whose header lacks a needed
    column. Columns are keyed by each table's header line, never by position:
    the three tables differ and kernels add columns. A line that does not fit
    its header is skipped; a missing table (no SysV IPC) holds nothing."""
    objects, problems = [], []
    for kind, id_column in _SYSVIPC_TABLES:
        try:
            with open(os.path.join(directory, kind), "rb") as table:
                lines = table.read().decode("ascii", "replace").splitlines()
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append((kind, exc))
            continue
        columns = lines[0].split() if lines else []
        wanted = (id_column, "uid", "cuid")
        if not set(wanted) <= set(columns):
            problems.append((kind, ValueError("no %s/uid/cuid header" % id_column)))
            continue
        at = [columns.index(name) for name in wanted]
        for line in lines[1:]:
            values = line.split()
            if len(values) != len(columns):
                continue  # not a row of this table
            try:
                ipc_id, owner, creator = (int(values[i]) for i in at)
            except ValueError:
                continue
            if ipc_id >= 0 and uid in (owner, creator):
                objects.append((kind, ipc_id))
    return objects, problems


def _ipc_remove(kind, ipc_id):
    """``IPC_RMID`` on the one SysV object *kind*/*ipc_id*, as the helper's
    effective uid (which the kernel checks against the object's owner and
    creator). Returns False when it is gone already (EINVAL, EIDRM); raises
    ``OSError`` otherwise, e.g. EPERM for an object U neither owns nor made."""
    if kind == "shm":
        rc = _libc.shmctl(ctypes.c_int(ipc_id), ctypes.c_int(_IPC_RMID), None)
    elif kind == "sem":  # semctl is variadic; IPC_RMID reads no fourth argument
        rc = _libc.semctl(
            ctypes.c_int(ipc_id),
            ctypes.c_int(0),
            ctypes.c_int(_IPC_RMID),
            ctypes.c_int(0),
        )
    elif kind == "msg":
        rc = _libc.msgctl(ctypes.c_int(ipc_id), ctypes.c_int(_IPC_RMID), None)
    else:
        raise ValueError("not a SysV IPC kind: %r" % (kind,))
    if rc == 0:
        return True
    err = ctypes.get_errno()
    if err in (errno.EINVAL, errno.EIDRM):
        return False
    raise OSError(err, os.strerror(err))


def _sweep_sysvipc(directory, uid, log):
    """U's SysV shared memory segments, semaphore sets and message queues
    (owner or creator U), removed as U."""
    objects, problems = _sysvipc_owned(directory, uid)
    for kind, exc in problems:
        log.fail("cannot read %s" % os.path.join(directory, kind), exc)
    for kind, ipc_id in objects:
        try:
            _ipc_remove(kind, ipc_id)
        except OSError as exc:
            log.fail("cannot remove SysV %s %d" % (kind, ipc_id), exc)


def _sweep_ipc(uid, test_same_uid, log):
    """The ``shm`` step: the IPC objects a run leaves behind, which outlive it
    and which the next run leased its uid could open — Landlock mediates
    none of SysV IPC. U's entries directly in /dev/shm, its SysV shared memory,
    semaphores and message queues, and its POSIX message queues in
    /dev/mqueue when that is mounted. Under the test-only ``--test-same-uid``
    flag U is the developer's own uid: only a test shm dir is swept
    (:func:`_shm_dir`), and SysV IPC and /dev/mqueue are left alone."""
    _sweep_shm(_shm_dir(test_same_uid), uid, log)
    if test_same_uid:
        _log("cleanup for uid %d: SysV IPC and /dev/mqueue skipped in test mode" % uid)
        return
    _sweep_sysvipc(_SYSVIPC_DIR, uid, log)
    _sweep_owned(_MQUEUE_DIR, uid, log, missing_ok=True)


# --- the op helpers ----------------------------------------------------------


def _run_cleanup(sock, req, test_same_uid):
    """A cleanup helper's whole life, forked by the resident: become U as a
    supervisor does, then clear, remove, expire and sweep the uid's IPC objects
    (each step runs whatever an earlier one hit), and answer ``(ST_EXITED, 0)``
    — or 1 when anything failed. The alarm ends a cleanup that hangs; the
    gateway then sees the link close without a record."""
    uid = req["uid"]
    try:
        _become_supervisor(uid, req["gid"], test_same_uid)
    except Exception as exc:
        _log("refusing the cleanup for uid %d: %s" % (uid, exc))
        _send(sock, ll.ST_REFUSED, 0)
        return 0
    signal.alarm(_CLEANUP_ALARM)
    log = _Failures(uid)
    try:
        for path in req["clear"]:
            _clear(path, log)
        for path in req["remove"]:
            _remove(path, log)
        if req["expire"] is not None:
            _expire_step(req["expire"], log)
        if req["shm"]:
            _sweep_ipc(uid, test_same_uid, log)
    except Exception as exc:  # a definite answer rather than a closed link
        log.fail("the cleanup stopped", repr(exc))
    log.summary()
    _send(sock, ll.ST_EXITED, 1 if log.count else 0)
    return 0


def _run_transcript(sock, req, test_same_uid):
    """A transcript helper's whole life: become U as a supervisor does, read
    the transcript and answer ``(ST_TRANSCRIPT, value)``."""
    uid = req["uid"]
    try:
        _become_supervisor(uid, req["gid"], test_same_uid)
    except Exception as exc:
        _log("refusing the transcript read for uid %d: %s" % (uid, exc))
        _send(sock, ll.ST_REFUSED, 0)
        return 0
    signal.alarm(_TRANSCRIPT_ALARM)
    try:
        value = _read_transcript(req["path"], req["cwd"])
    except Exception as exc:  # a definite answer rather than a closed link
        _log("the transcript read for uid %d stopped: %r" % (uid, exc))
        value = TRANSCRIPT_UNREADABLE
    _send(sock, ll.ST_TRANSCRIPT, value)
    return 0


# --- the resident broker -----------------------------------------------------

_KILLER_TIMEOUT = 2.0  # the orphan killer's whole run: a uid switch and kill()s


def _kill_orphans(uid, gid, pids, test_same_uid):
    """The orphan killer's side of :func:`_kill_as`: SIGKILL uid U's *pids*.

    It takes the supervisor's credentials (:func:`_become_supervisor`), not
    ``(U, U, U)``: the effective uid U is all kill(2) needs to reach U's
    processes, and with the real and saved uid left the broker's, none of them
    can signal it back. A fully switched killer could be SIGSTOPped by the very
    orphans it was sent for on a kernel without Landlock signal scoping
    (ABI < 6), while the single-threaded broker waited on it. The broker itself
    can still SIGKILL it: same real uid, no CAP_KILL needed.
    """
    _become_supervisor(uid, gid, test_same_uid)
    for pid in pids:
        _signal_pid(pid, signal.SIGKILL)


def _reap_killer(helper):
    """Wait for the orphan killer, bounded; return whether it exited 0.

    The resident loop is single-threaded, so an untimed ``waitpid`` on a child
    that never exits would stop it accepting, spawning and reaping for every
    user — and a plain ``waitpid`` never returns for a stopped child.
    ``WUNTRACED`` reports a stop at once; a killer that stops or overruns
    ``_KILLER_TIMEOUT`` is SIGKILLed and reaped instead, and the sweep is
    reported failed.
    """
    end = time.monotonic() + _KILLER_TIMEOUT
    delay = 0.001
    while True:
        try:
            pid, status = os.waitpid(helper, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            return False
        if pid == helper and not os.WIFSTOPPED(status):
            return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        if pid == helper or time.monotonic() >= end:
            break
        time.sleep(delay)
        delay = min(delay * 2, 0.05)
    why = "stopped" if pid == helper else "overran %.1fs" % _KILLER_TIMEOUT
    _log("orphan killer %d %s; killing it" % (helper, why))
    _signal_pid(helper, signal.SIGKILL)
    end = time.monotonic() + _KILLER_TIMEOUT
    while time.monotonic() < end:
        try:
            if os.waitpid(helper, os.WNOHANG)[0] == helper:
                return False
        except ChildProcessError:
            return False
        time.sleep(0.01)
    # SIGKILL is pending: it can start no further kill(); _reap collects it.
    _log("orphan killer %d is not dead yet; leaving it to the reaper" % helper)
    return False


def _kill_as(uid, gid, pids, test_same_uid):
    """SIGKILL *pids* from a short-lived child that takes their uid as its
    effective uid (the broker holds no CAP_KILL); return whether it managed to.
    Waited for before returning (bounded, :func:`_reap_killer`), so none of
    *pids* can be reaped — and its pid recycled — while the child can still
    signal it."""
    try:
        helper = os.fork()
    except OSError as exc:
        _log("cannot fork an orphan killer: %s" % exc)
        return False
    if helper == 0:
        code = 1
        try:
            _kill_orphans(uid, gid, pids, test_same_uid)
            code = 0
        except Exception as exc:
            _log("the orphan killer for uid %d failed: %s" % (uid, exc))
        finally:
            os._exit(code)
    return _reap_killer(helper)


class _Pending:
    """One handshake in flight: what the peer has sent so far."""

    __slots__ = ("sock", "fd", "buf", "fds", "deadline", "what", "req")

    def __init__(self, sock, deadline):
        self.sock = sock
        self.fd = sock.fileno()
        self.buf = bytearray()
        self.fds = []
        self.deadline = deadline  # a parked cleanup's: when it gives up
        self.what = "spawn"  # what the request turned out to be, for the log
        self.req = None  # a parked cleanup's request

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
        self.parked = {}  # fd -> _Pending: cleanups waiting for their uid
        self.supervisors = {}  # pid -> (session uid, workspace)
        self.cleaners = {}  # pid -> (session uid, workspace or "")
        self.readers = {}  # pid -> session uid (transcript helpers)
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
        for fd in self.parked:
            poller.register(fd, 0)  # a hang-up only: it sent all it will send
        timeout = _PARK_RECHECK if self.parked else 1.0
        if self.pending:
            soonest = min(p.deadline for p in self.pending.values())
            timeout = min(timeout, max(0.0, soonest - time.monotonic()))
        for fd, _events in poller.poll(timeout * 1000):
            if fd == self.listen.fileno():
                self._accept()
            elif fd in self.pending:
                self._service(self.pending[fd])
            elif fd in self.parked:
                self._drop_parked(fd)
        self._expire()
        self._reap()
        self._unpark()
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
        leftover = bytes(pending.buf[4 + hlen :])
        # Verify in the RESIDENT PARENT: the single-use nonce store must be
        # authoritative, and a per-connection fork would each keep its own copy
        # and never see a replay (#241 review).
        self.clock = max(self.clock, time.time())
        payload = verify_ticket(header["ticket"], self.secret, self.seen, self.clock)
        op = _ticket_op(payload)
        if op == _OP_SPAWN:
            if len(pending.fds) != 3:
                raise ValueError("expected fds 0/1/2, got %d" % len(pending.fds))
            req = _spawn_request(header, payload)
            _authorize(req["uid"], req["gid"], self.test_same_uid)
            self._reap()  # a helper that has just ended holds its uid no more
            self._check_uid_free(req["uid"], req["workspace"])
            self._spawn(pending, req, leftover)
            return
        pending.what = op
        if pending.fds or header["argv"] or header["env"] or leftover:
            raise ValueError(
                "a %s request passes no fds, argv, env or data after its header" % op
            )
        req = _op_request(op, payload)
        _authorize(req["uid"], req["gid"], self.test_same_uid)
        if self.test_same_uid:
            _check_test_paths(req)
        if op == _OP_CLEANUP:
            self._reap()  # a session or helper that has just ended is gone
            busy = self._cleanup_busy(req)
            if busy is not None:
                if not req["wait"]:
                    raise ValueError(busy)
                self._park(pending, req)
                return
        self._run_op(pending, req)

    def _ours(self):
        """The children that are no orphan: supervisors and op helpers."""
        return set(self.supervisors) | set(self.cleaners) | set(self.readers)

    def _orphan_of(self, uid):
        """The pid of an unswept orphan holding *uid*, or ``None``.

        Only after a supervisor died abnormally (or a sweep gave up on what it
        left: ``untargetable``, a stopped or overrunning killer is not retried)
        can there be one. An op helper is never one: its real uid is the
        broker's (and the developer's same-uid tests would mistake it)."""
        if not (self.orphan_check or self.untargetable):
            return None
        children = _children()
        self.untargetable &= children  # forget reaped pids: they may be reused
        for pid in children - self._ours():
            ids = _proc_ids(pid)
            if ids is not None and ids[0] == uid:
                return pid
        return None

    def _check_uid_free(self, uid, workspace):
        """Refuse a uid still live for a different workspace.

        A uid belongs to one key, and a key to one workspace (a named user's
        dir, or one anonymous ``_tmp_`` run), so a live supervisor holding
        *uid* elsewhere means the gateway reassigned it while that run was
        still alive — the two would share DAC access and ``/dev/shm``
        (#244 review 4). The gateway's lease pool should never ask; this is
        the authoritative backstop, and it also covers an anonymous workspace
        removed before its CLI finished dying. An unswept orphan of a dead
        supervisor holds its uid the same way until the sweep kills it — or
        for as long as it lives once a sweep gave up on it (``untargetable``:
        a stopped or overrunning killer is not retried). So does a running
        workspace cleanup of the uid: it is wiping what a new session would
        start on (the gateway leases the uid again only once that is done).
        """
        for held_uid, held_ws in self.supervisors.values():
            if held_uid == uid and held_ws != workspace:
                raise ValueError(
                    "uid %d is still live for another workspace" % uid
                )
        if any(c_uid == uid and c_ws for c_uid, c_ws in self.cleaners.values()):
            raise ValueError("uid %d is being cleaned up" % uid)
        pid = self._orphan_of(uid)
        if pid is not None:
            raise ValueError(
                "uid %d still has an unswept orphan (pid %d)" % (uid, pid)
            )

    def _cleanup_busy(self, req):
        """Why the cleanup *req* may not run yet, or ``None``: the uid's files
        may still be in use — by a live session in its workspace, or in any
        workspace when the cleanup sweeps the uid's IPC objects (all of its
        sessions share them) or expires its state (a long-lived session's
        ``session-env/<id>`` keeps an old mtime) — by an unswept orphan, or by
        another workspace cleanup of the uid."""
        uid, workspace = req["uid"], req["workspace"]
        expire = req["expire"]
        whole_uid = req["shm"] or bool(expire and (expire["units"] or expire["nested"]))
        for held_uid, held_ws in self.supervisors.values():
            if held_uid == uid and (whole_uid or (workspace and held_ws == workspace)):
                return "uid %d still has a live session" % uid
        if any(c_uid == uid and c_ws for c_uid, c_ws in self.cleaners.values()):
            return "another cleanup of uid %d is running" % uid
        pid = self._orphan_of(uid)
        if pid is not None:
            return "uid %d still has an unswept orphan (pid %d)" % (uid, pid)
        return None

    def _park(self, pending, req):
        """Hold the cleanup *req* until its uid is free or ``wait`` seconds
        have passed (:meth:`_unpark`). Its connection leaves the handshake
        set, so waiting cleanups never hold up a spawn."""
        if len(self.parked) >= _MAX_PARKED:
            raise ValueError("%d cleanups are waiting already" % len(self.parked))
        self.pending.pop(pending.fd, None)
        pending.req = req
        pending.deadline = time.monotonic() + req["wait"]
        self.parked[pending.fd] = pending

    def _unpark(self):
        """Run each waiting cleanup whose uid is free now; refuse the ones
        whose wait is over."""
        now = time.monotonic()
        for pending in list(self.parked.values()):
            try:
                busy = self._cleanup_busy(pending.req)
                if busy is None:
                    self._run_op(pending, pending.req)
                elif now >= pending.deadline:
                    waited = pending.req["wait"]
                    self._refuse(pending, "%s after %ds" % (busy, waited))
            except Exception as exc:  # one bad request never stops the broker
                self._refuse(pending, exc)

    def _drop_parked(self, fd):
        """A waiting cleanup whose client hung up: nobody waits for it now."""
        pending = self.parked.pop(fd)
        _log(
            "dropping a waiting cleanup for uid %d: its client went away"
            % pending.req["uid"]
        )
        pending.close()

    def _forget_connections(self):
        """In a forked child: drop every other connection and the listener
        (fork hygiene, before the child closes all fds it does not keep)."""
        for other in self.pending.values():
            other.forget()
        for other in self.parked.values():
            other.forget()
        _close(self.listen.detach())

    def _run_op(self, pending, req):
        """Fork the helper that runs the verified op *req* as its uid; the
        helper answers on the connection itself."""
        self.pending.pop(pending.fd, None)
        self.parked.pop(pending.fd, None)
        try:
            pid = os.fork()
        except OSError as exc:
            self._refuse(pending, "fork failed: %s" % exc)
            return
        if pid == 0:
            code = 1
            try:
                self._forget_connections()
                _close_fds_except([pending.fd])
                if req["op"] == _OP_CLEANUP:
                    code = _run_cleanup(pending.sock, req, self.test_same_uid)
                else:
                    code = _run_transcript(pending.sock, req, self.test_same_uid)
            except BaseException as exc:
                _log("the %s helper crashed: %r" % (req["op"], exc))
            finally:
                os._exit(code)
        if req["op"] == _OP_CLEANUP:
            self.cleaners[pid] = (req["uid"], req["workspace"])
        else:
            self.readers[pid] = req["uid"]
        pending.close()

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
                self._forget_connections()
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
        self.parked.pop(pending.fd, None)
        _log("rejecting %s: %s" % (pending.what, reason))
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
            if pid in self.cleaners or pid in self.readers:
                if pid in self.cleaners:
                    what, uid = "cleanup", self.cleaners.pop(pid)[0]
                else:
                    what, uid = "transcript", self.readers.pop(pid)
                if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
                    # It forks nothing: no orphan to sweep, only a lost answer
                    # (e.g. its alarm fired: the gateway saw no record).
                    _log(
                        "the %s helper for uid %d ended abnormally (%s)"
                        % (what, uid, _describe(status))
                    )
                continue
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
        session's survivors re-parent to this subreaper, so kill them with
        their own in-band uid as the effective uid (one short-lived killer per
        uid, :func:`_kill_as`). Their children then re-parent here in turn and
        are caught on a later tick."""
        targets = {}
        children = _children()
        self.untargetable &= children  # forget reaped pids: they may be reused
        for pid in children - self._ours() - self.untargetable:
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
        _log(
            "TEST MODE: the cleanup op's shm step sweeps %s"
            % (_shm_dir(True) or "nothing (%s is not set)" % _TEST_SHM_ENV)
        )

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
