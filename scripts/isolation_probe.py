#!/usr/bin/env python3
"""Isolation capability probe for the Claude gateway container.

Run INSIDE the prod gateway container, as the app user that sessions run as:

    python3 -I isolation_probe.py

Read-only and non-destructive: every privileged test (unshare, mount, setuid)
runs in a short-lived forked child that exits immediately; the parent only reads
/proc. Nothing is created, mounted, or changed in the live process.

It answers which per-user isolation mechanism is available in THIS container:
  * uid-per-user (needs CAP_SETUID/SETGID or root)
  * Landlock filesystem confinement (needs kernel >=5.13 + landlock LSM active)
  * bubblewrap/nsjail namespaces (needs unprivileged userns + mount in-userns)
and whether /proc/1/environ is currently readable (the reported finding).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import sys

# ---- syscall numbers by arch -------------------------------------------------
_MACH = platform.machine()
# landlock_* were added together as 444/445/446 on the common arches.
_SYS = {
    "x86_64": {"unshare": 272, "landlock_create_ruleset": 444},
    "aarch64": {"unshare": 97, "landlock_create_ruleset": 444},
    "arm64": {"unshare": 97, "landlock_create_ruleset": 444},
}
_NR = _SYS.get(_MACH, _SYS["x86_64"])

CLONE_NEWNS = 0x00020000
CLONE_NEWPID = 0x20000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def _syscall(nr: int, *args: int) -> tuple[int, int]:
    _libc.syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    cargs = [ctypes.c_long(nr)] + [ctypes.c_long(a) for a in args]
    res = _libc.syscall(*cargs)
    return res, ctypes.get_errno()


def _hdr(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _kv(key: str, val) -> None:
    print(f"  {key:<34} {val}")


# ---- run a test in a forked child, return (rc, errno-name) -------------------
def _in_child(fn) -> tuple[int, str]:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(r)
        try:
            ok, en = fn()
            os.write(w, f"{int(ok)} {en}".encode())
        except BaseException as exc:  # pragma: no cover - defensive
            os.write(w, f"0 EXC:{exc}".encode())
        finally:
            os.close(w)
            os._exit(0)
    os.close(w)
    out = os.read(r, 256).decode()
    os.close(r)
    os.waitpid(pid, 0)
    ok_s, _, en = out.partition(" ")
    return (int(ok_s or 0), en.strip())


def _errname(en: int) -> str:
    try:
        return errno.errorcode.get(en, f"errno {en}")
    except Exception:
        return f"errno {en}"


# ---- individual probes -------------------------------------------------------
def probe_kernel() -> None:
    _hdr("KERNEL / PLATFORM")
    _kv("uname -r", platform.release())
    _kv("machine", _MACH)
    try:
        with open("/proc/version") as f:
            _kv("/proc/version", f.read().strip()[:80])
    except OSError:
        pass
    rel = platform.release().split("-")[0].split(".")
    try:
        major, minor = int(rel[0]), int(rel[1])
        _kv("Landlock kernel floor (>=5.13)", "OK" if (major, minor) >= (5, 13) else "TOO OLD")
    except Exception:
        _kv("Landlock kernel floor", "unknown")


def probe_identity() -> None:
    _hdr("PROCESS IDENTITY")
    _kv("uid / euid", f"{os.getuid()} / {os.geteuid()}")
    _kv("gid / egid", f"{os.getgid()} / {os.getegid()}")
    _kv("running as root", os.geteuid() == 0)


def _decode_caps(hexval: str) -> set[str]:
    bits = int(hexval, 16)
    names = {
        0: "DAC_OVERRIDE", 6: "SETGID", 7: "SETUID",
        19: "SYS_PTRACE", 21: "SYS_ADMIN", 23: "SYS_NICE",
    }
    return {n for b, n in names.items() if bits & (1 << b)}


def probe_caps_seccomp() -> dict:
    _hdr("CAPABILITIES / SECCOMP (from /proc/self/status)")
    info = {}
    eff = bnd = "0"
    seccomp = "?"
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    eff = line.split()[1]
                elif line.startswith("CapBnd:"):
                    bnd = line.split()[1]
                elif line.startswith("Seccomp:"):
                    seccomp = line.split()[1]
                elif line.startswith("Seccomp_filters:"):
                    info["seccomp_filters"] = line.split()[1]
    except OSError as exc:
        _kv("error", exc)
    eff_caps = _decode_caps(eff)
    bnd_caps = _decode_caps(bnd)
    _kv("CapEff (effective)", f"{eff}  -> {sorted(eff_caps) or 'none of interest'}")
    _kv("CapBnd (bounding)", f"{bnd}  -> {sorted(bnd_caps) or 'none of interest'}")
    _kv("Seccomp", {"0": "disabled", "1": "strict", "2": "filter"}.get(seccomp, seccomp))
    _kv("Seccomp_filters", info.get("seccomp_filters", "n/a"))
    info["eff"] = eff_caps
    info["bnd"] = bnd_caps
    return info


def probe_proc_hardening() -> None:
    _hdr("/proc HARDENING")
    hidepid = "not set (cross-uid /proc visible)"
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split()
                mp = parts[4]
                if mp == "/proc":
                    opts = line.rsplit(" - ", 1)[-1]
                    if "hidepid=" in opts:
                        hidepid = [o for o in opts.replace(",", " ").split() if "hidepid" in o]
                    break
    except OSError:
        pass
    _kv("/proc hidepid", hidepid)
    # The reported finding: is PID 1's environ readable from here?
    try:
        with open("/proc/1/comm") as f:
            _kv("/proc/1/comm (PID 1 is)", f.read().strip())
    except OSError as exc:
        _kv("/proc/1/comm", exc)
    try:
        with open("/proc/1/environ", "rb") as f:
            data = f.read(4096)
        names = sorted({b.split(b"=", 1)[0].decode("latin1") for b in data.split(b"\0") if b})
        _kv("/proc/1/environ READABLE", f"YES ({len(names)} vars visible) <-- finding")
        hot = [n for n in names if any(s in n.upper() for s in ("KEY", "TOKEN", "SECRET", "PASS", "URL"))]
        _kv("  secret-ish names in PID1 env", hot[:12])
    except PermissionError:
        _kv("/proc/1/environ READABLE", "NO (EACCES) -- already isolated from PID1")
    except OSError as exc:
        _kv("/proc/1/environ", exc)


def probe_landlock() -> dict:
    _hdr("LANDLOCK (filesystem confinement, no privilege needed)")
    out = {}
    lsm = ""
    try:
        with open("/sys/kernel/security/lsm") as f:
            lsm = f.read().strip()
    except OSError:
        lsm = "(unreadable)"
    _kv("active LSMs (informational)", lsm)
    _kv("landlock in LSM list", "landlock" in lsm if lsm != "(unreadable)" else "unknown")
    # The syscall is authoritative: it returns an ABI version (>=1) only when
    # Landlock is compiled AND active in the running kernel's LSM stack;
    # otherwise -ENOSYS (not compiled) or -EOPNOTSUPP (not in the lsm= list).
    abi, en = _syscall(_NR["landlock_create_ruleset"], 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if abi >= 1:
        _kv("landlock ABI version", f"{abi}  (ABI>=1 => active & usable)")
        out["usable"] = True
    else:
        _kv("landlock_create_ruleset", f"FAILED ({_errname(en)}) => not usable")
        out["usable"] = False
    _kv("=> Landlock usable", out["usable"])
    return out


def probe_namespaces() -> dict:
    _hdr("NAMESPACES (bubblewrap / nsjail viability)")
    out = {}

    def t_userns():
        _, en = _syscall(_NR["unshare"], CLONE_NEWUSER)
        return (en == 0, _errname(en))

    def t_pidns():
        # NEWUSER first grants CAP in the new userns, then NEWPID.
        _, en1 = _syscall(_NR["unshare"], CLONE_NEWUSER)
        if en1 != 0:
            return (False, f"userns:{_errname(en1)}")
        _, en2 = _syscall(_NR["unshare"], CLONE_NEWPID)
        return (en2 == 0, _errname(en2))

    def t_mountns():
        _, en1 = _syscall(_NR["unshare"], CLONE_NEWUSER | CLONE_NEWNS)
        if en1 != 0:
            return (False, f"unshare:{_errname(en1)}")
        # Try a tmpfs mount inside the fresh (userns-owned) mount ns.
        _libc.mount.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = _libc.mount(b"none", b"/tmp", b"tmpfs", 0, None)
        return (rc == 0, _errname(ctypes.get_errno()))

    ok_u, d_u = _in_child(t_userns)
    _kv("unshare(CLONE_NEWUSER)", "OK" if ok_u else f"DENIED ({d_u})")
    ok_p, d_p = _in_child(t_pidns)
    _kv("+ CLONE_NEWPID", "OK" if ok_p else f"DENIED ({d_p})")
    ok_m, d_m = _in_child(t_mountns)
    _kv("+ CLONE_NEWNS + mount tmpfs", "OK" if ok_m else f"DENIED ({d_m})")
    out["userns"] = ok_u
    out["mountns"] = ok_m
    _kv("=> bwrap/nsjail viable (needs all 3)", ok_u and ok_p and ok_m)
    return out


def probe_setuid(caps: dict) -> dict:
    _hdr("UID SWITCHING (uid-per-user viability)")
    out = {}
    can = os.geteuid() == 0 or "SETUID" in caps.get("eff", set())

    def t_setuid():
        try:
            os.setgid(65534)
            os.setuid(65534)  # nobody
            return (os.getuid() == 65534, "ok")
        except OSError as exc:
            return (False, _errname(exc.errno))

    if can:
        ok, d = _in_child(t_setuid)
        _kv("fork+setgid/setuid(nobody)", "OK" if ok else f"FAILED ({d})")
        out["usable"] = ok
    else:
        _kv("CAP_SETUID present", False)
        _kv("fork+setuid test", "skipped (no privilege) — needs cap_add: [SETUID,SETGID]")
        out["usable"] = False
    out["grantable"] = "SETUID" in caps.get("bnd", set())
    _kv("SETUID in bounding set (grantable)", out["grantable"])
    _kv("=> uid-per-user usable now", out["usable"])
    return out


def probe_shared_assets() -> None:
    _hdr("SHARED ASSET LAYOUT (must stay readable by every user)")
    home = os.environ.get("HOME", "")
    cdir = os.path.join(home, ".claude")
    _kv("HOME", home or "(unset)")
    _kv(".claude dir", cdir)
    for sub in ("plugins", "skills", "agents", "commands", "projects", "plans"):
        p = os.path.join(cdir, sub)
        try:
            st = os.stat(p)
            others_read = bool(st.st_mode & 0o004)
            _kv(f"  {sub}/", f"mode={oct(st.st_mode & 0o777)} uid={st.st_uid} "
                             f"world-read={others_read} entries={len(os.listdir(p)) if os.path.isdir(p) else '-'}")
        except OSError as exc:
            _kv(f"  {sub}/", f"absent/err ({exc.errno})")
    print("  note: under isolation, plugins/skills/agents/commands must be a")
    print("        read-only tree every session uid/jail is granted ro access to;")
    print("        only projects/plans/workspace + SDK-written state get split.")


def verdict(ll: dict, ns: dict, uid: dict) -> None:
    _hdr("VERDICT")
    if uid.get("usable") and ll.get("usable"):
        print("  BEST PATH (a/single-container): uid-per-user + Landlock is READY now.")
    elif (uid.get("grantable") or uid.get("usable")) and ll.get("usable"):
        print("  BEST PATH (a/single-container): uid-per-user + Landlock.")
        if not uid.get("usable"):
            print("    - add  cap_add: [SETUID, SETGID]  to the gateway container.")
    elif ns.get("userns") and ns.get("mountns"):
        print("  bwrap/nsjail namespaces are viable here; uid+Landlock preferred if")
        print("  Landlock can be enabled (lighter, no mount-ns / seccomp changes).")
    else:
        print("  Neither path is fully available as-is. Needed, in order of preference:")
        if not ll.get("usable"):
            print("    - Landlock: kernel >=5.13 with 'landlock' in the active LSM list.")
        if not (uid.get("usable") or uid.get("grantable")):
            print("    - CAP_SETUID/SETGID: grant via cap_add (narrow) for uid-per-user.")
        if not ns.get("mountns"):
            print("    - OR relax seccomp / add CAP_SYS_ADMIN for bwrap (widens surface).")
    print("\n  /proc/1/environ: fixed the moment sessions run as a uid != the gateway's.")


def main() -> int:
    print("Claude gateway isolation capability probe")
    print("(run inside the prod container as the session/app user)")
    probe_kernel()
    probe_identity()
    caps = probe_caps_seccomp()
    probe_proc_hardening()
    ll = probe_landlock()
    ns = probe_namespaces()
    uid = probe_setuid(caps)
    probe_shared_assets()
    verdict(ll, ns, uid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
