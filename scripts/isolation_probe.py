#!/usr/bin/env python3
"""Isolation capability probe for the Claude gateway container.

Run INSIDE the prod gateway container, as the uid:gid that sessions run as (the
gateway's -- `docker compose exec` attaches as root here unless given `-u`):

    docker compose exec -u app gateway python3 -I -S /tmp/isolation_probe.py

With APP_GID != the app user's group, `-u app` is NOT the sessions' identity;
the probe then prints the exact `-u <uid>:<gid>` to re-run with.

Read-only and non-destructive: every privileged or killable test (unshare,
mount, setuid, landlock_create_ruleset) runs in a short-lived forked child that
exits immediately; the parent only reads /proc, /sys, PATH and the ~/.claude
tree. Sessions can write that tree, so it is read as hostile: a registry file is
stat'ed first and opened (non-blocking, no controlling tty) only as a small
regular file, walks are bounded in entries, depth and time, and names are
escaped before they reach the terminal. Nothing is created, mounted, or changed
in the live process.

It answers which per-user isolation mechanism is available in THIS container:
  * uid-per-user (needs CAP_SETUID/SETGID in the bounding set, for a broker the
    root entrypoint forks before its drop or for file caps on the wrapper --
    never held by the gateway itself, whose children would inherit them)
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
import sys
import sysconfig
import time

# ---- syscall numbers ---------------------------------------------------------
_MACH = platform.machine()
# unshare() goes through libc's wrapper, which knows this arch's number. Landlock
# has none, but syscalls added since the 5.1 table unification share one number on
# every arch except alpha/ia64/mips (they add an ABI offset), where the probe
# reports Landlock as untested rather than issue the wrong syscall.
_NR_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_NR_KNOWN = not _MACH.startswith(("alpha", "ia64", "mips"))

CLONE_NEWNS = 0x00020000
CLONE_NEWPID = 0x20000000
CLONE_NEWUSER = 0x10000000
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

# The interpreter's own symbols include libc's; find_library would spawn ldconfig.
_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long

# PID 1 under compose `init: true` / `docker run --init` (or podman) is a shim and
# the gateway is its child.
_INIT_SHIMS = ("docker-init", "tini", "dumb-init", "catatonit")
# Name fragments that make an env var worth flagging (names only, never values).
_SECRETISH = ("KEY", "TOKEN", "SECRET", "PASS", "AUTH", "CRED", "COOKIE", "DSN", "URL")
# Bounds for the session-writable tree.
_JSON_CAP = 4 << 20  # registry/settings files above this size are skipped
_PATHS_CAP = 500  # absolute paths taken from the registries
_OUTSIDE_CAP = 10  # registry roots outside the checked rows that get walked
_WALK_CAP = 50000  # entries per walk
_DEPTH_CAP = 256  # path components followed in a symlink target
_TIME_CAP = 60.0  # seconds for all shared-asset walks together
_CODE_TIME_CAP = 10.0  # its own budget: planted shared trees can't starve it
_TRUE = ("true", "1", "yes", "on")  # the gateway's boolean spellings
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


def _unrun(detail: str) -> str:
    """Display text for a child test that gave no answer of its own, else ""."""
    kind, _, why = detail.partition(":")
    if kind == "fork":
        return f"UNKNOWN (fork failed: {why}; pids/nproc limit?)"
    if kind == "killed":
        return f"KILLED ({why}{': seccomp kill' if why == 'SIGSYS' else ''})"
    if kind == "died":
        return "UNKNOWN (test child died without reporting)"
    return ""


def _errname(en: int) -> str:
    return errno.errorcode.get(en, f"errno {en}")


def _kernel_version() -> tuple[int, int] | None:
    rel = platform.release().split("-")[0].split(".")
    try:
        return int(rel[0]), int(rel[1])
    except (IndexError, ValueError):
        return None


def _line(path: str) -> str:
    """First line of a procfs/sysfs file ("" if unreadable)."""
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return ""


def _read_status(pid: str) -> dict:
    """The /proc/<pid>/status fields this probe uses (the file is world-readable)."""
    keys = ("PPid", "Uid", "Gid", "CapEff", "CapPrm", "CapAmb", "CapBnd")
    keys += ("Seccomp", "Seccomp_filters", "NoNewPrivs")
    out: dict = {}
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                key, _, val = line.partition(":")
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
        with open("/proc/self/mountinfo") as f:
            for line in f:
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


def _gateway() -> tuple[str, str, list[str]]:
    """(pid, note, rivals): the gateway process, and others that look as likely.

    PID 1, unless PID 1 is an init shim (compose `init: true`, `--init`), runs
    as root, or is hidden from us by hidepid -- then a non-root child of PID 1:
    one whose cmdline names uvicorn/src.main first, then a python one (under a
    shim, any). Session orphans and sidecars are reparented to PID 1 as well, so
    a tie is reported as rivals rather than guessed.
    """
    comm = _line("/proc/1/comm")
    try:
        euid = int(_read_status("1")["Uid"][1])
    except (KeyError, IndexError, ValueError):
        euid = None  # hidden by hidepid: its children we can see still tell
    shim = comm in _INIT_SHIMS
    if euid not in (None, 0) and not shim:
        return "1", "", []
    ranked: dict[int, list[str]] = {}
    for kid in sorted(_children("1"), key=int):
        if kid == str(os.getpid()):
            continue  # the probe itself, when PID 1 started it
        try:
            if int(_read_status(kid)["Uid"][1]) == 0:
                continue
        except (KeyError, IndexError, ValueError):
            continue
        cmd = _cmdline(kid)
        if "uvicorn" in cmd or "src.main" in cmd:
            rank = 2
        elif _line(f"/proc/{kid}/comm").startswith("python"):
            rank = 1
        elif shim:
            rank = 0
        else:
            continue
        ranked.setdefault(rank, []).append(kid)
    who = f"PID 1 is {comm} (uid {euid})" if euid is not None else "PID 1 is hidden"
    if not ranked:
        return "1", "" if euid is None else f"{who}: gateway not identified", []
    best = ranked[max(ranked)]
    return best[0], f"{who}; this is its child", best[1:]


def _rerun_ids(ident: dict) -> str:
    """The `-u` value to re-run with: the gateway's ids, never root's."""
    if ident.get("uid") and not ident.get("rivals"):  # known, non-zero, unique
        return f"{ident['uid']}:{ident['gid']}"
    user = str(os.geteuid() or "app")
    gid = os.environ.get("APP_GID", "").strip()
    return f"{user}:{gid}" if gid else user


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
    gw, note, rivals = _gateway()
    ident = {"root": euid == 0, "gw": gw, "uid": None, "gid": None}
    ident.update(same_creds=False, invisible=False, hidepid=_hidepid())
    ident["rivals"] = rivals
    _kv("uid / euid", f"{os.getuid()} / {euid}")
    _kv("gid / egid", f"{os.getgid()} / {egid}")
    _kv("running as root", ident["root"])
    shown = f"PID {gw} ({_line(f'/proc/{gw}/comm') or '?'})"
    _kv("gateway process", f"{shown} -- {note}" if note else shown)
    if rivals:
        for rival in rivals:
            _kv("!! also a candidate", f"PID {rival}: {_cmdline(rival)[:60]}")
        _kv("", "several look like the gateway: F2 and -u advice not concluded")
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
        if isinstance(err, FileNotFoundError) and ident["hidepid"]:
            ident["invisible"] = True
            _kv("gateway uid:gid", f"INVISIBLE ({ident['hidepid']}) to this uid:gid")
            _kv(
                "!! WARNING",
                "sessions run with the gateway's ids and see it -- re-run with "
                f"`docker compose exec -u {_rerun_ids(ident)}`",
            )
        else:
            _kv("gateway uid:gid", err or "unreadable")
        return ident
    ident["uid"], ident["gid"] = uids[1], gids[1]  # effective ids
    shown = f"{uids[1]}:{gids[1]}"
    if len(set(uids)) > 1 or len(set(gids)) > 1:
        shown += f"  (real/eff/saved uid {uids}, gid {gids})"
    _kv("gateway uid:gid", shown)
    ident["same_creds"] = set(uids) == {euid} and set(gids) == {egid}
    if ident["root"]:
        _kv(
            "!! WARNING",
            f"running as root -- use `docker compose exec -u {_rerun_ids(ident)}`; "
            "setuid/F2 results are wrong, so no VERDICT is given",
        )
    elif uids[1] == 0:
        _kv(
            "!! WARNING",
            "the gateway runs as root (or was not found), so the sessions' ids "
            "are unknown -- re-run with `-u app` or `-u <uid>:<APP_GID>`",
        )
    elif not ident["same_creds"] and not rivals:
        _kv(
            "!! WARNING",
            f"this probe is {euid}:{egid} but sessions inherit the gateway's "
            f"{uids[1]}:{gids[1]}, so F2/uid results below are not theirs -- "
            f"re-run with `docker compose exec -u {_rerun_ids(ident)}`",
        )
    return ident


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


def probe_proc_hardening(ident: dict) -> dict:
    _hdr("/proc HARDENING")
    gw = ident["gw"]
    out = {"non_dumpable": None}
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
    if owner is None or not ident.get("uid"):
        dumpable = "unknown (gateway runs as root or is not visible)"
    else:
        out["non_dumpable"] = owner == 0
        dumpable = "yes"
        if out["non_dumpable"]:
            dumpable = "NO (non-dumpable: its /proc files are owned by root)"
    _kv("gateway dumpable", dumpable)
    label = f"/proc/{gw}/environ READABLE"
    if ident["rivals"]:
        others = ", ".join(ident["rivals"])
        _kv(label, f"NOT CONCLUDED -- gateway candidates PID {gw}, {others}")
        return out
    try:
        with open(f"/proc/{gw}/environ", "rb") as f:
            data = f.read()  # read to EOF; prod env easily exceeds one 4 KiB page
        names = sorted(
            {b.split(b"=", 1)[0].decode("latin1") for b in data.split(b"\0") if b}
        )
        _kv(label, f"YES ({len(names)} vars visible) <-- finding")
        hot = [n for n in names if any(s in n.upper() for s in _SECRETISH)]
        more = f" (+{len(hot) - 20} more)" if len(hot) > 20 else ""
        _kv("  secret-ish names in its env", f"{hot[:20]}{more}")
    except PermissionError:
        if not ident.get("same_creds"):
            why = "not a session's view: uid:gid differs from the gateway's"
        elif out["non_dumpable"]:
            why = "the gateway is non-dumpable, not uid-isolated"
        else:
            why = "blocked for the gateway's own uid:gid"
        _kv(label, f"NO (EACCES) -- {why}")
        if out["non_dumpable"] and ident.get("same_creds"):
            _kv("  but", "exec'd CLI children are dumpable again: their env")
            _kv("", "stays readable to every other same-uid session")
    except FileNotFoundError:
        if ident["hidepid"]:
            # hidepid hides a process whose ids differ from ours -- not F2 closed.
            _kv(label, f"INVISIBLE ({ident['hidepid']}) -- not a session's view")
            _kv("", f"re-run with `-u {_rerun_ids(ident)}`")
        else:
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

    # In a child: a seccomp KILL action on the syscall must not take the probe
    # (and its report so far) down with it.
    _, detail = _in_child(t_landlock)
    try:
        abi_s, en_s = detail.split()
        abi, en = int(abi_s), int(en_s)
    except ValueError:
        killed = detail.startswith("killed:")
        out["cause"] = "seccomp-kill" if killed else "untested"
        shown = _unrun(detail) or f"UNKNOWN ({detail})"
        if killed:
            shown = f"KILLED ({detail[7:]}) => seccomp kills the syscall"
        _kv("landlock_create_ruleset", shown)
        _kv("=> Landlock usable (ABI>=2)", False if killed else "unknown")
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
        out["usable"] = True
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
        # outside its domain -- reading their /proc/<pid>/environ included. ABI 6
        # (6.12) adds opt-in scopes for signals and abstract unix sockets.
        scoped = "yes (ABI>=6)" if abi >= 6 else f"no (ABI {abi} < 6)"
        _kv("signal/abstract-socket scoping", scoped)
    _kv("=> Landlock usable (ABI>=2)", out["usable"])
    return out


def probe_namespaces(caps: dict) -> dict:
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
        shown = _unrun(detail)
        if shown:
            if not detail.startswith("killed:"):  # killed counts as denied
                out["untested"].append(key)
            _kv(label, shown)
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
    # The CLI's own OS sandbox (sandbox.enabled -- the gateway turns it on only
    # with the workspace sandbox or CLAUDE_SANDBOX_ENABLED, as prod does) checks
    # that bwrap and socat are on PATH (ripgrep: the CLI uses its bundled one):
    # missing one, it prints "Sandbox disabled" and runs Bash unconfined.
    # Otherwise every Bash call runs under `bwrap --unshare-user --unshare-pid`
    # plus a fresh `--proc /proc` -- a bind of this /proc instead under
    # sandbox.enableWeakerNestedSandbox (CLAUDE_SANDBOX_WEAKER_NESTED) -- so a
    # denied namespace or proc mount makes every sandboxed Bash call fail.
    bins = {b: shutil.which(b) for b in ("bwrap", "socat")}
    for name, path in bins.items():
        _kv(f"{name} on PATH", path or "NO")
    out["bwrap"] = bool(bins["bwrap"])
    weaker = os.environ.get("CLAUDE_SANDBOX_WEAKER_NESTED", "").lower() in _TRUE
    missing = [b for b, p in bins.items() if not p]
    out["cli_missing"] = missing
    need = ["userns", "pidns", "mountns"] + ([] if weaker else ["procns"])
    out["cli_denied"] = [k for k in need if not out[k]]
    off = os.environ.get("CLAUDE_SANDBOX_ENABLED", "").lower()
    if off in ("false", "0", "no", "off"):
        out["cli_sandbox"] = "off"
        state = "n/a (CLAUDE_SANDBOX_ENABLED=false)"
    elif missing:
        out["cli_sandbox"] = "inert"
        state = f"NO -- {', '.join(missing)} missing: Bash runs unconfined"
    elif out["cli_denied"]:
        out["cli_sandbox"] = "broken"
        denied = ", ".join(out["cli_denied"])
        state = f"NO -- {denied} denied: every sandboxed Bash call fails"
    else:
        out["cli_sandbox"] = "ok"
        state = "yes"
    _kv("=> CLI OS sandbox can run", state)
    if out["cli_sandbox"] != "off":
        _kv("", "(when on: workspace sandbox or CLAUDE_SANDBOX_ENABLED)")
    if weaker and out["cli_sandbox"] != "off":
        _kv("", "(weaker nested: sandboxed Bash still sees this /proc)")
    unsandboxed = os.environ.get("CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", "").lower()
    if out["cli_sandbox"] == "broken" and unsandboxed in _TRUE:
        _kv("", "(CLAUDE_SANDBOX_ALLOW_UNSANDBOXED: the model may retry unsandboxed)")
    return out


def probe_setuid(caps: dict) -> dict:
    _hdr("UID SWITCHING (uid-per-user viability)")
    want = {"SETUID", "SETGID"}
    # The bounding set caps every process here, the root entrypoint included. A
    # broker the entrypoint forks before its root->app drop can keep the caps;
    # file caps on the wrapper need them in the bounding set AND NoNewPrivs=0.
    # The gateway itself must hold none: ambient/KEEPCAPS caps are inherited by
    # everything it execs (plugin installs, MCP tests, the bundled-CLI fallback).
    out = {"grantable": want <= caps["CapBnd"], "nnp": caps.get("nnp") == "1"}
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
    nnp_note = "  (file caps on a wrapper are ignored)" if out["nnp"] else ""
    _kv("NoNewPrivs (gateway)", f"{out['nnp']}{nnp_note}")
    # The least-privilege broker needs only SETUID/SETGID: it stops and cleans up
    # sessions through a short-lived child that switches to the session's uid
    # and acts as the owner. These four matter only for the alternative
    # "privileged broker" that acts on session files and processes as itself.
    alt = ("KILL", "DAC_OVERRIDE", "FOWNER", "CHOWN")
    held_alt = " ".join(c for c in alt if c in caps["CapBnd"]) or "none"
    _kv("privileged-broker caps (alt. only)", f"{held_alt} in bounding set")

    _kv("=> uid-per-user buildable", out["grantable"])
    return out


def _mode_block(path: str, need: int, st: os.stat_result | None = None) -> str:
    """`path` with its mode/owner when `need` other-bits are missing, else ""."""
    try:
        st = st or os.stat(path)
    except OSError as exc:
        return f"{path} ({_errname(exc.errno)})"
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
    return _mode_block(cur, 0o005 if stat.S_ISDIR(st.st_mode) else 0o004, st)


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
) -> tuple[list[tuple[str, str]], str, int]:
    """Entries under `root` that a session under another uid could not use, as
    (path, why) pairs.

    Same "other" bits as above: a dir needs o+rx, a file o+r, and an
    owner-executable file (hook/MCP launcher) o+rx. A symlinked entry is judged by
    its target, which the CLI follows: a target chain blocked for other uids
    counts, and a linked dir outside the tree is walked too. A dir the probe
    itself cannot list counts as well (its contents stay unchecked). `skip`
    prunes subtrees that are writable state, not shared assets. Returns (entries,
    capped, unlisted): capped is "entries" or "time" when the walk stopped early
    -- after `limit` entries in all, or at `deadline` -- so no tree can stall it.
    """
    bad: list[tuple[str, str]] = []
    unlisted: list[str] = []
    seen = 0
    pending, walked = [root], set()
    while pending:
        top = pending.pop()
        real_top = os.path.realpath(top)
        if real_top in walked:
            continue
        walked.add(real_top)
        stack = [top]
        while stack:
            current = stack.pop()
            try:
                it = os.scandir(current)
            except OSError:
                unlisted.append(current)
                continue
            with it:
                for entry in it:
                    seen += 1
                    capped = "entries" if seen > limit else ""
                    if not capped and time.monotonic() > deadline:
                        capped = "time"
                    if capped:
                        return _with_unchecked(bad, unlisted), capped, len(unlisted)
                    path = entry.path
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        bad += _link_problem(path, real_top, pending)
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if path in skip:
                            continue
                        stack.append(path)
                    needs_x = stat.S_ISDIR(st.st_mode) or bool(st.st_mode & 0o100)
                    need = 0o005 if needs_x else 0o004
                    if (st.st_mode & need) != need:
                        bad.append((path, f"({oct(st.st_mode & 0o777)})"))
    return _with_unchecked(bad, unlisted), "", len(unlisted)


def _link_problem(path: str, real_top: str, pending: list[str]) -> list:
    """A symlinked entry, judged by its target (its own mode bits never count).
    A linked dir outside the tree is queued in `pending` to be walked too."""
    try:
        deep = os.readlink(path).count("/") >= _DEPTH_CAP
    except OSError:
        return []
    if deep:  # resolving it would cost time quadratic in its depth
        return [(path, f"-> a target over {_DEPTH_CAP} components (not followed)")]
    target = os.path.realpath(path)
    try:
        is_dir = stat.S_ISDIR(os.stat(path).st_mode)
    except OSError:
        return []  # dangling or looping: broken for every uid alike
    block = _chain_block(target)
    if block:
        return [(path, f"-> {target} (blocked at {block})")]
    if is_dir and not _inside(target, real_top):
        pending.append(path)
    return []


def _with_unchecked(
    bad: list[tuple[str, str]], unlisted: list[str]
) -> list[tuple[str, str]]:
    """Add the unlistable dirs not already reported by their own mode."""
    known = {path for path, _ in bad}
    extra = [(d, "(unlistable: contents unchecked)") for d in unlisted]
    return bad + [e for e in extra if e[0] not in known]


def _report_shared(
    label: str, path: str, deadline: float, skip: tuple[str, ...] = ()
) -> tuple[str, bool]:
    """One shared asset: its mode, and whether a session under another uid can
    reach it and use everything inside. Returns (problem, timed_out); an asset
    that is absent, or broken for the probe's own uid too, is no problem a uid
    split would add."""
    literal, real = os.path.abspath(path), os.path.realpath(path)
    try:
        st = os.stat(path)
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
        return "", False
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
        return problem, False
    bad, capped, unlisted = _private_entries(path, skip, deadline)
    count = f"{len(bad)}+ ({capped}-capped)" if capped else str(len(bad))
    if unlisted:
        count += f" ({unlisted} dirs unlistable by the probe: incomplete)"
    eg = ", ".join(f"{p} {why}" for p, why in bad[:3])
    _kv("    entries they cannot use", f"{count}  e.g. {eg}" if bad else count)
    if bad and not problem:
        problem = f"{len(bad)} entries other uids cannot use"
    return problem, capped == "time"


def _load_json(path: str, notes: list[str]):
    """A registry/settings JSON from the session-writable tree, or None. It is
    stat'ed first, so a FIFO or device planted in its place is never opened;
    then opened non-blocking without a controlling tty, re-checked with fstat
    (it may have been swapped meanwhile) and read once, at most _JSON_CAP."""
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
        return json.loads(data)
    except (ValueError, RecursionError):
        notes.append(f"{path}: not JSON, skipped")
        return None


def _registry_paths(
    registries: list[str], settings: str, notes: list[str]
) -> tuple[list[str], bool]:
    """Absolute paths the CLI records for plugins: marketplace installLocation /
    source.path (known_marketplaces.json), each install's installPath
    (installed_plugins.json), and settings.json extraKnownMarketplaces source
    paths. The CLI loads plugin skills and hooks (CLAUDE_PLUGIN_ROOT) from the
    marketplace clone these point at, not from plugins/cache. Returns (paths,
    truncated): at most _PATHS_CAP are kept."""
    found: dict[str, None] = {}

    def add(value) -> None:
        if isinstance(value, str) and os.path.isabs(value):
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
    conf = _load_json(settings, notes)
    extra = conf.get("extraKnownMarketplaces") if isinstance(conf, dict) else None
    for entry in extra.values() if isinstance(extra, dict) else ():
        add_source(entry)
    paths = list(found)
    return paths[:_PATHS_CAP], len(paths) > _PATHS_CAP


def _inventory(label: str, path: str) -> None:
    """Writable state that gets split (or stays writable): inventoried only."""
    try:
        st = os.stat(path)
        n = _count(path)
    except OSError as exc:
        _kv(label, "absent" if exc.errno == errno.ENOENT else _errname(exc.errno))
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


def _code_writable(
    code: str, deadline: float, limit: int = 20000
) -> tuple[int, int, str]:
    """(writable, checked, capped) for the gateway's code dir itself and the
    entries under it, leaving out data/ (gateway state, writable by design) and
    symlinks. A writable code dir alone lets a session drop a module there."""
    writable, checked = int(_can_write(code)), 1
    stack = [code]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                if time.monotonic() > deadline:
                    return writable, checked, "time"
                try:
                    if entry.is_symlink() or (current == code and entry.name == "data"):
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                checked += 1
                if checked > limit:
                    return writable, limit, "entries"
                writable += _can_write(entry.path)
                if is_dir:
                    stack.append(entry.path)
    return writable, checked, ""


def _passwd_home() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        return "/"


def probe_shared_assets(ident: dict) -> dict:
    _hdr("SHARED ASSET LAYOUT (must stay readable by every user)")
    # The CLI's home is $HOME, else the passwd entry (os.homedir()).
    home = os.environ.get("HOME") or _passwd_home()
    cdir_env = os.environ.get("CLAUDE_CONFIG_DIR", "")
    cdir = os.path.abspath(cdir_env or os.path.join(home, ".claude"))
    # The CLI's plugin root (registries, cache, marketplaces AND data/) moves with
    # CLAUDE_CODE_PLUGIN_CACHE_DIR; CLAUDE_CODE_PLUGIN_SEED_DIR adds read-only
    # pre-populated roots. install_plugins.py clones remote marketplaces under
    # CLAUDE_PLUGIN_CLONE_ROOT, else $HOME/.claude/plugin-marketplaces -- HOME,
    # not CLAUDE_CONFIG_DIR, both stripped, and the temp dir when HOME is blank.
    cache_env = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR", "")
    seed_env = os.environ.get("CLAUDE_CODE_PLUGIN_SEED_DIR", "")
    clone_env = os.environ.get("CLAUDE_PLUGIN_CLONE_ROOT", "").strip()
    tmps = [os.environ.get(v, "") for v in ("TMPDIR", "TEMP", "TMP")]
    inst_home = os.environ.get("HOME", "").strip() or next(filter(None, tmps), "/tmp")
    plugins = os.path.abspath(cache_env or os.path.join(cdir, "plugins"))
    seeds = [os.path.abspath(s) for s in seed_env.split(os.pathsep) if s]
    clones = os.path.abspath(
        clone_env or os.path.join(inst_home, ".claude", "plugin-marketplaces")
    )
    data = os.path.join(plugins, "data")
    _kv("HOME", os.environ.get("HOME") or f"(unset -> {home})")
    _kv("CLAUDE_CONFIG_DIR", cdir_env or "(unset -> HOME/.claude)")
    _kv("CLAUDE_CODE_PLUGIN_CACHE_DIR", cache_env or f"(unset -> {plugins})")
    _kv("CLAUDE_CODE_PLUGIN_SEED_DIR", seed_env or "(unset)")
    _kv("CLAUDE_PLUGIN_CLONE_ROOT", clone_env or f"(unset -> {clones})")
    _kv(".claude dir", cdir)
    result: dict = {"problems": [], "code": "", "code_writable": False}
    # One deadline for every walk below: no planted tree can stall the probe.
    deadline = time.monotonic() + _TIME_CAP
    timed_out = False

    def check(label: str, path: str, skip: tuple[str, ...] = ()) -> None:
        nonlocal timed_out
        problem, late = _report_shared(label, path, deadline, skip)
        timed_out |= late
        if problem:
            result["problems"].append(f"{label.strip()} {problem}")

    # plugins/data/ is writable plugin state, not a shared asset: pruned here.
    check("  plugins/", plugins, (data,))
    subs = ("skills", "agents", "commands", "output-styles")
    for sub in subs:
        check(f"  {sub}/", os.path.join(cdir, sub))
    # settings.json is SHARED POLICY (admin env block + enabledPlugins), not
    # per-session writable state, so it -- like the user-scope CLAUDE.md -- must
    # stay readable by every session too.
    for name in ("settings.json", "CLAUDE.md"):
        check(f"  {name}", os.path.join(cdir, name))
    check("  plugin-marketplaces/ (clones)", clones)
    for seed in seeds:
        check(f"  seed {seed}", seed)
    roots = [plugins, clones, *seeds] + [os.path.join(cdir, s) for s in subs]
    # A path under a checked root -- literally or via the root's symlink target,
    # which that root's rows already walked -- needs no row of its own.
    roots += [os.path.realpath(r) for r in roots]
    notes: list[str] = []
    registry, truncated = _registry_paths(
        [plugins, *seeds], os.path.join(cdir, "settings.json"), notes
    )
    for note in notes:
        _kv("  !! registry file", note)
    outside = [p for p in registry if not any(_inside(p, r) for r in roots)]
    total = f"{len(registry)}{'+ (truncated)' if truncated else ''}"
    _kv("registry paths", f"{total} absolute, {len(outside)} outside the above")
    for path in outside[:_OUTSIDE_CAP]:
        check(f"  {path}", path)
    if len(outside) > _OUTSIDE_CAP:
        _kv("  !! not walked", f"{len(outside) - _OUTSIDE_CAP} more outside paths")
    # Writable state: split per session (or kept writable), so only inventoried.
    for sub in ("projects", "plans"):
        _inventory(f"  {sub}/ (split)", os.path.join(cdir, sub))
    _inventory("  plugins/data/ (writable)", data)
    # The gateway's code must stay read-only to sessions under any option that
    # keeps them at its uid; the image chowns /app to app. The packages it
    # imports live in this interpreter's site-packages (the image's pip target).
    gw = ident["gw"]
    try:
        code = os.readlink(f"/proc/{gw}/cwd")
    except OSError:
        code = "/app"  # the image's WORKDIR; the gateway's cwd is unreadable here
    result["code"] = code
    if os.path.isdir(code):
        writable, checked, capped = _code_writable(
            code, time.monotonic() + _CODE_TIME_CAP
        )
        timed_out |= capped == "time"
        result["code_writable"] = writable > 0
        more = f"+ ({capped}-capped)" if capped else ""
        state = f"{writable} of {checked}{more} entries writable (data/ skipped)"
    else:
        state = "absent"
    _kv(f"  gateway code (PID {gw} cwd)", f"{code}: {state}")
    site = sysconfig.get_paths()["purelib"]
    if not os.path.isdir(site):
        state = "absent"
    elif _can_write(site):
        state = "WRITABLE by this uid"
        result["code_writable"] = True
    else:
        state = "read-only"
    _kv(f"    {site}", state)
    if timed_out:
        _kv("  !! time-capped", "walks stopped at their time cap: partial")
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
    "arch": ("Landlock: untested on this arch (its syscall number differs).",),
    "untested": ("Landlock: untested (the test child could not run).",),
}
_LANDLOCK_DEFAULT = ("Landlock: kernel >=5.19 + 'landlock' in the LSM list.",)


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
        print(f"    docker compose exec -u {_rerun_ids(ident)} gateway ...")
        return
    ids = _rerun_ids(ident)
    if ident.get("rivals"):
        pids = ", ".join([ident["gw"], *ident["rivals"]])
        print(f"  !! several gateway candidates (PID {pids}): F2 and the -u advice")
        print("     are not concluded; find the one whose cmdline names uvicorn/")
        print("     src.main and re-run when it is the only one.")
    elif ident.get("invisible"):
        print("  !! the gateway is INVISIBLE to this probe (hidepid): the F2/uid")
        print(f"     results are not the sessions'. Re-run with `-u {ids}`.")
    elif not ident.get("same_creds") and ident.get("uid"):
        print(f"  !! creds: sessions run as the gateway's {ids}, this probe did not;")
        print(f"     the F2 results above are not theirs. Re-run with `-u {ids}`.")
    ll_ok = ll.get("usable")  # ABI >= 2
    if ll_ok and uid.get("grantable"):
        print("  BEST PATH (a/single-container): uid-per-user + Landlock is buildable.")
        print("    - assumes the root-started entrypoint (the stock one): it forks,")
        print("      before its drop, a broker that holds only SETUID/SETGID and")
        print("      stops/cleans up sessions through a short-lived child switched")
        print("      to the session's uid. A non-root start (compose `user:`) has")
        print("      no root step: file caps on the wrapper then cover the spawn")
        print("      only; stopping and cleaning up sessions stays unsolved.")
        print("    - never in the gateway: KEEPCAPS/ambient caps leak into all it")
        print("      execs. The session drops every cap and sets NoNewPrivs.")
        if uid.get("nnp"):
            print("    - NoNewPrivs=1 here: file caps are out, so this holds only if")
            print("      the entrypoint starts as root (broker).")
        problems = shared.get("problems", [])
        if problems:
            print("    - !! shared assets fail for other uids -- fix before uid split:")
            for problem in problems[:3]:
                print(f"         {_safe(problem)}")
            if len(problems) > 3:
                print(f"         (+{len(problems) - 3} more in SHARED ASSET LAYOUT)")
    elif ll_ok:
        print("  LANDLOCK-ONLY: Landlock is usable; uid switching is not:")
        print("  SETUID/SETGID are missing from the bounding set (cap_drop?).")
    elif ns.get("viable"):
        print("  BWRAP: bwrap/nsjail (userns+pidns+fresh procfs) is viable here.")
        if not ns.get("bwrap"):
            print("    - bubblewrap is not installed in this image.")
        print("    - installing bwrap + socat also switches ON the CLI's own sandbox.")
        if ll.get("cause") in _SECCOMP_CAUSES:
            print("    - Landlock failed only on seccomp: allowing landlock_* in")
            print("      the profile makes the preferred uid-per-user + Landlock")
            print("      reachable.")
    else:
        print("  NO PATH: nothing is fully available as-is. Needed, in order:")
        first, *rest = _LANDLOCK_REMEDY.get(ll.get("cause"), _LANDLOCK_DEFAULT)
        print(f"    - {first}")
        for line in rest:
            print(f"      {line}")
        if uid.get("grantable"):
            print("    - uid-per-user alone is buildable (SETUID/SETGID grantable): it")
            print("      closes F2 and cross-user reads, but without Landlock sessions")
            print("      still read every world-readable path.")
        else:
            print("    - uid-per-user: keep SETUID/SETGID in the bounding set.")
        untested = ns.get("untested", [])
        missing = [
            f"{k}?" if k in untested else k
            for k in ("userns", "pidns", "mountns", "procns")
            if not ns.get(k)
        ]
        print(f"    - OR bwrap (missing: {', '.join(missing)}): relax seccomp")
        print("      (unshare/clone namespace flags AND mount) or add CAP_SYS_ADMIN;")
        print("      under AppArmor docker-default (deny mount) also")
        print("      apparmor=unconfined or a custom profile; install bubblewrap.")
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
        if shared.get("code_writable"):
            code = _safe(shared["code"])
            print(f"  Its ruleset must keep {code} read-only: this uid can write the")
            print("  gateway's code now.")
    gw = ident.get("gw", "1")
    nd = proc.get("non_dumpable")
    state = "unknown" if nd is None else ("yes" if nd else "no")
    if ident.get("rivals"):
        state = "not concluded"
    print(f"\n  /proc/{gw}/environ (F2): closed for sessions by a uid != the")
    print("  gateway's, a new PID ns with its own procfs, or Landlock on the")
    print("  session (every ABI, 5.13+: no scope flag needed). Zero-privilege")
    print("  interim for the gateway itself: PR_SET_DUMPABLE(0) hides its")
    print(f"  environ/mem from same-uid sessions (here: non-dumpable = {state}).")
    print("  Not isolation: exec'd CLI children are dumpable again, so each")
    print("  session's env stays readable to the others.")
    cli = ns.get("cli_sandbox")
    if cli == "inert":
        missing = ", ".join(ns.get("cli_missing", []))
        print(f"\n  CLI OS sandbox: inert here ({missing} missing): the CLI logs")
        print("  'Sandbox disabled' and runs Bash unconfined.")
    elif cli == "broken":
        denied = ", ".join(ns.get("cli_denied", []))
        print("\n  CLI OS sandbox: BROKEN here -- bwrap and socat are present,")
        print(f"  but {denied} denied.")
        print("  Every sandboxed Bash call fails with a bwrap error, and nothing")
        print("  logs 'Sandbox disabled'.")


def main() -> int:
    # A probe killed part-way (seccomp, OOM) must leave what it printed so far.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    # An inherited SIGCHLD=SIG_IGN would auto-reap the test children.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    print("Claude gateway isolation capability probe")
    print("(run inside the prod container as the session/app user)")
    probe_kernel()
    ident = probe_identity()
    caps = probe_caps_seccomp(ident)
    proc = probe_proc_hardening(ident)
    ll = probe_landlock()
    ns = probe_namespaces(caps)
    uid = probe_setuid(caps)
    shared = probe_shared_assets(ident)
    verdict(ident, proc, ll, ns, uid, shared)
    return 0


if __name__ == "__main__":
    sys.exit(main())
