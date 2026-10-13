#!/usr/bin/env python3
"""Isolation capability probe for the Claude gateway container.

Run INSIDE the gateway container, as the uid:gid that sessions run as (the
gateway's -- `docker compose exec` attaches as root here unless given `-u`), fed
on stdin so no session-writable path in the container is involved:

    docker compose exec -T -u app gateway python3 -I -S - < scripts/isolation_probe.py

With APP_GID != the app user's group, `-u app` is NOT the sessions' identity;
the probe then prints the exact `-u <uid>:<gid>` to re-run with.

Read-only and non-destructive: every privileged or killable test (unshare,
mount, the landlock_create_ruleset version query AND a full create/add_rule/
restrict_self enforce chain) runs in a short-lived forked child that exits
immediately -- Landlock restrict_self binds only that child, never the probe;
uid-per-user is judged from the bounding set, not a setuid attempt. The parent
only reads /proc, /sys, PATH, the CLI's settings files and the ~/.claude
tree. Sessions can write that tree, so it is read as hostile: a registry or
settings file is stat'ed first and opened (non-blocking, no controlling tty)
only as a small regular file, every walk and scan (shared-asset and code
walks, registry paths, the LISTEN-socket fd scan) is bounded in entries, path
length/depth and time, and names are escaped before they reach the terminal.
Nothing is created, mounted, or changed in the live process.

It answers which per-user isolation mechanism is available in THIS container:
  * uid-per-user (needs CAP_SETUID/SETGID in the bounding set, for the resident
    spawn broker the root entrypoint forks before its drop -- or file caps on
    the broker binary under a non-root start; never held by the gateway
    itself, whose children would inherit them)
  * Landlock filesystem confinement (needs ABI >= 2: kernel >= 5.19 or a distro
    backport, the landlock LSM, and a seccomp profile that allows the syscalls)
  * bubblewrap/nsjail namespaces (needs userns + a fresh procfs in a new PID ns)
plus whether the gateway's environ is readable (the reported finding, F2),
whether the CLI's own OS sandbox can run, and whether a session under its own
uid could still read the shared plugin/skill tree.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import platform
import pwd
import shutil
import signal
import stat
import struct
import sys
import sysconfig
import textwrap
import time

# ---- syscall numbers ---------------------------------------------------------
_MACH = platform.machine()
# unshare() goes through libc's wrapper, which knows this arch's number. Landlock
# has none, but syscalls added since the 5.1 table unification share one number on
# every arch except alpha/ia64/mips (they add an ABI offset), where the probe
# reports Landlock as untested rather than issue the wrong syscall.
_NR_LANDLOCK_CREATE_RULESET = 444
_NR_LANDLOCK_ADD_RULE = 445  # same arch caveat as create_ruleset (see above)
_NR_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_NR_KNOWN = not _MACH.startswith(("alpha", "ia64", "mips"))

CLONE_NEWNS = 0x00020000
CLONE_NEWPID = 0x20000000
CLONE_NEWUSER = 0x10000000
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2  # ABI 1; enough for a minimal ruleset
_PR_SET_NO_NEW_PRIVS = 38

# The interpreter's own symbols include libc's; find_library would spawn ldconfig.
_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long

# PID 1 under compose `init: true` / `docker run --init` (or podman) is a shim and
# the gateway is its child.
_INIT_SHIMS = ("docker-init", "tini", "dumb-init", "catatonit")
# Name fragments that make an env var worth flagging (names only, never values).
_SECRETISH = tuple("KEY TOKEN SECRET PASS PRIVATE AUTH CRED COOKIE DSN URL".split())
# Bounds for the session-writable tree.
_JSON_CAP = 4 << 20  # registry/settings files above this size are skipped
_PATHS_CAP = 500  # absolute paths taken from the registries
_PATH_MAX = 4096  # longer path values are unusable (ENAMETOOLONG): skipped
_SEEDS_CAP = 16  # distinct CLAUDE_CODE_PLUGIN_SEED_DIR entries checked
_SHOWN_CAP = 160  # chars of a session-written value echoed in a row
_OUTSIDE_CAP = 10  # off-root registry paths listed (not walked) in the report
_RESOLVE_TIME_CAP = 5.0  # seconds for resolving the off-root registry paths
_WALK_CAP = 50000  # entries per walk
_EXAMPLES_CAP = 32  # bad-entry examples kept per walk (count stays exact)
_DEPTH_CAP = 256  # path components followed in a symlink target
_TIME_CAP = 60.0  # seconds for all shared-asset walks together
_ROW_TIME_CAP = 15.0  # per-row slice of _TIME_CAP: one tree can't starve the rest
_CODE_TIME_CAP = 10.0  # its own budget: planted shared trees can't starve it
# The LISTEN-socket owner scan readlinks fds of every process this uid can see,
# sessions included: a process holding a million fds must not stall the probe.
_FD_SCAN_CAP = 200000
_FD_SCAN_TIME_CAP = 5.0
_TRUE = ("true", "1", "yes", "on")  # the gateway's boolean spellings
_FALSE = ("false", "0", "no", "off")
# The CLI's managed (policy) settings on Linux: managed-settings.json, then the
# *.json drop-ins of managed-settings.d in name order, later files winning.
_MANAGED_DIR = "/etc/claude-code"
_DROPINS_CAP = 64
# Reading a session-writable path can raise beyond OSError: an embedded NUL or a
# lone surrogate (ValueError) or a long symlink chain (RecursionError, py3.12
# realpath recurses per hop). Fail the one entry, never the whole probe.
_PATH_ERRORS = (OSError, ValueError, RecursionError)
# Landlock failures that relaxing seccomp alone would fix.
_SECCOMP_CAUSES = ("seccomp", "seccomp-kill", "seccomp-old")


def _syscall(nr: int, *args: int) -> tuple[int, int]:
    ctypes.set_errno(0)
    cargs = [ctypes.c_long(nr)] + [ctypes.c_long(a) for a in args]
    res = _libc.syscall(*cargs)
    return res, ctypes.get_errno()


def _unshare(flags: int) -> int:
    """unshare(2) through libc's wrapper; 0 on success, else the errno."""
    ctypes.set_errno(0)
    if _libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno()


def _mount(source: bytes, target: bytes, fstype: bytes) -> int:
    """mount(2) through libc's wrapper; 0 on success, else the errno."""
    ctypes.set_errno(0)
    if _libc.mount(source, target, fstype, 0, None) == 0:
        return 0
    return ctypes.get_errno()


def _safe(text) -> str:
    """`text` with non-printables escaped: names in the session-writable tree can
    carry terminal escape sequences aimed at the operator's screen."""
    s = str(text)
    if s.isprintable():
        return s
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in s)


def _hdr(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _kv(key: str, val) -> None:
    print(f"  {_safe(key):<34} {_safe(val)}")


# ---- run a test in a forked child, return (ok, errno-name) -------------------
_REPORT_FDS: list[int] = []  # report-pipe write ends this (test child) holds


def _in_child(fn) -> tuple[bool, str]:
    r, w = os.pipe()
    try:
        pid = os.fork()
    except OSError as exc:  # e.g. EAGAIN under a pids limit
        os.close(r)
        os.close(w)
        return (False, f"fork:{_errname(exc.errno)}")
    if pid == 0:  # child
        os.close(r)
        # A nested test (t_proc_in_pidns) forks again: the grandchild must not
        # keep the outer report pipe open, or the probe's read-to-EOF would hang
        # on it for as long as the grandchild lives.
        for fd in _REPORT_FDS:
            try:
                os.close(fd)
            except OSError:
                pass
        _REPORT_FDS[:] = [w]
        try:
            ok, en = fn()
            os.write(w, f"{int(ok)} {en}".encode())
        except BaseException as exc:  # noqa: BLE001 - always report, then _exit
            os.write(w, f"0 EXC:{exc}".encode())
        finally:
            os.close(w)
            os._exit(0)
    os.close(w)
    chunks = []
    while chunk := os.read(r, 256):  # to EOF: one read may split a message
        chunks.append(chunk)
    os.close(r)
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:  # SIGCHLD ignored after all: reaped, status lost
        status = None
    out = b"".join(chunks).decode(errors="replace")
    if not out:
        # A seccomp KILL action ends the child before it can report anything.
        if status is not None and os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            try:
                return (False, f"killed:{signal.Signals(sig).name}")
            except ValueError:
                return (False, f"killed:signal {sig}")
        return (False, "died:no report")
    ok_s, _, en = out.partition(" ")
    return (ok_s == "1", en.strip())


def _child_result(ok: bool, detail: str) -> tuple[str, str]:
    """(kind, text): the one reading of an _in_child result every test uses.

    kind is "ok"; "denied" (the test ran and its call failed -- `detail` names
    the step/errno); "seccomp" (killed by SIGSYS: a seccomp KILL action, which
    denies the call); or "unknown" (fork failed, killed by any other signal,
    the test raised, or it died without reporting) -- a test that never gave
    its own answer, so never a denial: it must not make a mechanism or the CLI
    sandbox look broken. `text` is the display form for the last two kinds.
    """
    if ok:
        return "ok", ""
    kind, _, why = detail.partition(":")
    if kind == "killed":
        if why == "SIGSYS":
            return "seccomp", "KILLED (SIGSYS: seccomp kill)"
        return "unknown", f"UNKNOWN (test child killed by {why})"
    if kind == "fork":
        return "unknown", f"UNKNOWN (fork failed: {why}; pids/nproc limit?)"
    if kind == "died":
        return "unknown", "UNKNOWN (test child died without reporting)"
    if kind == "EXC":
        return "unknown", f"UNKNOWN (test child raised: {why})"
    return "denied", ""


def _errname(en: int) -> str:
    return errno.errorcode.get(en, f"errno {en}")


def _kernel_version() -> tuple[int, int] | None:
    rel = platform.release().split("-")[0].split(".")
    try:
        return int(rel[0]), int(rel[1])
    except (IndexError, ValueError):
        return None


# procfs text is read as bytes and decoded leniently: a process name (comm,
# status Name:) or a mount path can hold any bytes, and a UnicodeDecodeError
# must never end the probe before its VERDICT.
def _line(path: str) -> str:
    """First line of a procfs/sysfs file ("" if unreadable)."""
    try:
        with open(path, "rb") as f:
            return f.readline(4096).decode(errors="replace").strip()
    except OSError:
        return ""


def _read_status(pid: str) -> dict:
    """The /proc/<pid>/status fields this probe uses (the file is world-readable)."""
    keys = ("PPid", "Uid", "Gid", "CapEff", "CapPrm", "CapAmb", "CapBnd")
    keys += ("Seccomp", "Seccomp_filters", "NoNewPrivs")
    out: dict = {}
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for raw in f:
                key, _, val = raw.decode(errors="replace").partition(":")
                if key in keys:
                    out[key] = val.split()
    except OSError as exc:
        out["error"] = exc
    return out


def _hidepid() -> str:
    """hidepid= of the /proc mount in effect -- the LAST one, which stacks over
    any earlier -- or ""."""
    found = ""
    try:
        with open("/proc/self/mountinfo", "rb") as f:
            for raw in f:
                line = raw.decode(errors="replace")
                parts = line.split()
                if len(parts) > 4 and parts[4] == "/proc":
                    opts = line.rsplit(" - ", 1)[-1].replace(",", " ").split()
                    found = " ".join(o for o in opts if o.startswith("hidepid="))
    except OSError:
        pass
    return found


def _cmdline(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read(4096)
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace").strip()


def _children(pid: str) -> list[str]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as f:
            kids = f.read().split()
        if kids:
            return kids
    except OSError:
        pass
    return [
        name
        for name in os.listdir("/proc")
        if name.isdigit() and _read_status(name).get("PPid", [""])[0] == pid
    ]


def _port() -> int:
    """The port the gateway listens on ($PORT, else the image default 8000)."""
    raw = (os.environ.get("PORT") or "").strip()
    try:
        return int(raw) if raw else 8000
    except ValueError:
        return 8000


def _listen_inodes(port: int) -> dict[str, str]:
    """Socket inodes in TCP_LISTEN state on `port` (IPv4 + IPv6), each mapped to
    the uid that owns the socket (the tables' uid column)."""
    want = f"{port:04X}"
    inodes: dict[str, str] = {}
    for proc in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc) as f:
                next(f, None)  # header row
                for line in f:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":  # 0A = TCP_LISTEN
                        continue
                    if parts[1].rsplit(":", 1)[-1].upper() == want:
                        inodes[parts[9]] = parts[7]
        except OSError:
            continue
    return inodes


def _listen_owners(inodes: dict[str, str]) -> tuple[list[str], str]:
    """(pids, gap): processes holding one of the LISTEN `inodes` as an open fd,
    and why that list cannot be trusted on its own: "" (it can), "unheld" (some
    LISTEN socket has no readable holder), "unheld-own" (one of those sockets
    belongs to this uid -- a non-dumpable same-uid holder, as the gateway is
    under #232) or "capped" (the scan stopped early).

    Only a /proc/<pid>/fd this uid may read is scanned: another uid's is not,
    and neither is a non-dumpable process's (its fd dir turns root-owned, as
    #232 makes the gateway's). So when some LISTEN socket on the port has no
    readable holder, a holder that WAS found may be a session's own listener
    on the same port (a dev server on [::]:8000 beside the gateway's
    0.0.0.0:8000) rather than the gateway. Sessions share this uid and can hold
    any number of fds, so the scan stops at _FD_SCAN_CAP entries or
    _FD_SCAN_TIME_CAP seconds.
    """
    if not inodes:
        return [], ""
    targets = {f"socket:[{i}]": i for i in inodes}
    pids: list[str] = []
    held: set[str] = set()
    left = _FD_SCAN_CAP
    deadline = time.monotonic() + _FD_SCAN_TIME_CAP
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return [], "unheld"
    for name in names:
        hit = False
        try:
            with os.scandir(f"/proc/{name}/fd") as it:
                for entry in it:
                    left -= 1
                    if left < 0 or time.monotonic() > deadline:
                        return (pids + [name] if hit else pids), "capped"
                    try:
                        inode = targets.get(os.readlink(entry.path))
                    except OSError:
                        continue
                    if inode is not None:
                        held.add(inode)
                        hit = True
        except OSError:
            pass  # unreadable fd dir, or the process exited mid-scan
        if hit:
            pids.append(name)
    unheld = set(inodes) - held
    if not unheld:
        return pids, ""
    me = str(os.geteuid())
    return pids, "unheld-own" if any(inodes[i] == me for i in unheld) else "unheld"


def _gw(pid: str, note: str, rivals=(), unsure: str = "", found=True) -> dict:
    return {
        "pid": pid,
        "note": note,
        "rivals": list(rivals),
        "unsure": unsure,
        "found": found,
    }


def _gateway() -> dict:
    """The gateway process: {"pid", "note", "rivals", "unsure", "found"}.

    Authoritative first: the process that owns the LISTEN socket on $PORT (its
    /proc/<pid>/fd is readable because the probe runs as the gateway's uid) --
    trusted only when every LISTEN socket on the port has a readable holder
    (see _listen_owners). Otherwise (probe run as another uid, a non-dumpable
    gateway, $PORT wrong) it falls back to PID 1 (_gateway_from_pid1), and any
    holder found that is not that pick becomes a rival. `unsure` names why the
    pick is not concluded (a tie, an untrusted holder, a capped scan, or a pick
    whose fds the scan read although a LISTEN socket has no readable holder),
    with the other candidates in `rivals`; `found` is False when nothing was
    identified.
    """
    me = str(os.getpid())
    port = _port()
    owners, gap = _listen_owners(_listen_inodes(port))
    owners = sorted((p for p in owners if p != me), key=int)
    if owners and not gap:
        if len(owners) == 1:
            return _gw(owners[0], f"owns the LISTEN socket on :{port}")
        # Disambiguate master/worker etc. by cmdline before declaring a tie.
        named = [
            p for p in owners if "uvicorn" in _cmdline(p) or "src.main" in _cmdline(p)
        ]
        if len(named) == 1:
            return _gw(named[0], f"owns :{port} (uvicorn among {len(owners)} holders)")
        tie = f"several processes hold :{port}"
        return _gw(owners[0], tie, owners[1:], tie)
    pick = _gateway_from_pid1(me)
    others = [p for p in owners if p != pick["pid"]]
    pick["rivals"] += others
    if pick["unsure"]:
        pass
    elif gap == "capped":
        pick["unsure"] = f"the fd scan for the :{port} holder hit its cap"
    elif others:
        pick["unsure"] = (
            f"a LISTEN socket on :{port} has no readable holder (non-dumpable?),"
            " so the holder found may be a session's own listener"
        )
    elif (
        gap.startswith("unheld")
        and _fd_listable(pick["pid"])
        and (pick["pid"] not in owners or gap == "unheld-own")
    ):
        # The scan read this pick's fds, so had it held the unheld LISTEN socket
        # it would have been found: the gateway may be that socket's unreadable
        # holder -- a hidden PID 1, a shim's non-dumpable child -- and this pick a
        # session listener, a CLI or an orphan. A pick that holds a socket on
        # the port is doubted only when the unheld one is this uid's.
        pick["unsure"] = (
            f"a LISTEN socket on :{port} has no readable holder and PID"
            f" {pick['pid']} does not hold it, so the gateway may be that holder"
        )
    return pick


def _fd_listable(pid: str) -> bool:
    """Whether this uid can list /proc/<pid>/fd (as _listen_owners did)."""
    try:
        with os.scandir(f"/proc/{pid}/fd"):
            return True
    except OSError:
        return False


def _pid1_euid() -> "int | None":
    """PID 1's effective uid: from its status, else from the /proc/1 dir --
    hidepid=noaccess lists it but refuses its files, and the dir keeps the
    task's euid even when non-dumpable. None when hidden (hidepid=invisible)."""
    try:
        return int(_read_status("1")["Uid"][1])
    except (KeyError, IndexError, ValueError):
        pass
    try:
        return os.stat("/proc/1").st_uid
    except OSError:
        return None


def _gateway_from_pid1(me: str) -> dict:
    """_gateway()'s fallback: PID 1 -- unless PID 1 is an init shim (compose
    `init: true`, `--init`), runs as root, or is hidden by hidepid, in which
    case a non-root child of PID 1: cmdline naming uvicorn/src.main first, then
    a python one (under a shim, any). Under a hidden PID 1 only the first kind
    counts: PID 1 may be the gateway itself, hidden because it is non-dumpable,
    and its visible children then are its CLIs or reparented orphans."""
    comm = _line("/proc/1/comm")
    euid = _pid1_euid()  # None: hidden by hidepid (non-dumpable, or another uid)
    shim = comm in _INIT_SHIMS
    if euid not in (None, 0) and not shim:
        return _gw("1", "")
    ranked: dict[int, list[str]] = {}
    for kid in sorted(_children("1"), key=int):
        if kid == me:
            continue  # the probe itself, when PID 1 started it
        try:
            if int(_read_status(kid)["Uid"][1]) == 0:
                continue
        except (KeyError, IndexError, ValueError):
            continue
        cmd = _cmdline(kid)
        if "uvicorn" in cmd or "src.main" in cmd:
            rank = 2
        elif euid is None:
            continue
        elif _line(f"/proc/{kid}/comm").startswith("python"):
            rank = 1
        elif shim:
            rank = 0
        else:
            continue
        ranked.setdefault(rank, []).append(kid)
    who = "PID 1 is hidden"
    if euid is not None:
        who = f"PID 1 is {comm or '?'} (uid {euid})"
    if not ranked:
        if euid is None:
            return _gw("1", f"{who}; no visible child names uvicorn/src.main")
        return _gw("1", f"{who}: gateway not identified", found=False)
    best = ranked[max(ranked)]
    tie = "several children of PID 1 look alike" if best[1:] else ""
    return _gw(best[0], f"{who}; this is its child", best[1:], tie)


def _app_ids() -> tuple[int, int] | None:
    """The uid:gid a root start drops to: APP_UID/APP_GID, blank = 1000, as
    docker/entrypoint.py parses them (None if malformed: it would not start)."""
    ids = []
    for name in ("APP_UID", "APP_GID"):
        raw = os.environ.get(name, "").strip()
        try:
            ids.append(int(raw) if raw else 1000)
        except ValueError:
            return None
    return (ids[0], ids[1]) if min(ids) > 0 else None


def _rerun_ids(ident: dict) -> str:
    """The `-u` value to re-run with: the sessions' ids when known and
    concluded, else what the entrypoint drops to (APP_UID:APP_GID) -- never
    root's, and never the probe's own euid (with APP_UID != 1000 that would
    print the wrong id and loop)."""
    ids = ident.get("session_ids")
    if ids and ids[0] and not ident.get("unsure"):  # known, non-zero, concluded
        return f"{ids[0]}:{ids[1]}"
    app = _app_ids()
    return f"{app[0]}:{app[1]}" if app else "app"


def _rerun_cmd(ident: dict) -> str:
    """The re-run command, fed on stdin like the module docstring's."""
    return (
        f"docker compose exec -T -u {_rerun_ids(ident)} gateway"
        " python3 -I -S - < scripts/isolation_probe.py"
    )


def _quota_mb() -> int:
    """USER_WORKSPACE_QUOTA_MB as an int (0 when unset/blank/invalid). The gateway
    treats a non-negative int here; a positive value force-enables the sandbox."""
    raw = (os.environ.get("USER_WORKSPACE_QUOTA_MB") or "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


# ---- individual probes -------------------------------------------------------
def probe_kernel() -> None:
    _hdr("KERNEL / PLATFORM")
    _kv("uname -r", platform.release())
    _kv("machine", _MACH)
    _kv("/proc/version", _line("/proc/version")[:80])
    ver = _kernel_version()
    if ver is None:
        _kv("mainline ABI-2 floor (>=5.19)", "unknown")
        return
    # Mainline got ABI 2 (LANDLOCK_ACCESS_FS_REFER) in 5.19, but distro kernels
    # backport Landlock (RHEL 9's 5.14 reports ABI 6): the syscall below decides.
    ge = "yes" if ver >= (5, 19) else "no"
    _kv("mainline ABI-2 floor (>=5.19)", f"{ge}  (informational; backports exist)")


def probe_identity() -> dict:
    _hdr("PROCESS IDENTITY")
    euid, egid = os.geteuid(), os.getegid()
    found = _gateway()
    gw = found["pid"]
    ident = {"root": euid == 0, "gw": gw, "gw_found": found["found"]}
    ident.update(rivals=found["rivals"], unsure=found["unsure"])
    ident.update(uid=None, session_ids=None, same_creds=False)
    ident.update(gw_root=False, hidepid=_hidepid(), hidden="")
    # When the gateway is not PID 1 (an init shim, a uvicorn master), PID 1
    # holds the full container env too; probe_proc_hardening checks it (F2).
    ident["pid1_comm"] = _line("/proc/1/comm")
    ident["pid1_uid"] = _pid1_euid()  # a shim's uid tells a root start from not
    _kv("uid / euid", f"{os.getuid()} / {euid}")
    _kv("gid / egid", f"{os.getgid()} / {egid}")
    _kv("running as root", ident["root"])
    shown = f"PID {gw} ({_line(f'/proc/{gw}/comm') or '?'})"
    _kv("gateway process", f"{shown} -- {found['note']}" if found["note"] else shown)
    for rival in ident["rivals"]:
        _kv("!! also a candidate", f"PID {rival}: {_cmdline(rival)[:60]}")
    if ident["unsure"]:
        _kv("!! not concluded", f"{ident['unsure']}: F2 and -u advice not concluded")
    # This probe is only meaningful run AS a session: with the gateway's uid AND
    # gid. Reading another process's /proc/<pid>/environ needs our fsuid/fsgid to
    # equal all of its real/effective/saved uids AND gids, so a uid-only match
    # misleads: with APP_GID != 1000, `-u app` (1000:1000) gets EACCES while
    # sessions (1000:APP_GID, inherited from the gateway) still read it.
    st = _read_status(gw)
    try:
        uids = [int(x) for x in st["Uid"][:3]]
        gids = [int(x) for x in st["Gid"][:3]]
    except (KeyError, ValueError):
        err = st.get("error")
        hidden = isinstance(err, (FileNotFoundError, PermissionError))
        if ident["hidepid"] and hidden and ident["gw_found"]:
            _hidden_gateway(ident, err)
        elif isinstance(err, OSError):
            _kv("gateway uid:gid", f"unreadable ({_errname(err.errno)})")
        else:
            _kv("gateway uid:gid", "unreadable")
        return ident
    ident["uid"] = uids[1]  # effective uid
    ident["session_ids"] = (uids[1], gids[1])
    ident["gw_root"] = uids[1] == 0
    shown = f"{uids[1]}:{gids[1]}"
    if len(set(uids)) > 1 or len(set(gids)) > 1:
        shown += f"  (real/eff/saved uid {uids}, gid {gids})"
    _kv("gateway uid:gid", shown)
    ident["same_creds"] = set(uids) == {euid} and set(gids) == {egid}
    if ident["root"]:
        _kv(
            "!! WARNING",
            f"running as root -- use `{_rerun_cmd(ident)}`; "
            "setuid/F2 results are wrong, so no VERDICT is given",
        )
    elif uids[1] == 0:
        _kv(
            "!! WARNING",
            "the gateway runs as root (or was not found), so the sessions' ids "
            f"are unknown -- re-run with `{_rerun_cmd(ident)}`",
        )
    elif not ident["same_creds"] and not ident["unsure"]:
        _kv(
            "!! WARNING",
            f"this probe is {euid}:{egid} but sessions inherit the gateway's "
            f"{uids[1]}:{gids[1]}, so F2/uid results below are not theirs -- "
            f"re-run with `{_rerun_cmd(ident)}`",
        )
    return ident


def _hidden_gateway(ident: dict, err: OSError) -> None:
    """hidepid hides the gateway's /proc from this uid:gid. It does so for
    another uid -- AND for the same uid when the gateway is non-dumpable (#232):
    hidepid applies the ptrace-read check -- so this IS the sessions' view
    exactly when the probe holds their ids. Those are read off the /proc/<pid>
    dir under hidepid=noaccess (it keeps the task's euid/egid even when
    non-dumpable), else taken from APP_UID:APP_GID (what a root start drops to).
    """
    hp = ident["hidepid"]
    ident["hidden"] = hp
    if isinstance(err, FileNotFoundError):
        how = f"INVISIBLE ({hp})"
    else:  # noaccess: EPERM, not EACCES
        how = f"UNREADABLE ({_errname(err.errno)}, {hp})"
    try:
        st = os.stat(f"/proc/{ident['gw']}")
        ids, src = (st.st_uid, st.st_gid), "its /proc dir"
    except OSError:
        ids, src = _app_ids(), "APP_UID:APP_GID"
    if ids is None:
        _kv("gateway uid:gid", f"{how} to this uid:gid; APP_UID/APP_GID malformed")
        return
    me = (os.geteuid(), os.getegid())
    ident["session_ids"] = ids
    ident["gw_root"] = ids[0] == 0
    ident["same_creds"] = ids == me
    sessions = f"{ids[0]}:{ids[1]} ({src})"
    if ident["same_creds"]:
        _kv("gateway uid:gid", f"{how} to this uid:gid = the sessions' {sessions}")
        _kv("", "so sessions are refused too: hidepid hides a non-dumpable")
        _kv("", "gateway even from its own uid")
    else:
        _kv("gateway uid:gid", f"{how} to this uid:gid; sessions run as {sessions}")
        _kv(
            "!! WARNING", f"not the sessions' view -- re-run with `{_rerun_cmd(ident)}`"
        )


def _view(ident: dict) -> tuple[str, str]:
    """(kind, why): whether this probe sees what the sessions see -- the one
    classification /proc hardening and the verdict both print. Checked in this
    order, the first match winning:

      "unknown" the gateway was not identified, or runs as root -- this wins
                even over a pick that is not concluded
      "unsure"  the gateway pick is not concluded (rivals, untrusted holder)
      "unknown" the gateway's uid:gid could not be read
      "other"   this probe's uid:gid is not the sessions': its F2/uid results
                are not theirs (re-run with _rerun_cmd)
      "ok"      the probe holds the sessions' uid:gid -- also when hidepid
                hides the gateway from it (then F2 is closed for the gateway)

    "unknown" means no verdict can be read off the results.
    """
    unsure = ident.get("unsure")
    if not ident.get("gw_found"):
        return "unknown", "the gateway was not identified" + (
            f"; {unsure}" if unsure else ""
        )
    if ident.get("gw_root"):
        return "unknown", "the gateway runs as root"
    if unsure:
        return "unsure", unsure
    if not ident.get("session_ids"):
        return "unknown", "the gateway's uid:gid could not be read"
    if not ident.get("same_creds"):
        if ident.get("hidden"):
            uid, gid = ident["session_ids"]
            why = f"{ident['hidden']} hides the gateway from this uid:gid"
            return "other", f"{why}, but sessions run as {uid}:{gid}"
        return "other", "this probe's uid:gid differs from the gateway's"
    return "ok", ""


def _decode_caps(hexval: str) -> set[str]:
    bits = int(hexval, 16)
    # Bit numbers per linux/capability.h: CAP_CHOWN is 0, DAC_OVERRIDE is 1.
    names = {
        0: "CHOWN",
        1: "DAC_OVERRIDE",
        2: "DAC_READ_SEARCH",
        3: "FOWNER",
        5: "KILL",
        6: "SETGID",
        7: "SETUID",
        8: "SETPCAP",
        19: "SYS_PTRACE",
        21: "SYS_ADMIN",
    }
    return {n for b, n in names.items() if bits & (1 << b)}


def _lsm_label() -> str:
    """This process's AppArmor/SELinux label ("" when no such LSM is active).

    `docker exec` runs under the container's profile, so this is the sessions'.
    """
    for path in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        label = _line(path).strip("\0 ")
        if label:
            return label
    return ""


def probe_caps_seccomp(ident: dict) -> dict:
    gw = ident["gw"]
    _hdr(f"CAPABILITIES / SECCOMP / LSM (gateway PID {gw}, /proc/{gw}/status)")
    # The gateway's sets are what sessions inherit and what uid-per-user has to
    # work with. The probe's own sets are `docker exec`'s, which never has
    # effective caps as non-root, so reading /proc/self would hide what it holds.
    p1, me = _read_status(gw), _read_status("self")
    if "error" in p1:
        # Bounding/seccomp/NoNewPrivs are container-wide, so ours is the fallback.
        _kv("gateway status", f"unreadable ({p1['error']}): using the probe's own")
        p1 = me
    info: dict = {}
    for key, label in (
        ("CapEff", "effective"),
        ("CapPrm", "permitted"),
        ("CapAmb", "ambient"),
        ("CapBnd", "bounding"),
    ):
        raw = p1.get(key, ["0"])[0]
        info[key] = _decode_caps(raw)
        _kv(f"PID {gw} {key} ({label})", f"{raw}  -> {sorted(info[key]) or 'none'}")
    raw = me.get("CapEff", ["0"])[0]
    _kv("probe's own CapEff", f"{raw}  -> {sorted(_decode_caps(raw)) or 'none'}")
    modes = {"0": "disabled", "1": "strict", "2": "filter"}
    seccomp = p1.get("Seccomp", ["?"])[0]
    _kv(f"Seccomp (PID {gw})", modes.get(seccomp, seccomp))
    _kv("Seccomp_filters", p1.get("Seccomp_filters", ["n/a"])[0])
    info["nnp"] = p1.get("NoNewPrivs", ["?"])[0]
    _kv(f"NoNewPrivs (PID {gw})", info["nnp"])
    label = _lsm_label()
    # Only an enforcing AppArmor profile matters below: Docker's docker-default
    # carries `deny mount`, which no seccomp or capability change lifts.
    info["apparmor"] = label if label.endswith("(enforce)") else ""
    _kv("LSM label (AppArmor/SELinux)", label or "none")
    return info


def _env_names(data: bytes) -> tuple[int, str]:
    """(count, secret-ish names) of an environ blob -- names only, never values."""
    names = sorted(
        {b.split(b"=", 1)[0].decode("latin1") for b in data.split(b"\0") if b}
    )
    hot = [n for n in names if any(s in n.upper() for s in _SECRETISH)]
    more = f" (+{len(hot) - 20} more)" if len(hot) > 20 else ""
    return len(names), f"{hot[:20]}{more}"


def _report_pid1_environ(ident: dict) -> "bool | None":
    """F2 via PID 1 itself, when the gateway is not PID 1: an init shim started
    non-root (compose `user:`) runs docker-init/tini as the session uid and holds
    the full container env, so its environ stays readable to sessions even when
    the gateway's is not (#232 makes only the gateway non-dumpable)."""
    label = f"/proc/1/environ ({ident.get('pid1_comm') or 'PID 1'}, F2)"
    try:
        with open("/proc/1/environ", "rb") as f:
            data = f.read()
    except PermissionError as exc:
        why = "PID 1 runs as root or another uid, or is non-dumpable"
        _kv(label, f"NO ({_errname(exc.errno)}) -- {why}")
        return False
    except FileNotFoundError:
        hp = ident.get("hidepid")
        _kv(label, f"hidden ({hp}) from this uid:gid" if hp else "no such process")
        return None
    except OSError as exc:
        _kv(label, exc)
        return None
    count, hot = _env_names(data)
    _kv(label, f"YES ({count} vars visible) <-- F2 OPEN via PID 1")
    _kv("  secret-ish names in its env", hot)
    return True


def probe_proc_hardening(ident: dict) -> dict:
    _hdr("/proc HARDENING")
    gw = ident["gw"]
    out = {"non_dumpable": None, "pid1_environ": None}
    _kv("/proc hidepid", ident["hidepid"] or "not set (cross-uid /proc visible)")
    # Host-wide link protections. With protected_hardlinks=0 a session can
    # hardlink files it does not own into a tree that root later chowns.
    links = [_line(f"/proc/sys/fs/protected_{k}links") or "?" for k in ("hard", "sym")]
    _kv("fs.protected_hardlinks/symlinks", " / ".join(links))
    # The reported finding: is the gateway's environ readable from here?
    _kv(f"/proc/{gw}/comm (gateway is)", _line(f"/proc/{gw}/comm") or "?")
    # A non-dumpable process (PR_SET_DUMPABLE 0, or a credential change without
    # exec) has every FILE under /proc/<pid> owned by root (the dir keeps the
    # task's uid), so a same-uid session gets EACCES on its environ and mem.
    try:
        owner = os.stat(f"/proc/{gw}/status").st_uid
    except OSError:
        owner = None
    if ident.get("hidden"):
        dumpable = f"unknown -- hidden ({ident['hidden']}) from this uid:gid"
    elif owner is None or not ident.get("uid"):
        dumpable = "unknown (gateway runs as root or is not visible)"
    else:
        out["non_dumpable"] = owner == 0
        dumpable = "yes"
        if out["non_dumpable"]:
            dumpable = "NO (non-dumpable: its /proc files are owned by root)"
    _kv("gateway dumpable", dumpable)
    if gw != "1":
        out["pid1_environ"] = _report_pid1_environ(ident)
    label = f"/proc/{gw}/environ READABLE"
    kind, caveat = _view(ident)  # the verdict prints the same classification
    if kind == "unsure":
        _kv(label, f"NOT CONCLUDED -- {caveat}")
        return out
    if ident.get("hidden"):
        if kind == "ok":
            _kv(label, f"NO -- hidden ({ident['hidden']}) from the sessions' ids:")
            _kv("", "F2 closed for the gateway itself (not for its CLI children)")
        else:
            _kv(label, f"HIDDEN -- not a session's view: {caveat}")
        return out
    try:
        with open(f"/proc/{gw}/environ", "rb") as f:
            data = f.read()  # read to EOF; a real env easily exceeds a 4 KiB page
        count, hot = _env_names(data)
        _kv(label, f"YES ({count} vars visible) <-- finding")
        _kv("  secret-ish names in its env", hot)
    except PermissionError as exc:
        # same_creds first: a non-dumpable same-uid gateway is EACCES for the
        # dumpable reason, never because the ids "differ" (nothing was compared).
        if ident.get("same_creds") and out["non_dumpable"]:
            why = "the gateway is non-dumpable, not uid-isolated"
        elif ident.get("same_creds"):
            why = "blocked for the gateway's own uid:gid"
        else:
            why = f"not a session's view: {caveat}" if caveat else "blocked"
        _kv(label, f"NO ({_errname(exc.errno)}) -- {why}")
        if out["non_dumpable"] and ident.get("same_creds"):
            _kv("  but", "exec'd CLI children are dumpable again: their env")
            _kv("", "stays readable to every other same-uid session")
    except FileNotFoundError:
        _kv(label, "no such process")
    except OSError as exc:
        _kv(label, exc)
    return out


def probe_landlock() -> dict:
    _hdr("LANDLOCK (filesystem confinement, no privilege needed)")
    out = {"abi": 0, "usable": False, "cause": ""}
    try:
        with open("/sys/kernel/security/lsm") as f:
            lsm = f.read().strip()
    except OSError:
        lsm = "(unreadable)"
    _kv("active LSMs (informational)", lsm)
    in_list = "landlock" in lsm if lsm != "(unreadable)" else "unknown"
    _kv("landlock in LSM list", in_list)
    if not _LANDLOCK_NR_KNOWN:
        out["cause"] = "arch"
        _kv("landlock_create_ruleset", f"NOT TRIED (syscall number differs on {_MACH})")
        _kv("=> Landlock usable (ABI>=2)", "unknown")
        return out

    def t_landlock():
        abi, en = _syscall(
            _NR_LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION
        )
        return (abi >= 1, f"{abi} {en}")

    def t_landlock_enforce():
        # The full chain the gateway's confinement needs: create a real ruleset,
        # add a rule, set NoNewPrivs, restrict_self. Runs in a forked child, so
        # the restriction binds only that child; the probe keeps full access.
        attr = ctypes.create_string_buffer(
            struct.pack("=Q", LANDLOCK_ACCESS_FS_READ_FILE)
        )
        rs, en = _syscall(_NR_LANDLOCK_CREATE_RULESET, ctypes.addressof(attr), 8, 0)
        if rs < 0:
            return (False, f"create:{_errname(en)}")
        pfd = -1
        try:
            pfd = os.open("/", os.O_PATH | os.O_CLOEXEC)
            rule = ctypes.create_string_buffer(
                struct.pack("=Qi", LANDLOCK_ACCESS_FS_READ_FILE, pfd)
            )
            rc, en = _syscall(
                _NR_LANDLOCK_ADD_RULE,
                rs,
                LANDLOCK_RULE_PATH_BENEATH,
                ctypes.addressof(rule),
                0,
            )
            if rc != 0:
                return (False, f"add_rule:{_errname(en)}")
            ctypes.set_errno(0)
            if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                return (False, f"nnp:{_errname(ctypes.get_errno())}")
            rc, en = _syscall(_NR_LANDLOCK_RESTRICT_SELF, rs, 0)
            if rc != 0:
                return (False, f"restrict:{_errname(en)}")
            return (True, "0")
        except OSError as exc:
            return (False, f"open:{_errname(exc.errno)}")
        finally:
            if pfd >= 0:
                os.close(pfd)
            os.close(rs)

    # In a child: a seccomp KILL action on the syscall must not take the probe
    # (and its report so far) down with it.
    ok, reply = _in_child(t_landlock)
    kind, text = _child_result(ok, reply)
    try:
        if kind not in ("ok", "denied"):  # no "<abi> <errno>" reply of its own
            raise ValueError(reply)
        abi_s, en_s = reply.split()
        abi, en = int(abi_s), int(en_s)
    except ValueError:
        if kind == "seccomp":
            out["cause"] = "seccomp-kill"
            _kv("landlock_create_ruleset", f"{text} => seccomp kills it")
            _kv("=> Landlock usable (ABI>=2)", False)
        else:
            out["cause"] = "untested"
            _kv("landlock_create_ruleset", text or f"UNKNOWN ({reply})")
            _kv("=> Landlock usable (ABI>=2)", "unknown")
        return out
    # The syscall is authoritative: it returns an ABI version (>=1) only when
    # Landlock is compiled AND active in the running kernel's LSM stack.
    # EOPNOTSUPP = compiled but not in the lsm= list; EPERM = a seccomp filter
    # blocks the syscall. ENOSYS = not compiled -- OR a seccomp profile older than
    # the syscall: runc answers ENOSYS for numbers above the highest one a profile
    # names, and Docker <= 20.10.17's built-in profile predates landlock_*.
    out["abi"] = abi if abi >= 1 else 0
    if abi >= 2:
        _kv("landlock ABI version", f"{abi}  (>=2: cross-dir rename/link confinable)")
        # 444 answering a version is not proof 445/446 run: a seccomp profile can
        # allow the query and block the enforce syscalls. Verify the whole chain.
        ok_enf, d_enf = _in_child(t_landlock_enforce)
        kind, text = _child_result(ok_enf, d_enf)
        step, _, err = d_enf.partition(":")
        if kind == "ok":
            out["usable"] = True
            _kv("landlock enforce (create+add_rule+restrict_self)", "OK")
        elif kind == "seccomp":
            out["cause"] = "seccomp-kill"
            _kv("landlock enforce", f"{text} => 445/446 blocked by seccomp")
        elif kind == "unknown":  # never ran to an answer: cannot conclude
            out["cause"] = "untested"
            _kv("landlock enforce", text)
        elif step in ("create", "add_rule", "restrict") and err in ("EPERM", "ENOSYS"):
            # What a seccomp filter answers once 444's version query passed.
            out["cause"] = "seccomp"
            _kv("landlock enforce", f"FAILED ({d_enf}) => 445/446 blocked by seccomp")
        else:
            out["cause"] = "other"
            _kv("landlock enforce", f"FAILED ({d_enf}) => not usable")
    elif abi == 1:
        # ABI 1 (kernel 5.13-5.18) lacks LANDLOCK_ACCESS_FS_REFER, so ANY ruleset
        # forces cross-directory rename/link to EXDEV: git object finalize,
        # package installs, `mv a/x b/` all break inside sessions.
        out["cause"] = "abi1"
        _kv("landlock ABI version", "1  (no REFER: cross-dir rename/link => EXDEV)")
    elif en == errno.EPERM:
        out["cause"] = "seccomp"
        _kv("landlock_create_ruleset", "FAILED (EPERM) => blocked by seccomp")
    elif en == errno.ENOSYS:
        ver = _kernel_version()
        if "landlock" in lsm.split(","):
            # The kernel runs Landlock, so something in front of it said ENOSYS.
            out["cause"] = "seccomp-old"
            _kv("landlock_create_ruleset", "FAILED (ENOSYS) => blocked by seccomp:")
            _kv("", "the LSM list has landlock, so the profile predates it")
        elif ver is not None and ver < (5, 13):
            out["cause"] = "kernel"
            _kv("landlock_create_ruleset", "FAILED (ENOSYS) => kernel < 5.13")
        else:
            out["cause"] = "kernel-or-seccomp"
            _kv("landlock_create_ruleset", "FAILED (ENOSYS) => kernel without it, OR")
            _kv("", "a seccomp profile older than it (Docker <= 20.10.17)")
    elif en == errno.EOPNOTSUPP:
        out["cause"] = "lsm"
        _kv("landlock_create_ruleset", "FAILED (EOPNOTSUPP) => not in lsm= boot list")
    else:
        out["cause"] = "other"
        _kv("landlock_create_ruleset", f"FAILED ({_errname(en)}) => not usable")
    if abi >= 1:
        # Every ABI denies a sandboxed process ptrace-class access to processes
        # outside its domain -- reading their /proc/<pid>/environ included. ABI 4
        # (6.7) adds TCP bind/connect port rules (loopback between sessions);
        # ABI 6 (6.12) opt-in scopes for signals and abstract unix sockets.
        tcp = "yes (ABI>=4)" if abi >= 4 else f"no (ABI {abi} < 4)"
        _kv("TCP bind/connect port rules", tcp)
        scoped = "yes (ABI>=6)" if abi >= 6 else f"no (ABI {abi} < 6)"
        _kv("signal/abstract-socket scoping", scoped)
    _kv("=> Landlock usable (ABI>=2)", out["usable"])
    return out


def _home() -> str:
    """The CLI's home: $HOME, else the passwd entry (os.homedir())."""
    return os.environ.get("HOME") or _passwd_home()


def _claude_dirs() -> tuple[str, "str | None", str, str]:
    """(home, CLAUDE_CONFIG_DIR or None when unset, config dir, problem) the way
    the CLI finds them: the config dir is CLAUDE_CONFIG_DIR verbatim whenever it
    is set -- an empty value too, which the CLI's `??` keeps (cwd-relative) --
    else HOME/.claude; "" with the `problem` when the set value is unusable."""
    home = _home()
    cdir_env = os.environ.get("CLAUDE_CONFIG_DIR")
    if cdir_env is None:
        return home, None, os.path.join(home, ".claude"), ""
    cdir, problem = _usable_path(cdir_env, verbatim=True)
    return home, cdir_env, cdir, problem


def _setting_sources() -> list[str]:
    """The --setting-sources the gateway passes, parsed like client.py's
    _get_setting_sources: unset, blank or invalid -> project,local (no user)."""
    raw = os.environ.get("CLAUDE_SETTING_SOURCES")
    default = ["project", "local"]
    if raw is None or not raw.strip():
        return default
    sources = [part.strip() for part in raw.split(",") if part.strip()]
    if not sources or any(s not in ("user", "project", "local") for s in sources):
        return default
    return list(dict.fromkeys(sources))


def _setting(layers: list, keys: tuple[str, ...], kind: type) -> tuple:
    """(value, label) from the LAST layer whose nested `keys` hold a `kind` --
    the CLI merges settings key by key, a later layer winning -- else (None, "")."""
    for label, conf in reversed(layers):
        cur = conf
        for key in keys:
            cur = cur.get(key) if isinstance(cur, dict) else None
        if isinstance(cur, kind):
            return cur, label
    return None, ""


def _short(value: str) -> str:
    """A session-written value cut for display (_kv escapes it)."""
    if len(value) <= _SHOWN_CAP:
        return value
    return f"{value[:_SHOWN_CAP]}... ({len(value)} chars)"


def _env_value(value) -> "str | None":
    """A settings env value the way the CLI applies it: a string as-is, a number
    or boolean stringified, anything else -- or a value holding a NUL -- dropped,
    so the variable keeps the lower layer's value."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        value = str(int(value)) if value.is_integer() else repr(value)
    if not isinstance(value, str) or "\x00" in value:
        return None
    return value


def _setting_env(settings: dict, name: str) -> tuple[str, str]:
    """(value, source) of env var `name` as the CLI reads it: the last settings
    layer whose env block sets it to a kept value, else the container env ("")."""
    for label, conf in reversed(settings["env_layers"]):
        env = conf.get("env")
        if isinstance(env, dict) and name in env:
            value = _env_value(env[name])
            if value is not None:
                return value, f"{label} env"
    return os.environ.get(name, ""), ""


def _usable_path(value: str, verbatim: bool = False) -> tuple[str, str]:
    """(path, problem) for a path-valued setting: the absolute path the CLI
    would use, normalized, or "" and why the probe cannot check it. The CLI
    takes the config dir `verbatim`; a plugin cache/seed dir it expands only
    when it is "~" or starts "~/" (never "~user") -- after trimming the whole
    value, which the caller does. Anything left relative resolves against each
    session's own cwd. Settings values are session-writable, so this runs before
    any os.* call sees them: a value over _PATH_MAX or _DEPTH_CAP would stall
    realpath and the walks, and a lone surrogate or a NUL would raise."""
    if not verbatim and (value == "~" or value.startswith("~/")):
        value = _home() + value[1:]
    if not value:
        return "", "blank: resolved against each session's cwd"
    if len(value) > _PATH_MAX:
        return "", f"over {_PATH_MAX} chars"
    if value.count("/") > _DEPTH_CAP:
        return "", f"over {_DEPTH_CAP} components"
    try:
        if "\x00" in value:
            raise ValueError("embedded NUL")
        os.fsencode(value)
    except _PATH_ERRORS:
        return "", "not a valid path"
    if not value.startswith("/"):
        return "", "relative: resolved against each session's cwd"
    return os.path.normpath(value), ""


def _seed_entries(value: str) -> tuple[list[str], bool]:
    """The first _SEEDS_CAP distinct entries of a (trimmed) SEED_DIR list, split
    on ':' with empty entries dropped as the CLI does -- it neither trims an
    entry nor drops a blank one -- and whether entries were left unchecked.
    Scanned piecewise, never in one split: a session-written value may hold
    millions of entries."""
    entries: list[str] = []
    start = 0
    for _ in range(64 * _SEEDS_CAP):
        end = value.find(":", start)
        item = value[start:] if end < 0 else value[start:end]
        if item and item not in entries:
            if len(entries) == _SEEDS_CAP:
                return entries, True
            entries.append(item)
        if end < 0:
            return entries, False
        start = end + 1
    return entries, bool(value[start:].strip(":"))  # only colons left: no entry


def _global_config(home: str, cdir_env: "str | None", cdir: str) -> str:
    """The CLI's global config file: <config dir>/.config.json when it exists
    (legacy), else .claude.json (.claude-custom-oauth.json under
    CLAUDE_CODE_CUSTOM_OAUTH_URL) in CLAUDE_CONFIG_DIR -- here the CLI uses
    `||`, so an empty value falls back -- else in HOME. "" when a non-empty
    CLAUDE_CONFIG_DIR is unusable."""
    if cdir and os.path.exists(os.path.join(cdir, ".config.json")):
        return os.path.join(cdir, ".config.json")
    suffix = "-custom-oauth" if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    if not cdir_env:
        return os.path.join(home, f".claude{suffix}.json")
    return os.path.join(cdir, f".claude{suffix}.json") if cdir else ""


def _settings_summary(conf: dict) -> str:
    parts = []
    env = conf.get("env")
    if isinstance(env, dict):
        parts.append(f"env: {len(env)} keys")
    sandbox = conf.get("sandbox")
    keys = ("enabled", "failIfUnavailable", "enableWeakerNestedSandbox")
    for key in keys + ("enabledPlatforms",) if isinstance(sandbox, dict) else ():
        if key not in sandbox:
            continue
        value = sandbox[key]
        if isinstance(value, list):  # never str() a session-shaped value
            value = ",".join(v for v in value[:8] if isinstance(v, str))[:60]
        elif not isinstance(value, (bool, int, float)):
            value = type(value).__name__
        parts.append(f"sandbox.{key}={value}")
    return ", ".join(parts) or "no env/sandbox keys"


def probe_cli_settings() -> dict:
    """The settings layers the CLI merges over the gateway's env and flags, as
    far as the probe can read them. The CLI copies each layer's `env` block onto
    its process env, so a settings value beats the container env, and merges
    the other keys one by one, the last layer winning: user < project < local <
    the gateway's --settings < managed. The global config (_global_config) adds
    only an env block, before user's -- and it may move the config dir
    (CLAUDE_CONFIG_DIR), under which the CLI then finds user settings and every
    later path; the user layer counts only when the gateway passes "user" in
    --setting-sources. project/local live in each workspace and are written by
    its session, so they are not read here (nor can they set the plugin
    cache/seed dirs or the config dir).
    """
    _hdr("CLI SETTINGS (layers the CLI merges over the gateway's env)")
    home, cdir_env, cdir, cdir_problem = _claude_dirs()
    sources = _setting_sources()
    out: dict = {"env_layers": [], "user": [], "managed": [], "unread": []}
    out["user_conf"] = None
    _kv("gateway --setting-sources", ",".join(sources))

    def layer(label: str, path: str, dest: str, used: bool = True) -> "dict | None":
        notes: list[str] = []
        conf = _load_json(path, notes)
        if notes or (conf is not None and not isinstance(conf, dict)):
            out["unread"].append(label)
            _kv(label, f"!! {notes[0] if notes else path + ': not a JSON object'}")
            return None
        if conf is None:
            _kv(label, f"{path}: absent")
            return None
        state = _settings_summary(conf)
        if not used:
            _kv(label, f"{path}: {state} -- not a gateway setting source: unused")
            return conf
        _kv(label, f"{path}: {state}")
        out["env_layers"].append((label, conf))
        if dest:
            out[dest].append((label, conf))
        return conf

    def config_dir() -> tuple[str, str, str, str]:
        """(dir, value, source, problem): CLAUDE_CONFIG_DIR as the env blocks
        applied so far leave it -- an env-block HOME does not move it."""
        value, src = _setting_env(out, "CLAUDE_CONFIG_DIR")
        if not src:
            return cdir, cdir_env or "", "", cdir_problem
        path, problem = _usable_path(value, verbatim=True)
        return path, value, src, problem

    gconf = _global_config(home, cdir_env, cdir)
    if gconf:
        layer("global config", gconf, "")
    else:
        out["unread"].append("global config")
        _kv("global config", "!! not read: CLAUDE_CONFIG_DIR is unusable")
    now, _, src, problem = config_dir()
    if now:
        user = os.path.join(now, "settings.json")
        out["user_conf"] = layer("user settings", user, "user", "user" in sources)
    else:
        out["unread"].append("user settings")
        who = src or "the container env"
        _kv("user settings", f"!! not read: {who} sets an unusable config dir")
    managed = os.path.join(_MANAGED_DIR, "managed-settings.json")
    layer("managed settings", managed, "managed")
    dropins = os.path.join(_MANAGED_DIR, "managed-settings.d")
    names: list[str] = []
    try:
        with os.scandir(dropins) as it:
            for n, entry in enumerate(it):
                if n >= 10 * _DROPINS_CAP:
                    names.append("")  # marks the scan as cut short
                    break
                name = entry.name
                if name.endswith(".json") and not name.startswith("."):
                    if entry.is_symlink() or entry.is_file():
                        names.append(name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        out["unread"].append("managed-settings.d")
        _kv("managed-settings.d", f"!! {dropins}: {_errname(exc.errno)}: not read")
    if "" in names:
        out["unread"].append("managed-settings.d")
        many = f"over {10 * _DROPINS_CAP} entries"
        _kv("managed-settings.d", f"!! {many}: scanned no further, the rest not read")
    elif len(names) > _DROPINS_CAP:
        out["unread"].append("managed-settings.d")
        _kv("managed-settings.d", f"!! over {_DROPINS_CAP} drop-ins: the rest not read")
    for name in sorted(n for n in names if n)[:_DROPINS_CAP]:
        label = f"managed-settings.d/{name}"
        layer(label, os.path.join(dropins, name), "managed")
    # The config dir every later CLI path hangs off: the last layer's
    # CLAUDE_CONFIG_DIR, else the container's.
    now, value, src, problem = config_dir()
    out.update(cdir=now, cdir_value=value, cdir_src=src, cdir_problem=problem)
    if src:
        _kv("config dir moved", _env_row(value, src, "", problem))
    return out


def _cli_platform() -> str:
    """The platform name the CLI gives this Linux host: "wsl" when WSL_DISTRO_NAME
    or WSL_INTEROP is set or the kernel version says microsoft/wsl, else "linux"."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return "wsl"
    kernel = _line("/proc/version").lower()
    return "wsl" if "microsoft" in kernel or "wsl" in kernel else "linux"


def _gateway_sandbox() -> tuple["bool | None", str]:
    """The sandbox.enabled the gateway puts in its --settings (None: none), and
    why -- client.py _configure_sandbox: CLAUDE_SANDBOX_ENABLED (lowercased, not
    stripped, by constants.py) decides; unset or invalid, the workspace sandbox
    or a quota force it on; otherwise options.sandbox stays unset."""
    raw = os.environ.get("CLAUDE_SANDBOX_ENABLED")
    if raw is not None and raw.lower() in _FALSE:
        return False, "CLAUDE_SANDBOX_ENABLED=false"
    if raw is not None and raw.lower() in _TRUE:
        return True, "CLAUDE_SANDBOX_ENABLED"
    if os.environ.get("WORKSPACE_SANDBOX_ENABLED", "").strip().lower() in _TRUE:
        return True, "WORKSPACE_SANDBOX_ENABLED"
    if _quota_mb() > 0:
        return True, "USER_WORKSPACE_QUOTA_MB>0"
    return None, ""


def probe_namespaces(caps: dict, settings: dict) -> dict:
    _hdr("NAMESPACES (bubblewrap / nsjail viability)")
    out: dict = {"untested": []}

    def t_userns():
        en = _unshare(CLONE_NEWUSER)
        return (en == 0, _errname(en))

    def t_pidns():
        # NEWUSER first grants CAP in the new userns, then NEWPID.
        en1 = _unshare(CLONE_NEWUSER)
        if en1 != 0:
            return (False, f"userns:{_errname(en1)}")
        en2 = _unshare(CLONE_NEWPID)
        return (en2 == 0, _errname(en2))

    def t_mountns():
        en1 = _unshare(CLONE_NEWUSER | CLONE_NEWNS)
        if en1 != 0:
            return (False, f"unshare:{_errname(en1)}")
        # Try a tmpfs mount inside the fresh (userns-owned) mount ns.
        en = _mount(b"none", b"/tmp", b"tmpfs")
        return (en == 0, _errname(en))

    def t_proc_in_pidns():
        # ②'s F2 fix needs a FRESH procfs in a new PID ns. A tmpfs mount
        # succeeding is NOT the same test: Docker masks parts of /proc, which can
        # make `mount -t proc` fail in a userns where tmpfs works, so test it
        # directly. The next fork is PID 1 of the new PID ns: mount proc there.
        en1 = _unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID)
        if en1 != 0:
            return (False, f"unshare:{_errname(en1)}")

        def t_mount_proc():
            en = _mount(b"proc", b"/proc", b"proc")
            return (en == 0, _errname(en))

        return _in_child(t_mount_proc)

    apparmor = caps.get("apparmor", "")

    def show(key: str, label: str, ok: bool, detail: str) -> None:
        kind, text = _child_result(ok, detail)
        if kind == "unknown":  # never ran: UNKNOWN, not DENIED
            out["untested"].append(key)
        if text:  # a SIGSYS kill counts as denied (bwrap would be killed too)
            _kv(label, text)
            return
        hint = ""
        if not ok and detail.endswith("EACCES") and apparmor:
            hint = f"  <- AppArmor {apparmor}: `deny mount`?"
        _kv(label, "OK" if ok else f"DENIED ({detail}){hint}")

    ok_u, d_u = _in_child(t_userns)
    show("userns", "unshare(CLONE_NEWUSER)", ok_u, d_u)
    ok_p, d_p = _in_child(t_pidns)
    show("pidns", "+ CLONE_NEWPID", ok_p, d_p)
    ok_m, d_m = _in_child(t_mountns)
    show("mountns", "+ CLONE_NEWNS + mount tmpfs", ok_m, d_m)
    ok_pm, d_pm = _in_child(t_proc_in_pidns)
    show("procns", "+ mount proc in new PID ns", ok_pm, d_pm)
    out.update(userns=ok_u, pidns=ok_p, mountns=ok_m, procns=ok_pm)
    # ② needs userns + a new PID ns + a FRESH procfs in it (the actual F2 fix).
    out["viable"] = ok_u and ok_p and ok_m and ok_pm
    _kv("=> bwrap/nsjail viable (userns+pidns+proc)", out["viable"])
    # The CLI's own OS sandbox (sandbox.enabled -- the gateway turns it on with
    # the workspace sandbox, USER_WORKSPACE_QUOTA_MB>0, or CLAUDE_SANDBOX_ENABLED)
    # checks only that bwrap and socat are on PATH (ripgrep: bundled): missing
    # one, it prints "Sandbox disabled" and runs Bash unconfined -- or, with
    # sandbox.failIfUnavailable, exits 1. Otherwise every Bash call runs under
    # `bwrap --unshare-user --unshare-pid` plus a fresh `--proc /proc` -- a bind
    # of this /proc instead under sandbox.enableWeakerNestedSandbox
    # (CLAUDE_SANDBOX_WEAKER_NESTED) -- so a denied namespace or proc mount makes
    # every sandboxed Bash call fail. All of it is measured here, outside any
    # Landlock domain: inside one, bwrap cannot start at all (see the verdict).
    bins = {b: shutil.which(b) for b in ("bwrap", "socat")}
    for name, path in bins.items():
        _kv(f"{name} on PATH", path or "NO")
    out["bwrap"] = bool(bins["bwrap"])
    out["apparmor"] = apparmor
    # The CLI decides from its merged settings (probe_cli_settings): the
    # gateway's --settings beat user settings, managed settings beat both.
    gw_on, trigger = _gateway_sandbox()
    flag: dict = {}
    if gw_on is not None:
        flag["enabled"] = gw_on
    if gw_on:  # the gateway passes its knob only alongside enabled=true
        knob = os.environ.get("CLAUDE_SANDBOX_WEAKER_NESTED", "").lower() in _TRUE
        flag["enableWeakerNestedSandbox"] = knob
    gateway = (f"the gateway via {trigger}", {"sandbox": flag})
    layers = settings["user"] + [gateway] + settings["managed"]
    enabled, by = _setting(layers, ("sandbox", "enabled"), bool)
    fail, fail_by = _setting(layers, ("sandbox", "failIfUnavailable"), bool)
    weaker, _ = _setting(layers, ("sandbox", "enableWeakerNestedSandbox"), bool)
    # Managed settings alone can limit the sandbox to listed platforms (lists
    # merge across the managed files), named the way the CLI names this host.
    platforms = None
    for _, conf in settings["managed"]:
        box = conf.get("sandbox")
        value = box.get("enabledPlatforms") if isinstance(box, dict) else None
        if isinstance(value, list):
            platforms = (platforms or []) + [v for v in value if isinstance(v, str)]
    if enabled and platforms is not None and _cli_platform() not in platforms:
        enabled, by = False, f"managed sandbox.enabledPlatforms ({_cli_platform()})"
    missing = [b for b, p in bins.items() if not p]
    out.update(cli_missing=missing, cli_by=by, cli_fail_by=fail_by)
    need = ["userns", "pidns", "mountns"] + ([] if weaker else ["procns"])
    untested = out["untested"]
    # A test that never ran is UNKNOWN, not DENIED: never report the sandbox
    # BROKEN about a namespace that never actually ran.
    out["cli_denied"] = [k for k in need if not out.get(k) and k not in untested]
    out["cli_untested"] = [k for k in need if k in untested]
    if enabled is False:
        out["cli_sandbox"] = "off"
        state = f"n/a (turned off by {by})"
    elif not enabled:
        out["cli_sandbox"] = "unset"
        state = (
            "not enabled by the gateway, user or managed settings -> a "
            "workspace's project/local settings decide, default off"
        )
    elif missing and fail:
        out["cli_sandbox"] = "fail"
        state = (
            f"NO -- {', '.join(missing)} missing and sandbox.failIfUnavailable "
            f"({fail_by}): the CLI exits 1"
        )
    elif missing:
        out["cli_sandbox"] = "inert"
        state = f"NO -- {', '.join(missing)} missing: Bash runs unconfined"
    elif out["cli_denied"]:
        out["cli_sandbox"] = "broken"
        denied = ", ".join(out["cli_denied"])
        state = f"NO -- {denied} denied: every sandboxed Bash call fails"
    elif out["cli_untested"]:
        out["cli_sandbox"] = "unknown"
        unk = ", ".join(out["cli_untested"])
        state = f"UNKNOWN -- {unk} untested (the test never ran): cannot conclude"
    else:
        out["cli_sandbox"] = "ok"
        state = "yes"
    _kv("=> CLI OS sandbox can run", state)
    if enabled:
        _kv("", f"(enabled by {by})")
    elif enabled is None:
        _kv(
            "",
            "(gateway enables it via WORKSPACE_SANDBOX_ENABLED, "
            "USER_WORKSPACE_QUOTA_MB>0, or CLAUDE_SANDBOX_ENABLED)",
        )
    if settings["unread"]:
        _kv("", f"(unverified: {', '.join(settings['unread'])} not read)")
    if weaker and out["cli_sandbox"] not in ("off", "unset"):
        _kv("", "(weaker nested: sandboxed Bash still sees this /proc)")
    unsandboxed = os.environ.get("CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", "").lower()
    if out["cli_sandbox"] == "broken" and unsandboxed in _TRUE:
        _kv("", "(CLAUDE_SANDBOX_ALLOW_UNSANDBOXED: the model may retry unsandboxed)")
    return out


def probe_setuid(caps: dict, ident: dict) -> dict:
    _hdr("UID SWITCHING (uid-per-user viability)")
    want = {"SETUID", "SETGID"}
    # The bounding set caps every process here, the root entrypoint included. The
    # spawn broker the entrypoint forks before its root->app drop keeps the caps
    # (under a non-root start, file caps on the broker binary need them in the
    # bounding set AND NoNewPrivs=0 at container start). The gateway itself must
    # hold none: ambient/KEEPCAPS caps are inherited by everything it execs
    # (plugin installs, MCP tests, the bundled-CLI fallback).
    out = {"grantable": want <= caps["CapBnd"], "nnp": caps.get("nnp") == "1"}
    # docker exec inherits the runtime's no-new-privileges, so NoNewPrivs on the
    # probe itself means it was set from container start, not by the gateway.
    out["nnp_start"] = _read_status("self").get("NoNewPrivs", ["?"])[0] == "1"
    out["gw_ambient"] = sorted(want & caps["CapAmb"])
    _kv("SETUID+SETGID in bounding set", out["grantable"])
    held = sorted(want & caps["CapPrm"])
    if out["gw_ambient"]:
        held_s = f"{held} -- !! AMBIENT: everything the gateway execs inherits them"
    elif held:
        held_s = f"{held} (permitted)"
    else:
        held_s = "none (expected: the root->app drop clears every set)"
    _kv("gateway holds SETUID/SETGID", held_s)
    nnp_note = "  (file caps on what it execs are ignored)" if out["nnp"] else ""
    _kv("NoNewPrivs (gateway)", f"{out['nnp']}{nnp_note}")
    _kv(
        "NoNewPrivs (this probe)",
        f"{out['nnp_start']}  (True: set from container start)",
    )
    # SETUID/SETGID let the broker fork each CLI as its session's uid (the
    # root-owned launcher stub at cli_path hands it the SDK's pipes over SCM_RIGHTS
    # with a single-use ticket) and act on session processes/files through
    # short-lived children switched to that uid. Such a broker is NOT
    # least-privilege: CAP_SETUID is root-equivalent (setresuid(0,0,0), regains the
    # full set on execve, writes root-owned files), so it must be locked down
    # (non-root ideally, else NNP + SECBIT_NOROOT_LOCKED + a set*id-limited seccomp
    # filter + a minimal bounding set). The four below are extra caps only an
    # alternative broker (acting on session files/processes as itself) would hold.
    # See docs/security-process-isolation.md section 3 for the full design.
    alt = ("KILL", "DAC_OVERRIDE", "FOWNER", "CHOWN")
    held_alt = " ".join(c for c in alt if c in caps["CapBnd"]) or "none"
    _kv("privileged-broker caps (alt. only)", f"{held_alt} in bounding set")
    buildable = _reachable(out, ident)
    if out["grantable"] and not buildable:
        buildable = "no, as started (non-root, NoNewPrivs from start)"
    _kv("=> uid-per-user buildable", buildable)
    return out


def _reachable(uid: dict, ident: dict) -> bool:
    """SETUID/SETGID grantable AND something can hand them to a broker. An init
    shim never changes uid, so one running as non-root proves a non-root start:
    no root entrypoint forks the broker, and with NoNewPrivs set from container
    start no file cap can grant the caps either."""
    shim = ident.get("pid1_comm") in _INIT_SHIMS
    nonroot = shim and ident.get("pid1_uid") not in (None, 0)
    return bool(uid.get("grantable")) and not (nonroot and uid.get("nnp_start"))


def _mode_block(path: str, need: int, st: os.stat_result | None = None) -> str:
    """`path` with its mode/owner when `need` other-bits are missing, else ""."""
    try:
        st = st or os.stat(path)
    except OSError as exc:
        return f"{path} ({_errname(exc.errno)})"
    except (ValueError, RecursionError) as exc:  # NUL/surrogate/deep chain
        return f"{path} ({type(exc).__name__})"
    if (st.st_mode & need) != need:
        return f"{path} ({oct(st.st_mode & 0o777)}, uid {st.st_uid})"
    return ""


_ANCESTORS: dict[str, str] = {}  # dir -> its "o+x" block ("" = passes)


def _chain_block(path: str) -> str:
    """First component that stops a session under another uid on its way to the
    absolute `path` ("" when none). Such a uid gets only the "other" bits (unless
    a shared group is arranged): o+x on every ancestor dir, then o+rx on a dir or
    o+r on a file. os.stat follows symlinks, as the kernel's lookup does. Every
    entry of a walk shares its ancestors, so their results are memoized, and a
    path deeper than _DEPTH_CAP is not followed at all."""
    parts = [p for p in path.split("/") if p]
    if len(parts) > _DEPTH_CAP:
        return f"a {len(parts)}-component path (over {_DEPTH_CAP}: not followed)"
    cur = "/"
    for part in parts:
        if cur not in _ANCESTORS:
            _ANCESTORS[cur] = _mode_block(cur, 0o001)
        if _ANCESTORS[cur]:
            return _ANCESTORS[cur]
        cur = os.path.join(cur, part)
    try:
        st = os.stat(cur)
    except OSError as exc:
        return f"{cur} ({_errname(exc.errno)})"
    except (ValueError, RecursionError) as exc:  # NUL/surrogate/deep chain
        return f"{cur} ({type(exc).__name__})"
    # A dir needs o+rx; a file o+r, and o+rx when owner-executable (a hook/launcher
    # other uids must be able to exec) -- the same rule _private_entries applies.
    if stat.S_ISDIR(st.st_mode) or (st.st_mode & 0o100):
        need = 0o005
    else:
        need = 0o004
    return _mode_block(cur, need, st)


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # one relative, one absolute: not inside
        return False


def _count(path: str, cap: int = 100000) -> str:
    """Entries in a dir, without listing a huge one into memory."""
    n = 0
    with os.scandir(path) as it:
        for _ in it:
            n += 1
            if n >= cap:
                return f"{cap}+"
    return str(n)


def _private_entries(
    root: str, skip: tuple[str, ...], deadline: float, limit: int = _WALK_CAP
) -> tuple[list[tuple[str, str]], int, str, int]:
    """Entries under `root` that a session under another uid could not use.

    Same "other" bits as above: a dir needs o+rx, a file o+r, and an
    owner-executable file (hook/MCP launcher) o+rx. A symlinked entry is judged by
    its target, which the CLI follows: a target chain blocked for other uids
    counts, and a linked dir outside the tree is walked too. A dir the probe
    itself cannot list is counted separately (its contents stay unchecked). `skip`
    prunes subtrees that are writable state, not shared assets.

    Returns (examples, total, capped, unlisted): `total` is the exact count of
    other-uid-unusable entries, `examples` at most _EXAMPLES_CAP of them (memory
    stays bounded regardless of tree size), `capped` is "entries" or "time" when
    the walk stopped early, and `unlisted` is how many dirs the probe could not
    list. A single malformed path (NUL/surrogate/deep chain) fails only itself.
    """
    examples: list[tuple[str, str]] = []
    total = 0
    unlisted = 0

    def add(pair: tuple[str, str]) -> None:
        if len(examples) < _EXAMPLES_CAP:
            examples.append(pair)

    seen = 0
    pending, walked = [root], set()
    while pending:
        top = pending.pop()
        try:
            real_top = os.path.realpath(top)
        except _PATH_ERRORS:
            continue
        if real_top in walked:
            continue
        walked.add(real_top)
        stack = [top]
        while stack:
            current = stack.pop()
            try:
                it = os.scandir(current)
            except OSError:
                unlisted += 1
                add((current, "(unlistable: contents unchecked)"))
                continue
            with it:
                for entry in it:
                    seen += 1
                    capped = "entries" if seen > limit else ""
                    if not capped and time.monotonic() > deadline:
                        capped = "time"
                    if capped:
                        return examples, total, capped, unlisted
                    path = entry.path
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        for pair in _link_problem(path, real_top, pending):
                            total += 1
                            add(pair)
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if path in skip:
                            continue
                        stack.append(path)
                    needs_x = stat.S_ISDIR(st.st_mode) or bool(st.st_mode & 0o100)
                    need = 0o005 if needs_x else 0o004
                    if (st.st_mode & need) != need:
                        total += 1
                        add((path, f"({oct(st.st_mode & 0o777)})"))
    return examples, total, "", unlisted


def _link_problem(path: str, real_top: str, pending: list[str]) -> list:
    """A symlinked entry, judged by its target (its own mode bits never count).
    A linked dir outside the tree is queued in `pending` to be walked too --
    unlike an off-root registry value (probe_shared_assets): a symlinked entry
    IS content of the tree the CLI loads, and its walk spends this row's caps."""
    try:
        deep = os.readlink(path).count("/") >= _DEPTH_CAP
    except OSError:
        return []
    if deep:  # resolving it would cost time quadratic in its depth
        return [(path, f"-> a target over {_DEPTH_CAP} components (not followed)")]
    try:
        target = os.path.realpath(path)
    except _PATH_ERRORS:  # NUL/surrogate/deep symlink chain: skip this one entry
        return [(path, "-> unresolvable target (not followed)")]
    try:
        is_dir = stat.S_ISDIR(os.stat(path).st_mode)
    except _PATH_ERRORS:
        return []  # dangling or looping: broken for every uid alike
    block = _chain_block(target)
    if block:
        return [(path, f"-> {target} (blocked at {block})")]
    if is_dir and not _inside(target, real_top):
        pending.append(path)
    return []


def _report_shared(
    label: str, path: str, deadline: float, skip: tuple[str, ...] = ()
) -> tuple[str, str]:
    """One shared asset: its mode, and whether a session under another uid can
    reach it and use everything inside. Returns (problem, incomplete): the
    `incomplete` cause is set when the walk hit its entry/time cap or met dirs
    the probe cannot list, so a blocker past them may be unreported and the
    clean rows are not an all-clear. An asset that is absent, or broken for the
    probe's own uid too, is no problem a uid split would add."""
    try:
        literal, real = os.path.abspath(path), os.path.realpath(path)
    except _PATH_ERRORS:  # a malformed registry value must not kill the walk
        _kv(label, "unreadable (malformed path)")
        return "", ""
    try:
        st = os.stat(path)
    except (ValueError, RecursionError):
        _kv(label, "unreadable (malformed path)")
        return "", ""
    except OSError as exc:
        try:
            is_link = stat.S_ISLNK(os.lstat(path).st_mode)
        except OSError:
            is_link = False
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            _kv(label, f"dangling symlink -> {real}" if is_link else "absent")
        else:
            where = _chain_block(real) or _chain_block(literal)
            _kv(label, f"unreachable for this uid too: blocked at {where}")
        return "", ""
    is_dir = stat.S_ISDIR(st.st_mode)
    entries = ""
    if is_dir:
        try:
            entries = f" entries={_count(path)}"
        except OSError as exc:
            entries = f" entries=? ({_errname(exc.errno)})"
    block = _chain_block(literal)
    _kv(label, f"mode={oct(st.st_mode & 0o777)} uid={st.st_uid}{entries}")
    _kv("    other uids reach it", f"NO -- blocked at {block}" if block else "yes")
    if real != literal:
        # The lookup walks the literal path AND the link target's own ancestors;
        # show both, since either can be the one that blocks.
        target = _chain_block(real)
        _kv("    via its symlink target", real)
        _kv("      reach the target", f"NO -- blocked at {target}" if target else "yes")
        block = block or target
    problem = f"blocked at {block}" if block else ""
    if not is_dir:
        return problem, ""
    examples, total, capped, unlisted = _private_entries(path, skip, deadline)
    count = f"{total}+ ({capped}-capped)" if capped else str(total)
    if unlisted:
        count += f" ({unlisted} dirs unlistable by the probe: incomplete)"
    eg = ", ".join(f"{p} {why}" for p, why in examples[:3])
    _kv("    entries they cannot use", f"{count}  e.g. {eg}" if examples else count)
    if total and not problem:
        problem = f"{total} entries other uids cannot use"
    causes = [f"{capped} cap hit"] if capped else []
    if unlisted:
        causes.append(f"{unlisted} dir(s) this uid cannot list")
    return problem, ", ".join(causes)


def _load_json(path: str, notes: list[str]):
    """A registry/settings JSON from the session-writable tree, or None. It is
    stat'ed first, so a FIFO or device planted in its place is never opened;
    then opened non-blocking without a controlling tty, re-checked with fstat
    (it may have been swapped meanwhile) and read once, at most _JSON_CAP.
    NaN/Infinity, which the CLI's JSON.parse rejects, make it "not JSON" too."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            notes.append(f"{path}: not a regular file, skipped")
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        notes.append(f"{path}: {_errname(exc.errno)}")
        return None
    except (ValueError, RecursionError):  # NUL/surrogate: never a usable file
        notes.append(f"{_short(path)}: malformed path, skipped")
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            notes.append(f"{path}: not a regular file, skipped")
            return None
        data = os.read(fd, _JSON_CAP + 1)
    except OSError as exc:
        notes.append(f"{path}: {_errname(exc.errno)}")
        return None
    finally:
        os.close(fd)
    if len(data) > _JSON_CAP:
        notes.append(f"{path}: over {_JSON_CAP} bytes, skipped")
        return None
    try:
        return json.loads(data, parse_constant=_not_json)
    except (ValueError, RecursionError):
        notes.append(f"{path}: not JSON, skipped")
        return None


def _not_json(name: str):
    raise ValueError(f"{name} is not JSON")


def _registry_paths(
    registries: list[str], settings: "dict | None", notes: list[str]
) -> tuple[list[str], int]:
    """Absolute paths the CLI records for plugins: marketplace installLocation /
    source.path (known_marketplaces.json), each install's installPath
    (installed_plugins.json), and the user settings' (already parsed)
    extraKnownMarketplaces source paths. The CLI loads plugin skills and hooks
    (CLAUDE_PLUGIN_ROOT) from the marketplace clone these point at, not from
    plugins/cache. Returns (paths, dropped): at most _PATHS_CAP are kept and
    `dropped` counts the rest. A value longer than _PATH_MAX (the kernel refuses
    it, so the CLI cannot use it either) or deeper than _DEPTH_CAP components is
    dropped too and noted, never resolved: resolving such session-written
    strings costs time super-linear in their length."""
    found: dict[str, None] = {}
    skipped = 0

    def add(value) -> None:
        nonlocal skipped
        if not isinstance(value, str) or not os.path.isabs(value):
            return
        if "\x00" in value:  # a NUL would raise ValueError at every os.* call site
            return
        if len(value) > _PATH_MAX or value.count("/") > _DEPTH_CAP:
            skipped += 1
            return
        try:
            os.fsencode(value)  # a lone surrogate would raise at os.stat/realpath
        except (UnicodeEncodeError, ValueError):
            return
        found.setdefault(os.path.normpath(value))

    def add_source(entry) -> None:
        src = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(src, dict):
            add(src.get("path"))

    for reg in registries:
        known = _load_json(os.path.join(reg, "known_marketplaces.json"), notes)
        for entry in known.values() if isinstance(known, dict) else ():
            if isinstance(entry, dict):
                add(entry.get("installLocation"))
                add_source(entry)
        installed = _load_json(os.path.join(reg, "installed_plugins.json"), notes)
        plugins = installed.get("plugins") if isinstance(installed, dict) else None
        for installs in plugins.values() if isinstance(plugins, dict) else ():
            for inst in installs if isinstance(installs, list) else ():
                if isinstance(inst, dict):
                    add(inst.get("installPath"))
    conf = settings
    extra = conf.get("extraKnownMarketplaces") if isinstance(conf, dict) else None
    for entry in extra.values() if isinstance(extra, dict) else ():
        add_source(entry)
    if skipped:
        limits = f"over {_PATH_MAX} chars or {_DEPTH_CAP} components"
        notes.append(f"{skipped} path values {limits}: skipped (unverified)")
    paths = list(found)
    return paths[:_PATHS_CAP], max(0, len(paths) - _PATHS_CAP) + skipped


def _inventory(label: str, path: str) -> None:
    """Writable state that gets split (or stays writable): inventoried only."""
    try:
        st = os.stat(path)
        n = _count(path)
    except OSError as exc:
        _kv(label, "absent" if exc.errno == errno.ENOENT else _errname(exc.errno))
        return
    except (ValueError, RecursionError):
        _kv(label, "unreadable (malformed path)")
        return
    _kv(label, f"mode={oct(st.st_mode & 0o777)} uid={st.st_uid} entries={n}")


def _can_write(path: str) -> bool:
    """W_OK, or owned by this uid on a writable mount: an owner can chmod it back,
    so mode bits alone don't make it read-only."""
    if os.access(path, os.W_OK):
        return True
    try:
        if os.lstat(path).st_uid != os.geteuid():
            return False
        return not os.statvfs(path).f_flag & os.ST_RDONLY
    except OSError:
        return False


def _creatable(path: str) -> str:
    """The nearest existing ancestor dir of an absent `path` when this uid can
    write it -- so it could create `path` below it -- else ""."""
    cur = path
    for _ in range(_DEPTH_CAP):
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        cur = parent
        if os.path.isdir(cur):
            return cur if _can_write(cur) else ""
    return ""


def _code_writable(
    code: str, deadline: float, limit: int = 20000
) -> tuple[int, int, str, str]:
    """(writable, checked, capped, incomplete) for the gateway's code dir and the
    entries under it, leaving out data/ (gateway state, writable by design). A
    symlink is judged by its RESOLVED TARGET's ownership -- the import machinery
    follows it, so a link to an app-owned path is still writable imported code;
    symlinked dirs are not descended into (loop/escape). A dir the probe cannot
    list, or an entry it cannot stat, is flagged incomplete rather than dropped. A
    writable code path alone lets a session drop a module the gateway imports."""
    writable, checked, incomplete = int(_can_write(code)), 1, ""
    stack = [code]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            incomplete = "unlistable dirs"
            continue
        with it:
            for entry in it:
                if time.monotonic() > deadline:
                    return writable, checked, "time", incomplete
                try:
                    if current == code and entry.name == "data":
                        continue
                    is_link = entry.is_symlink()
                except OSError:
                    incomplete = "unstattable entries"
                    continue
                checked += 1
                if checked > limit:
                    return writable, limit, "entries", incomplete
                if is_link:
                    # Judge by the target's ownership, which import resolution
                    # follows; don't descend (a symlinked dir could loop/escape).
                    try:
                        target = os.path.realpath(entry.path)
                    except _PATH_ERRORS:
                        incomplete = "unresolvable links"
                        continue
                    writable += _can_write(target)
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    incomplete = "unstattable entries"
                    continue
                writable += _can_write(entry.path)
                if is_dir:
                    stack.append(entry.path)
    return writable, checked, "", incomplete


def _passwd_home() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        return "/"


def _env_row(value: str, source: str, default: str, problem: str = "") -> str:
    shown = _short(value)
    if not value.strip() or value != value.strip():  # make blanks visible
        shown = repr(shown)
    if problem:
        src = f"  (from {source})" if source else ""
        return f"{shown}{src} -- !! unusable ({problem}): not checked"
    if value.strip():
        return f"{shown}  (from {source})" if source else shown
    return f"(blank in {source} -> {default})" if source else f"(unset -> {default})"


def probe_shared_assets(ident: dict, settings: dict) -> dict:
    _hdr("SHARED ASSET LAYOUT (must stay readable by every user)")
    home = _home()
    # The config dir the CLI ends up with (probe_cli_settings): a settings
    # layer's CLAUDE_CONFIG_DIR beats the container's; "" when it is unusable.
    cdir = settings["cdir"]
    # The CLI's plugin root (registries, cache, marketplaces AND data/) moves with
    # CLAUDE_CODE_PLUGIN_CACHE_DIR; CLAUDE_CODE_PLUGIN_SEED_DIR adds read-only
    # pre-populated roots. The CLI reads both only after copying the settings'
    # env blocks onto its env, so a global-config, user or managed settings value
    # beats the container env -- and arrives session-written, so every value is
    # vetted (_usable_path) before it reaches an os.* call. install_plugins.py
    # clones remote marketplaces under CLAUDE_PLUGIN_CLONE_ROOT, else
    # $HOME/.claude/plugin-marketplaces -- HOME, not CLAUDE_CONFIG_DIR, both
    # stripped, and the temp dir when HOME is blank.
    cache_env, cache_src = _setting_env(settings, "CLAUDE_CODE_PLUGIN_CACHE_DIR")
    seed_env, seed_src = _setting_env(settings, "CLAUDE_CODE_PLUGIN_SEED_DIR")
    clone_env = os.environ.get("CLAUDE_PLUGIN_CLONE_ROOT", "").strip()
    tmps = [os.environ.get(v, "") for v in ("TMPDIR", "TEMP", "TMP")]
    inst_home = os.environ.get("HOME", "").strip() or next(filter(None, tmps), "/tmp")
    cache, cache_bad = (
        _usable_path(cache_env.strip()) if cache_env.strip() else ("", "")
    )
    plugins = "" if cache_bad else cache or (cdir and os.path.join(cdir, "plugins"))
    clones = os.path.abspath(
        clone_env or os.path.join(inst_home, ".claude", "plugin-marketplaces")
    )
    data = os.path.join(plugins, "data") if plugins else ""
    result: dict = {
        "problems": [],  # shared assets other uids cannot reach or use
        "incomplete": [],  # "<row> <cause>": walks that stopped short
        "unverified": 0,  # session-written path values never checked
        "code_dir": "",  # the gateway's code dir (stays OUT of a ruleset)
        "code_writable": False,
        "code_incomplete": "",  # why the code walk stopped short of a "yes"
        "site_writable": [],  # site-packages this uid can write or create
    }
    _kv("HOME", os.environ.get("HOME") or f"(unset -> {home})")
    _kv(
        "CLAUDE_CONFIG_DIR",
        _env_row(
            settings["cdir_value"],
            settings["cdir_src"],
            "HOME/.claude",
            settings["cdir_problem"],
        ),
    )
    default = plugins or "not checked"
    _kv(
        "CLAUDE_CODE_PLUGIN_CACHE_DIR",
        _env_row(cache_env, cache_src, default, cache_bad),
    )
    _kv("CLAUDE_CODE_PLUGIN_SEED_DIR", _env_row(seed_env, seed_src, "none"))
    seeds: list[str] = []
    entries, cut = _seed_entries(seed_env.strip())
    for item in entries:
        path, problem = _usable_path(item)
        if path and path not in seeds:
            seeds.append(path)
        elif problem:
            result["unverified"] += 1
            _kv("  !! seed entry", f"{_short(item)!r} -- unusable ({problem})")
    if cut:
        result["unverified"] += 1
        why = f"only the first {_SEEDS_CAP} distinct entries are checked"
        _kv("  !! seed entries", f"the rest not scanned ({why})")
    _kv("CLAUDE_PLUGIN_CLONE_ROOT", clone_env or f"(unset -> {clones})")
    if settings["unread"]:
        _kv("", f"(unverified: {', '.join(settings['unread'])} not read)")
    _kv(".claude dir", cdir or "not checked (unusable config dir)")
    if not cdir or cache_bad:  # the CLI's real roots cannot be checked
        result["unverified"] += 1
    # A global deadline bounds every walk together; each row also gets its own slice
    # (_ROW_TIME_CAP) so one planted tree cannot starve the rows that follow it.
    deadline = time.monotonic() + _TIME_CAP

    def check(label: str, path: str, skip: tuple[str, ...] = ()) -> None:
        row_deadline = min(deadline, time.monotonic() + _ROW_TIME_CAP)
        problem, incomplete = _report_shared(label, path, row_deadline, skip)
        if incomplete:
            result["incomplete"].append(f"{label.strip()} {incomplete}")
        if problem:
            result["problems"].append(f"{label.strip()} {problem}")

    subs = ("skills", "agents", "commands", "output-styles")
    # plugins/data/ is writable plugin state, not a shared asset: pruned here.
    if plugins:
        check("  plugins/", plugins, (data,))
    for sub in subs if cdir else ():
        check(f"  {sub}/", os.path.join(cdir, sub))
    # settings.json is SHARED POLICY (admin env block + enabledPlugins), not
    # per-session writable state, so it -- like the user-scope CLAUDE.md -- must
    # stay readable by every session too.
    for name in ("settings.json", "CLAUDE.md") if cdir else ():
        check(f"  {name}", os.path.join(cdir, name))
    check("  plugin-marketplaces/ (clones)", clones)
    for seed in seeds:
        check(f"  seed {_short(seed)}", seed)
    roots = [r for r in (plugins, clones, *seeds) if r]
    roots += [os.path.join(cdir, s) for s in subs] if cdir else []
    # A path under a checked root -- literally or via the root's symlink target,
    # which that root's rows already walked -- needs no row of its own.
    real_roots = []
    for r in roots:
        try:
            real_roots.append(os.path.realpath(r))
        except _PATH_ERRORS:
            continue
    roots += real_roots
    notes: list[str] = []
    registry, dropped = _registry_paths(
        [plugins, *seeds] if plugins else seeds, settings["user_conf"], notes
    )
    for note in notes:
        _kv("  !! registry file", note)
    # The registry files are session-writable, so a path they declare is trusted
    # only when it lies UNDER a known shared root (walked above). An off-root
    # value (/etc, /root, /proc/1, or a symlink out of the tree) gets only its
    # ancestor chain checked and is never walked -- walking it would let a
    # session aim this report, and the operator's VERDICT, at arbitrary paths --
    # so the verdict reports it as unverified; one that does not exist at all is
    # a stale entry, counted apart. The literal test comes first; only what it
    # leaves is stat'ed and resolved, under its own deadline.
    off_root, absent = [], 0
    resolve_by = time.monotonic() + _RESOLVE_TIME_CAP
    for p in registry:
        if any(_inside(p, r) for r in roots):
            continue  # under a shared root: covered by that root's own walk
        if time.monotonic() < resolve_by:
            try:
                os.stat(p)
                rp = os.path.realpath(p)
            except FileNotFoundError:
                absent += 1
                continue
            except _PATH_ERRORS:
                rp = p
            if any(_inside(rp, r) for r in roots):
                continue
        off_root.append(p)
    result["unverified"] += len(off_root) + dropped
    kept = f"{len(registry)} absolute" + (f" (+{dropped} dropped)" if dropped else "")
    stale = f", {absent} of those absent (stale)" if absent else ""
    _kv(
        "registry paths",
        f"{kept}, {len(off_root) + absent} off the shared roots{stale}",
    )
    for path in off_root[:_OUTSIDE_CAP]:
        try:
            os.stat(path)
            block = _chain_block(path)
            where = (
                f"other uids: blocked at {block}" if block else "other uids reach it"
            )
        except FileNotFoundError:
            where = "absent"
        except _PATH_ERRORS:
            where = "unreachable for this uid too"
        _kv("  registry-declared (not walked)", f"{_short(path)} -- {where}")
    if len(off_root) > _OUTSIDE_CAP:
        _kv("  !! and more", f"{len(off_root) - _OUTSIDE_CAP} further off-root paths")
    # Writable state: split per session (or kept writable), so only inventoried.
    for sub in ("projects", "plans") if cdir else ():
        _inventory(f"  {sub}/ (split)", os.path.join(cdir, sub))
    if data:
        _inventory("  plugins/data/ (writable)", data)
    # Sessions that keep the gateway's uid must not write its code, and the image
    # chowns /app to app: a Landlock ruleset therefore leaves the code dir OUT
    # (no rule = no read, no write -- a read-only rule would also expose the
    # credentials under its data/). The packages it imports live in this
    # interpreter's site-packages (the image's pip target).
    gw = ident["gw"]
    try:
        code = os.readlink(f"/proc/{gw}/cwd")
    except OSError:
        code = "/app"  # the image's WORKDIR; the gateway's cwd is unreadable here
    if os.path.isdir(code):
        result["code_dir"] = code
        writable, checked, capped, code_inc = _code_writable(
            code, time.monotonic() + _CODE_TIME_CAP
        )
        result["code_writable"] = writable > 0
        if not writable and (capped or code_inc):
            # Only a walk that found nothing writable leaves the answer open.
            cut = f"{capped} cap hit" if capped else ""
            result["code_incomplete"] = ", ".join(filter(None, (cut, code_inc)))
        more = f"+ ({capped}-capped)" if capped else ""
        inc = f"; {code_inc}: partial" if code_inc else ""
        state = f"{writable} of {checked}{more} entries writable (data/ skipped){inc}"
    else:
        state = "absent"
    _kv(f"  gateway code (PID {gw} cwd)", f"{code}: {state}")
    # The gateway runs `python -m uvicorn` (no -s/-I), so system site-packages AND,
    # unless PYTHONNOUSERSITE, the user site run their .pth/sitecustomize at startup
    # (user site precedes system site on sys.path); both must stay read-only. An
    # absent user site is no safer when this uid can create it: site.py adds it at
    # the next interpreter start once it exists (the gateway, install_plugins.py,
    # and a root entrypoint python run without -I).
    sites = [("system site", sysconfig.get_paths()["purelib"])]
    if not os.environ.get("PYTHONNOUSERSITE"):
        try:
            import site as _site

            sites.append(("user site", _site.getusersitepackages()))
        except Exception:  # noqa: BLE001 - site is best-effort (probe runs -I -S)
            pass
    for name, path in sites:
        under = "" if not path or os.path.isdir(path) else _creatable(path)
        if not path:
            state = "unknown"
        elif under:
            state = f"absent, but CREATABLE by this uid (under {under})"
            result["site_writable"].append(path)
        elif not os.path.isdir(path):
            state = "absent"
        elif _can_write(path):
            state = "WRITABLE by this uid"
            result["site_writable"].append(path)
        else:
            state = "read-only"
        _kv(f"    {path or '?'} ({name})", state)
    for cause in result["incomplete"]:
        _kv("  !! INCOMPLETE", f"{cause}: a blocker past it may be unreported")
    if result["code_incomplete"]:
        cause = result["code_incomplete"]
        _kv("  !! INCOMPLETE", f"gateway code walk ({cause}): not an all-clear")
    _kv(
        "  note",
        "reachability reflects CURRENT (session-mutable) mode bits; a recreated",
    )
    _kv("", "container restores image modes, so a session-set 'yes' is not final")
    print("  note: everything above but projects/plans/plugins-data must stay")
    print("        readable by every session uid/jail; only projects/plans/workspace")
    print("        + SDK cache/history get split. The CLI mkdirs plugins/data/<id>")
    print("        before every plugin hook (CLAUDE_PLUGIN_DATA), so that subtree")
    print("        must stay writable or every hook fails. 'other uids' = a session")
    print("        uid that is neither owner nor in the group: it needs o+x on EVERY")
    print("        ancestor, so a 0700 HOME blocks all of it under uid-per-user (a")
    print("        bwrap ro-bind skips the ancestors, not the modes inside).")
    return result


# Landlock remedies for the NO PATH verdict, by probe_landlock()'s cause.
_LANDLOCK_REMEDY = {
    "seccomp": (
        "Landlock: seccomp-BLOCKED -- allow the landlock_* syscalls in",
        "the seccomp profile.",
    ),
    "seccomp-kill": (
        "Landlock: seccomp KILLS landlock_create_ruleset -- allow the",
        "landlock_* syscalls in the profile.",
    ),
    "seccomp-old": (
        "Landlock: the kernel has it, but the seccomp profile predates",
        "landlock_* (e.g. Docker <= 20.10.17): update or allow them.",
    ),
    "kernel-or-seccomp": (
        "Landlock: ENOSYS = kernel without it OR a seccomp profile",
        "older than the syscall (Docker <= 20.10.17): check `docker",
        "version`, or rerun with --security-opt seccomp=unconfined.",
    ),
    "lsm": ("Landlock: compiled in but off -- add 'landlock' to lsm=.",),
    "abi1": (
        "Landlock: kernel has ABI 1 only; need >=5.19 (ABI 2) so",
        "cross-dir rename/link is not forced to EXDEV.",
    ),
    "kernel": (
        "Landlock: the running kernel lacks it (< 5.13) -- upgrade, or use",
        "a distro kernel that backports Landlock (RHEL 9's 5.14 has it).",
    ),
    "other": (
        "Landlock: the create/enforce test failed for an unexpected reason",
        "-- see the LANDLOCK section above for the errno.",
    ),
    "arch": ("Landlock: untested on this arch (its syscall number differs).",),
}


def _say(text: str, first: str = "  ", rest: str = "     ") -> None:
    """Print verdict prose built from probe data, escaped and wrapped at 76."""
    for line in textwrap.wrap(
        _safe(text),
        76,
        initial_indent=first,
        subsequent_indent=rest,
        break_long_words=False,
        break_on_hyphens=False,
    ):
        print(line)


def _shared_problems(shared: dict) -> None:
    """For every verdict that recommends uid-per-user: the shared assets other
    uids cannot reach or use (SHARED ASSET LAYOUT lists them all)."""
    problems = shared.get("problems", [])
    if problems:
        print("    - !! shared assets fail for other uids -- fix before uid split:")
        for problem in problems[:3]:
            print(f"         {_safe(problem)}")
        if len(problems) > 3:
            print(f"         (+{len(problems) - 3} more in SHARED ASSET LAYOUT)")


def _gaps(shared: dict) -> None:
    """The verdict's '!!' lines for what the shared-asset and code checks could
    not settle: each names the walk and its cause."""
    incomplete = shared.get("incomplete", [])
    if incomplete:
        print("  !! shared-asset check INCOMPLETE -- a blocker past these may be")
        print("     unreported, so the clean rows are not an all-clear:")
        for cause in incomplete[:5]:
            _say(cause, "       ", "         ")
        if len(incomplete) > 5:
            print(f"       (+{len(incomplete) - 5} more in SHARED ASSET LAYOUT)")
        print("     Rerun after a cap hit; list unlistable dirs as their owner.")
    if shared.get("code_incomplete"):
        _say(
            f"!! gateway-code check INCOMPLETE ({shared['code_incomplete']}): "
            "nothing session-writable turned up in the part walked, but that is "
            "not an all-clear."
        )
    if shared.get("unverified"):
        _say(
            f"!! {shared['unverified']} plugin path value(s) from the registries "
            "or settings were NOT checked (session-written: off the shared "
            "roots, unusable, or past a cap) -- unverified: confirm the sessions' "
            "uid/jail can read the real ones (SHARED ASSET LAYOUT lists them)."
        )


def _ruleset(shared: dict, ns: dict, abi: int, both: bool) -> None:
    """What a Landlock ruleset must hold -- the same for uid-per-user + Landlock
    and for the interim (docs/security-process-isolation.md section 6.3)."""
    if both:
        print("\n  Landlock ruleset (uid-per-user + Landlock and the interim alike):")
    else:
        print("\n  Landlock ruleset (the Landlock-only interim):")
    code = shared.get("code_dir")
    if code:
        now = "writable" if shared.get("code_writable") else "read-only"
        if not shared.get("code_writable") and shared.get("code_incomplete"):
            now = "not seen writable (walk incomplete)"
        _say(
            f"- keep {code} (the gateway's code; {now} to this uid today) OUT of "
            "it: no rule = no read and no write, while a read-only rule would "
            f"also expose {os.path.join(code, 'data')} credentials to every session.",
            "    ",
            "      ",
        )
    if shared.get("site_writable"):
        print("    - grant no write on these (this uid can write or create them, and")
        print("      the gateway imports from them at startup):")
        for path in shared["site_writable"]:
            print(f"        {_safe(path)}")
    print("    - include /proc (read-only) and /dev/urandom -- the CLI aborts at")
    print("      exec without both -- a /dev/null file rule, a per-session TMPDIR,")
    print("      the package caches, and EXECUTE on its read-only rules.")
    tcp = "available" if abi >= 4 else "missing"
    print("    - loopback TCP between sessions stays open unless it also sets the")
    print(f"      TCP bind/connect port rules (ABI >= 4; {tcp} here, ABI {abi}).")
    state = ns.get("cli_sandbox")
    if state == "off":
        return
    # The CLI's availability check only looks for bwrap and socat on PATH, so
    # with both present it starts and every sandboxed Bash call then fails;
    # failIfUnavailable makes it exit 1 only while one of them is missing.
    missing = ", ".join(ns.get("cli_missing", []))
    one = len(ns.get("cli_missing", [])) == 1
    stay, them = ("stays", "it") if one else ("stay", "them")
    if state == "unset" and missing:
        tail = (
            "settings: nothing enables it here today, but a workspace's "
            f"project/local settings could -- inert there while {missing} {stay} "
            f"absent, every sandboxed Bash call failing once {them} "
            f"{'is' if one else 'are'} installed."
        )
    elif state == "unset":
        tail = (
            "settings: nothing enables it here today, but a workspace's "
            "project/local settings could, and every sandboxed Bash call there "
            "would fail."
        )
    elif state == "inert":
        tail = (
            f"settings. With {missing} absent it is inert here today, but "
            f"installing {them} would make every sandboxed Bash call fail."
        )
    elif state == "fail":
        tail = (
            f"settings. With {missing} absent and failIfUnavailable set, the "
            "CLI exits 1 here already."
        )
    else:  # ok / broken / unknown: enabled, with bwrap and socat present
        tail = "settings, else every sandboxed Bash call fails."
    print("    - the CLI's own sandbox cannot start inside it (no bwrap in a")
    print("      Landlock domain): set CLAUDE_SANDBOX_ENABLED=false and keep")
    text = f"sandbox.enabled / failIfUnavailable out of user and managed {tail}"
    _say(text, "      ", "      ")


def verdict(
    ident: dict, proc: dict, ll: dict, ns: dict, uid: dict, shared: dict
) -> None:
    _hdr("VERDICT")
    if ident.get("root"):
        # root can always setuid and holds the container's caps, so the uid
        # branch below would read READY for sessions that cannot setuid at all.
        print("  NONE: this ran as root, which can always setuid, so the uid/caps")
        print("  results above are root's, not a session's (app uid, CapEff empty).")
        print("  Re-run as the session user:")
        print(f"    {_rerun_cmd(ident)}")
        return
    kind, why = _view(ident)  # the classification /proc hardening printed too
    if kind == "unknown":
        # No confident path without the gateway's real identity: the uid/caps/F2
        # results above are not a session's -- stop here (like the root-probe
        # case) instead of reading a BEST PATH off them, which would contradict
        # the warning printed earlier.
        _say(
            "INCONCLUSIVE: no confident path without the gateway's own identity "
            f"({why}). Is it running, with $PORT its port? Re-run as the session "
            "user:",
            "  ",
            "  ",
        )
        print(f"    {_rerun_cmd(ident)}")
        return
    if kind == "unsure":
        _say(
            f"!! gateway not concluded: {why}. F2 and the -u advice are not "
            "concluded; find the process whose cmdline names uvicorn/src.main "
            "and re-run when it is the only candidate."
        )
    elif kind == "other":
        _say(f"!! {why}; the F2/uid results above are not the sessions'. Re-run:")
        print(f"       {_rerun_cmd(ident)}")
    _gaps(shared)
    ll_ok = ll.get("usable")  # ABI >= 2 AND the create/enforce chain succeeded
    ll_unknown = ll.get("cause") == "untested"
    four = ("userns", "pidns", "mountns", "procns")
    untested = ns.get("untested", [])
    denied = [k for k in four if not ns.get(k) and k not in untested]
    ns_unknown = bool(untested) and not denied  # bwrap neither shown nor ruled out
    grantable = uid.get("grantable")
    reachable = _reachable(uid, ident)  # grantable, and something can use it
    shim, uid1 = ident.get("pid1_comm"), ident.get("pid1_uid")
    needs = "a root start, or NoNewPrivs=0 at start for a file-cap broker"
    stuck = (
        f"PID 1 ({shim}) runs as uid {uid1} (a non-root start) and NoNewPrivs "
        "is set from container start, so nothing can give a broker SETUID/SETGID"
    )
    cli = ns.get("cli_sandbox")
    if ll_ok and reachable:
        print("  BEST PATH (a/single-container): uid-per-user + Landlock is buildable.")
        print("    - spawn: cli_path is a fixed, root-owned launcher stub with no file")
        print("      caps, running as the gateway's uid: it presents a single-use")
        print("      ticket and hands the SDK's pipes (fds 0/1/2) to a resident broker")
        print("      over SCM_RIGHTS; the broker forks each CLI on them as its")
        print("      session's uid. The gateway keeps cap 0 and may set NoNewPrivs.")
        print("    - the broker's SETUID/SETGID are NOT least-privilege: CAP_SETUID is")
        print("      root-equivalent, so lock it down -- non-root ideally, else NNP +")
        print("      SECBIT_NOROOT_LOCKED + a set*id seccomp filter + a minimal")
        print("      bounding set. See docs/security-process-isolation.md section 3.")
        if shim in _INIT_SHIMS and uid1 == 0:
            print(f"    - a root start (PID 1, {shim}, runs as root): the entrypoint")
            print("      can fork the broker before its drop.")
        elif shim in _INIT_SHIMS and uid1 is not None:
            _say(
                f"- PID 1 ({shim}) runs as uid {uid1}: a non-root start, so no "
                "root entrypoint can fork the broker -- it needs file caps on its "
                "binary and NoNewPrivs=0 at container start.",
                "    ",
                "      ",
            )
        else:
            print("    - this assumes a root start: the entrypoint forks the broker")
            print("      before its drop (not checked here).")
        print("    - never in the gateway: KEEPCAPS/ambient caps leak into all it")
        print("      execs. The session drops every cap and sets NoNewPrivs.")
        if uid.get("nnp") and uid.get("nnp_start"):
            print("    - NoNewPrivs=1 from container start (this probe has it too):")
            print("      fine for the root-started broker; it rules out a non-root")
            print("      start (file caps on the broker binary) and the in-place")
            print("      file-cap wrapper alternative.")
        elif uid.get("nnp"):
            print("    - NoNewPrivs=1 on the gateway only (set after start): fine for")
            print("      the broker; it rules out the in-place file-cap wrapper.")
        _shared_problems(shared)
    elif ll_ok:
        print("  LANDLOCK-ONLY: Landlock is usable; uid switching is not:")
        if grantable:
            _say(f"{stuck}. uid-per-user needs {needs}.", "  ", "  ")
        else:
            print("  SETUID/SETGID are missing from the bounding set (cap_drop?).")
    elif ns.get("viable"):
        print("  BWRAP: bwrap/nsjail (userns+pidns+fresh procfs) is viable here.")
        if not ns.get("bwrap"):
            print("    - bubblewrap is not installed in this image.")
        if cli in ("inert", "fail"):
            _say(
                "- installing bwrap + socat also makes the CLI's own sandbox run "
                f"(it is enabled by {ns.get('cli_by')}).",
                "    ",
                "      ",
            )
        if ll_unknown:
            print("    - Landlock untested (its test child never ran): uid-per-user +")
            print("      Landlock is not ruled out -- re-run before choosing this.")
        elif ll.get("cause") in _SECCOMP_CAUSES:
            if reachable:
                print("    - Landlock failed only on seccomp: allowing landlock_* in")
                print("      the profile makes the preferred uid-per-user + Landlock")
                print("      reachable.")
                _shared_problems(shared)
            elif grantable:
                _say(
                    "- Landlock failed only on seccomp: allowing landlock_* in the "
                    "profile makes LANDLOCK-ONLY reachable. uid-per-user also needs "
                    f"{needs}: {stuck}.",
                    "    ",
                    "      ",
                )
            else:
                print("    - Landlock failed only on seccomp: allowing landlock_* in")
                print("      the profile makes LANDLOCK-ONLY reachable (uid-per-user")
                print("      still needs SETUID/SETGID in the bounding set).")
    elif ll_unknown or ns_unknown:
        lost = (["Landlock"] if ll_unknown else []) + (
            [f"namespaces {', '.join(untested)}"] if untested else []
        )
        _say(
            f"INCONCLUSIVE: the deciding tests never ran to an answer "
            f"({'; '.join(lost)}: a test child failed to fork, was killed, or "
            "raised), so the verdict cannot be read off them. Re-run (a "
            "pids/nproc limit, or an OOM kill?).",
            "  ",
            "  ",
        )
    else:
        print("  NO PATH: nothing is fully available as-is. Needed, in order:")
        first, *rest = _LANDLOCK_REMEDY.get(ll.get("cause"), _LANDLOCK_REMEDY["other"])
        print(f"    - {first}")
        for line in rest:
            print(f"      {line}")
        if reachable:
            print("    - uid-per-user alone is buildable (SETUID/SETGID grantable): it")
            print("      closes F2 and cross-user reads, but without Landlock sessions")
            print("      still read every world-readable path.")
            _shared_problems(shared)
        elif grantable:
            _say(f"- uid-per-user: {stuck}; it needs {needs}.", "    ", "      ")
        else:
            print("    - uid-per-user: keep SETUID/SETGID in the bounding set.")
        failed = [k for k in four if not ns.get(k)]
        shown = [f"{k}?" if k in untested else k for k in failed]
        print(f"    - OR bwrap (missing: {', '.join(shown) or 'none'}):")
        if "userns" in failed:
            print("      enable user namespaces (sysctl user.max_user_namespaces>0,")
            print("      kernel.unprivileged_userns_clone=1) + allow clone/unshare")
            print("      namespace flags in seccomp.")
        if "pidns" in failed:
            print("      allow CLONE_NEWPID in seccomp.")
        if "mountns" in failed or "procns" in failed:
            print(
                "      allow unshare(CLONE_NEWNS)+mount in seccomp (or CAP_SYS_ADMIN);"
            )
            if ns.get("apparmor"):
                print(
                    f"      AppArmor {ns['apparmor']} (deny mount): apparmor=unconfined"
                )
                print("      or a custom profile.")
            print("      --security-opt systempaths=unconfined unmasks /proc.")
        if failed == ["procns"] and "procns" not in untested:
            print("      (only the fresh procfs failed: enableWeakerNestedSandbox /")
            print(
                "       CLAUDE_SANDBOX_WEAKER_NESTED binds this /proc instead -- which"
            )
            print("       leaves F2 open: sandboxed commands still read the gateway's")
            print("       /proc/<pid>/environ unless it is non-dumpable.)")
        if not ns.get("bwrap"):
            print("      install bubblewrap (+ socat for the CLI sandbox).")
        elif ns.get("cli_missing"):
            print(f"      install {', '.join(ns['cli_missing'])} (CLI sandbox).")
        print("      Widens the outer surface.")
    if uid.get("gw_ambient"):
        print("  !! the gateway holds SETUID/SETGID in its AMBIENT set: everything")
        print("     it execs (plugin installs, MCP tests, the CLI) inherits them.")
    if ll_ok:
        abi = ll.get("abi", 0)
        print("\n  INTERIM, no privilege: Landlock-only (no uid switch, no caps). It")
        print("  confines file reads and hides /proc/<pid>/environ of processes")
        print("  outside the session's domain. Sessions keep the gateway's uid, so")
        if abi >= 6:
            print("  there is no DAC isolation, and unless the ruleset sets the signal")
            print(f"  scope (ABI {abi} has it), sessions can signal each other and")
            print("  the gateway.")
        else:
            print(f"  there is no DAC isolation and no signal scoping (ABI {abi} < 6,")
            print("  needs 6.12): sessions can signal each other and the gateway.")
        _ruleset(shared, ns, abi, bool(reachable))
    gw = ident.get("gw", "1")
    nd = proc.get("non_dumpable")
    if kind == "unsure":
        state = "not concluded"
    elif ident.get("hidden") and kind == "ok":
        state = f"unknown; hidden ({ident['hidden']}) from the sessions' ids"
    else:
        state = "unknown" if nd is None else ("yes" if nd else "no")
    print(f"\n  /proc/{gw}/environ (F2): closed for sessions by a uid != the")
    print("  gateway's, a new PID ns with its own procfs, or Landlock on the")
    print("  session (every ABI, 5.13+: no scope flag needed). Zero-privilege")
    print("  interim for the gateway itself: PR_SET_DUMPABLE(0) hides its")
    _say(
        f"environ/mem from same-uid sessions (here: non-dumpable = {state}).",
        "  ",
        "  ",
    )
    print("  Not isolation: exec'd CLI children are dumpable again, so each")
    print("  session's env stays readable to the others.")
    if proc.get("pid1_environ"):
        comm = ident.get("pid1_comm") or "?"
        _say(
            f"!! /proc/1/environ is READABLE here too: PID 1 ({comm}) shares the "
            "sessions' uid and holds the full container env, which the gateway's "
            "PR_SET_DUMPABLE(0) does not cover. The mechanisms above close it"
            + (
                ", and so does a root start (the shim then runs as root)."
                if comm in _INIT_SHIMS
                else "."
            )
        )
    if cli == "inert":
        missing = ", ".join(ns.get("cli_missing", []))
        print(f"\n  CLI OS sandbox: inert here ({missing} missing): the CLI logs")
        print("  'Sandbox disabled' and runs Bash unconfined.")
    elif cli == "fail":
        missing = ", ".join(ns.get("cli_missing", []))
        print()
        _say(
            f"CLI OS sandbox: FAILS here -- enabled by {ns.get('cli_by')}, "
            f"sandbox.failIfUnavailable set by {ns.get('cli_fail_by')}, and "
            f"{missing} missing: the CLI exits 1 at start, so every session fails.",
            "  ",
            "  ",
        )
    elif cli == "broken":
        denied_s = ", ".join(ns.get("cli_denied", []))
        print("\n  CLI OS sandbox: BROKEN here -- bwrap and socat are present,")
        print(f"  but {denied_s} denied.")
        print("  Every sandboxed Bash call fails with a bwrap error, and nothing")
        print("  logs 'Sandbox disabled'.")
    elif cli == "unknown":
        unk = ", ".join(ns.get("cli_untested", []))
        print(f"\n  CLI OS sandbox: UNKNOWN here -- {unk} untested (the test never")
        print("  ran), so whether sandboxed Bash would run cannot be concluded.")


def main() -> int:
    # A probe killed part-way (seccomp, OOM) must leave what it printed so far.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    # An inherited SIGCHLD=SIG_IGN would auto-reap the test children.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    print("Claude gateway isolation capability probe")
    print("(run inside the gateway container as the session/app user)")
    probe_kernel()
    ident = probe_identity()
    caps = probe_caps_seccomp(ident)
    proc = probe_proc_hardening(ident)
    ll = probe_landlock()
    settings = probe_cli_settings()
    ns = probe_namespaces(caps, settings)
    uid = probe_setuid(caps, ident)
    shared = probe_shared_assets(ident, settings)
    verdict(ident, proc, ll, ns, uid, shared)
    return 0


if __name__ == "__main__":
    sys.exit(main())
