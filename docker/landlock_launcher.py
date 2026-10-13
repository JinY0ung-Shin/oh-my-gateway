#!/usr/bin/env python3
"""Landlock confinement launcher for Claude CLI children (issue #241, PR-A).

The gateway points ``cli_path`` at this fixed, root-owned stub instead of at the
real CLI. The SDK execs it as ``[launcher, *cli_args]``; the launcher applies a
Landlock ruleset to itself and then ``execv``s the real CLI with the same argv.
Landlock is inherited across ``execve``, so every descendant — the CLI, its Bash
tool, a script the agent wrote and runs, stdio MCP servers — is confined at the
``open()`` layer regardless of how a path string is built. That is what closes
the "write a script, then run it" read path (F1) the command-line checks cannot.

This is the no-privilege interim of ``docs/security-process-isolation.md`` §3:
no uid switch, so no broker and no capabilities. It confines filesystem reads
and writes; it does **not** give DAC or signal isolation between same-uid
sessions (that is the uid-per-user follow-up, PR-C).

Design constraints (see the design doc):

* **Fail closed.** A missing real CLI, an unusable Landlock (ABI < 2, or the
  enforce chain refused), or a ruleset that cannot be built aborts the spawn
  with a non-zero exit. The gateway must never fall through to an unconfined
  CLI once it has decided to confine.
* **Default deny.** Only paths the gateway lists (plus a minimal always-on set
  the CLI needs to start) are granted. ``/app`` (gateway code and
  ``/app/data`` credentials) is simply never listed.
* **Isolated interpreter.** The launcher starts in the agent-controlled
  workspace cwd, so it re-execs itself under ``python3 -I`` before importing
  anything beyond the two builtins used for that guard — a planted module in
  the workspace must never be importable.
* stdlib only, like ``scripts/isolation_probe.py``.
"""

import os
import sys

# Re-exec under an isolated interpreter before importing anything else: the cwd
# is the agent's workspace. ``sys``/``os`` resolve from the launcher's own
# (fixed, root-owned) directory and the stdlib, never the cwd, so this guard is
# safe to run first. ``-I`` drops cwd/env/user-site injection for the real run.
if not sys.flags.isolated:
    os.execv(
        sys.executable,
        [sys.executable, "-I", os.path.realpath(__file__), *sys.argv[1:]],
    )

import ctypes
import struct

# --- Landlock syscall surface (mirrors scripts/isolation_probe.py) -----------

_MACH = os.uname().machine
# The syscall numbers are identical on the architectures the image runs on
# (x86-64, aarch64). A few historical arches renumber them; refuse there rather
# than issue a wrong syscall.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446
_NR_KNOWN = not _MACH.startswith(("alpha", "ia64", "mips"))

_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# Filesystem access rights, by the ABI that introduced them. The handled set
# passed to create_ruleset must contain only bits the kernel knows, or it is
# EINVAL, so we cap the mask at the reported ABI.
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5

# Access the handled mask reaches, per ABI. Index by min(abi, len-1).
_ACCESS_BY_ABI = {
    1: (1 << 13) - 1,  # EXECUTE .. MAKE_SYM
    2: (1 << 14) - 1,  # + REFER
    3: (1 << 15) - 1,  # + TRUNCATE
    5: (1 << 16) - 1,  # + IOCTL_DEV
}

# What we grant a read-only tree: read + execute + traverse. Landlock's read
# right does not imply execute, so a CLI/plugin exec needs EXECUTE explicitly.
_RO_RIGHTS = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
# What we grant a writable tree: everything (capped at the ABI in _rights_mask).
_RW_RIGHTS_FULL = (1 << 16) - 1

_libc = ctypes.CDLL(None, use_errno=True)


def _die(msg):
    sys.stderr.write("landlock_launcher: %s\n" % msg)
    raise SystemExit(70)


def _syscall(nr, *args):
    ctypes.set_errno(0)
    res = _libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(a) for a in args])
    return res, ctypes.get_errno()


def _abi():
    if not _NR_KNOWN:
        _die("Landlock syscall numbers unknown on %s; refusing to spawn" % _MACH)
    abi, errno = _syscall(_NR_CREATE_RULESET, 0, 0, _CREATE_RULESET_VERSION)
    if abi < 0:
        _die("Landlock unavailable (create_ruleset query: errno %d)" % errno)
    if abi < 2:
        # ABI 1 forces EXDEV on every cross-directory rename/link (no REFER),
        # which breaks git/uv/npm inside the session. The design treats it as
        # unusable; the kernel floor is 5.19.
        _die("Landlock ABI %d < 2 (need kernel >= 5.19); refusing to spawn" % abi)
    return abi


def _rights_mask(abi):
    return _ACCESS_BY_ABI[min(abi, 5) if abi >= 5 else (3 if abi >= 3 else abi)]


def _parse_paths(var):
    raw = os.environ.get(var, "")
    return [p for p in raw.split(os.pathsep) if p]


def _add_rule(ruleset_fd, path, rights):
    """Add one path-beneath rule; return True if it was added.

    A path that does not exist (or cannot be O_PATH-opened) is skipped: we
    cannot grant access to something absent, and for a read-only asset that is
    simply "nothing to allow". Writable dirs are created first by the caller.
    """
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        rule = struct.pack("=Qi", rights, fd)
        buf = ctypes.create_string_buffer(rule)
        rc, errno = _syscall(
            _NR_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, ctypes.addressof(buf), 0
        )
        if rc != 0:
            _die("landlock add_rule failed for %r (errno %d)" % (path, errno))
        return True
    finally:
        os.close(fd)


def _confine(real_cli):
    abi = _abi()
    handled = _rights_mask(abi)
    attr = ctypes.create_string_buffer(struct.pack("=Q", handled))
    ruleset_fd, errno = _syscall(_NR_CREATE_RULESET, ctypes.addressof(attr), 8, 0)
    if ruleset_fd < 0:
        _die("landlock create_ruleset failed (errno %d)" % errno)

    try:
        ro = _default_ro(real_cli) + _parse_paths("GATEWAY_LANDLOCK_RO")
        rw = _default_rw() + _parse_paths("GATEWAY_LANDLOCK_RW")

        ro_rights = _RO_RIGHTS & handled
        rw_rights = _RW_RIGHTS_FULL & handled
        for path in ro:
            _add_rule(ruleset_fd, path, ro_rights)
        for path in rw:
            _ensure_dir(path)
            _add_rule(ruleset_fd, path, rw_rights)
        for path in _RO_FILES:
            _add_rule(ruleset_fd, path, _FS_READ_FILE & handled)
        for path in _RW_FILES:
            _add_rule(ruleset_fd, path, (_FS_READ_FILE | _FS_WRITE_FILE) & handled)

        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _die("prctl(NO_NEW_PRIVS) failed (errno %d)" % ctypes.get_errno())
        rc, errno = _syscall(_NR_RESTRICT_SELF, ruleset_fd, 0)
        if rc != 0:
            _die("landlock restrict_self failed (errno %d)" % errno)
    finally:
        os.close(ruleset_fd)


# System roots the CLI (node + bundled binaries) and ordinary shell tooling need
# to read and execute. /etc is read-only on purpose: the CLI and libc resolvers
# read it, and the design keeps system paths readable (a passwd entry is needed
# for whoami/os.userInfo). Confidentiality comes from what is NOT here: /app,
# other workspaces, other sessions' state.
_SYSTEM_RO = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/proc",  # read-only; the CLI aborts at exec without it. Same-uid environ
    # of out-of-domain processes still reads EACCES (that is the uid follow-up).
)
_RO_FILES = ("/dev/urandom",)
_RW_FILES = ("/dev/null", "/dev/zero", "/dev/tty")


def _default_ro(real_cli):
    roots = list(_SYSTEM_RO)
    # The real CLI's own directory and its resolved target (the SDK's bundled
    # install lives under site-packages; node may sit elsewhere).
    cli_dir = os.path.dirname(os.path.realpath(real_cli))
    roots.append(cli_dir)
    return roots


def _default_rw():
    roots = [_cwd()]
    # The CLI writes temp files directly under its tmpdir (a "claude-<pid>-cwd"
    # marker, spilled tool results). Redirect that tmpdir into the workspace so
    # it lands in a granted, per-session location instead of the shared /tmp
    # (which would be a cross-session channel when sessions share a uid). See
    # _setup_tmpdir, called before this. /dev/shm backs POSIX semaphores.
    roots.append(_session_tmpdir())
    roots.append("/dev/shm")
    return roots


def _cwd():
    return os.environ.get("PWD") or os.getcwd()


def _session_tmpdir():
    """A workspace-local tmpdir, honored over the shared /tmp.

    An operator ``CLAUDE_CODE_TMPDIR`` that already resolves under the cwd is
    kept; otherwise the launcher points TMPDIR/CLAUDE_CODE_TMPDIR at
    ``<cwd>/.cli-tmp`` so the CLI's temp writes stay inside the granted
    workspace. Exported into the environment the real CLI inherits.
    """
    cwd = os.path.realpath(_cwd())
    configured = os.environ.get("CLAUDE_CODE_TMPDIR", "").strip()
    if configured and os.path.realpath(configured).startswith(cwd + os.sep):
        tmp = configured
    else:
        tmp = os.path.join(cwd, ".cli-tmp")
    os.environ["TMPDIR"] = tmp
    os.environ["CLAUDE_CODE_TMPDIR"] = tmp
    return tmp


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass  # add_rule will skip it if it still cannot be opened


def main():
    real_cli = os.environ.get("GATEWAY_REAL_CLI", "").strip()
    if not real_cli:
        _die("GATEWAY_REAL_CLI is unset; refusing to spawn an unconfined CLI")
    if not (os.path.isfile(real_cli) and os.access(real_cli, os.X_OK)):
        _die("GATEWAY_REAL_CLI=%r is not an executable file" % real_cli)

    _confine(real_cli)

    # argv[0] is this launcher; hand the real CLI its own name plus the rest.
    os.execv(real_cli, [real_cli, *sys.argv[1:]])


if __name__ == "__main__":
    main()
