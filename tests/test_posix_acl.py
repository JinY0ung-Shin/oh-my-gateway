"""POSIX ACL helper for uid isolation (``src/backends/claude/posix_acl.py``).

The encoder is pinned to the bytes this kernel stores back for the workspace
root's grant (``docs/isolation-uid-ownership-design.md`` §2.2), the filesystem
half to this kernel: what a grant stores, the mask the kernel computes for
objects created under the default preset (§3.2), and the one-time migration
walk (§3.7). The suite runs as one unprivileged uid, so another uid's access is
not exercised (the named ids are arbitrary), and foreign-owned entries and
mount points are covered through the walk's decision helper. Filesystem tests
skip where ``tmp_path`` takes no POSIX ACLs.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from src.backends.claude import posix_acl as pa
from src.backends.claude.posix_acl import (
    ACL_GROUP,
    ACL_GROUP_OBJ,
    ACL_MASK,
    ACL_OTHER,
    ACL_UNDEFINED_ID,
    ACL_USER,
    ACL_USER_OBJ,
    Entry,
)

# u::rwx, u:20000:rwx, g::---, m::rwx, o::--- as setfacl writes it and as the
# kernel stores it back.
KNOWN = bytes.fromhex(
    "0200000001000700ffffffff02000700204e000004000000ffffffff"
    "10000700ffffffff20000000ffffffff"
)
U, APP = 20000, 20001  # arbitrary ids: the kernel only needs them mapped
ACCESS_RW = "u::rwx,u:20000:rwx,g::---,m::rwx,o::---"
DEFAULT_RW = "u::rwx,u:20000:rwx,u:20001:rwx,g::---,m::rwx,o::---"

_TAG_TEXT = {
    ACL_USER_OBJ: "u",
    ACL_USER: "u",
    ACL_GROUP_OBJ: "g",
    ACL_GROUP: "g",
    ACL_MASK: "m",
    ACL_OTHER: "o",
}


def _text(entries):
    """getfacl-style short text of *entries* (``None`` stays ``None``)."""
    if entries is None:
        return None
    out = []
    for e in entries:
        who = "" if e.id == ACL_UNDEFINED_ID else str(e.id)
        perm = "".join(c if e.perm & b else "-" for c, b in zip("rwx", (4, 2, 1)))
        out.append(f"{_TAG_TEXT[e.tag]}:{who}:{perm}")
    return ",".join(out)


def _acl(path, *, default=False):
    """The ACL text of *path* read with a raw lgetxattr (not the module)."""
    name = pa.DEFAULT_XATTR if default else pa.ACCESS_XATTR
    try:
        data = os.getxattr(path, name, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return None
        raise
    return _text(pa.decode(data))


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


@contextlib.contextmanager
def _open(path, flags=os.O_RDONLY | os.O_DIRECTORY):
    fd = os.open(path, flags)
    try:
        yield fd
    finally:
        os.close(fd)


def _entries(root):
    """Every non-symlink path below *root* (``os.walk`` never follows links)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                out.append(path)
    return sorted(out)


@pytest.fixture
def acl_root(tmp_path):
    """A directory on a filesystem that takes POSIX ACLs naming U and APP."""
    root = tmp_path / "acl"
    root.mkdir()
    if not pa.supported(root):
        pytest.skip("tmp_path does not support POSIX ACLs")
    probe = root / ".probe"
    probe.mkdir()
    with _open(probe) as fd:
        try:
            pa.grant_dir(fd, U, APP)
        except OSError as exc:  # e.g. ids this user namespace does not map
            pytest.skip(f"cannot set an ACL naming {U}/{APP}: {exc}")
    probe.rmdir()
    return root


# --- encoding ---------------------------------------------------------------


def test_known_vector():
    assert pa.encode(pa.session_dir_access(U)) == KNOWN
    assert pa.decode(KNOWN) == pa.session_dir_access(U)


def test_encode_sorts_by_tag_then_id_not_by_perm():
    entries = [
        Entry(ACL_OTHER, 0),
        Entry(ACL_USER, 5, 20005),
        Entry(ACL_MASK, 7),
        Entry(ACL_GROUP, 4, 30000),
        Entry(ACL_USER, 7, U),
        Entry(ACL_GROUP_OBJ, 0),
        Entry(ACL_USER_OBJ, 7),
    ]
    stored = pa.decode(pa.encode(entries))
    assert (
        _text(stored)
        == "u::rwx,u:20000:rwx,u:20005:r-x,g::---,g:30000:r--,m::rwx,o::---"
    )
    # Entry's own ordering is (tag, perm, id): it would put u:20005:r-x first.
    assert sorted(entries) != stored


@pytest.mark.parametrize(
    "entries",
    [
        pa.session_dir_access(U),
        pa.session_dir_access(U, write=False),
        pa.session_dir_default(U, APP),
        pa.session_dir_default(U, U),
        [
            Entry(ACL_USER_OBJ, 6),
            Entry(ACL_GROUP_OBJ, 4),
            Entry(ACL_GROUP, 5, 0),
            Entry(ACL_MASK, 5),
            Entry(ACL_OTHER, 4),
        ],
        [
            Entry(ACL_OTHER, 0),
            Entry(ACL_USER, 7, 0xFFFFFFFE),
            Entry(ACL_USER, 0, 0),
            Entry(ACL_MASK, 0),
            Entry(ACL_GROUP_OBJ, 4),
            Entry(ACL_USER_OBJ, 6),
        ],
    ],
)
def test_round_trip(entries):
    data = pa.encode(entries)
    assert len(data) == 4 + 8 * len(entries)
    assert pa.decode(data) == sorted(entries, key=lambda e: (e.tag, e.id))
    assert pa.encode(pa.decode(data)) == data


_BASE = [Entry(ACL_USER_OBJ, 7), Entry(ACL_GROUP_OBJ, 0), Entry(ACL_OTHER, 0)]
_MASK = Entry(ACL_MASK, 7)
_NAMED = Entry(ACL_USER, 7, U)
_STR_ID = Entry(ACL_USER, 7, "20000")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "entries, match",
    [
        ([], "no USER_OBJ"),
        (_BASE[1:], "no USER_OBJ"),
        ([_BASE[0], _BASE[2]], "no GROUP_OBJ"),
        (_BASE[:2], "no OTHER"),
        ([*_BASE, Entry(ACL_USER_OBJ, 5)], "duplicate"),
        ([*_BASE, _NAMED], "need a MASK"),
        ([*_BASE, Entry(ACL_GROUP, 5, 30000)], "need a MASK"),
        ([*_BASE, _MASK], "no named entry"),
        ([*_BASE, _MASK, _NAMED, Entry(ACL_MASK, 5)], "duplicate"),
        ([*_BASE, _MASK, _NAMED, Entry(ACL_USER, 5, U)], "duplicate"),
        ([*_BASE, _MASK, Entry(ACL_USER, 7, ACL_UNDEFINED_ID)], "invalid id"),
        ([*_BASE, _MASK, Entry(ACL_USER, 7, -1)], "invalid id"),
        ([*_BASE, _MASK, Entry(ACL_GROUP, 7, 2**32)], "invalid id"),
        ([*_BASE, _MASK, _STR_ID], "not an int"),
        ([Entry(ACL_USER_OBJ, 7, 1000), *_BASE[1:]], "unnamed ACL entry with an id"),
        ([*_BASE[:2], Entry(ACL_OTHER, 8)], "outside rwx"),
        ([*_BASE[:2], Entry(ACL_OTHER, -1)], "outside rwx"),
        ([*_BASE[:2], Entry(ACL_OTHER, True)], "outside rwx"),
        ([*_BASE, Entry(0x40, 7)], "unknown ACL tag"),
        ([*_BASE, (ACL_MASK, 7, ACL_UNDEFINED_ID)], "not an ACL entry"),
    ],
)
def test_encode_rejects(entries, match):
    with pytest.raises(ValueError, match=match):
        pa.encode(entries)


def _blob(*entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)


@pytest.mark.parametrize(
    "data, match",
    [
        (b"", "malformed ACL xattr value"),
        (b"\x02\x00\x00", "malformed ACL xattr value"),
        (KNOWN + b"\x00", "malformed ACL xattr value"),
        (KNOWN[:-1], "malformed ACL xattr value"),
        (b"\x01\x00\x00\x00" + KNOWN[4:], "version 1"),
        (_blob((0x40, 7, ACL_UNDEFINED_ID)), "malformed ACL entry"),
        (_blob((ACL_OTHER, 8, ACL_UNDEFINED_ID)), "malformed ACL entry"),
    ],
)
def test_decode_rejects(data, match):
    with pytest.raises(ValueError, match=match):
        pa.decode(data)


def test_decode_reads_any_well_formed_value():
    # setfacl may store a lone mask; decode does not re-check the ACL rules.
    lone_mask = _blob(
        (ACL_USER_OBJ, 7, ACL_UNDEFINED_ID),
        (ACL_GROUP_OBJ, 5, ACL_UNDEFINED_ID),
        (ACL_MASK, 3, ACL_UNDEFINED_ID),
        (ACL_OTHER, 0, ACL_UNDEFINED_ID),
    )
    assert _text(pa.decode(lone_mask)) == "u::rwx,g::r-x,m::-wx,o::---"
    assert pa.decode(_blob()) == []
    assert pa.decode(bytearray(KNOWN)) == pa.session_dir_access(U)


def test_presets():
    assert _text(pa.session_dir_access(U)) == ACCESS_RW
    assert (
        _text(pa.session_dir_access(U, write=False))
        == "u::rwx,u:20000:r-x,g::---,m::r-x,o::---"
    )
    assert _text(pa.session_dir_default(U, APP)) == DEFAULT_RW
    assert _text(pa.session_dir_default(APP, U)) == DEFAULT_RW
    # The same uid twice is one named entry (the kernel would store both).
    assert (
        _text(pa.session_dir_default(U, U)) == "u::rwx,u:20000:rwx,g::---,m::rwx,o::---"
    )
    assert Entry(ACL_MASK, 7).id == ACL_UNDEFINED_ID
    # In stored order, so get_acl() after set_acl(preset) compares equal.
    for preset in (
        pa.session_dir_access(U),
        pa.session_dir_access(U, write=False),
        pa.session_dir_default(U, APP),
        pa.session_dir_default(APP, U),
        pa.session_dir_default(U, 1000),
    ):
        assert preset == sorted(preset, key=lambda e: (e.tag, e.id))


# --- get / set on this filesystem -------------------------------------------


def test_set_and_get_on_a_directory(acl_root):
    d = acl_root / "d"
    d.mkdir()
    os.chmod(d, 0o755)
    with _open(d) as fd:
        assert pa.get_acl(fd) is None
        assert pa.get_acl(fd, default=True) is None
        pa.set_acl(fd, pa.session_dir_access(U))
        assert os.getxattr(fd, pa.ACCESS_XATTR) == KNOWN  # what the kernel kept
        assert pa.get_acl(fd) == pa.session_dir_access(U)
        pa.set_acl(fd, pa.session_dir_default(U, APP), default=True)
        assert pa.get_acl(fd, default=True) == pa.session_dir_default(U, APP)
        assert pa.get_acl(fd) == pa.session_dir_access(U)
    assert _mode(d) == 0o770  # the group bits are the mask


def test_set_and_get_on_a_file(acl_root):
    f = acl_root / "f"
    f.write_text("x")
    os.chmod(f, 0o644)
    acl = [
        Entry(ACL_USER_OBJ, 6),
        Entry(ACL_USER, 4, U),
        Entry(ACL_GROUP_OBJ, 4),
        Entry(ACL_MASK, 4),
        Entry(ACL_OTHER, 0),
    ]
    with _open(f, os.O_RDONLY) as fd:
        pa.set_acl(fd, acl)
        assert pa.get_acl(fd) == acl
        with pytest.raises(PermissionError):  # a default ACL is for directories
            pa.set_acl(fd, pa.session_dir_default(U, APP), default=True)
        assert pa.get_acl(fd, default=True) is None
    assert _mode(f) == 0o640


def test_a_base_only_access_acl_is_stored_as_the_mode(acl_root):
    d = acl_root / "d"
    d.mkdir()
    base = [Entry(ACL_USER_OBJ, 7), Entry(ACL_GROUP_OBJ, 5), Entry(ACL_OTHER, 1)]
    with _open(d) as fd:
        pa.set_acl(fd, pa.session_dir_access(U))
        pa.set_acl(fd, base)
        assert pa.get_acl(fd) is None
    assert _mode(d) == 0o751


def test_an_o_path_fd_cannot_carry_an_acl(acl_root):
    d = acl_root / "d"
    d.mkdir()
    with _open(d, os.O_PATH | os.O_DIRECTORY) as fd:
        with pytest.raises(OSError) as info:
            pa.grant_dir(fd, U, APP)
    assert info.value.errno == errno.EBADF
    assert _acl(d) is None


def test_named_users(acl_root):
    d = acl_root / "d"
    d.mkdir()
    with _open(d) as fd:
        assert pa.named_users(fd) == []
        pa.set_acl(fd, pa.session_dir_default(U, APP), default=True)
        assert pa.named_users(fd) == []  # only the access ACL counts
        pa.set_acl(fd, pa.session_dir_access(U))
        assert pa.named_users(fd) == [U]
        pa.set_acl(
            fd,
            [
                Entry(ACL_USER_OBJ, 7),
                Entry(ACL_USER, 7, 20005),
                Entry(ACL_USER, 5, U),
                Entry(ACL_GROUP_OBJ, 0),
                Entry(ACL_GROUP, 7, 30000),
                Entry(ACL_MASK, 7),
                Entry(ACL_OTHER, 0),
            ],
        )
        assert pa.named_users(fd) == [U, 20005]


def test_unsupported_and_absent_acls(tmp_path, monkeypatch):
    def failing(code):
        def fake(*args, **kwargs):
            raise OSError(code, os.strerror(code))

        return fake

    with _open(tmp_path) as fd:
        monkeypatch.setattr(pa.os, "getxattr", failing(errno.ENOTSUP))
        with pytest.raises(pa.AclUnsupported) as info:
            pa.get_acl(fd)
        assert isinstance(info.value, OSError)
        assert info.value.errno == errno.ENOTSUP
        with pytest.raises(pa.AclUnsupported):
            pa.named_users(fd)
        monkeypatch.setattr(pa.os, "getxattr", failing(errno.ENODATA))
        assert pa.get_acl(fd) is None
        assert pa.get_acl(fd, default=True) is None
        assert pa.named_users(fd) == []
        monkeypatch.setattr(pa.os, "getxattr", failing(errno.EPERM))
        with pytest.raises(PermissionError):
            pa.get_acl(fd)
        monkeypatch.setattr(pa.os, "setxattr", failing(errno.EOPNOTSUPP))
        with pytest.raises(pa.AclUnsupported):
            pa.set_acl(fd, pa.session_dir_access(U))
        with pytest.raises(pa.AclUnsupported):
            pa.grant_dir(fd, U, APP)
        with pytest.raises(ValueError):  # refused before the filesystem is asked
            pa.set_acl(fd, [])
        monkeypatch.setattr(pa.os, "setxattr", failing(errno.EPERM))
        with pytest.raises(PermissionError):
            pa.set_acl(fd, pa.session_dir_access(U))


def test_supported(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        pa.supported(tmp_path / "missing")

    def answer(code):
        def fake(*args, **kwargs):
            raise OSError(code, os.strerror(code))

        return fake

    monkeypatch.setattr(pa.os, "getxattr", answer(errno.EOPNOTSUPP))
    assert pa.supported(tmp_path) is False
    monkeypatch.setattr(pa.os, "getxattr", answer(errno.ENODATA))
    assert pa.supported(tmp_path) is True
    monkeypatch.setattr(pa.os, "getxattr", lambda *args, **kwargs: KNOWN)
    assert pa.supported(tmp_path) is True
    monkeypatch.setattr(pa.os, "getxattr", answer(errno.EACCES))
    with pytest.raises(PermissionError):
        pa.supported(tmp_path)


def test_supported_follows_a_symlinked_directory(acl_root):
    link = acl_root / "link"
    os.symlink(acl_root, link)
    assert pa.supported(link) is True


# --- the default preset: masks the kernel computes ---------------------------

_CREATE = """
import os, sys
if len(sys.argv) > 2:
    os.umask(int(sys.argv[2], 8))
d = sys.argv[1]
flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
for name, mode in (("f600", 0o600), ("f666", 0o666)):
    os.close(os.open(os.path.join(d, name), flags, mode))
for name, mode in (("d700", 0o700), ("d777", 0o777)):
    os.mkdir(os.path.join(d, name), mode)
"""


def _assert_created_masks(ws):
    named = "u:20000:rwx,u:20001:rwx"
    assert _acl(ws / "f600") == f"u::rw-,{named},g::---,m::---,o::---"
    assert _acl(ws / "f666") == f"u::rw-,{named},g::---,m::rw-,o::---"
    assert _acl(ws / "d700") == f"u::rwx,{named},g::---,m::---,o::---"
    assert _acl(ws / "d777") == f"u::rwx,{named},g::---,m::rwx,o::---"
    for d in ("d700", "d777"):  # a new directory inherits the default as well
        assert _acl(ws / d, default=True) == DEFAULT_RW
    assert (_mode(ws / "f600"), _mode(ws / "f666")) == (0o600, 0o660)
    assert (_mode(ws / "d700"), _mode(ws / "d777")) == (0o700, 0o770)


@pytest.mark.parametrize("umask", [None, "077", "000"])
def test_kernel_mask_of_objects_created_under_the_default_preset(acl_root, umask):
    # Under a default ACL the umask plays no part: the mask is the default
    # mask cut to the group bits of the mode the creating call passed.
    ws = acl_root / "ws"
    ws.mkdir()
    with _open(ws) as fd:
        pa.grant_dir(fd, U, APP)
    argv = [sys.executable, "-I", "-c", _CREATE, str(ws)]
    subprocess.run(argv + ([umask] if umask else []), check=True, timeout=60)
    _assert_created_masks(ws)


def test_same_uid_default_has_one_named_entry(acl_root):
    ws = acl_root / "ws"
    ws.mkdir()
    with _open(ws) as fd:
        pa.grant_dir(fd, U, U)
    assert _acl(ws, default=True) == "u::rwx,u:20000:rwx,g::---,m::rwx,o::---"
    os.close(os.open(ws / "f", os.O_CREAT | os.O_WRONLY, 0o666))
    assert _acl(ws / "f") == "u::rw-,u:20000:rwx,g::---,m::rw-,o::---"


def test_a_named_entry_may_name_the_owner(acl_root):
    me = os.geteuid()  # as in production: the gateway names itself
    ws = acl_root / "ws"
    ws.mkdir()
    with _open(ws) as fd:
        pa.grant_dir(fd, U, me)
        assert pa.get_acl(fd, default=True) == pa.session_dir_default(U, me)
    os.mkdir(ws / "d", 0o777)
    with _open(ws / "d") as fd:
        assert pa.get_acl(fd, default=True) == pa.session_dir_default(U, me)


# --- grant_dir ----------------------------------------------------------------


def test_grant_dir(acl_root):
    d = acl_root / "d"
    d.mkdir()
    with _open(d) as fd:
        pa.grant_dir(fd, U, APP)
        assert _text(pa.get_acl(fd)) == ACCESS_RW
        assert _text(pa.get_acl(fd, default=True)) == DEFAULT_RW
    assert _mode(d) == 0o770  # the group bits are the mask
    os.chmod(d, 0o750)  # a chmod after the grant rewrites the mask ...
    assert _acl(d) == "u::rwx,u:20000:rwx,g::---,m::r-x,o::---"
    with _open(d) as fd:
        pa.grant_dir(fd, U, APP)  # ... and granting again restores it
    assert _acl(d) == ACCESS_RW
    assert _acl(d, default=True) == DEFAULT_RW
    assert _mode(d) == 0o770


def test_grant_dir_read_only_without_default(acl_root):
    d = acl_root / "d"
    d.mkdir()
    with _open(d) as fd:
        pa.grant_dir(fd, U, APP, write=False, default=False)
        assert pa.get_acl(fd, default=True) is None
    assert _acl(d) == "u::rwx,u:20000:r-x,g::---,m::r-x,o::---"
    assert _mode(d) == 0o750


def test_grant_dir_refuses_a_non_directory(acl_root):
    f = acl_root / "f"
    f.write_text("")
    with _open(f, os.O_RDONLY) as fd:
        with pytest.raises(NotADirectoryError):
            pa.grant_dir(fd, U, APP)
    assert _acl(f) is None


# --- grant_tree ---------------------------------------------------------------


def _tree(acl_root):
    """A workspace from before uid mode, plus what the walk must leave alone.

    Root entries: dir/ (dir/sub/deep.txt, dir/hard2.txt), file.txt,
    script.sh, hard.txt (linked as dir/hard2.txt), link.txt and dirlink/
    (symlinks out of the tree) and a FIFO."""
    root = acl_root / "ws"
    outside = acl_root / "outside"
    for d in (root, root / "dir", root / "dir" / "sub", outside):
        d.mkdir()
    os.chmod(root / "dir", 0o755)
    os.chmod(root / "dir" / "sub", 0o700)
    for path, mode in {
        root / "file.txt": 0o644,
        root / "script.sh": 0o755,
        root / "dir" / "sub" / "deep.txt": 0o600,
        root / "hard.txt": 0o644,
        outside / "target.txt": 0o644,
    }.items():
        path.write_text(path.name)
        os.chmod(path, mode)
    os.link(root / "hard.txt", root / "dir" / "hard2.txt")
    os.symlink(outside / "target.txt", root / "link.txt")
    os.symlink(outside, root / "dirlink")
    os.mkfifo(root / "fifo")
    return root, outside


def test_grant_tree(acl_root):
    root, outside = _tree(acl_root)
    before = _fd_count()
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    assert _fd_count() == before
    # skipped: the two names of the hard-linked file, two symlinks, the FIFO
    assert stats == pa.GrantStats(dirs=2, files=3, skipped=5)
    assert _acl(root) is None  # the root is the caller's (grant_dir)
    assert _acl(root, default=True) is None
    assert _acl(root / "dir") == "u::rwx,u:20000:rwx,g::r-x,m::rwx,o::r-x"
    assert _acl(root / "dir" / "sub") == "u::rwx,u:20000:rwx,g::---,m::rwx,o::---"
    for d in ("dir", "dir/sub"):
        assert _acl(root / d, default=True) == DEFAULT_RW
    assert _acl(root / "file.txt") == "u::rw-,u:20000:rw-,g::r--,m::rw-,o::r--"
    assert _acl(root / "script.sh") == "u::rwx,u:20000:rwx,g::r-x,m::rwx,o::r-x"
    deep = root / "dir" / "sub" / "deep.txt"
    assert _acl(deep) == "u::rw-,u:20000:rw-,g::---,m::rw-,o::---"
    # owner and other bits stay; the group bits now show the mask
    assert _mode(root / "dir") == 0o775
    assert _mode(root / "dir" / "sub") == 0o770
    assert _mode(root / "file.txt") == 0o664
    assert _mode(deep) == 0o660
    for path in (
        root / "hard.txt",
        root / "dir" / "hard2.txt",
        root / "fifo",
        outside,
        outside / "target.txt",
    ):
        assert _acl(path) is None, path
    assert _acl(outside, default=True) is None


def test_grant_tree_twice_changes_nothing(acl_root):
    # The second run must not take the mask (the group bits once an ACL
    # exists) for the group class: file.txt keeps g::r--, not g::rw-.
    root, _ = _tree(acl_root)

    def snapshot():
        return {p: (_acl(p), _acl(p, default=True), _mode(p)) for p in _entries(root)}

    with _open(root) as fd:
        first = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    granted = snapshot()
    with _open(root) as fd:
        second = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    assert second == first
    assert snapshot() == granted
    assert _acl(root / "file.txt") == "u::rw-,u:20000:rw-,g::r--,m::rw-,o::r--"


@pytest.mark.parametrize(
    "group_obj, mask, expected",
    [
        (7, 4, "u::rw-,u:20000:rw-,g::r--,m::rw-,o::---"),  # masked down
        (0, 7, "u::rw-,u:20000:rw-,g::---,m::rw-,o::---"),  # mask wider
        (5, 7, "u::rw-,u:20000:rw-,g::r-x,m::rwx,o::---"),
    ],
)
def test_grant_tree_keeps_the_group_class_of_an_existing_acl(
    acl_root, group_obj, mask, expected
):
    # With an ACL the group bits are the mask; the group class's permission
    # is GROUP_OBJ cut by it. Another named entry (u:30000) is replaced.
    root = acl_root / "ws"
    root.mkdir()
    f = root / "f"
    f.write_text("")
    existing = [
        Entry(ACL_USER_OBJ, 6),
        Entry(ACL_USER, 7, 30000),
        Entry(ACL_GROUP_OBJ, group_obj),
        Entry(ACL_MASK, mask),
        Entry(ACL_OTHER, 0),
    ]
    with _open(f, os.O_RDONLY) as fd:
        pa.set_acl(fd, existing)
    with _open(root) as fd:
        assert pa.grant_tree(fd, U, APP, owner=os.geteuid()) == pa.GrantStats(files=1)
    assert _acl(f) == expected


def test_grant_tree_leaves_entries_of_another_owner(acl_root):
    root, _ = _tree(acl_root)
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid() + 1)
    # nothing at the root level qualifies, so nothing below it is visited
    assert stats == pa.GrantStats(skipped=7)
    assert all(_acl(p) is None for p in _entries(root))


def test_grant_tree_stops_after_max_entries(acl_root):
    root = acl_root / "ws"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}").write_text("")
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid(), max_entries=4)
    assert stats == pa.GrantStats(files=4, truncated=True)
    assert sum(_acl(p) is not None for p in root.iterdir()) == 4
    with _open(root) as fd:  # exactly the cap: nothing was left over
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid(), max_entries=10)
    assert stats == pa.GrantStats(files=10)
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid(), max_entries=0)
    assert stats == pa.GrantStats(truncated=True)


def test_grant_tree_enters_at_most_max_depth_levels(acl_root, monkeypatch):
    monkeypatch.setattr(pa, "_MAX_DEPTH", 1)
    root = acl_root / "ws"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "x.txt").write_text("")
    (root / "a" / "b" / "c.txt").write_text("")
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    assert stats == pa.GrantStats(dirs=2, files=1, truncated=True)
    assert _acl(root / "a" / "b") is not None  # granted, not entered
    assert _acl(root / "a" / "b" / "c.txt") is None


def test_grant_tree_counts_an_entry_error_and_goes_on(acl_root, monkeypatch):
    root, _ = _tree(acl_root)
    bad = os.stat(root / "dir").st_ino
    real = pa.set_acl

    def flaky(fd, entries, *, default=False):
        if os.fstat(fd).st_ino == bad:
            raise OSError(errno.EIO, "injected")
        real(fd, entries, default=default)

    monkeypatch.setattr(pa, "set_acl", flaky)
    before = _fd_count()
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    assert _fd_count() == before
    # dir/ failed and is not entered; the root's other entries are done
    assert stats == pa.GrantStats(files=2, skipped=4, errors=1)
    assert _acl(root / "dir") is None
    assert _acl(root / "dir" / "sub") is None
    assert _acl(root / "file.txt") is not None
    assert _acl(root / "script.sh") is not None


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens through the mode bits")
def test_grant_tree_entries_the_owner_cannot_open_or_look_up(acl_root):
    root = acl_root / "ws"
    root.mkdir()
    (root / "locked").mkdir()
    (root / "locked" / "inner.txt").write_text("")
    (root / "noread.txt").write_text("")
    (root / "ok.txt").write_text("")
    os.chmod(root / "noread.txt", 0o200)  # the owner cannot open it O_RDONLY
    os.chmod(root / "locked", 0o600)  # listable, but no lookup inside
    try:
        with _open(root) as fd:
            stats = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    finally:
        os.chmod(root / "locked", 0o700)
    # locked/ granted, its inner.txt cannot be lstat'ed (an error),
    # noread.txt skipped, ok.txt granted
    assert stats == pa.GrantStats(dirs=1, files=1, skipped=1, errors=1)
    assert _acl(root / "noread.txt") is None


@pytest.mark.parametrize("swap", ["hardlink", "rename", "symlink"])
def test_grant_tree_skips_an_entry_swapped_after_its_lstat(acl_root, monkeypatch, swap):
    # The race the fstat re-check closes: a.txt qualified at its lstat, then
    # its name was pointed elsewhere before the open.
    root = acl_root / "ws"
    root.mkdir()
    entry = root / "a.txt"
    entry.write_text("a")
    outside = acl_root / "outside.txt"
    outside.write_text("secret")
    real_open = os.open

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "a.txt" and dir_fd is not None:
            if swap == "rename":  # another single-link file takes the name
                os.rename(outside, entry)
            else:
                entry.unlink()
                if swap == "hardlink":
                    os.link(outside, entry)
                else:
                    os.symlink(outside, entry)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pa.os, "open", racing_open)
    with _open(root) as fd:
        stats = pa.grant_tree(fd, U, APP, owner=os.geteuid())
    assert stats == pa.GrantStats(skipped=1)
    if swap == "rename":  # the name holds the other file now: not granted
        assert _acl(entry) is None
    else:  # the file outside (also behind the swapped-in name) is untouched
        assert _acl(outside) is None


def test_grant_tree_opens_relative_and_never_through_a_link(acl_root, monkeypatch):
    root, _ = _tree(acl_root)
    real_open = os.open
    seen = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        seen.append((path, flags, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pa.os, "open", recording_open)
    with _open(root) as fd:
        pa.grant_tree(fd, U, APP, owner=os.geteuid())
    walk = [(p, f) for p, f, dfd in seen if dfd is not None]
    assert {p for p, _ in walk} == {"dir", "sub", "file.txt", "script.sh", "deep.txt"}
    for path, flags in walk:
        assert flags & (os.O_NOFOLLOW | os.O_CLOEXEC) == os.O_NOFOLLOW | os.O_CLOEXEC
        assert not flags & (os.O_PATH | os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        if path in ("dir", "sub"):
            assert flags & os.O_DIRECTORY
        else:  # a FIFO swapped in cannot block the walk
            assert flags & (os.O_NONBLOCK | os.O_NOCTTY) == os.O_NONBLOCK | os.O_NOCTTY


def test_grant_tree_lets_unsupported_propagate(acl_root, monkeypatch):
    root, _ = _tree(acl_root)

    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

    monkeypatch.setattr(pa.os, "setxattr", unsupported)
    before = _fd_count()
    with _open(root) as fd:
        with pytest.raises(pa.AclUnsupported):
            pa.grant_tree(fd, U, APP, owner=os.geteuid())
    assert _fd_count() == before


def test_grant_tree_checks_the_uids_before_walking(acl_root):
    root, _ = _tree(acl_root)
    with _open(root) as fd:
        with pytest.raises(ValueError):
            pa.grant_tree(fd, -1, APP, owner=os.geteuid())
    assert all(_acl(p) is None for p in _entries(root))


def _st(mode, *, uid=1000, dev=1, nlink=1):
    return os.stat_result((mode, 7, dev, nlink, uid, uid, 0, 0, 0, 0))


@pytest.mark.parametrize(
    "st, kind",
    [
        (_st(stat.S_IFREG | 0o644), "file"),
        (_st(stat.S_IFREG | 0o000), "file"),  # whether it opens is decided later
        (_st(stat.S_IFREG | 0o644, nlink=2), None),  # a hard link
        (_st(stat.S_IFREG | 0o644, uid=20000), None),  # another owner
        (_st(stat.S_IFREG | 0o644, dev=2), "file"),  # only directories are mounts
        (_st(stat.S_IFDIR | 0o755), "dir"),
        (_st(stat.S_IFDIR | 0o755, nlink=5), "dir"),
        (_st(stat.S_IFDIR | 0o755, uid=0), None),
        (_st(stat.S_IFDIR | 0o755, dev=2), None),  # a mount point
        (_st(stat.S_IFLNK | 0o777), None),
        (_st(stat.S_IFIFO | 0o644), None),
        (_st(stat.S_IFSOCK | 0o755), None),
        (_st(stat.S_IFCHR | 0o666), None),
        (_st(stat.S_IFBLK | 0o660), None),
    ],
)
def test_classify(st, kind):
    assert pa._classify(st, 1000, 1) == kind
