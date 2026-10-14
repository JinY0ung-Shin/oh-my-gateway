"""Shared machinery for the isolation launcher and broker.

Both ``landlock_launcher.py`` (landlock mode: confine in place) and
``isolation_broker.py`` (uid mode: confine the forked child after the uid
switch) must confine with the **same** rules, so the syscall surface, the
access masks and the default system policy live here once rather than being
duplicated across two security-critical files. So do the few things both ends
of a confined spawn must agree on: the per-session temp/cache environment and
the uid-mode status wire format.

stdlib only. This module lives beside the launcher/broker under ``docker/`` —
a fixed, root-owned location in the image — and is imported as a sibling; it is
never loaded from the agent-writable gateway tree (``/app/src``). Callers run
under ``python3 -I`` so the sibling import cannot be redirected.
"""

import ctypes
import os
import signal
import stat
import struct

_MACH = os.uname().machine
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446
_NR_KNOWN = not _MACH.startswith(("alpha", "ia64", "mips"))

_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
# bits 4..12: REMOVE_*/MAKE_* (covered by the full rw mask)
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5

# Handled-access mask per ABI: only bits the kernel knows, or create_ruleset is
# EINVAL. Index by the reported ABI (capped).
_ACCESS_BY_ABI = {
    1: (1 << 13) - 1,  # EXECUTE .. MAKE_SYM
    2: (1 << 14) - 1,  # + REFER
    3: (1 << 15) - 1,  # + TRUNCATE
    5: (1 << 16) - 1,  # + IOCTL_DEV
}

# ABI 6 IPC scoping (struct landlock_ruleset_attr gains handled_access_net and
# scoped). A scoped domain cannot signal, or connect to an abstract unix socket
# bound by, a process outside itself; its own processes still can, and signals
# into it are unaffected. In uid mode that keeps a session from killing its
# supervisor to escape teardown; in both modes it blocks cross-session signals
# and the abstract-socket namespace, which has no DAC at all.
_SCOPE_ABI = 6
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1

# A read-only tree: read + execute + traverse. Landlock's read right does not
# imply execute, so a CLI/plugin exec needs EXECUTE explicitly.
RO_RIGHTS = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
RW_RIGHTS_FULL = (1 << 16) - 1

# The access rights that apply to a *non-directory* file. READ_DIR, REFER and
# the MAKE_*/REMOVE_* bits are directory-only: adding a rule that carries any of
# them for a regular file makes ``landlock_add_rule`` fail with EINVAL, which
# would abort the whole ruleset build and kill every confined spawn. A ro list
# legitimately contains files (e.g. the user ``CLAUDE.md`` shared asset), so a
# rule's rights are narrowed to this mask when its target is not a directory.
_FILE_RIGHTS = (
    _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | _FS_IOCTL_DEV
)

# Default system policy both modes apply. /etc and /usr stay readable on purpose
# (the CLI and libc resolvers read them; a passwd entry is needed for whoami /
# node os.userInfo). Confidentiality comes from what is NOT here: /app, other
# workspaces, other sessions' state. /proc (ro) and /dev/urandom are required or
# the bundled CLI aborts at exec.
SYSTEM_RO = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/proc",
)
RO_FILES = ("/dev/urandom",)
RW_FILES = ("/dev/null", "/dev/zero", "/dev/tty")

_libc = ctypes.CDLL(None, use_errno=True)


class LandlockError(RuntimeError):
    """A ruleset could not be built or enforced; the caller must fail closed."""


def _syscall(nr, *args):
    ctypes.set_errno(0)
    res = _libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(a) for a in args])
    return res, ctypes.get_errno()


def abi():
    """The kernel's Landlock ABI, or a value < 1 when unavailable."""
    if not _NR_KNOWN:
        return -1
    val, _ = _syscall(_NR_CREATE_RULESET, 0, 0, _CREATE_RULESET_VERSION)
    return val


def _rights_mask(a):
    if a >= 5:
        return _ACCESS_BY_ABI[5]
    if a >= 3:
        return _ACCESS_BY_ABI[3]
    return _ACCESS_BY_ABI.get(a, _ACCESS_BY_ABI[2])


def _add_rule(ruleset_fd, path, rights):
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return False  # absent path: nothing to grant (ro) / created first (rw)
    try:
        # A rule on a regular file must carry only file-applicable rights, or
        # the kernel rejects it with EINVAL and the whole build fails. O_PATH
        # fds are fstat-able, so narrow the mask for a non-directory target.
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                rights &= _FILE_RIGHTS
        except OSError:
            pass
        if rights == 0:
            return False
        buf = ctypes.create_string_buffer(struct.pack("=Qi", rights, fd))
        rc, errno = _syscall(
            _NR_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, ctypes.addressof(buf), 0
        )
        if rc != 0:
            raise LandlockError("add_rule failed for %r (errno %d)" % (path, errno))
        return True
    finally:
        os.close(fd)


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


def safe_tmpdir(cwd):
    """``<cwd>/.cli-tmp`` as a real directory, replacing a planted symlink.

    The session's temp dir rides inside the workspace (off the shared /tmp). It
    is **not** added as a Landlock rule — it lies beneath the already-granted
    workspace, so a rule would be redundant, and a rule on an agent-controlled
    path is an escape: ``_add_rule`` opens targets with ``O_PATH`` (follows
    symlinks), so a ``.cli-tmp`` the agent replaced with ``ln -s / .cli-tmp``
    would grant the link's target. Without a rule, Landlock resolves the real
    path at ``open()`` and a symlink out of the workspace is simply denied. We
    still unlink any symlink and remake a real dir so the CLI's temp writes
    land inside the sandbox rather than failing. Caller sets TMPDIR to this.

    Raises ``OSError`` when the dir cannot be made: the CLI cannot start
    without its tmpdir (it exits 1 on the failed ``mkdir``), and swallowing the
    error here hid the cause — e.g. a uid-mode session uid that cannot write an
    app-owned workspace."""
    tmp = os.path.join(cwd, ".cli-tmp")
    if os.path.islink(tmp):
        os.unlink(tmp)
    os.makedirs(tmp, exist_ok=True)
    return tmp


def session_tmp_env(tmp):
    """The env overrides that keep a confined CLI's temp files and tool caches
    in its own session tmp dir (beneath the granted workspace).

    The npm and uv caches must not be the shared, app-owned ``~/.npm`` /
    ``~/.cache/uv``: a package another session planted there is what the next
    ``npx -y`` / ``uv run`` executes. Inherited values are overridden. npm reads
    ``npm_config_*`` case-insensitively and the last spelling in the env wins,
    so both spellings are set."""
    npm_cache = os.path.join(tmp, "npm-cache")
    return {
        "TMPDIR": tmp,
        "CLAUDE_CODE_TMPDIR": tmp,
        "npm_config_cache": npm_cache,
        "NPM_CONFIG_CACHE": npm_cache,
        "UV_CACHE_DIR": os.path.join(tmp, "uv-cache"),
    }


def restore_exec_signals():
    """Reset what Python ignores at startup (SIGPIPE, SIGXFSZ) before ``exec``.

    Ignored dispositions survive ``execve``; the SDK spawns an unconfined CLI
    with them at the default (``restore_signals``), so a confined one should
    start the same way."""
    for name in ("SIGPIPE", "SIGXFSZ"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (OSError, ValueError):
                pass


# --- uid-mode wire format (launcher <-> broker) ------------------------------
#
# The launcher opens with one spawn header: a 4-byte length, then JSON
# ``{"ticket": str, "argv": [str], "env": {str: str}}``, with fds 0/1/2 as
# SCM_RIGHTS on its first byte. Everything after that, in both directions, is
# fixed-size ``(kind, value)`` records. The exit code and the terminating signal
# travel as separate kinds, so no exit code is ever mistaken for a signal.

MAX_HEADER = 8 << 20  # env + argv + the ticket's path lists, JSON-escaped
REC = struct.Struct("=Ii")
REC_RELAY = 1  # launcher -> broker: relay signal ``value`` to the CLI's group
ST_EXITED = 1  # broker -> launcher: the CLI exited with code ``value``
ST_SIGNALED = 2  # broker -> launcher: the CLI was killed by signal ``value``
ST_REFUSED = 3  # broker -> launcher: spawn refused; the CLI never started


_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_CAP_VERSION_3 = 0x20080522


def drop_all_caps():
    """Drop every capability: ambient, then effective/permitted/inheritable.

    Called in the session-uid watcher *before* it forks the CLI (uid mode).
    The broker holds CAP_SETUID/SETGID to switch uid; the session CLI and all
    its descendants must carry none, or a Bash/script could ``setresuid`` to
    any uid and void the DAC/signal isolation. Lowering caps is always
    permitted; a no-op when there are none (same-uid tests). Raises on failure
    so the broker fails closed rather than exec a CLI that kept privilege."""
    _libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)

    class _Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class _Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    hdr = _Header(_CAP_VERSION_3, 0)
    data = (_Data * 2)()  # zero-initialized: drop everything
    if _libc.capset(ctypes.byref(hdr), ctypes.byref(data)) != 0:
        raise LandlockError("capset(drop all) failed (errno %d)" % ctypes.get_errno())


def build_and_restrict(ro_dirs, rw_dirs, ro_files=RO_FILES, rw_files=RW_FILES):
    """Create a ruleset from the given paths, then NO_NEW_PRIVS + restrict_self.

    Requires ABI >= 2 (kernel >= 5.19); raises :class:`LandlockError` otherwise
    so the caller fails closed. Read-write dirs are created first (a rule needs
    an existing path to O_PATH-open); absent read-only paths are skipped. On
    ABI >= 6 the domain is also scoped (signals, abstract unix sockets).
    """
    a = abi()
    if a < 2:
        raise LandlockError(
            "Landlock ABI %d < 2 (need kernel >= 5.19); cannot confine" % a
        )
    handled = _rights_mask(a)
    if a >= _SCOPE_ABI:
        # handled_access_net stays 0: the network is not restricted.
        scoped = _SCOPE_SIGNAL | _SCOPE_ABSTRACT_UNIX_SOCKET
        raw = struct.pack("=QQQ", handled, 0, scoped)
    else:
        raw = struct.pack("=Q", handled)
    attr = ctypes.create_string_buffer(raw)
    ruleset_fd, errno = _syscall(
        _NR_CREATE_RULESET, ctypes.addressof(attr), len(raw), 0
    )
    if ruleset_fd < 0:
        raise LandlockError("create_ruleset failed (errno %d)" % errno)
    try:
        for path in ro_dirs:
            _add_rule(ruleset_fd, path, RO_RIGHTS & handled)
        for path in rw_dirs:
            _ensure_dir(path)
            _add_rule(ruleset_fd, path, RW_RIGHTS_FULL & handled)
        for path in ro_files:
            _add_rule(ruleset_fd, path, _FS_READ_FILE & handled)
        for path in rw_files:
            _add_rule(ruleset_fd, path, (_FS_READ_FILE | _FS_WRITE_FILE) & handled)
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockError(
                "prctl(NO_NEW_PRIVS) failed (errno %d)" % ctypes.get_errno()
            )
        rc, errno = _syscall(_NR_RESTRICT_SELF, ruleset_fd, 0)
        if rc != 0:
            raise LandlockError("restrict_self failed (errno %d)" % errno)
    finally:
        os.close(ruleset_fd)
