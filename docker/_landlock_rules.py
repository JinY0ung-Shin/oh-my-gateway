"""Shared machinery for the isolation launcher and broker.

Both ``landlock_launcher.py`` (landlock mode: confine in place) and
``isolation_broker.py`` (uid mode: confine the forked child after the uid
switch) must confine with the **same** rules, so the syscall surface, the
access masks and the default system policy live here once rather than being
duplicated across two security-critical files. So do the few things both ends
of a confined spawn must agree on: the per-session temp/cache/git environment,
the session shell wrapper (``claude-session-bash``, a sibling of this file) and
the uid-mode status wire format.

stdlib only. This module lives beside the launcher/broker under ``docker/`` —
a fixed, root-owned location in the image — and is imported as a sibling; it is
never loaded from the agent-writable gateway tree (``/app/src``). Callers run
under ``python3 -I`` so the sibling import cannot be redirected.
"""

import ctypes
import errno
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

# The lowest ABI either mode accepts. ABI 1/2 do not handle TRUNCATE, so there
# truncate(2)/O_TRUNC on a file outside the ruleset that DAC lets the session
# write (a shared asset, /app/data) still succeeds — the read-only and
# "outside the ruleset" guarantees would not hold (#244 review 4). Mirrored by
# ``src.backends.claude.landlock.MIN_LANDLOCK_ABI`` (parity-tested).
MIN_ABI = 3
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
# Read-only system caches outside the roots above. fontconfig's prebuilt cache
# (``fc-cache`` at image build) holds font metadata only: without it every
# fontconfig client in a session (fc-list, LibreOffice, Chromium, matplotlib)
# rescans every font file on every start. Only a real directory is granted
# (:func:`system_ro_caches`): a link there could point anywhere.
SYSTEM_RO_CACHES = ("/var/cache/fontconfig",)
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


def _warn(msg):
    try:
        os.write(2, ("landlock: %s\n" % msg).encode("utf-8", "backslashreplace"))
    except OSError:
        pass


def _add_rule_fd(ruleset_fd, fd, rights, path):
    # A rule on a regular file must carry only file-applicable rights, or the
    # kernel rejects it with EINVAL and the whole build fails. O_PATH fds are
    # fstat-able, so narrow the mask for a non-directory target.
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            rights &= _FILE_RIGHTS
    except OSError:
        pass
    if rights == 0:
        return False
    buf = ctypes.create_string_buffer(struct.pack("=Qi", rights, fd))
    rc, err = _syscall(
        _NR_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, ctypes.addressof(buf), 0
    )
    if rc != 0:
        raise LandlockError("add_rule failed for %r (errno %d)" % (path, err))
    return True


def system_ro_caches():
    """The :data:`SYSTEM_RO_CACHES` entries to grant: each one that is a real
    directory, never a symlink (``_add_rule`` follows links)."""
    out = []
    for path in SYSTEM_RO_CACHES:
        try:
            if stat.S_ISDIR(os.lstat(path).st_mode):
                out.append(path)
        except OSError:
            pass
    return out


def _add_rule(ruleset_fd, path, rights):
    """A rule for one of the launcher's own paths (the system roots, the real
    CLI's dir, the session shell, /dev entries), opened following symlinks:
    merged-usr /bin, /lib, /lib64 and /sbin are links."""
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return False  # absent path: nothing to grant (ro) / created first (rw)
    try:
        return _add_rule_fd(ruleset_fd, fd, rights, path)
    finally:
        os.close(fd)


class _Refused(Exception):
    """A gateway path that does not resolve without following a symlink."""


_NOFOLLOW_PATH = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_nofollow(path, create):
    """An ``O_PATH`` fd on the absolute *path*, walked from ``/`` one component
    at a time without following a symlink; ``None`` when it does not exist and,
    with *create*, cannot be made (missing directories are made the same way).
    Raises :class:`_Refused` when a component is a symlink, or one before the
    last is not a directory."""
    names = path.split("/")[1:]
    if not path.startswith("/") or any(n in ("", ".", "..") for n in names):
        raise _Refused("not a normalized absolute path")
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, name in enumerate(names):
            try:
                child = os.open(name, _NOFOLLOW_PATH, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(name, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError:
                    return None
                child = os.open(name, _NOFOLLOW_PATH, dir_fd=fd)
            except OSError:
                return None  # e.g. no search permission: as before, no rule
            os.close(fd)
            fd = child
            mode = os.fstat(fd).st_mode
            where = "/" + "/".join(names[: i + 1])
            if stat.S_ISLNK(mode):
                raise _Refused("%s is a symlink" % where)
            if i < len(names) - 1 and not stat.S_ISDIR(mode):
                raise _Refused("%s is not a directory" % where)
        found, fd = fd, -1
        return found
    finally:
        if fd >= 0:
            os.close(fd)


def _add_rule_nofollow(ruleset_fd, path, rights, create=False):
    """A rule for one of the gateway's entries, which the gateway resolved and
    checked: opened without following any symlink (:func:`_open_nofollow`), so
    one swapped in after that check — ``agents -> /app``, a planted
    ``projects/<name>`` link into another user's workspace — is refused rather
    than granted (#244 review). A file rule grants the inode, so a file target
    with more than one hard link — one added after the gateway's own check,
    to another user's transcript say — is refused too."""
    try:
        fd = _open_nofollow(path, create)
    except _Refused as exc:
        _warn("not granting %r: %s" % (path, exc))
        return False
    except (OSError, ValueError):
        return False
    if fd is None:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1:
            _warn("not granting %r: a file with %d links" % (path, st.st_nlink))
            return False
        return _add_rule_fd(ruleset_fd, fd, rights, path)
    finally:
        os.close(fd)


def cli_dir_problem(cli_dir, workspace):
    """Why the real CLI's directory must not become a read-only grant, or
    ``None``: ``/``, or a directory holding the workspace — a real CLI at
    ``/claude``, ``$HOME/claude`` or ``/app/claude`` would hand every session
    that whole tree (other users' workspaces, the config dir, ``/app/data``).
    The launcher and the broker grant it themselves, past the gateway's
    filter, so both check it here."""
    if cli_dir == "/":
        return "it is /"
    if workspace == cli_dir or workspace.startswith(cli_dir + "/"):
        return "it holds the workspace %r" % workspace
    return None


def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


# --- the session tmp dir ------------------------------------------------------
#
# The launcher prepares it unconfined (the broker's CLI child as the session
# uid, before its ruleset), on names the agent controls and may change while we
# work. So everything below is relative to fds opened from the workspace and
# follows no symlink: an agent that swaps .cli-tmp for a link mid-repair (rename
# it aside, `ln -s /elsewhere .cli-tmp`, in a loop) cannot steer an unlink,
# mkdir, chmod or write outside its workspace (#244 review). All it can do is
# make its own spawn give up after a few tries, which fails closed.

_TMPDIR_ATTEMPTS = 8
_DIR_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_PATH = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _is_ours(st):
    """Whether the directory *st* describes is ours to use and repair (a hook:
    the same-uid tests replace it to stand in for another uid's directory)."""
    return st.st_uid == os.geteuid()


def _lstat_at(dir_fd, name):
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _unlink_at(dir_fd, name):
    """Unlink the non-directory *name*; a directory that took its place
    meanwhile stays for the caller's next look."""
    try:
        os.unlink(name, dir_fd=dir_fd)
    except (FileNotFoundError, IsADirectoryError):
        pass


_ASIDE_TAKEN = (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR)


def _rename_aside(dir_fd, name, tag):
    """Move *name* to the first free ``<name>.<tag>-<n>`` beside it. Raises
    ``OSError`` when it cannot (e.g. *dir_fd* is not writable by us)."""
    for n in range(1, 101):
        aside = "%s.%s-%d" % (name, tag, n)
        if _lstat_at(dir_fd, aside) is not None:
            continue
        try:
            os.rename(name, aside, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno not in _ASIDE_TAKEN:
                raise
    raise OSError(errno.EEXIST, "no free name to move %r aside" % name)


def _repair_dir_at(dir_fd, name):
    """A directory *name* we could not open: give ours its owner rwx back, or
    move another uid's aside — through an ``O_PATH`` fd, which needs no
    permission and follows no link."""
    try:
        fd = os.open(name, _DIR_PATH, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        _unlink_at(dir_fd, name)
        return
    try:
        st = os.fstat(fd)
        if not _is_ours(st):
            _rename_aside(dir_fd, name, "stale")
        elif st.st_mode & 0o700 != 0o700:
            # fchmod refuses an O_PATH fd; its /proc/self/fd entry does not.
            os.chmod("/proc/self/fd/%d" % fd, stat.S_IMODE(st.st_mode) | 0o700)
    finally:
        os.close(fd)


def _dir_at(dir_fd, name):
    """An ``O_RDONLY`` fd on the directory *name*, ours and owner-rwx, or
    ``None`` after one repair step, for the caller to look again.

    A missing one is made; any non-directory (link, file, FIFO, socket,
    device) is unlinked; one of ours that lost its owner bits gets them back;
    another uid's moves aside to ``<name>.stale-<n>`` — e.g. a gateway-owned
    ``.cli-tmp`` that a landlock-shaped preflight left before the first
    uid-mode spawn, which the session uid could not use (the CLI exits 1 on
    its mkdir). Moving it needs a workspace we can write; without one this
    raises, and the spawn fails closed with its tmpdir message."""
    try:
        fd = os.open(name, _DIR_OPEN, dir_fd=dir_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o777, dir_fd=dir_fd)
        except FileExistsError:
            pass
        return None
    except PermissionError:
        _repair_dir_at(dir_fd, name)
        return None
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
        _unlink_at(dir_fd, name)
        return None
    try:
        st = os.fstat(fd)
        ours = _is_ours(st)
        if ours and st.st_mode & 0o700 != 0o700:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o700)
    except BaseException:
        os.close(fd)
        raise
    if not ours:
        os.close(fd)
        _rename_aside(dir_fd, name, "stale")
        return None
    return fd


def _repair_cli_root(tmp_fd):
    """CLI 2.1.283 keeps its own per-uid root at ``<tmpdir>/claude-<uid>``
    (mode 0700) and refuses to start — exit 1 — when it cannot open that as a
    directory of its uid. Repaired like the tmp dir; an absent one is left for
    the CLI to create."""
    name = "claude-%d" % os.getuid()
    st = _lstat_at(tmp_fd, name)
    if st is None:
        return
    if not stat.S_ISDIR(st.st_mode):
        _unlink_at(tmp_fd, name)
    elif not _is_ours(st) or st.st_mode & 0o700 != 0o700:
        _repair_dir_at(tmp_fd, name)


def session_tmpdir(cwd, name=".cli-tmp", cwd_fd=None):
    """``<cwd>/<name>`` made the confined CLI's tmp dir; returns that path.

    The session's temp dir rides inside the workspace (off the shared /tmp). It
    is **not** added as a Landlock rule — it lies beneath the already-granted
    workspace, so a rule would be redundant, and a rule on an agent-controlled
    path is an escape: ``_add_rule`` opens targets with ``O_PATH`` (follows
    symlinks), so a ``.cli-tmp`` the agent replaced with ``ln -s / .cli-tmp``
    would grant the link's target. Without a rule, Landlock resolves the real
    path at ``open()`` and a symlink out of the workspace is simply denied.

    What the agent left there is repaired (:func:`_dir_at`, also for the CLI's
    own root, :func:`_repair_cli_root`) — a planted file there once locked the
    workspace out for good — and the session's git config is written into it
    (:func:`_write_git_config`), all relative to fds, following no link. The
    path returned named that very directory when last checked. *cwd* is the
    gateway's (the agent cannot replace its own workspace) and *name* one entry.

    Raises ``OSError`` when the dir cannot be made — the CLI cannot start
    without it (it exits 1 on the failed ``mkdir``), and swallowing the error
    hid the cause, e.g. a uid-mode session uid that cannot write an app-owned
    workspace — or when the agent keeps swapping it."""
    # O_PATH: every *at() call below takes it, and it needs no read permission
    # on the workspace (the session uid may have only search and write there).
    # A caller that has just checked its kernel cwd passes an fd on it
    # (*cwd_fd*, borrowed, not closed here), so the repair cannot be steered
    # by a rename of the workspace path between that check and this open.
    own_fd = cwd_fd is None
    if own_fd:
        cwd_fd = os.open(cwd, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for _ in range(_TMPDIR_ATTEMPTS):
            fd = _dir_at(cwd_fd, name)
            if fd is None:
                continue
            try:
                _repair_cli_root(fd)
                tmp = os.path.join(cwd, name)
                _write_git_config(fd, tmp)
                now = _lstat_at(cwd_fd, name)
                if now is not None and os.path.samestat(now, os.fstat(fd)):
                    return tmp
            finally:
                os.close(fd)
    finally:
        if own_fd:
            os.close(cwd_fd)
    raise OSError(errno.EAGAIN, "%s in %s kept changing; giving up" % (name, cwd))


def safe_tmpdir(cwd, cwd_fd=None):
    """``<cwd>/.cli-tmp`` made the session tmp dir (:func:`session_tmpdir`).
    Caller sets TMPDIR to this (:func:`session_tmp_env`)."""
    return session_tmpdir(cwd, cwd_fd=cwd_fd)


# The session's global git config, in its tmp dir (GIT_CONFIG_GLOBAL).
GIT_CONFIG_NAME = "gitconfig"
_GIT_CONFIG_MAX = 1 << 20


def _git_quote(value):
    """*value* as a double-quoted git-config value (``\\``, ``"`` and the
    control characters git has escapes for; ``;``/``#`` are literal inside
    quotes)."""
    for raw, escaped in (
        ("\\", "\\\\"),
        ('"', '\\"'),
        ("\n", "\\n"),
        ("\t", "\\t"),
        ("\b", "\\b"),
    ):
        value = value.replace(raw, escaped)
    return '"%s"' % value


def _git_config_lines(tmp):
    """Our two settings, as the lines of the file."""
    return (
        "\texcludesFile = %s\n" % _git_quote(os.path.join(tmp, "git", "ignore")),
        "\tattributesFile = %s\n" % _git_quote(os.path.join(tmp, "git", "attributes")),
    )


def _git_config_text(tmp):
    return (
        "# The session's global git config (GIT_CONFIG_GLOBAL), written by the\n"
        "# gateway's isolation launcher. ~/.gitconfig and ~/.config/git/* are\n"
        "# outside the session's sandbox, so the per-user defaults live here.\n"
        "[core]\n"
    ) + "".join(_git_config_lines(tmp))


def _git_config_usable(tmp_fd, tmp):
    """Whether the gitconfig in place is one git can read and still ours —
    ``None`` when there is none: a regular file the owner can read, not empty,
    holding our ``excludesFile`` line, which git keeps byte for byte when the
    session's own ``git config --global`` rewrites the file around it."""
    st = _lstat_at(tmp_fd, GIT_CONFIG_NAME)
    if st is None:
        return None
    if not stat.S_ISREG(st.st_mode):
        return False
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    try:
        fd = os.open(GIT_CONFIG_NAME, flags, dir_fd=tmp_fd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= _GIT_CONFIG_MAX:
            return False
        data = b""
        while len(data) <= _GIT_CONFIG_MAX:
            chunk = os.read(fd, _GIT_CONFIG_MAX + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    return os.fsencode(_git_config_lines(tmp)[0]) in data


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _install_git_config(tmp_fd, data):
    """Write *data* to the gitconfig through a temp name and an atomic rename,
    so a failed write (ENOSPC) never leaves a half-written file in its place."""
    temp = "%s.new-%d" % (GIT_CONFIG_NAME, os.getpid())
    _unlink_at(tmp_fd, temp)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(temp, flags, 0o600, dir_fd=tmp_fd)
    try:
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        os.replace(temp, GIT_CONFIG_NAME, src_dir_fd=tmp_fd, dst_dir_fd=tmp_fd)
    except BaseException:
        _unlink_at(tmp_fd, temp)
        raise


def _write_git_config(tmp_fd, tmp):
    """Make ``<tmp>/gitconfig`` the session's global git config.

    A usable one stays (:func:`_git_config_usable`): the session's own ``git
    config --global`` writes land in it. Anything else in its place — a
    directory, an unreadable or empty file (an ENOSPC mid-write), content git
    cannot read as ours — moves aside to ``gitconfig.broken-<n>`` and a fresh
    one is written (:func:`_install_git_config`): a directory there made every
    config-reading git command of every later session exit 128 (#244 review).
    Best effort, never raises: a spawn must not fail over git (that was the
    ``.cli-tmp`` lockout), and without the file git still runs, only the
    excludes default goes back to the unreadable ``~/.config/git/ignore``."""
    try:
        usable = _git_config_usable(tmp_fd, tmp)
        if usable:
            return
        if usable is False:
            _rename_aside(tmp_fd, GIT_CONFIG_NAME, "broken")
        _install_git_config(tmp_fd, os.fsencode(_git_config_text(tmp)))
    except (OSError, ValueError):
        pass


def session_tmp_env(tmp):
    """The env overrides that keep a confined CLI's temp files, tool caches and
    per-user git config in its own session tmp dir (beneath the granted
    workspace, made by :func:`session_tmpdir`).

    The npm and uv caches must not be the shared, app-owned ``~/.npm`` /
    ``~/.cache/uv``: a package another session planted there is what the next
    ``npx -y`` / ``uv run`` executes. Inherited values are overridden. npm reads
    ``npm_config_*`` case-insensitively and the last spelling in the env wins,
    so both spellings are set.

    uv also keeps its managed Pythons and its tool envs under ``$HOME``
    (``~/.local/share/uv/{python,tools}``), and it treats an unreadable
    directory there as fatal, unlike a missing one: as a session uid, which
    cannot enter the 0700 ``/home/app``, every ``uv run`` died with "Failed to
    discover managed Python installations" and every ``uvx`` with "failed to
    create directory .../uv/tools" — plugin MCP servers bootstrapped that way
    among them (#246). Both move beneath the tmp dir; a project venv stays
    beside its project (no ``UV_PROJECT_ENVIRONMENT``: an absolute one would
    make every project in the workspace share a venv).

    git: the ruleset grants none of ``~/.gitconfig``, ``~/.config/git/config``
    and the default excludes file ``~/.config/git/ignore``. Landlock does not
    mediate ``access()``, so git's ``access(R_OK)`` passes, the ``open()`` is
    denied, and every config-reading git command died with exit 128 whenever
    one of them existed (#244 review). ``GIT_CONFIG_GLOBAL`` (git >= 2.32)
    replaces both global config files with the session's own (written by
    :func:`session_tmpdir`), which also points ``core.excludesFile`` /
    ``core.attributesFile`` beneath the tmp dir — ``GIT_CONFIG_GLOBAL`` alone
    leaves the excludes default at ``~/.config/git/ignore``. Not
    ``XDG_CONFIG_HOME``: the CLI reads its own config beneath that too. The
    system config (/etc/gitconfig) stays readable and in effect, except for
    those two keys, which the session's global config overrides.

    ``XDG_CACHE_HOME``: the shared ``~/.cache`` is not granted, so fontconfig,
    matplotlib, pip and the CLI's own cache fell back to rebuilding (or not
    writing) their caches on every run; beneath the tmp dir they persist per
    workspace without becoming a cross-session plant channel."""
    npm_cache = os.path.join(tmp, "npm-cache")
    return {
        "TMPDIR": tmp,
        "CLAUDE_CODE_TMPDIR": tmp,
        "npm_config_cache": npm_cache,
        "NPM_CONFIG_CACHE": npm_cache,
        "UV_CACHE_DIR": os.path.join(tmp, "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": os.path.join(tmp, "uv-python"),
        "UV_TOOL_DIR": os.path.join(tmp, "uv-tools"),
        "GIT_CONFIG_GLOBAL": os.path.join(tmp, GIT_CONFIG_NAME),
        "XDG_CACHE_HOME": os.path.join(tmp, "cache"),
    }


# The session shell wrapper: a sibling of this module (docker/ in a checkout,
# /usr/local/bin in the image — fixed and root-owned either way). Its name must
# contain "bash": the CLI honours CLAUDE_CODE_SHELL only for a path containing
# "bash" or "zsh", and picks its shell dialect from that name.
SESSION_SHELL = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "claude-session-bash"
)
REAL_BASH_ENV = "GATEWAY_REAL_BASH"
# CLI 2.1.283's fixed shell dirs (``QAn``), searched after CLAUDE_CODE_SHELL,
# SHELL and a PATH lookup.
_SHELL_DIRS = ("/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")


def _executable(path):
    try:
        return os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _which(name, search_path):
    for entry in search_path.split(os.pathsep):
        if os.path.isabs(entry) and _executable(os.path.join(entry, name)):
            return os.path.join(entry, name)
    return None


def _cli_shell(environ, wrapper):
    """The shell CLI 2.1.283 runs its Bash tool with (``QAn``), skipping the
    *wrapper* itself, or ``None``: ``CLAUDE_CODE_SHELL``, then ``SHELL``, when
    either names an executable bash or zsh; then a PATH lookup and the fixed
    dirs — zsh before bash unless ``SHELL`` names a bash. Unlike the CLI's
    ``Bun.which``, the lookup skips relative PATH entries: they resolve in the
    agent-writable workspace."""
    configured = environ.get("CLAUDE_CODE_SHELL", "")
    login = environ.get("SHELL", "")
    order = ("bash", "zsh") if "bash" in login else ("zsh", "bash")
    search_path = environ.get("PATH", "")
    candidates = [p for p in (configured, login) if "bash" in p or "zsh" in p]
    candidates.append(_which(order[0], search_path))
    candidates += [os.path.join(d, name) for name in order for d in _SHELL_DIRS]
    candidates.append(_which(order[1], search_path))
    for path in candidates:
        if path and _executable(path) and os.path.realpath(path) != wrapper:
            return path
    return None


def session_shell_env(environ):
    """The env that runs the confined CLI's Bash tool through
    :data:`SESSION_SHELL`, given the env the CLI will get (*environ*).

    Confined, the CLI cannot write ``<config dir>/shell-snapshots``, and that
    stays so: every session sources its snapshot before each command, so a
    shared writable dir would let one session plant code in another's. Without
    a snapshot the CLI runs every Bash call as a login shell, ``bash -c -l
    <cmd>``: /etc/profile may reset PATH (Debian's does) and ``~/.profile`` is
    denied, so each result started with "Permission denied" (#244 review). The
    CLI has no switch for either but honours ``CLAUDE_CODE_SHELL``; the wrapper
    turns exactly that leading ``-c -l`` into ``--norc -c`` (Debian's bash
    otherwise reads ``~/.bashrc`` for an ssh-looking env) and execs the bash
    the CLI itself would have
    picked (:func:`_cli_shell`), resolved here once and handed over in
    ``GATEWAY_REAL_BASH``. Which shell runs does not change.

    Empty, leaving ``CLAUDE_CODE_SHELL`` as it is, when the wrapper is missing,
    there is no shell, or the CLI would pick a zsh: zsh skips the startup files
    the ruleset denies without a word, and forcing bash instead would change
    the agent's shell (zsh also moves on to the next PATH match when the ruleset
    denies executing the first, bash fails). Never raises."""
    if not _executable(SESSION_SHELL):
        return {}
    shell = _cli_shell(environ, os.path.realpath(SESSION_SHELL))
    if shell is None or "bash" not in shell:
        return {}
    return {"CLAUDE_CODE_SHELL": SESSION_SHELL, REAL_BASH_ENV: shell}


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
#
# The gateway's own broker ops (cleanup, transcript: the ticket's ``op``) send
# the same header with an empty argv and env and no fds, and read back exactly
# one record: ST_EXITED (cleanup: 0 done, 1 something failed), ST_TRANSCRIPT,
# or ST_REFUSED.

MAX_HEADER = 8 << 20  # env + argv + the ticket's path lists, JSON-escaped
REC = struct.Struct("=Ii")
REC_RELAY = 1  # launcher -> broker: relay signal ``value`` to the CLI's group
ST_EXITED = 1  # broker -> launcher: the CLI exited with code ``value``
ST_SIGNALED = 2  # broker -> launcher: the CLI was killed by signal ``value``
ST_REFUSED = 3  # broker -> launcher: spawn refused; the CLI never started
# broker -> gateway: a transcript op's answer ``value``: the user turns (>= 0),
# or a negative TRANSCRIPT_* code (src/backends/claude/transcript_turns.py)
ST_TRANSCRIPT = 4


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


def build_and_restrict(
    ro_dirs,
    rw_dirs,
    ro_files=RO_FILES,
    rw_files=RW_FILES,
    gateway_ro=(),
    gateway_rw=(),
):
    """Create a ruleset from the given paths, then NO_NEW_PRIVS + restrict_self.

    Requires ABI >= MIN_ABI (3, kernel >= 6.2); raises :class:`LandlockError` otherwise
    so the caller fails closed. Read-write dirs are created first (a rule needs
    an existing path to O_PATH-open); absent read-only paths are skipped. On
    ABI >= 6 the domain is also scoped (signals, abstract unix sockets).

    *ro_dirs*/*rw_dirs*/*ro_files*/*rw_files* are the caller's own paths and
    are opened following symlinks. *gateway_ro*/*gateway_rw* are the gateway's
    entries (``GATEWAY_LANDLOCK_RO/RW``, the ticket's lists), which it resolved
    and checked: each is walked from ``/`` without following any symlink, and a
    missing rw directory is made the same way (:func:`_add_rule_nofollow`).
    """
    a = abi()
    if a < MIN_ABI:
        raise LandlockError(
            "Landlock ABI %d < %d (need kernel >= 6.2 for truncate control); "
            "cannot confine" % (a, MIN_ABI)
        )
    handled = _rights_mask(a)
    if a >= _SCOPE_ABI:
        # handled_access_net stays 0: the network is not restricted.
        scoped = _SCOPE_SIGNAL | _SCOPE_ABSTRACT_UNIX_SOCKET
        raw = struct.pack("=QQQ", handled, 0, scoped)
    else:
        raw = struct.pack("=Q", handled)
    attr = ctypes.create_string_buffer(raw)
    ruleset_fd, err = _syscall(_NR_CREATE_RULESET, ctypes.addressof(attr), len(raw), 0)
    if ruleset_fd < 0:
        raise LandlockError("create_ruleset failed (errno %d)" % err)
    try:
        for path in ro_dirs:
            _add_rule(ruleset_fd, path, RO_RIGHTS & handled)
        for path in rw_dirs:
            _ensure_dir(path)
            _add_rule(ruleset_fd, path, RW_RIGHTS_FULL & handled)
        for path in gateway_ro:
            _add_rule_nofollow(ruleset_fd, path, RO_RIGHTS & handled)
        for path in gateway_rw:
            _add_rule_nofollow(ruleset_fd, path, RW_RIGHTS_FULL & handled, True)
        for path in ro_files:
            _add_rule(ruleset_fd, path, _FS_READ_FILE & handled)
        for path in rw_files:
            _add_rule(ruleset_fd, path, (_FS_READ_FILE | _FS_WRITE_FILE) & handled)
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockError(
                "prctl(NO_NEW_PRIVS) failed (errno %d)" % ctypes.get_errno()
            )
        rc, err = _syscall(_NR_RESTRICT_SELF, ruleset_fd, 0)
        if rc != 0:
            raise LandlockError("restrict_self failed (errno %d)" % err)
    finally:
        os.close(ruleset_fd)
