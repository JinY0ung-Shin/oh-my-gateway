"""Copy a local marketplace a confined session cannot read into the clone root.

With Landlock confinement on (``CLAUDE_ISOLATION_MODE=landlock|uid``), every
spawn refuses grants under the gateway app root (``/app``: code and the
``/app/data`` credentials), the workspace base, the temp dir and the private
system roots. A local marketplace registered in place there (``/app/data/
plugins/<mp>``, a natural persistent spot) installs and lists fine, but the
CLI never sees its skills (#255). Remote repos already go through a clone
into the clone root, which sessions may read; this gives a local path the same
treatment (#258): a copy in the clone root is registered, the manifest keeps
the original path, and every start and admin refresh copies it again.

The copy lands where every session reads, beside nothing the source sat next
to, so it must never widen what a session reaches:

* no link is followed: the walk is fd-relative with ``O_NOFOLLOW``, and a link
  is kept only when it is relative and stays inside the copied tree (a link to
  ``/app/data/x.json``, or ``../..`` out of the tree, is dropped — beside the
  clone root a relative link could otherwise name the config dir);
* hard-linked files and special files (FIFO, socket, device) are skipped;
* file count and total size are capped (:data:`MAX_FILES`, :data:`MAX_BYTES`);
* a source that is or contains the app root, its ``data`` dir, the workspace
  base, the temp dir or ``$HOME`` is refused, and it must be a marketplace
  (a regular ``.claude-plugin/marketplace.json``).

Standalone on purpose (stdlib only, no ``src`` import): the startup script
``docker/install_plugins.py`` loads it from beside itself
(``/usr/local/bin`` in the image) or from ``src/`` in a checkout.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import logging
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

MODE_ENV = "CLAUDE_ISOLATION_MODE"
_CONFINED_MODES = ("landlock", "uid")
# Mirrors landlock._PRIVATE_SYSTEM_ROOTS: never granted to a session.
_PRIVATE_SYSTEM_ROOTS = ("/run", "/var/run", "/dev", "/sys", "/root", "/var/tmp")

MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024
_CHUNK = 1024 * 1024

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)


class LocalCopyError(ValueError):
    """The local marketplace cannot (or must not) be copied."""


def confinement_on(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(MODE_ENV) or "").strip().lower() in _CONFINED_MODES


def _real(path) -> str:
    return os.path.realpath(os.path.abspath(str(path)))


def _beneath(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def workspace_base(env=None) -> str:
    """``USER_WORKSPACES_DIR``, else the workspace manager's temp default."""
    env = os.environ if env is None else env
    configured = (env.get("USER_WORKSPACES_DIR") or "").strip()
    if configured:
        return configured
    return os.path.join(tempfile.gettempdir(), "oh-my-gateway-workspaces")


def _unreadable_roots(app_root, env) -> List[str]:
    roots = [str(app_root), workspace_base(env), tempfile.gettempdir()]
    return [_real(r) for r in roots + list(_PRIVATE_SYSTEM_ROOTS) if r]


def needs_copy(path, *, app_root, env=None) -> bool:
    """Whether a confined session is refused *path*: confinement is on and it
    lies in the app root, the workspace base, the temp dir or a private system
    root. A path sessions can read is registered in place, as before."""
    if not confinement_on(env):
        return False
    real = _real(path)
    return any(_beneath(real, root) for root in _unreadable_roots(app_root, env))


def source_problem(path, *, app_root, env=None) -> Optional[str]:
    """Why *path* must not be copied, or ``None``: a broad dir that holds the
    gateway's state, or no marketplace at all."""
    env = os.environ if env is None else env
    real = _real(path)
    app = _real(app_root)
    if real in (app, os.path.join(app, "data")):
        return f"{real} is the gateway's {'app root' if real == app else 'data dir'}"
    home = (env.get("HOME") or "").strip()
    held = [
        ("the gateway app root", app),
        ("the workspace base", _real(workspace_base(env))),
        ("the temp dir", _real(tempfile.gettempdir())),
    ]
    if home:
        held.append(("$HOME", _real(home)))
    for label, root in held:
        if real == "/" or (real != root and _beneath(root, real)):
            return f"{real} contains {label}"
    manifest = os.path.join(real, ".claude-plugin", "marketplace.json")
    try:
        st = os.lstat(manifest)
    except OSError:
        st = None
    if st is None or not stat.S_ISREG(st.st_mode):
        return (
            f"{real} is not a marketplace (no regular .claude-plugin/marketplace.json)"
        )
    return None


class _Budget:
    def __init__(self, max_files: int, max_bytes: int):
        self.files = 0
        self.bytes = 0
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.skipped: List[str] = []

    def take(self, rel: str, size: int = 0) -> None:
        self.files += 1
        self.bytes += size
        if self.files > self.max_files:
            raise LocalCopyError(f"more than {self.max_files} entries (at {rel})")
        if self.bytes > self.max_bytes:
            raise LocalCopyError(f"more than {self.max_bytes} bytes (at {rel})")


def _link_stays_inside(rel_dir: str, target: str) -> bool:
    if os.path.isabs(target):
        return False
    joined = os.path.normpath(os.path.join(rel_dir, target))
    return joined != ".." and not joined.startswith(".." + os.sep)


def _copy_file(src_dir_fd: int, name: str, dest: str, rel: str, budget) -> None:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=src_dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            budget.skipped.append(rel)  # swapped for a link, or a socket
            return
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            budget.skipped.append(rel)
            return
        budget.take(rel, st.st_size)
        mode = 0o755 if st.st_mode & stat.S_IXUSR else 0o644
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            copied = 0
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > st.st_size:  # grew while copying: count it
                    budget.take(rel, len(chunk))
                os.write(out, chunk)
            os.fchmod(out, mode)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _copy_dir(src_fd: int, dest: str, rel: str, budget, depth: int) -> None:
    if depth > 64:
        raise LocalCopyError(f"nested deeper than 64 levels (at {rel})")
    with os.scandir(src_fd) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        name = entry.name
        child_rel = os.path.join(rel, name) if rel else name
        child_dest = os.path.join(dest, name)
        st = os.lstat(name, dir_fd=src_fd)
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(name, dir_fd=src_fd)
            if _link_stays_inside(rel, target):
                budget.take(child_rel)
                os.symlink(target, child_dest)
            else:
                budget.skipped.append(child_rel)
        elif stat.S_ISDIR(st.st_mode):
            try:
                fd = os.open(name, _DIR_FLAGS, dir_fd=src_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    budget.skipped.append(child_rel)
                    continue
                raise
            try:
                budget.take(child_rel)
                os.mkdir(child_dest, 0o755)
                _copy_dir(fd, child_dest, child_rel, budget, depth + 1)
                os.chmod(child_dest, 0o755)
            finally:
                os.close(fd)
        elif stat.S_ISREG(st.st_mode):
            _copy_file(src_fd, name, child_dest, child_rel, budget)
        else:
            budget.skipped.append(child_rel)


def copy_marketplace(
    source,
    dest: Path,
    *,
    app_root,
    env=None,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
) -> List[str]:
    """Copy the marketplace at *source* to *dest* and publish it there.

    One copy per *dest* at a time (an ``flock`` on ``<dest>.lock``, so an admin
    refresh and the startup replay never interleave), staged in a fresh
    ``<dest>.new-*`` dir of its own, then swapped in by :func:`_publish`: a
    failure at any step leaves the copy *dest* holds now, and a reader of
    *dest* sees the old tree or the new one, never a partial one. Returns the
    relative paths skipped (links out of the tree, hard links, special files).
    Raises :class:`LocalCopyError` for a refused source or a cap."""
    problem = source_problem(source, app_root=app_root, env=env)
    if problem:
        raise LocalCopyError(f"refusing to copy the local marketplace: {problem}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _locked(dest):
        _sweep(dest)
        staging = Path(tempfile.mkdtemp(prefix=dest.name + _NEW, dir=dest.parent))
        budget = _Budget(max_files, max_bytes)
        committed: List[bool] = []
        try:
            src_fd = os.open(_real(source), _DIR_FLAGS)
            try:
                _copy_dir(src_fd, str(staging), "", budget, 0)
            finally:
                os.close(src_fd)
            os.chmod(staging, 0o755)  # mkdtemp made it 0700
            _publish(staging, dest, committed)
        except BaseException:
            if not committed:  # after the exchange, staging holds the old tree
                _remove(staging)
            raise
    return budget.skipped


# Sibling names beside a copy: its lock, staging dirs and replaced trees.
_LOCK = ".lock"
_NEW = ".new-"
_OLD = ".old-"
# A replaced tree stays this long: a reader that resolved part of a path into it
# just before the swap still finds it. Swept by the next copy of the same dest.
OLD_GRACE_SECONDS = 600
# A staging dir is this old only when its copy died without cleaning up.
STALE_STAGING_SECONDS = 3600

_RENAME_EXCHANGE = 2
_AT_FDCWD = -100


@contextlib.contextmanager
def _locked(dest: Path):
    fd = os.open(
        str(dest) + _LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _exchange(a: Path, b: Path) -> None:
    """Atomically swap two paths (``renameat2(RENAME_EXCHANGE)``, Linux)."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available")
    rc = renameat2(
        _AT_FDCWD, os.fsencode(a), _AT_FDCWD, os.fsencode(b), _RENAME_EXCHANGE
    )
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(a))


def _publish(staging: Path, dest: Path, committed: List[bool]) -> None:
    """Make *staging* the tree at *dest*; the tree *dest* held moves aside to
    ``<dest>.old-*`` (removed after :data:`OLD_GRACE_SECONDS`).

    An existing copy is exchanged in one ``renameat2(RENAME_EXCHANGE)`` call:
    *dest* always names a whole tree. A filesystem without it is refused
    (:class:`LocalCopyError`) and keeps the current copy, rather than leave
    *dest* missing between two renames. The exchange is the commit: what
    follows only parks the old tree, and a failure there is logged, never
    raised — the new copy is published, and the old tree stays where the
    exchange left it (``<dest>.new-*``, swept as a stale staging dir). Each
    commit appends to *committed*, so the caller never removes *staging*
    after it (by then it is the tree a reader may still be inside)."""
    try:
        st = os.lstat(dest)
    except FileNotFoundError:
        os.rename(staging, dest)  # no copy yet: nothing for a reader to lose
        committed.append(True)
        return
    if not stat.S_ISDIR(st.st_mode):
        _remove(dest)  # not a copy of ours: a link or file planted there
        os.rename(staging, dest)
        committed.append(True)
        return
    try:
        _exchange(staging, dest)
    except OSError as exc:
        if exc.errno in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise LocalCopyError(
                f"cannot replace {dest} atomically (renameat2 RENAME_EXCHANGE: "
                f"{exc.strerror}); the current copy is kept"
            ) from exc
        raise
    committed.append(True)
    old = dest.with_name(dest.name + _OLD + os.urandom(6).hex())
    try:
        os.rename(staging, old)
        # The grace counts from now: the old tree's own mtime is its copy time.
        os.utime(old)
    except OSError:
        logger.warning(
            "Published %s; could not park the tree it replaced (left at %s)",
            dest,
            staging if staging.exists() else old,
            exc_info=True,
        )


def _sweep(dest: Path) -> None:
    """Remove replaced trees past their grace and staging dirs a dead copy
    left. Runs under the lock, so no live copy's staging dir is touched."""
    now = time.time()
    for entry in dest.parent.iterdir():
        name = entry.name
        if name.startswith(dest.name + _OLD):
            limit = OLD_GRACE_SECONDS
        elif name.startswith(dest.name + _NEW):
            limit = STALE_STAGING_SECONDS
        else:
            continue
        try:
            if now - os.lstat(entry).st_mtime > limit:
                _remove(entry)
        except OSError:
            pass


def _remove(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)
