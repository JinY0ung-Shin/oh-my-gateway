"""Claude compatibility layer for backend-neutral workspace resources.

ChatDRAGON users edit skills and subagents through the workspace file manager. The
user-facing paths are deliberately backend-neutral::

    workspace/
      skills/                 # canonical, user-visible
      agents/                 # canonical, user-visible
      .claude/
        skills/               # Claude compatibility mirror
        agents/               # Claude compatibility mirror

Claude Code still discovers project resources from ``.claude/skills`` and
``.claude/agents``. Rather than making those directories the source of truth, the
gateway keeps real Claude-native directories as mirrors of the visible resource
trees. Regular files are hard-linked whenever the filesystem allows it (so edits
through either path immediately address the same inode); a normal copy is the
portable fallback. Directories are real directories, avoiding Claude Code's
historically inconsistent subagent discovery through directory symlinks.

Preparation and materialization are intentionally separate. Workspace resolution is
also used by the polling file browser, so it only creates/migrates the resource
roots. Recursive mirror synchronization happens immediately before Claude SDK
catalog/query calls, where freshness actually matters.

Existing workspaces are migrated lazily. If only the legacy native directory
exists, it is moved to the visible location first and a managed mirror is created
in its place. If both locations contain unmanaged data, neither side is modified:
choosing one would silently shadow or delete user data.

The mirror is best-effort. Canonical user data is never deleted by this module;
failures only make the Claude compatibility view stale and are logged.

Everything below the workspace root is the session's to change: under uid isolation
the session uid may write anywhere in the tree (``skills/``, ``agents/`` and
``.claude/`` included) and plant a symlink, a hard link, a FIFO or a swapped
directory at any time, also between a check here and its use. So the root is opened
once and everything below it is reached from directory fds, one component at a time
with ``O_NOFOLLOW``; entries are created, linked, renamed and removed relative to
those fds, and files are checked on their open descriptor. A plant can make the
session's own mirror stale or unmanaged, but never redirect a gateway write, link,
copy or removal out of the directories the gateway opened, never park it on a FIFO,
and never fail a caller (the polling file browser, an SDK call): errors are logged
and skip the entry they hit.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_RESOURCE_DIRS = ("skills", "agents")
_CLAUDE_DIR = ".claude"
_MANAGED_MARKER = ".oh-my-gateway-managed"

# The root is a gateway-built path. Everything below it is entered with
# O_NOFOLLOW, so a planted symlink fails the open instead of leading the gateway
# out of the workspace.
_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_DIR_FLAGS = _ROOT_FLAGS | os.O_NOFOLLOW
# O_NONBLOCK: a FIFO in a file's place must not park the caller in open() (the
# S_ISREG check on the descriptor refuses it right after). O_NOCTTY: nothing
# opened here may become the gateway's controlling terminal.
_SAFE_OPEN = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _SAFE_OPEN
_MARKER_FLAGS = os.O_WRONLY | os.O_CREAT | _SAFE_OPEN
_COPY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# What opening a symlink, or anything else that is not a directory, as one reports.
_NOT_A_DIRECTORY = (errno.ENOTDIR, errno.ELOOP, errno.EMLINK)
# Resource trees are shallow. Deeper levels -- the session can plant them on
# either side -- are left as they are instead of being walked with fds per level.
_MAX_DEPTH = 64
_COPY_CHUNK = 1024 * 1024


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _lstat_at(dir_fd: int, name: str) -> Optional[os.stat_result]:
    """``lstat`` of ``name`` in ``dir_fd``; ``None`` when there is no such entry."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_dir_at(dir_fd: int, name: str) -> int:
    """A new fd for the real directory ``name`` in ``dir_fd``, never a symlink."""
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _ensure_dir_at(dir_fd: int, name: str) -> int:
    """:func:`_open_dir_at`, creating the directory first when the name is free."""
    try:
        return _open_dir_at(dir_fd, name)
    except FileNotFoundError:
        pass
    try:
        os.mkdir(name, 0o777, dir_fd=dir_fd)
    except FileExistsError:
        pass
    return _open_dir_at(dir_fd, name)


def _same_regular(a: Optional[os.stat_result], b: Optional[os.stat_result]) -> bool:
    """Whether ``a`` and ``b`` are the same regular file."""
    return (
        a is not None
        and b is not None
        and stat.S_ISREG(a.st_mode)
        and stat.S_ISREG(b.st_mode)
        and os.path.samestat(a, b)
    )


def _scan(dir_fd: int) -> List[Tuple[str, os.stat_result]]:
    """``(name, lstat)`` of each entry in ``dir_fd``, sorted by name.

    Entries that vanish while the directory is listed are left out; any other
    error is raised.
    """
    entries = []
    with os.scandir(dir_fd) as it:
        for entry in it:
            try:
                entries.append((entry.name, entry.stat(follow_symlinks=False)))
            except FileNotFoundError:
                continue
    entries.sort(key=lambda item: item[0])
    return entries


def _remove_at(dir_fd: int, name: str, depth: int = 0) -> None:
    """Remove the entry ``name`` in ``dir_fd``; a directory with all it holds.

    Never follows a symlink: links are unlinked as links, and every directory is
    entered with ``O_NOFOLLOW``, so whatever is swapped in meanwhile is removed or
    refused, never something outside. Removes what it can, then raises the first
    error.
    """
    st = _lstat_at(dir_fd, name)
    if st is None:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    if depth >= _MAX_DEPTH:
        raise OSError(errno.ELOOP, "directory tree too deep to remove", name)
    first: Optional[OSError] = None
    fd = _open_dir_at(dir_fd, name)
    try:
        with os.scandir(fd) as it:
            children = [entry.name for entry in it]
        for child in children:
            try:
                _remove_at(fd, child, depth + 1)
            except OSError as exc:
                first = first or exc
    finally:
        os.close(fd)
    if first is not None:
        raise first
    os.rmdir(name, dir_fd=dir_fd)


# --- the managed marker ------------------------------------------------------


def _marker_status(native_fd: int) -> Optional[bool]:
    """Whether the mirror directory ``native_fd`` carries the gateway's marker.

    ``None`` when there is none, ``False`` when something else sits in its
    place. Only what the gateway itself leaves there counts: a regular file by
    ``lstat`` (a planted symlink is no marker) with a single name (a hard link
    would give the session a second name for whatever a marker write touches).
    """
    try:
        st = os.stat(_MANAGED_MARKER, dir_fd=native_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _managed(native_fd: int) -> bool:
    return _marker_status(native_fd) is True


def _marker_status_at(dir_fd: int, name: str) -> Optional[bool]:
    """:func:`_marker_status` of the real directory ``name`` in ``dir_fd``."""
    try:
        native_fd = _open_dir_at(dir_fd, name)
    except OSError:
        return None
    try:
        return _marker_status(native_fd)
    finally:
        os.close(native_fd)


def _refuse_marker(where: Path, why: str) -> None:
    logger.warning(
        "Refusing Claude resource marker %s (%s); leaving the mirror unmanaged",
        where,
        why,
    )


def _write_marker(native_fd: int, where: Path) -> bool:
    """Leave the zero-byte marker in ``native_fd``; ``False`` when refused.

    The marker is gateway metadata, not user data. Keep it zero bytes so the
    compatibility layer does not consume the user's workspace quota; an existing
    one with content (an earlier version of this bridge stored explanatory text
    in it) is emptied. A marker that already is an empty single-link regular file
    is left alone. Anything else is opened with ``O_NOFOLLOW|O_NONBLOCK`` relative
    to the mirror's fd and checked on the descriptor before a byte is written: a
    symlink, FIFO, socket, directory or a file with a second name in its place is
    refused, so a planted marker can never make the gateway create or truncate
    anything but its own marker.
    """
    st = _lstat_at(native_fd, _MANAGED_MARKER)
    if (
        st is not None
        and stat.S_ISREG(st.st_mode)
        and st.st_nlink == 1
        and st.st_size == 0
    ):
        return True
    # 0o666 is what ``Path.write_bytes`` asked for before: 0o644 under umask 022.
    try:
        fd = os.open(_MANAGED_MARKER, _MARKER_FLAGS, 0o666, dir_fd=native_fd)
    except OSError as exc:
        _refuse_marker(where, _reason(exc))
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            _refuse_marker(where, "not a single-link regular file")
            return False
        if st.st_size:
            os.ftruncate(fd, 0)
        return True
    finally:
        os.close(fd)


# --- mirroring one file ------------------------------------------------------


def _link_fd(fd: int, dst_dir_fd: int, name: str) -> bool:
    """Hard-link the open file ``fd`` as the new name ``name`` in ``dst_dir_fd``.

    ``linkat(AT_EMPTY_PATH)`` needs CAP_DAC_READ_SEARCH; following the
    ``/proc/self/fd`` magic link is its documented unprivileged form and links
    exactly the opened inode, never whatever a path names by now. ``False`` when
    the kernel refuses (EXDEV, EPERM from ``fs.protected_hardlinks`` for a file
    the gateway may not write, EMLINK, ENOENT for a file deleted since it was
    opened or a missing ``/proc``, a filesystem without hard links): the caller
    copies instead.
    """
    try:
        os.link(
            f"/proc/self/fd/{fd}", name, dst_dir_fd=dst_dir_fd, follow_symlinks=True
        )
    except FileExistsError:
        raise
    except OSError:
        return False
    return True


def _unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view = view[written:]
        offset += written


def _copy_data(src: int, out: int, size: int) -> None:
    """Copy the first ``size`` bytes of ``src`` to ``out``, keeping holes holes.

    ``SEEK_DATA``/``SEEK_HOLE`` locate the data (a filesystem without them
    reports EINVAL, and then everything is data). The size is set last, so a
    trailing hole stays a hole and a source that grows meanwhile is not followed.
    """
    pos = 0
    while pos < size:
        try:
            start = os.lseek(src, pos, os.SEEK_DATA)
            end = os.lseek(src, start, os.SEEK_HOLE)
        except OSError as exc:
            if exc.errno == errno.ENXIO:  # only a hole is left
                break
            if exc.errno != errno.EINVAL:
                raise
            start, end = pos, size
        end = min(end, size)
        offset = start
        while offset < end:
            chunk = os.pread(src, min(_COPY_CHUNK, end - offset), offset)
            if not chunk:  # the source shrank since it was opened
                break
            _pwrite_all(out, chunk, offset)
            offset += len(chunk)
        # Always advance, even past a hole punched at ``start`` between the two
        # lseek calls (SEEK_HOLE then returns ``start`` itself).
        pos = max(end, start + 1)
    os.ftruncate(out, size)


def _copy_fd(fd: int, st: os.stat_result, dst_dir_fd: int, name: str) -> bool:
    """Copy the open regular file ``fd`` to the new name ``name`` in ``dst_dir_fd``.

    Like ``shutil.copy2`` (bytes, permission bits, atime/mtime), but read from the
    already-checked descriptor into an ``O_EXCL|O_NOFOLLOW`` file. Where it
    deliberately differs: set-id bits are dropped (the copy is the gateway's own
    file, and a copied setuid bit would let the session's executable run as the
    gateway uid), no extended attributes are copied (the copy keeps the ACL it
    inherits from the mirror directory rather than one the session wrote), and
    holes stay holes, since under uid isolation the session decides when a link
    is refused and a sparse file must not make the gateway fill the volume with
    zeros. ``False`` for a source deleted since it was opened.
    """
    if os.fstat(fd).st_nlink == 0:
        return False
    out = os.open(name, _COPY_FLAGS, 0o600, dir_fd=dst_dir_fd)
    try:
        try:
            os.set_blocking(fd, True)
            _copy_data(fd, out, st.st_size)
            os.fchmod(out, stat.S_IMODE(st.st_mode) & 0o777)
            os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(out)
    except BaseException:
        _unlink_quietly(dst_dir_fd, name)
        raise
    return True


def _materialize_file(src_dir_fd: int, dst_dir_fd: int, name: str, where: Path) -> bool:
    """Make ``name`` in ``dst_dir_fd`` the regular file ``name`` in ``src_dir_fd``.

    A target that already is the same inode is left alone. Otherwise the source
    is opened with ``O_NOFOLLOW|O_NONBLOCK`` relative to its directory and checked
    on the descriptor, then exactly that inode is hard-linked (:func:`_link_fd`) or,
    where the kernel refuses the link, copied from the same descriptor -- under a
    temporary name, renamed over the target, so a reader never finds the entry
    missing and whatever sat there (a symlink included) is replaced, not followed.
    A source that is (or became) a symlink, FIFO, socket or device is refused:
    nothing from outside the canonical tree is ever linked or copied. Returns
    whether the mirror entry now matches; errors on the mirror side raise.
    """
    if _same_regular(_lstat_at(src_dir_fd, name), _lstat_at(dst_dir_fd, name)):
        return True
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=src_dir_fd)
    except OSError as exc:
        logger.warning("Not mirroring workspace resource %s: %s", where, _reason(exc))
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.warning(
                "Not mirroring workspace resource %s: not a regular file", where
            )
            return False
        target = _lstat_at(dst_dir_fd, name)
        if _same_regular(st, target):
            return True
        if target is not None and stat.S_ISDIR(target.st_mode):
            _remove_at(dst_dir_fd, name)  # rename cannot replace a directory
        tmp = f".omg-mirror-{secrets.token_hex(8)}.tmp"
        if not _link_fd(fd, dst_dir_fd, tmp) and not _copy_fd(fd, st, dst_dir_fd, tmp):
            return False
        try:
            os.rename(tmp, name, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)
        finally:
            # Gone after a rename, unless the target already was that inode
            # (then rename(2) changes nothing and both names stay).
            _unlink_quietly(dst_dir_fd, tmp)
        return True
    finally:
        os.close(fd)


# --- mirroring a tree --------------------------------------------------------


def _sync_dir(
    src_fd: int, dst_fd: int, visible: Path, native: Path, depth: int
) -> None:
    """Make the mirror directory ``dst_fd`` match the canonical ``src_fd``.

    One level at a time: stale or wrongly-typed mirror entries are removed, then
    regular files are linked (or copied) and subdirectories created and entered.
    Symlinks are deliberately skipped: a user-visible resource symlink can point
    outside the workspace, and mirroring it into a trusted ``.claude`` discovery
    path would turn that external target into project configuration. FIFOs,
    sockets and devices are not resources either. A directory that either side
    cannot list is left as it is (stale), never emptied.
    """
    try:
        source = _scan(src_fd)
    except OSError as exc:
        logger.warning(
            "Cannot read workspace resource directory %s (%s); "
            "leaving its Claude mirror unchanged",
            visible,
            _reason(exc),
        )
        return
    dirs: List[str] = []
    files: List[str] = []
    for name, st in source:
        if depth == 0 and name == _MANAGED_MARKER:
            # The marker's slot: a user file mirrored onto it would be what the
            # next marker write truncates.
            logger.warning(
                "Not mirroring workspace resource %s: the name is reserved",
                visible / name,
            )
        elif stat.S_ISLNK(st.st_mode):
            logger.warning(
                "Skipping symlinked workspace resource entry %s", visible / name
            )
        elif stat.S_ISDIR(st.st_mode):
            dirs.append(name)
        elif stat.S_ISREG(st.st_mode):
            files.append(name)
    try:
        mirror = _scan(dst_fd)
    except OSError as exc:
        logger.warning(
            "Cannot read Claude mirror directory %s (%s); leaving it unchanged",
            native,
            _reason(exc),
        )
        return
    want_dirs, want_files = set(dirs), set(files)
    for name, st in mirror:
        if depth == 0 and name == _MANAGED_MARKER:
            continue
        if stat.S_ISDIR(st.st_mode) and name in want_dirs:
            continue
        if stat.S_ISREG(st.st_mode) and name in want_files:
            continue  # refreshed below unless it already is the same inode
        try:
            _remove_at(dst_fd, name)
        except OSError as exc:
            logger.warning(
                "Failed to remove stale Claude mirror entry %s: %s",
                native / name,
                _reason(exc),
            )
    for name in files:
        try:
            _materialize_file(src_fd, dst_fd, name, visible / name)
        except OSError as exc:
            logger.warning(
                "Failed to mirror workspace resource %s: %s",
                visible / name,
                _reason(exc),
            )
    for name in dirs:
        _sync_subdir(src_fd, dst_fd, name, visible / name, native / name, depth + 1)


def _sync_subdir(
    src_fd: int, dst_fd: int, name: str, visible: Path, native: Path, depth: int
) -> None:
    if depth > _MAX_DEPTH:
        logger.warning(
            "Not mirroring workspace resource directory %s: nested deeper than %d",
            visible,
            _MAX_DEPTH,
        )
        return
    with ExitStack() as stack:
        try:
            sub_src = _open_dir_at(src_fd, name)
        except FileNotFoundError:
            return  # removed since it was listed
        except OSError as exc:
            logger.warning(
                "Cannot read workspace resource directory %s (%s); "
                "leaving its Claude mirror unchanged",
                visible,
                _reason(exc),
            )
            return
        stack.callback(os.close, sub_src)
        try:
            sub_dst = _ensure_dir_at(dst_fd, name)
        except OSError as exc:
            logger.warning(
                "Failed to create Claude mirror directory %s: %s",
                native,
                _reason(exc),
            )
            return
        stack.callback(os.close, sub_dst)
        _sync_dir(sub_src, sub_dst, visible, native, depth)


# --- preparation -------------------------------------------------------------


def _points_at(dir_fd: int, name: str, target: os.stat_result) -> bool:
    """Whether the symlink ``name`` in ``dir_fd`` resolves to ``target``'s inode.

    Only ``stat`` follows the link; nothing is opened or written through it.
    """
    try:
        return os.path.samestat(os.stat(name, dir_fd=dir_fd), target)
    except OSError:
        return False


def _prepare_resource_dir(
    workspace: Path, root_fd: int, claude_fd: int, name: str
) -> None:
    """Create/migrate one canonical resource root without recursively syncing it."""
    visible = workspace / name
    native = workspace / _CLAUDE_DIR / name
    visible_st = _lstat_at(root_fd, name)
    native_st = _lstat_at(claude_fd, name)

    if visible_st is not None and not stat.S_ISDIR(visible_st.st_mode):
        logger.warning(
            "Cannot prepare Claude workspace resource %s: "
            "visible path is not a safe directory",
            visible,
        )
        return

    # Upgrade the first version of this bridge, which used a directory symlink.
    # It is safe to replace only when it points at the canonical visible directory.
    if native_st is not None and stat.S_ISLNK(native_st.st_mode):
        if visible_st is None or not _points_at(claude_fd, name, visible_st):
            logger.warning(
                "Cannot prepare Claude workspace resource %s: "
                "native symlink has a different target",
                native,
            )
            return
        try:
            os.unlink(name, dir_fd=claude_fd)
        except OSError as exc:
            logger.warning(
                "Failed to replace Claude resource symlink %s: %s", native, _reason(exc)
            )
            return
        native_st = None

    status = _marker_status_at(claude_fd, name) if native_st is not None else None
    if status is False:
        _refuse_marker(native / _MANAGED_MARKER, "not a single-link regular file")
    native_is_managed = status is True

    if visible_st is None:
        try:
            if native_st is not None and not native_is_managed:
                if not stat.S_ISDIR(native_st.st_mode):
                    logger.warning(
                        "Cannot migrate Claude workspace resource %s: "
                        "native path is not a safe directory",
                        native,
                    )
                    return
                # One-time migration from legacy .claude/{skills,agents}. Whatever
                # is swapped in after the lstat moves as it is (rename never
                # follows a symlink), and only within the workspace.
                os.rename(name, name, src_dir_fd=claude_fd, dst_dir_fd=root_fd)
                native_st = None
            else:
                os.mkdir(name, 0o777, dir_fd=root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            logger.warning(
                "Failed to create/migrate workspace resource %s: %s",
                visible,
                _reason(exc),
            )
            return

    if native_st is not None and not native_is_managed:
        # Both locations contain real, independently-managed data. Never delete
        # or silently shadow either side. Operators can merge once, then remove
        # the legacy native directory; the next resolve will create the mirror.
        logger.warning(
            "Claude workspace has both canonical %s and unmanaged native %s; "
            "leaving both unchanged to avoid data loss",
            visible,
            native,
        )
        return

    try:
        if native_st is None:
            os.mkdir(name, 0o777, dir_fd=claude_fd)
        native_fd = _open_dir_at(claude_fd, name)
    except OSError as exc:
        logger.warning(
            "Failed to prepare Claude workspace resource %s: %s", native, _reason(exc)
        )
        return
    try:
        _write_marker(native_fd, native / _MANAGED_MARKER)
    finally:
        os.close(native_fd)


def _prepare_claude_dir(root_fd: int, where: Path) -> Optional[int]:
    """Open the root's real ``.claude`` directory, creating it when absent."""
    try:
        return _ensure_dir_at(root_fd, _CLAUDE_DIR)
    except OSError as exc:
        if exc.errno in _NOT_A_DIRECTORY:
            logger.warning(
                "Cannot prepare Claude workspace resources: "
                "%s is not a safe directory",
                where,
            )
        else:
            logger.warning(
                "Failed to prepare Claude workspace resource root %s: %s",
                where,
                _reason(exc),
            )
        return None


def _prepare(workspace: Path) -> None:
    claude_where = workspace / _CLAUDE_DIR
    with ExitStack() as stack:
        try:
            root_fd = os.open(workspace, _ROOT_FLAGS)
        except OSError as exc:
            logger.warning(
                "Failed to prepare Claude workspace resource root %s: %s",
                claude_where,
                _reason(exc),
            )
            return
        stack.callback(os.close, root_fd)
        claude_fd = _prepare_claude_dir(root_fd, claude_where)
        if claude_fd is None:
            return
        stack.callback(os.close, claude_fd)
        for name in _RESOURCE_DIRS:
            try:
                _prepare_resource_dir(workspace, root_fd, claude_fd, name)
            except OSError as exc:
                logger.warning(
                    "Failed to prepare Claude workspace resource %s: %s",
                    workspace / name,
                    _reason(exc),
                )


# --- materialization ---------------------------------------------------------


def _skip_unmaterialized(where: Path, exc: OSError) -> None:
    """Log why ``where`` cannot be entered, unless it is just unprepared.

    A missing entry or one that is not a real directory (a symlink included) is
    an unprepared or operator-managed layout, left untouched without a word.
    """
    if exc.errno not in (errno.ENOENT, *_NOT_A_DIRECTORY):
        logger.warning(
            "Cannot materialize Claude workspace resources in %s: %s",
            where,
            _reason(exc),
        )


def _materialize_resource(
    workspace: Path, root_fd: int, claude_fd: int, name: str
) -> None:
    visible = workspace / name
    native = workspace / _CLAUDE_DIR / name
    with ExitStack() as stack:
        try:
            visible_fd = _open_dir_at(root_fd, name)
            stack.callback(os.close, visible_fd)
        except OSError as exc:
            _skip_unmaterialized(visible, exc)
            return
        try:
            native_fd = _open_dir_at(claude_fd, name)
            stack.callback(os.close, native_fd)
        except OSError as exc:
            _skip_unmaterialized(native, exc)
            return
        if _managed(native_fd):
            _sync_dir(visible_fd, native_fd, visible, native, 0)


def _materialize(workspace: Path) -> None:
    with ExitStack() as stack:
        try:
            root_fd = os.open(workspace, _ROOT_FLAGS)
        except OSError:
            return  # no workspace (yet): nothing to refresh
        stack.callback(os.close, root_fd)
        try:
            claude_fd = _open_dir_at(root_fd, _CLAUDE_DIR)
        except OSError as exc:
            _skip_unmaterialized(workspace / _CLAUDE_DIR, exc)
            return
        stack.callback(os.close, claude_fd)
        for name in _RESOURCE_DIRS:
            _materialize_resource(workspace, root_fd, claude_fd, name)


# --- public API ----------------------------------------------------------------


def prepare_workspace_resources(workspace: Path) -> None:
    """Prepare canonical roots and native mirror roots, but do not scan contents.

    This is the cheap path used by :class:`WorkspaceManager`. It may run on every
    file-browser poll, so it performs only constant-number filesystem operations
    plus a one-time directory rename for legacy migration. It never raises.
    """
    workspace = Path(workspace)
    try:
        _prepare(workspace)
    except Exception:
        logger.warning(
            "Failed to prepare Claude workspace resources in %s",
            workspace,
            exc_info=True,
        )


def materialize_workspace_resources(workspace: Path) -> None:
    """Refresh managed Claude-native mirrors from canonical resource trees.

    This is the recursive path and should run immediately before Claude SDK
    discovery/query operations, not on generic workspace/file-browser access.
    Unprepared or operator-managed layouts are left untouched. It never raises.
    """
    workspace = Path(workspace)
    try:
        _materialize(workspace)
    except Exception:
        logger.warning(
            "Failed to materialize Claude workspace resources in %s",
            workspace,
            exc_info=True,
        )


def ensure_workspace_resources(workspace: Path) -> None:
    """Prepare then materialize resources; convenient for migrations/tests."""
    prepare_workspace_resources(workspace)
    materialize_workspace_resources(workspace)
