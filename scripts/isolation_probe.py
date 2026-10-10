#!/usr/bin/env python3
"""Isolation capability probe for the Claude gateway container.

Run INSIDE the prod gateway container, as the app user that sessions run as
(`docker compose exec` attaches as root here unless given `-u app`):

    docker compose exec -u app gateway python3 -I /tmp/isolation_probe.py

Read-only and non-destructive: every privileged test (unshare, mount, setuid)
runs in a short-lived forked child that exits immediately; the parent only reads
/proc, /sys and the ~/.claude tree. Nothing is created, mounted, or changed in
the live process.

It answers which per-user isolation mechanism is available in THIS container:
  * uid-per-user (needs CAP_SETUID/SETGID kept across the root->app drop, or root)
  * Landlock filesystem confinement (needs kernel >=5.19 for ABI 2 + landlock LSM)
  * bubblewrap/nsjail namespaces (needs userns + a fresh procfs in a new PID ns)
and whether /proc/1/environ is currently readable (the reported finding), and
whether a session under its own uid could still read the shared plugin/skill tree.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import stat
import sys

# ---- syscall numbers ---------------------------------------------------------
_MACH = platform.machine()
# unshare() goes through libc's wrapper, which knows this arch's number. Landlock
# has none, but syscalls added since the 5.1 table unification share one number on
# every arch except alpha/ia64/mips (they add an ABI offset), where the probe
# reports Landlock as unknown rather than issue the wrong syscall.
_NR_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_NR_KNOWN = not _MACH.startswith(("alpha", "ia64", "mips"))

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


def _unshare(flags: int) -> int:
    """unshare(2) through libc's wrapper; 0 on success, else the errno."""
    ctypes.set_errno(0)
    if _libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno()


def _hdr(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def _kv(key: str, val) -> None:
    print(f"  {key:<34} {val}")


# ---- run a test in a forked child, return (ok, errno-name) -------------------
def _in_child(fn) -> tuple[bool, str]:
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
    return (ok_s == "1", en.strip())


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


def probe_identity() -> dict:
    _hdr("PROCESS IDENTITY")
    ident = {"root": os.geteuid() == 0, "pid1_uid": None}
    _kv("uid / euid", f"{os.getuid()} / {os.geteuid()}")
    _kv("gid / egid", f"{os.getgid()} / {os.getegid()}")
    _kv("running as root", ident["root"])
    # This probe is only meaningful run AS the session/app user. Warn loudly if
    # it runs as root (forgot `-u app`) or as a uid that already differs from
    # PID 1's -- either inverts the F2 / uid verdicts below.
    try:
        ident["pid1_uid"] = os.stat("/proc/1").st_uid
        _kv("PID 1 uid", ident["pid1_uid"])
    except OSError as exc:
        _kv("PID 1 uid", exc)
    if ident["root"]:
        _kv(
            "!! WARNING",
            "running as root -- use `docker compose exec -u app`; "
            "setuid/F2 results are wrong, so no VERDICT is given",
        )
    elif ident["pid1_uid"] is not None and os.geteuid() != ident["pid1_uid"]:
        _kv(
            "!! WARNING",
            f"euid {os.geteuid()} != PID1 uid {ident['pid1_uid']}: not the "
            "gateway's uid; F2/uid verdicts do not reflect real sessions",
        )
    return ident


def _decode_caps(hexval: str) -> set[str]:
    bits = int(hexval, 16)
    # Bit numbers per linux/capability.h: CAP_CHOWN is 0, DAC_OVERRIDE is 1.
    names = {
        0: "CHOWN",
        1: "DAC_OVERRIDE",
        2: "DAC_READ_SEARCH",
        5: "KILL",
        6: "SETGID",
        7: "SETUID",
        19: "SYS_PTRACE",
        21: "SYS_ADMIN",
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
                        hidepid = " ".join(
                            o for o in opts.replace(",", " ").split() if "hidepid" in o
                        )
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
    if not _LANDLOCK_NR_KNOWN:
        _kv(
            "landlock_create_ruleset",
            f"NOT TRIED (syscall number differs on {_MACH})",
        )
        out.update(abi=0, seccomp_blocked=False, usable=False)
        _kv("=> Landlock usable (ABI>=2)", "unknown")
        return out
    # The syscall is authoritative: it returns an ABI version (>=1) only when
    # Landlock is compiled AND active in the running kernel's LSM stack.
    # ENOSYS = not compiled; EOPNOTSUPP = not in the lsm= list; EPERM = a seccomp
    # filter blocks the syscall (fix the seccomp profile, not the kernel/LSM).
    abi, en = _syscall(
        _NR_LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION
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
        _libc.mount.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = _libc.mount(b"none", b"/tmp", b"tmpfs", 0, None)
        return (rc == 0, _errname(ctypes.get_errno()))

    def t_proc_in_pidns():
        # ②'s F2 fix needs a FRESH procfs in a new PID ns. A tmpfs mount
        # succeeding is NOT the same test: Docker masks parts of /proc, which
        # usually makes `mount -t proc` fail (EPERM) in a userns even when tmpfs
        # works. Fork into the new PID ns (child is PID 1 there) and mount proc.
        en1 = _unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID)
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


def _other_uid_block(path: str) -> str:
    """Where a session under another uid is stopped on its way to `path`.

    Such a uid gets only the "other" bits (unless a shared group is arranged):
    o+x on every ancestor dir -- of the literal path and of its symlink target --
    then o+rx on a dir or o+r on a file. Returns "" when nothing blocks it.
    """
    for p in dict.fromkeys((os.path.abspath(path), os.path.realpath(path))):
        parts = p.strip("/").split("/")
        for i in range(len(parts) + 1):
            cur = "/" + "/".join(parts[:i])
            st = os.stat(cur)
            if i < len(parts):
                need = 0o001
            elif stat.S_ISDIR(st.st_mode):
                need = 0o005
            else:
                need = 0o004
            if (st.st_mode & need) != need:
                return f"{cur} ({oct(st.st_mode & 0o777)}, uid {st.st_uid})"
    return ""


def _private_entries(root: str, limit: int = 50000) -> tuple[list[str], bool]:
    """Entries under `root` that a session under another uid could not use.

    Same "other" bits as above: a dir needs o+rx, a file o+r, and an
    owner-executable file (hook/MCP launcher) o+rx. Returns (entries, capped);
    the walk stops after `limit` entries so a huge plugin tree cannot stall it.
    """
    bad: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            seen += 1
            if seen > limit:
                return bad, True
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue  # a symlink's own mode bits are never checked
            needs_x = stat.S_ISDIR(st.st_mode) or bool(st.st_mode & 0o100)
            need = 0o005 if needs_x else 0o004
            if (st.st_mode & need) != need:
                bad.append(f"{path} ({oct(st.st_mode & 0o777)})")
    return bad, False


def _report_shared(label: str, path: str) -> None:
    """One shared asset: its mode, and whether a session under another uid can
    reach it and use everything inside."""
    try:
        st = os.stat(path)
        is_dir = stat.S_ISDIR(st.st_mode)
        entries = f" entries={len(os.listdir(path))}" if is_dir else ""
        block = _other_uid_block(path)
    except OSError as exc:
        _kv(label, f"absent/err ({exc.errno})")
        return
    _kv(label, f"mode={oct(st.st_mode & 0o777)} uid={st.st_uid}{entries}")
    _kv("    other uids reach it", f"NO -- blocked at {block}" if block else "yes")
    if is_dir:
        bad, capped = _private_entries(path)
        count = f"{len(bad)}+ (walk capped)" if capped else str(len(bad))
        eg = f"  e.g. {', '.join(bad[:3])}" if bad else ""
        _kv("    entries they cannot use", count + eg)


def probe_shared_assets() -> None:
    _hdr("SHARED ASSET LAYOUT (must stay readable by every user)")
    home = os.environ.get("HOME", "")
    cdir_env = os.environ.get("CLAUDE_CONFIG_DIR", "")
    cdir = cdir_env or os.path.join(home, ".claude")
    _kv("HOME", home or "(unset)")
    _kv("CLAUDE_CONFIG_DIR", cdir_env or "(unset -> HOME/.claude)")
    _kv(".claude dir", cdir)
    for sub in ("plugins", "skills", "agents", "commands", "output-styles"):
        _report_shared(f"  {sub}/", os.path.join(cdir, sub))
    # settings.json is SHARED POLICY (admin env block + enabledPlugins), not
    # per-session writable state, so it -- like the user-scope CLAUDE.md -- must
    # stay readable by every session too.
    for name in ("settings.json", "CLAUDE.md"):
        _report_shared(f"  {name}", os.path.join(cdir, name))
    # Writable per-session state: split per session, so only inventoried here.
    for sub in ("projects", "plans"):
        p = os.path.join(cdir, sub)
        try:
            st = os.stat(p)
            n = len(os.listdir(p))
        except OSError as exc:
            _kv(f"  {sub}/ (split)", f"absent/err ({exc.errno})")
            continue
        mode = oct(st.st_mode & 0o777)
        _kv(f"  {sub}/ (split)", f"mode={mode} uid={st.st_uid} entries={n}")
    print("  note: everything above but projects/plans must stay readable by every")
    print("        session uid/jail; only projects/plans/workspace + SDK")
    print("        cache/history get split. 'other uids' = a session uid that is")
    print("        neither owner nor in the group: it needs o+x on EVERY ancestor,")
    print("        so a 0700 HOME blocks all of it under uid-per-user (a bwrap")
    print("        ro-bind skips the ancestors, not the modes inside).")


def verdict(ident: dict, ll: dict, ns: dict, uid: dict) -> None:
    _hdr("VERDICT")
    if ident.get("root"):
        # root can always setuid and holds the container's caps, so the uid
        # branch below would read READY for sessions that cannot setuid at all.
        print("  NONE: this ran as root, which can always setuid, so the uid/caps")
        print("  results above are root's, not a session's (app uid, CapEff empty).")
        print("  Re-run as the session user: docker compose exec -u app gateway ...")
        return
    euid, pid1_uid = os.geteuid(), ident.get("pid1_uid")
    if pid1_uid is not None and euid != pid1_uid:
        print(f"  !! euid {euid} != PID 1 uid {pid1_uid}: the results above may not")
        print("     reflect real sessions (see PROCESS IDENTITY).")
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
    ident = probe_identity()
    caps = probe_caps_seccomp()
    probe_proc_hardening()
    ll = probe_landlock()
    ns = probe_namespaces()
    uid = probe_setuid(caps)
    probe_shared_assets()
    verdict(ident, ll, ns, uid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
