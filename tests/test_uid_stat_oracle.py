"""uid mode closes the stat oracle into another user's workspace (#253).

Landlock refuses ``open()`` outside a session's ruleset but does not mediate
``stat(2)`` or path lookup, so in landlock mode — every session on one uid — a
session can learn whether a path in another user's workspace exists and read
its size, mtime, inode and owner. uid mode adds the DAC check Landlock lacks:
the workspace root's access ACL (``uid_layout._grant_writable``) names only the
session's uid and gives ``other`` nothing, so another session uid cannot search
the root, and every lookup below it fails with EACCES before any inode is seen.

What stays visible is the root itself and its parent (``<base>/<user>``):
another uid can still tell that a user has a workspace, and its mtime. That is
pinned below as the known residual, not hidden.

Needs root (to act as two session uids) and a filesystem with POSIX ACLs; skips
otherwise, like the other cross-uid checks.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile

import pytest

from src.backends.claude import posix_acl as pa
from src.backends.claude import uid_layout

APP, ALICE, BOB = 1000, 20001, 20002


def _stat_as(uid: int, path: str) -> str:
    """``stat(path)`` in a child running as *uid*: ``ok`` or the errno name."""
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        os.close(read)
        try:
            os.setgroups([])
            os.setresgid(uid, uid, uid)
            os.setresuid(uid, uid, uid)
            try:
                os.stat(path)
                result = "ok"
            except OSError as exc:
                result = errno.errorcode.get(exc.errno, str(exc.errno))
        except BaseException as exc:  # noqa: BLE001
            result = f"setup failed: {exc}"
        os.write(write, result.encode())
        os._exit(0)
    os.close(write)
    with os.fdopen(read) as pipe:
        out = pipe.read()
    os.waitpid(pid, 0)
    return out


@pytest.fixture
def workspace(monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("needs root to act as two session uids")
    # The parents of a pytest tmp_path are 0700: a session uid could not even
    # reach its own workspace. Production keeps the base traversable (o+x).
    base = tempfile.mkdtemp(prefix="omg-stat-oracle-", dir="/tmp")
    try:
        os.chmod(base, 0o755)
        if not pa.supported(base):
            pytest.skip("/tmp does not support POSIX ACLs")
        user_dir = os.path.join(base, "alice")
        root = os.path.join(user_dir, "claude")
        os.makedirs(root)
        secret = os.path.join(root, "CLAUDE.md")
        with open(secret, "w") as f:
            f.write("alice's notes\n")
        for path, mode in ((user_dir, 0o755), (root, 0o755), (secret, 0o644)):
            os.chown(path, APP, APP)
            os.chmod(path, mode)
        monkeypatch.setattr(uid_layout, "_app_uid", lambda: APP)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            uid_layout._grant_writable(fd, ALICE)
        finally:
            os.close(fd)
        yield user_dir, root, secret
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_another_session_uid_cannot_stat_inside_the_workspace(workspace):
    _user_dir, root, secret = workspace
    assert _stat_as(ALICE, secret) == "ok", "the owner session still works"
    # The oracle of #253: existence, size, mtime, inode, owner — all refused.
    assert _stat_as(BOB, secret) == "EACCES"
    assert (
        _stat_as(BOB, os.path.join(root, "no-such-file")) == "EACCES"
    ), "a missing name answers like an existing one: no existence oracle"


def test_known_residual_the_workspace_root_and_user_dir_are_visible(workspace):
    """Another uid still sees that alice has a workspace (and its mtime). Closing
    this needs the user dir unsearchable too, or opaque directory names."""
    user_dir, root, _secret = workspace
    assert _stat_as(BOB, user_dir) == "ok"
    assert _stat_as(BOB, root) == "ok"
