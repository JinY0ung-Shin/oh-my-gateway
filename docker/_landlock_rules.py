"""Shared Landlock ruleset machinery for the isolation launcher and broker.

Both ``landlock_launcher.py`` (landlock mode: confine in place) and
``isolation_broker.py`` (uid mode: confine the forked child after the uid
switch) must confine with the **same** rules, so the syscall surface, the
access masks and the default system policy live here once rather than being
duplicated across two security-critical files.

stdlib only. This module lives beside the launcher/broker under ``docker/`` —
a fixed, root-owned location in the image — and is imported as a sibling; it is
never loaded from the agent-writable gateway tree (``/app/src``). Callers run
under ``python3 -I`` so the sibling import cannot be redirected.
"""

import ctypes
import os
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

# A read-only tree: read + execute + traverse. Landlock's read right does not
# imply execute, so a CLI/plugin exec needs EXECUTE explicitly.
RO_RIGHTS = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
RW_RIGHTS_FULL = (1 << 16) - 1

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


def build_and_restrict(ro_dirs, rw_dirs, ro_files=RO_FILES, rw_files=RW_FILES):
    """Create a ruleset from the given paths, then NO_NEW_PRIVS + restrict_self.

    Requires ABI >= 2 (kernel >= 5.19); raises :class:`LandlockError` otherwise
    so the caller fails closed. Read-write dirs are created first (a rule needs
    an existing path to O_PATH-open); absent read-only paths are skipped.
    """
    a = abi()
    if a < 2:
        raise LandlockError(
            "Landlock ABI %d < 2 (need kernel >= 5.19); cannot confine" % a
        )
    handled = _rights_mask(a)
    attr = ctypes.create_string_buffer(struct.pack("=Q", handled))
    ruleset_fd, errno = _syscall(_NR_CREATE_RULESET, ctypes.addressof(attr), 8, 0)
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
