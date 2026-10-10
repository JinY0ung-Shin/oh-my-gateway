#!/usr/bin/env python3
"""Isolation capability probe for the Claude gateway container.

Run INSIDE the prod gateway container, as the app user that sessions run as:

    python3 -I isolation_probe.py

Read-only and non-destructive: every privileged test (unshare, mount, setuid)
runs in a short-lived forked child that exits immediately; the parent only reads
/proc. Nothing is created, mounted, or changed in the live process.

It answers which per-user isolation mechanism is available in THIS container:
  * uid-per-user (needs CAP_SETUID/SETGID kept across the root->app drop, or root)
  * Landlock filesystem confinement (needs kernel >=5.19 for ABI 2 + landlock LSM)
  * bubblewrap/nsjail namespaces (needs userns + a fresh procfs in a new PID ns)
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
        # ABI 2 (LANDLOCK_ACCESS_FS_REFER, cross-dir rename/link) needs 5.19.
        floor = "OK" if (major, minor) >= (5, 19) else "TOO OLD (need >=5.19)"
        _kv("Landlock ABI-2 floor (>=5.19)", floor)
    except Exception:
        _kv("Landlock kernel floor", "unknown")


def probe_identity() -> None:
    _hdr("PROCESS IDENTITY")
    _kv("uid / euid", f"{os.getuid()} / {os.geteuid()}")
    _kv("gid / egid", f"{os.getgid()} / {os.getegid()}")
    _kv("running as root", os.geteuid() == 0)
    # This probe is only meaningful run AS the session/app user. Warn loudly if
    # it runs as root (forgot `-u app`) or as a uid that already differs from
    # PID 1's -- either inverts the F2 / uid verdicts below.
    try:
        pid1_uid = os.stat("/proc/1").st_uid
        _kv("PID 1 uid", pid1_uid)
        if os.geteuid() == 0:
            _kv(
                "!! WARNING",
                "running as root -- use `docker compose exec -u app`; "
                "setuid/F2 results will be wrong",
            )
        elif os.geteuid() != pid1_uid:
            _kv(
                "!! WARNING",
                f"euid {os.geteuid()} != PID1 uid {pid1_uid}: not the gateway's "
                "uid; F2/uid verdicts do not reflect real sessions",
            )
    except OSError as exc:
        _kv("PID 1 uid", exc)


def _decode_caps(hexval: str) -> set[str]:
    bits = int(hexval, 16)
    # Bit numbers per linux/capability.h: CAP_CHOWN is 0, DAC_OVERRIDE is 1.
    names = {
        0: "CHOWN", 1: "DAC_OVERRIDE", 2: "DAC_READ_SEARCH", 5: "KILL",
        6: "SETGID", 7: "SETUID", 19: "SYS_PTRACE", 21: "SYS_ADMIN",
        23: "SYS_NICE",
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
                elif line.startswith("NoNewPrivs:"):
                    info["nnp"] = line.split()[1]
    except OSError as exc:
        _kv("error", exc)
    eff_caps = _decode_caps(eff)
    bnd_caps = _decode_caps(bnd)
    _kv("CapEff (effective)", f"{eff}  -> {sorted(eff_caps) or 'none of interest'}")
    _kv("CapBnd (bounding)", f"{bnd}  -> {sorted(bnd_caps) or 'none of interest'}")
    modes = {"0": "disabled", "1": "strict", "2": "filter"}
    _kv("Seccomp", modes.get(seccomp, seccomp))
    _kv("Seccomp_filters", info.get("seccomp_filters", "n/a"))
    _kv("NoNewPrivs", info.get("nnp", "?"))
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
                        hidepid = [
                            o for o in opts.replace(",", " ").split() if "hidepid" in o
                        ]
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
            data = f.read()  # read to EOF; prod env easily exceeds one 4 KiB page
        names = sorted(
            {b.split(b"=", 1)[0].decode("latin1") for b in data.split(b"\0") if b}
        )
        _kv("/proc/1/environ READABLE", f"YES ({len(names)} vars visible) <-- finding")
        hot = [
            n
            for n in names
            if any(s in n.upper() for s in ("KEY", "TOKEN", "SECRET", "PASS", "URL"))
        ]
        _kv("  secret-ish names in PID1 env", hot[:20])
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
    in_list = "landlock" in lsm if lsm != "(unreadable)" else "unknown"
    _kv("landlock in LSM list", in_list)
    # The syscall is authoritative: it returns an ABI version (>=1) only when
    # Landlock is compiled AND active in the running kernel's LSM stack.
    # ENOSYS = not compiled; EOPNOTSUPP = not in the lsm= list; EPERM = a seccomp
    # filter blocks the syscall (fix the seccomp profile, not the kernel/LSM).
    abi, en = _syscall(
        _NR["landlock_create_ruleset"], 0, 0, LANDLOCK_CREATE_RULESET_VERSION
    )
    out["abi"] = abi if abi >= 1 else 0
    out["seccomp_blocked"] = en == errno.EPERM
    if abi >= 2:
        _kv("landlock ABI version", f"{abi}  (>=2: cross-dir rename/link confinable)")
        out["usable"] = True
    elif abi == 1:
        # ABI 1 (kernel 5.13-5.18) lacks LANDLOCK_ACCESS_FS_REFER, so ANY ruleset
        # forces cross-directory rename/link to EXDEV: git object finalize,
        # package installs, `mv a/x b/` all break inside sessions.
        _kv("landlock ABI version", "1  (no REFER: cross-dir rename/link => EXDEV)")
        out["usable"] = False
    elif out["seccomp_blocked"]:
        _kv("landlock_create_ruleset", "FAILED (EPERM) => blocked by seccomp")
        out["usable"] = False
    else:
        _kv("landlock_create_ruleset", f"FAILED ({_errname(en)}) => not usable")
        out["usable"] = False
    _kv("=> Landlock usable (ABI>=2)", out["usable"])
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

    def t_proc_in_pidns():
        # ②'s F2 fix needs a FRESH procfs in a new PID ns. A tmpfs mount
        # succeeding is NOT the same test: Docker masks parts of /proc, which
        # usually makes `mount -t proc` fail (EPERM) in a userns even when tmpfs
        # works. Fork into the new PID ns (child is PID 1 there) and mount proc.
        _, en1 = _syscall(_NR["unshare"], CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID)
        if en1 != 0:
            return (False, f"unshare:{_errname(en1)}")
        r2, w2 = os.pipe()
        pid = os.fork()
        if pid == 0:  # PID 1 of the new pid ns
            os.close(r2)
            _libc.mount.restype = ctypes.c_int
            ctypes.set_errno(0)
            rc = _libc.mount(b"proc", b"/proc", b"proc", 0, None)
            en = ctypes.get_errno()
            os.write(w2, f"{int(rc == 0)} {_errname(en)}".encode())
            os.close(w2)
            os._exit(0)
        os.close(w2)
        msg = os.read(r2, 64).decode()
        os.close(r2)
        os.waitpid(pid, 0)
        ok_s, _, en = msg.partition(" ")
        return (int(ok_s or 0) == 1, en.strip())

    ok_u, d_u = _in_child(t_userns)
    _kv("unshare(CLONE_NEWUSER)", "OK" if ok_u else f"DENIED ({d_u})")
    ok_p, d_p = _in_child(t_pidns)
    _kv("+ CLONE_NEWPID", "OK" if ok_p else f"DENIED ({d_p})")
    ok_m, d_m = _in_child(t_mountns)
    _kv("+ CLONE_NEWNS + mount tmpfs", "OK" if ok_m else f"DENIED ({d_m})")
    ok_pm, d_pm = _in_child(t_proc_in_pidns)
    _kv("+ mount proc in new PID ns", "OK" if ok_pm else f"DENIED ({d_pm})")
    out["userns"] = ok_u
    out["pidns"] = ok_p
    out["mountns"] = ok_m
    out["procns"] = ok_pm
    # ② needs userns + a new PID ns + a FRESH procfs in it (the actual F2 fix).
    out["viable"] = ok_u and ok_p and ok_m and ok_pm
    _kv("=> bwrap/nsjail viable (userns+pidns+proc)", out["viable"])
    return out


def probe_setuid(caps: dict) -> dict:
    _hdr("UID SWITCHING (uid-per-user viability)")
    out = {}
    eff = caps.get("eff", set())
    # t_setuid calls setgid() THEN setuid(), so BOTH caps are required (unless root).
    can = os.geteuid() == 0 or {"SETUID", "SETGID"} <= eff

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
        present = sorted({"SETUID", "SETGID"} & eff) or "neither"
        _kv("CAP_SETUID/SETGID effective", present)
        _kv("fork+setuid test", "skipped (not both caps effective)")
        out["usable"] = False
    # NOTE: SETUID/SETGID in the BOUNDING set is not enough here.
    # docker/entrypoint.py switches root->1000, which clears every effective and
    # permitted cap, so a bare `cap_add: [SETUID, SETGID]` leaves CapEff empty.
    # Making uid-per-user work needs the caps kept ACROSS that drop (prctl
    # KEEPCAPS + ambient) or a setuid-root helper -- and a setuid helper is
    # blocked when NoNewPrivs=1.
    out["grantable"] = {"SETUID", "SETGID"} <= caps.get("bnd", set())
    out["nnp"] = caps.get("nnp") == "1"
    _kv("SETUID+SETGID in bounding set", out["grantable"])
    _kv(
        "  (bounding alone insufficient)",
        "entrypoint root->1000 clears CapEff; needs ambient/keepcaps or helper",
    )
    _kv("NoNewPrivs (blocks setuid helper)", out["nnp"])
    _kv("=> uid-per-user usable now", out["usable"])
    return out


def probe_shared_assets() -> None:
    _hdr("SHARED ASSET LAYOUT (must stay readable by every user)")
    home = os.environ.get("HOME", "")
    cdir_env = os.environ.get("CLAUDE_CONFIG_DIR", "")
    cdir = cdir_env or os.path.join(home, ".claude")
    _kv("HOME", home or "(unset)")
    _kv("CLAUDE_CONFIG_DIR", cdir_env or "(unset -> HOME/.claude)")
    _kv(".claude dir", cdir)
    shared = ("plugins", "skills", "agents", "commands", "output-styles")
    split = ("projects", "plans")
    for sub in shared + split:
        p = os.path.join(cdir, sub)
        try:
            st = os.stat(p)
            others_read = bool(st.st_mode & 0o004)
            entries = len(os.listdir(p)) if os.path.isdir(p) else "-"
            _kv(
                f"  {sub}/",
                f"mode={oct(st.st_mode & 0o777)} uid={st.st_uid} "
                f"world-read={others_read} entries={entries}",
            )
        except OSError as exc:
            _kv(f"  {sub}/", f"absent/err ({exc.errno})")
    # settings.json is SHARED POLICY (admin env block + enabledPlugins), not
    # per-session writable state -- flag its ownership too.
    sp = os.path.join(cdir, "settings.json")
    try:
        st = os.stat(sp)
        mode = oct(st.st_mode & 0o777)
        _kv("  settings.json (shared policy)", f"mode={mode} uid={st.st_uid}")
    except OSError as exc:
        _kv("  settings.json", f"absent/err ({exc.errno})")
    print("  note: plugins/skills/agents/commands/output-styles (+ settings.json)")
    print("        must stay a read-only tree every session uid/jail can read;")
    print("        only projects/plans/workspace + SDK cache/history get split.")


def verdict(ll: dict, ns: dict, uid: dict) -> None:
    _hdr("VERDICT")
    ll_ok = ll.get("usable")  # ABI >= 2
    if uid.get("usable") and ll_ok:
        print("  BEST PATH (a/single-container): uid-per-user + Landlock is READY now.")
        print("    - still drop ambient/permitted caps + set NoNewPrivs before exec,")
        print("      and give the gateway CAP_KILL (or a helper) to reap children.")
    elif ll_ok and not uid.get("usable"):
        print("  Landlock is usable; uid switching is NOT effective yet.")
        print("    - cap_add alone will NOT work: entrypoint root->1000 clears CapEff.")
        print("      Keep caps across the drop (ambient) or add a setuid helper.")
        if uid.get("nnp"):
            print("      (NoNewPrivs=1 is set, which BLOCKS a setuid-root helper).")
        print("    - OR interim: Landlock-ONLY (no uid switch, no caps). It confines")
        print("      filesystem reads and (new kernel) hides /proc/1/environ.")
    elif ns.get("viable"):
        print("  bwrap/nsjail (userns+pidns+fresh procfs) is viable here.")
        print("  uid+Landlock is preferred if Landlock can be enabled (lighter; no")
        print("  mount-ns / seccomp changes).")
    else:
        print("  No path is fully available as-is. Needed, in order of preference:")
        if not ll_ok:
            if ll.get("seccomp_blocked"):
                print("    - Landlock: allowed by kernel but seccomp-BLOCKED -- allow")
                print("      the landlock_* syscalls in the seccomp profile.")
            elif ll.get("abi") == 1:
                print("    - Landlock: kernel has ABI 1 only; need >=5.19 (ABI 2) so")
                print("      cross-dir rename/link is not forced to EXDEV.")
            else:
                print("    - Landlock: kernel >=5.19 + 'landlock' in the LSM list.")
        if not uid.get("usable"):
            print("    - uid-per-user: keep CAP_SETUID/SETGID across root->1000")
            print("      (ambient) or add a setuid helper; bare cap_add is not enough.")
        if not ns.get("viable"):
            missing = [
                k for k in ("userns", "pidns", "mountns", "procns") if not ns.get(k)
            ]
            print(
                "    - OR for bwrap: relax seccomp / add CAP_SYS_ADMIN (missing: "
                f"{', '.join(missing) or 'none'}); widens the outer surface."
            )
    print("\n  /proc/1/environ: fixed by a uid != the gateway's, a new PID ns, OR")
    print("  Landlock ptrace-scoping on a new enough kernel.")


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
