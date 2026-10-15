"""POSIX ACLs (``system.posix_acl_*`` xattrs) in pure Python, for uid isolation.

``CLAUDE_ISOLATION_MODE=uid`` keeps the workspace and the per-uid config dir
owned by the gateway (app) uid and opens them to the session uid ``U`` with
POSIX ACLs (``docs/isolation-uid-ownership-design.md`` §3.1, §3.7, §4.1). The
image ships no ``acl`` package (no ``setfacl``), so this module writes the
kernel's xattr format itself with ``os.setxattr`` on an fd, byte-identical to
what ``setfacl`` writes (§2.2): a little-endian ``u32`` version 2, then
``(u16 tag, u16 perm, u32 id)`` entries sorted by (tag, id), id ``0xffffffff``
for the unnamed ones. stdlib only, and importing it does no work: the gateway
imports it on the spawn path.

Kernel rules a caller has to keep in mind (Linux):

* An access ACL with named entries sets the object's mode group bits to its
  MASK, and a later ``chmod`` rewrites the mask from the new group bits
  (``chmod 0750`` caps every named entry at r-x). Never chmod a granted object;
  grant again instead (a grant is idempotent).
* Under a default ACL the umask is ignored. A new object inherits the default
  ACL as its access ACL with USER_OBJ, MASK and OTHER each cut to the matching
  bits of the mode the creating call passed (named entries, and GROUP_OBJ when
  there is a MASK, are copied as they are), and a new directory inherits it as
  its own default ACL too. So what the session uid creates with 0666/0777
  stays open to the app uid's named entry (mask rw-/rwx), while its 0600/0700
  objects get mask --- and are closed to every named entry: those the broker
  removes or reads as that uid.
* A named entry may name any uid, the owner's included. The kernel takes the
  same id twice and named entries in any order (the first match wins), so
  :func:`encode` sorts and refuses duplicates itself. Named entries need a
  MASK, and a default ACL must be a complete valid ACL as well.
* An access ACL of only the three base entries is stored as the mode alone
  (the xattr goes away). Symlinks carry no ACLs (EOPNOTSUPP), and
  ``fsetxattr`` on an ``O_PATH`` fd is EBADF: open what you grant
  ``O_RDONLY | O_NOFOLLOW``. Only the owner may set an ACL (EPERM), a default
  ACL on a non-directory is EACCES, and a named id the caller's user namespace
  does not map is EINVAL.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

# Entry tags (``e_tag``), as <linux/posix_acl.h> numbers them.
ACL_USER_OBJ = 0x01
ACL_USER = 0x02
ACL_GROUP_OBJ = 0x04
ACL_GROUP = 0x08
ACL_MASK = 0x10
ACL_OTHER = 0x20
ACL_UNDEFINED_ID = 0xFFFFFFFF  # the id of an unnamed entry

R, W, X = 4, 2, 1
_RWX = R | W | X

ACCESS_XATTR = "system.posix_acl_access"
DEFAULT_XATTR = "system.posix_acl_default"

_VERSION = 2
_HEADER = struct.Struct("<I")
_ENTRY = struct.Struct("<HHI")
_NAMED_TAGS = frozenset((ACL_USER, ACL_GROUP))
_TAGS = frozenset(
    (ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_GROUP, ACL_MASK, ACL_OTHER)
)
_REQUIRED = {ACL_USER_OBJ: "USER_OBJ", ACL_GROUP_OBJ: "GROUP_OBJ", ACL_OTHER: "OTHER"}
_UNSUPPORTED = frozenset((errno.EOPNOTSUPP, errno.ENOTSUP))

# grant_tree opens nothing through a symlink; files non-blocking and without a
# controlling tty, so a FIFO or device swapped in after the lstat cannot hang
# or claim the walk (the fstat re-check then skips it).
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
# open() failures that mean "the owner cannot open it" or "it is no longer what
# the lstat saw" (a symlink, not a directory, a socket): skipped, not errors.
_OPEN_SKIP = frozenset((errno.EACCES, errno.ELOOP, errno.ENOTDIR, errno.ENXIO))
# grant_tree enters at most this many directory levels below its root. Each
# entered level holds two fds (the directory and its scandir listing), so a
# pathologically deep tree cannot run the gateway out of descriptors.
_MAX_DEPTH = 128
_DIR, _FILE = "dir", "file"


class AclUnsupported(OSError):
    """The filesystem does not support POSIX ACLs (EOPNOTSUPP / ENOTSUP)."""


@dataclass(frozen=True, order=True)
class Entry:
    """One ACL entry: a tag, its permission bits (:data:`R` | :data:`W` |
    :data:`X`) and, for ``ACL_USER``/``ACL_GROUP``, the uid/gid it names.

    Comparison follows the field order (tag, perm, id); the stored order is
    (tag, id), which :func:`encode` applies itself.
    """

    tag: int
    perm: int
    id: int = ACL_UNDEFINED_ID


@dataclass
class GrantStats:
    """What :func:`grant_tree` did: entries granted per kind, entries left as
    they were (``skipped``), per-entry failures, and whether part of the tree
    was not visited."""

    dirs: int = 0
    files: int = 0
    skipped: int = 0
    errors: int = 0
    truncated: bool = False


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _order(entry: Entry) -> Tuple[int, int]:
    return entry.tag, entry.id


def encode(entries: Iterable[Entry]) -> bytes:
    """The xattr value of the ACL *entries*, as ``setfacl`` writes it.

    The entries are sorted by (tag, id). Raises :class:`ValueError` unless they
    form a valid ACL: exactly one ``ACL_USER_OBJ``, ``ACL_GROUP_OBJ`` and
    ``ACL_OTHER``; an ``ACL_MASK`` exactly when there is a named entry (stricter
    than the kernel, which also takes a lone mask); perms within rwx; named ids
    in ``[0, 0xfffffffe]`` and unnamed entries with :data:`ACL_UNDEFINED_ID`; no
    (tag, id) twice (the kernel would store the duplicate).
    """
    items = list(entries)
    seen = set()
    for entry in items:
        if not isinstance(entry, Entry):
            raise ValueError(f"not an ACL entry: {entry!r}")
        if not _is_int(entry.tag) or entry.tag not in _TAGS:
            raise ValueError(f"unknown ACL tag in {entry!r}")
        if not _is_int(entry.perm) or not 0 <= entry.perm <= _RWX:
            raise ValueError(f"ACL perm outside rwx in {entry!r}")
        if not _is_int(entry.id):
            raise ValueError(f"ACL id is not an int in {entry!r}")
        if entry.tag in _NAMED_TAGS:
            if not 0 <= entry.id < ACL_UNDEFINED_ID:
                raise ValueError(f"named ACL entry with an invalid id: {entry!r}")
        elif entry.id != ACL_UNDEFINED_ID:
            raise ValueError(f"unnamed ACL entry with an id: {entry!r}")
        key = _order(entry)
        if key in seen:
            raise ValueError(f"duplicate ACL entry: {entry!r}")
        seen.add(key)
    for tag, name in _REQUIRED.items():
        if (tag, ACL_UNDEFINED_ID) not in seen:
            raise ValueError(f"ACL has no {name} entry")
    named = any(tag in _NAMED_TAGS for tag, _ in seen)
    if named != ((ACL_MASK, ACL_UNDEFINED_ID) in seen):
        raise ValueError(
            "named ACL entries need a MASK entry"
            if named
            else "ACL has a MASK entry but no named entry"
        )
    items.sort(key=_order)
    body = b"".join(_ENTRY.pack(e.tag, e.perm, e.id) for e in items)
    return _HEADER.pack(_VERSION) + body


def decode(data: bytes) -> List[Entry]:
    """The entries of the ACL xattr value *data*, in stored order.

    Raises :class:`ValueError` on a malformed value: shorter than the header, a
    length that is not the header plus whole entries, a version other than 2,
    an unknown tag or perm bits outside rwx. The ACL rules :func:`encode`
    enforces are not re-checked (``setfacl`` may store a lone mask).
    """
    data = bytes(data)
    if len(data) < _HEADER.size or (len(data) - _HEADER.size) % _ENTRY.size:
        raise ValueError(f"malformed ACL xattr value ({len(data)} bytes)")
    (version,) = _HEADER.unpack_from(data)
    if version != _VERSION:
        raise ValueError(f"unsupported ACL xattr version {version}")
    entries = []
    for tag, perm, id_ in _ENTRY.iter_unpack(data[_HEADER.size :]):
        if tag not in _TAGS or perm & ~_RWX:
            raise ValueError(f"malformed ACL entry (tag {tag:#x}, perm {perm:#o})")
        entries.append(Entry(tag, perm, id_))
    return entries


def _xattr(default: bool) -> str:
    return DEFAULT_XATTR if default else ACCESS_XATTR


def _raise_if_unsupported(exc: OSError) -> None:
    if exc.errno in _UNSUPPORTED:
        raise AclUnsupported(
            exc.errno, f"POSIX ACLs not supported here: {exc.strerror}"
        ) from exc


def get_acl(fd: int, *, default: bool = False) -> Optional[List[Entry]]:
    """The access (or *default*) ACL of the object open as *fd*, in stored
    order; ``None`` when it has none (ENODATA: for an access ACL, the mode alone
    describes the object). Raises :class:`AclUnsupported` on a filesystem
    without POSIX ACLs; any other :class:`OSError` (EBADF for an ``O_PATH`` fd)
    propagates."""
    try:
        data = os.getxattr(fd, _xattr(default))
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return None
        _raise_if_unsupported(exc)
        raise
    return decode(data)


def set_acl(fd: int, entries: Iterable[Entry], *, default: bool = False) -> None:
    """Replace the access (or *default*) ACL of the object open as *fd* with
    *entries* (validated by :func:`encode`, which raises ``ValueError``).

    Raises :class:`AclUnsupported` on a filesystem without POSIX ACLs; other
    :class:`OSError` propagate (EPERM: not the owner, EACCES: a default ACL on
    a non-directory, EINVAL: an unmapped id, EBADF: an ``O_PATH`` fd).
    """
    data = encode(entries)
    try:
        os.setxattr(fd, _xattr(default), data)
    except OSError as exc:
        _raise_if_unsupported(exc)
        raise


def named_users(fd: int) -> List[int]:
    """The uids named by ``ACL_USER`` entries of *fd*'s access ACL, ascending
    and distinct; ``[]`` when it has no access ACL. :class:`AclUnsupported`
    propagates."""
    return sorted({e.id for e in get_acl(fd) or () if e.tag == ACL_USER})


def session_dir_access(uid: int, *, write: bool = True) -> List[Entry]:
    """The access ACL that opens a gateway-owned directory to *uid*.

    ``write``: ``u::rwx, u:<uid>:rwx, g::---, m::rwx, o::---``; otherwise
    ``u::rwx, u:<uid>:r-x, g::---, m::r-x, o::---``. Sorted as stored.
    """
    perm = _RWX if write else R | X
    return [
        Entry(ACL_USER_OBJ, _RWX),
        Entry(ACL_USER, perm, uid),
        Entry(ACL_GROUP_OBJ, 0),
        Entry(ACL_MASK, perm),
        Entry(ACL_OTHER, 0),
    ]


def session_dir_default(uid: int, app_uid: int) -> List[Entry]:
    """The default ACL of a directory *uid* works in: ``d:u::rwx,
    d:u:<uid>:rwx, d:u:<app_uid>:rwx, d:g::---, d:m::rwx, d:o::---``.

    What *uid* creates inside is owned by *uid*, so the ``<app_uid>`` entry is
    what keeps the gateway's access to it; the ``<uid>`` entry opens what the
    gateway creates inside to *uid*. One named entry when the two are the same
    uid. Sorted as stored.
    """
    named = [Entry(ACL_USER, _RWX, u) for u in sorted({uid, app_uid})]
    return [
        Entry(ACL_USER_OBJ, _RWX),
        *named,
        Entry(ACL_GROUP_OBJ, 0),
        Entry(ACL_MASK, _RWX),
        Entry(ACL_OTHER, 0),
    ]


def grant_dir(
    fd: int, uid: int, app_uid: int, *, write: bool = True, default: bool = True
) -> None:
    """Open the directory open as *fd* to *uid*: :func:`session_dir_access`,
    then, with *default*, :func:`session_dir_default` (*app_uid*).

    *fd* must be an ``O_RDONLY | O_DIRECTORY`` fd of a directory the caller
    owns; anything but a directory raises :class:`NotADirectoryError` before an
    ACL is touched. Idempotent: granting again restores a mask a ``chmod``
    narrowed. Never chmod the directory afterwards. The default ACL is the
    read-write preset whatever *write* says, so for a directory *uid* may only
    read (the config dir ``P``) pass ``default=False``; that leaves an
    existing default ACL as it is.
    """
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
    set_acl(fd, session_dir_access(uid, write=write))
    if default:
        set_acl(fd, session_dir_default(uid, app_uid), default=True)


def _classify(st: os.stat_result, owner: int, dev: int) -> Optional[str]:
    """What :func:`grant_tree` does with an entry whose lstat is *st*: grant a
    ``"dir"`` or a ``"file"``, or ``None`` to leave it as it is.

    Only *owner*'s regular files with a single link and *owner*'s directories
    on the root's filesystem *dev* qualify: a symlink, FIFO, socket or device
    does not, a hard-linked file would be granted under its other names too,
    and a directory on another device is a mount point.
    """
    if st.st_uid != owner:
        return None
    if stat.S_ISDIR(st.st_mode):
        return _DIR if st.st_dev == dev else None
    if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
        return _FILE
    return None


def _base_perms(fd: int, st: os.stat_result) -> Tuple[int, int, int]:
    """The (owner, group class, other) permissions of *fd* today.

    Once an access ACL exists the mode's group bits are its mask, and the
    group class's own permission is ``GROUP_OBJ`` cut by that mask: taking the
    group bits alone would widen ``GROUP_OBJ`` to the mask on a second run.
    """
    mode = st.st_mode
    user, group, other = (mode >> 6) & _RWX, (mode >> 3) & _RWX, mode & _RWX
    for entry in get_acl(fd) or ():
        if entry.tag == ACL_GROUP_OBJ:
            group &= entry.perm
    return user, group, other


def _tree_acl(
    uid: int, user: int, named: int, group: int, mask: int, other: int
) -> List[Entry]:
    return [
        Entry(ACL_USER_OBJ, user),
        Entry(ACL_USER, named, uid),
        Entry(ACL_GROUP_OBJ, group),
        Entry(ACL_MASK, mask),
        Entry(ACL_OTHER, other),
    ]


def _grant_entry(
    dfd: int,
    name: str,
    uid: int,
    dir_default: List[Entry],
    owner: int,
    dev: int,
    stats: GrantStats,
) -> Optional[int]:
    """Grant the entry *name* of the directory *dfd* (see :func:`grant_tree`)
    and count the outcome in *stats*. Returns the open fd of a granted
    directory, for the caller to enter and close."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        stats.errors += 1
        return None
    kind = _classify(st, owner, dev)
    if kind is None:
        stats.skipped += 1
        return None
    try:
        fd = os.open(name, _DIR_FLAGS if kind == _DIR else _FILE_FLAGS, dir_fd=dfd)
    except OSError as exc:
        if exc.errno in _OPEN_SKIP:
            stats.skipped += 1
        else:
            stats.errors += 1
        return None
    keep = False
    try:
        now = os.fstat(fd)
        same = (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino)
        if not same or _classify(now, owner, dev) != kind:
            stats.skipped += 1  # replaced or re-linked since the lstat
            return None
        user, group, other = _base_perms(fd, now)
        if kind == _DIR:
            set_acl(fd, _tree_acl(uid, user, _RWX, group, _RWX, other))
            set_acl(fd, dir_default, default=True)
            stats.dirs += 1
            keep = True
            return fd
        set_acl(fd, _tree_acl(uid, user, user, group, user | group, other))
        stats.files += 1
    except AclUnsupported:
        raise
    except OSError:
        stats.errors += 1
    finally:
        if not keep:
            os.close(fd)
    return None


def grant_tree(
    root_fd: int, uid: int, app_uid: int, *, owner: int, max_entries: int = 200000
) -> GrantStats:
    """Open the existing tree below the directory *root_fd* to *uid* (§3.7).

    The one-time migration of a workspace built before uid mode: a default ACL
    reaches only what is created after it, so the content already there gets
    an explicit grant. *root_fd* itself is not touched (give it
    :func:`grant_dir`); everything below it is walked depth first, never
    through a symlink, and granted with:

    * a directory: ``u::<owner bits>, u:<uid>:rwx, g::<group>, m::rwx,
      o::<other bits>``, and :func:`session_dir_default` (*app_uid*);
    * a regular file: ``u::<owner bits>, u:<uid>:<owner bits>, g::<group>,
      m::<owner bits | group>, o::<other bits>``: *uid* may do what the owner
      may, everyone else keeps what they had.

    ``<group>`` is the group class's permission: the mode's group bits, or on an
    object that already has an access ACL its ``GROUP_OBJ`` cut by the mask, so
    a second run changes nothing. Other named entries of an existing ACL are
    replaced.

    Left as they are and not entered (``skipped``): entries not owned by
    *owner* (by lstat), symlinks, anything but a regular file or a directory,
    regular files with more than one link (the grant would reach the file under
    its other names, e.g. a hard link to another user's transcript),
    directories on another filesystem than *root_fd* (mount points), entries
    *owner* cannot open, and entries that changed between the lstat and the
    open (the fd must be the same (st_dev, st_ino) and still qualify).

    The walk stops after *max_entries* entries, and a directory more than
    :data:`_MAX_DEPTH` levels below the root is granted but not entered: both
    set ``truncated``. An :class:`OSError` on one entry counts in ``errors``
    and the walk goes on (a directory whose grant failed is not entered).
    :class:`AclUnsupported` propagates, and so does a failure to list
    *root_fd* itself. Only *owner* (the caller's euid) can set the ACLs, and
    the caller should treat ``errors`` or ``truncated`` as "not done".
    """
    stats = GrantStats()
    dir_default = session_dir_default(uid, app_uid)
    encode(dir_default)  # an invalid uid fails here, before anything changes
    dev = os.fstat(root_fd).st_dev
    stack: list = []  # (directory fd, its os.scandir listing), root first
    try:
        stack.append((root_fd, os.scandir(root_fd)))
        visited = 0
        while stack:
            dfd, listing = stack[-1]
            try:
                item = next(listing, None)
            except OSError:  # reading the directory failed midway
                stats.errors += 1
                item = None
            if item is None:
                stack.pop()
                listing.close()
                if dfd != root_fd:
                    os.close(dfd)
                continue
            if visited >= max_entries:
                stats.truncated = True
                break
            visited += 1
            child = _grant_entry(dfd, item.name, uid, dir_default, owner, dev, stats)
            if child is None:
                continue
            if len(stack) > _MAX_DEPTH:
                stats.truncated = True
                os.close(child)
                continue
            try:
                stack.append((child, os.scandir(child)))
            except OSError:
                stats.errors += 1
                os.close(child)
    finally:
        for dfd, listing in stack:
            listing.close()
            if dfd != root_fd:
                os.close(dfd)
    return stats


def supported(path) -> bool:
    """Whether the filesystem holding *path* (a directory, a symlink to one
    followed) takes POSIX ACLs: reading its access ACL finds one or none
    (ENODATA) instead of failing with EOPNOTSUPP / ENOTSUP. Any other
    :class:`OSError` (a missing path, no search permission) propagates."""
    try:
        os.getxattr(path, ACCESS_XATTR)
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return True
        if exc.errno in _UNSUPPORTED:
            return False
        raise
    return True
