"""Tests for backend-neutral Claude workspace skills/subagents."""

import errno
import logging
import os
import shutil
import socket
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.backends.claude import workspace_resources as wr
from src.backends.claude.workspace_resources import ensure_workspace_resources
from src.workspace_manager import WorkspaceManager


def _write(path: Path, text: str = "body\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fresh_workspace_gets_visible_resources_and_real_claude_mirrors(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ensure_workspace_resources(workspace)

    for name in ("skills", "agents"):
        visible = workspace / name
        native = workspace / ".claude" / name
        marker = native / ".oh-my-gateway-managed"
        assert visible.is_dir() and not visible.is_symlink()
        assert native.is_dir() and not native.is_symlink()
        assert marker.is_file()
        assert marker.stat().st_size == 0  # gateway metadata must not consume user quota


def test_visible_skill_is_materialized_under_claude_native_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    source = workspace / "skills" / "review" / "SKILL.md"
    _write(source, "---\nname: review\n---\nfirst\n")

    ensure_workspace_resources(workspace)

    mirror = workspace / ".claude" / "skills" / "review" / "SKILL.md"
    assert mirror.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert not mirror.is_symlink()


def test_materialization_refreshes_replaced_file_and_removes_stale_entry(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    source = workspace / "agents" / "reviewer.md"
    _write(source, "v1\n")
    ensure_workspace_resources(workspace)

    # Replacing the canonical inode (not just writing through the hard link) must
    # still be picked up on the next workspace resolve/materialization.
    replacement = workspace / "agents" / "replacement.tmp"
    replacement.write_text("v2\n", encoding="utf-8")
    replacement.replace(source)
    stale = workspace / "agents" / "stale.md"
    stale.write_text("stale\n", encoding="utf-8")
    ensure_workspace_resources(workspace)
    stale.unlink()

    ensure_workspace_resources(workspace)

    native = workspace / ".claude" / "agents"
    assert (native / "reviewer.md").read_text(encoding="utf-8") == "v2\n"
    assert not (native / "stale.md").exists()


def test_legacy_native_resources_migrate_to_visible_canonical_location(tmp_path):
    workspace = tmp_path / "workspace"
    legacy = workspace / ".claude" / "agents" / "reviewer.md"
    _write(legacy, "legacy\n")

    ensure_workspace_resources(workspace)

    visible = workspace / "agents" / "reviewer.md"
    native = workspace / ".claude" / "agents" / "reviewer.md"
    assert visible.read_text(encoding="utf-8") == "legacy\n"
    assert native.read_text(encoding="utf-8") == "legacy\n"
    assert (workspace / ".claude" / "agents" / ".oh-my-gateway-managed").is_file()


def test_old_directory_symlink_bridge_is_upgraded_to_real_native_directory(tmp_path):
    workspace = tmp_path / "workspace"
    visible = workspace / "skills"
    _write(visible / "review" / "SKILL.md", "skill\n")
    claude = workspace / ".claude"
    claude.mkdir()
    (claude / "skills").symlink_to(Path("..") / "skills", target_is_directory=True)

    ensure_workspace_resources(workspace)

    native = claude / "skills"
    assert native.is_dir()
    assert not native.is_symlink()
    assert (native / "review" / "SKILL.md").read_text(encoding="utf-8") == "skill\n"


def test_conflicting_unmanaged_native_and_visible_trees_are_preserved(tmp_path):
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "visible" / "SKILL.md", "visible\n")
    _write(workspace / ".claude" / "skills" / "native" / "SKILL.md", "native\n")

    ensure_workspace_resources(workspace)

    assert (workspace / "skills" / "visible" / "SKILL.md").read_text() == "visible\n"
    assert (workspace / ".claude" / "skills" / "native" / "SKILL.md").read_text() == "native\n"
    assert not (workspace / ".claude" / "skills" / ".oh-my-gateway-managed").exists()


def test_symlinked_visible_entry_is_not_promoted_into_claude_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    outside = tmp_path / "outside"
    _write(outside / "SKILL.md", "outside\n")
    (workspace / "skills" / "linked").symlink_to(outside, target_is_directory=True)

    ensure_workspace_resources(workspace)

    assert not (workspace / ".claude" / "skills" / "linked").exists()


def test_workspace_manager_prepares_resources_only_for_named_claude_workspace(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")

    claude = manager.resolve("alice", backend="claude")
    codex = manager.resolve("alice", backend="codex")
    anonymous = manager.resolve(None, backend="claude")

    assert (claude / "skills").is_dir()
    assert (claude / "agents").is_dir()
    assert (claude / ".claude" / "skills").is_dir()
    assert not (codex / "skills").exists()
    assert not (codex / ".claude").exists()
    assert not (anonymous / "skills").exists()


# --- what the session can plant ------------------------------------------------
# Under uid isolation the session uid may write anywhere in the workspace while
# the gateway keeps preparing (every resolve, file-browser polls included) and
# materializing it. Nothing it plants may redirect a gateway write, link, copy or
# removal outside the workspace, park the gateway on a FIFO, or make a call fail.

MARKER = ".oh-my-gateway-managed"
_not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")


def _managed_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    return workspace


def _tree(root: Path) -> dict:
    """Every entry below ``root`` with its kind and content, for before/after."""
    tree = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            tree[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            tree[rel] = ("dir",)
        else:
            tree[rel] = ("file", path.read_bytes(), path.stat().st_nlink)
    return tree


@contextmanager
def _dir_fd(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _within_deadline(fn, fifo: Path, timeout: float = 5.0):
    """Run ``fn`` in a thread; fail, after releasing it, if it parks on ``fifo``."""
    outcome = {}

    def run():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # re-raised in the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # A read-write open of a FIFO never blocks and completes any open parked
        # on it, so the worker finishes before the test fails.
        os.close(os.open(fifo, os.O_RDWR | os.O_NONBLOCK))
        worker.join(timeout)
        pytest.fail(f"blocked on the FIFO {fifo}")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _refuse_link(*args, **kwargs):
    raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))


@pytest.fixture
def lock_mode():
    """chmod paths for one test; their modes come back before tmp cleanup."""
    locked = []

    def lock(path: Path, mode: int = 0) -> None:
        locked.append((path, stat.S_IMODE(os.lstat(path).st_mode)))
        os.chmod(path, mode)

    yield lock
    for path, mode in reversed(locked):
        os.chmod(path, mode)


def test_mirror_file_is_a_hard_link_of_the_canonical_inode(tmp_path):
    workspace = _managed_workspace(tmp_path)
    source = workspace / "skills" / "review" / "SKILL.md"
    _write(source, "skill\n")

    ensure_workspace_resources(workspace)

    mirror = workspace / ".claude" / "skills" / "review" / "SKILL.md"
    assert os.path.samestat(os.stat(source), os.lstat(mirror))
    assert os.stat(source).st_nlink == 2


def test_planted_marker_symlink_is_not_followed_and_unmanages_the_mirror(
    tmp_path, caplog
):
    workspace = _managed_workspace(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    marker = workspace / ".claude" / "skills" / MARKER
    marker.unlink()
    marker.symlink_to(victim)
    _write(workspace / "skills" / "new" / "SKILL.md", "new\n")

    with caplog.at_level(logging.WARNING, logger=wr.__name__):
        ensure_workspace_resources(workspace)

    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert marker.is_symlink()
    assert not (workspace / ".claude" / "skills" / "new").exists()
    assert "Refusing Claude resource marker" in caplog.text


def test_dangling_marker_symlink_never_creates_its_target(tmp_path):
    workspace = _managed_workspace(tmp_path)
    target = tmp_path / "created-by-gateway"
    marker = workspace / ".claude" / "agents" / MARKER
    marker.unlink()
    marker.symlink_to(target)

    ensure_workspace_resources(workspace)
    # The marker write itself, as if the link appeared after the managed check.
    with _dir_fd(marker.parent) as native_fd:
        assert wr._write_marker(native_fd, marker) is False

    assert not os.path.lexists(target)
    assert marker.is_symlink()


def test_hard_linked_marker_is_refused_and_its_other_name_untouched(tmp_path):
    workspace = _managed_workspace(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    marker = workspace / ".claude" / "skills" / MARKER
    marker.unlink()
    os.link(victim, marker)
    _write(workspace / "skills" / "new" / "SKILL.md", "new\n")

    ensure_workspace_resources(workspace)
    with _dir_fd(marker.parent) as native_fd:
        assert wr._write_marker(native_fd, marker) is False
        assert wr._managed(native_fd) is False

    assert victim.read_text(encoding="utf-8") == "precious\n"
    assert not (workspace / ".claude" / "skills" / "new").exists()


def test_fifo_marker_does_not_hang_and_is_refused(tmp_path):
    workspace = _managed_workspace(tmp_path)
    marker = workspace / ".claude" / "skills" / MARKER
    marker.unlink()
    os.mkfifo(marker)
    _write(workspace / "skills" / "new" / "SKILL.md", "new\n")

    _within_deadline(lambda: ensure_workspace_resources(workspace), marker)
    with _dir_fd(marker.parent) as native_fd:
        refused = _within_deadline(lambda: wr._write_marker(native_fd, marker), marker)
        # With a reader, O_WRONLY|O_NONBLOCK opens the FIFO; the check on the
        # descriptor still refuses it before anything is written.
        reader = os.open(marker, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with_reader = _within_deadline(
                lambda: wr._write_marker(native_fd, marker), marker
            )
        finally:
            os.close(reader)

    assert refused is False and with_reader is False
    assert stat.S_ISFIFO(os.lstat(marker).st_mode)
    assert not (workspace / ".claude" / "skills" / "new").exists()


def _plant_symlink(marker: Path, tmp_path: Path) -> Path:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    marker.symlink_to(victim)
    return victim


def _plant_dangling_symlink(marker: Path, tmp_path: Path) -> Path:
    marker.symlink_to(tmp_path / "nowhere")
    return tmp_path / "nowhere"


def _plant_hard_link(marker: Path, tmp_path: Path) -> Path:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    os.link(victim, marker)
    return victim


def _plant_directory(marker: Path, tmp_path: Path) -> Path:
    marker.mkdir()
    (marker / "keep").write_text("keep\n", encoding="utf-8")
    return marker / "keep"


@pytest.mark.parametrize(
    "plant",
    [_plant_symlink, _plant_dangling_symlink, _plant_hard_link, _plant_directory],
)
def test_marker_write_refuses_whatever_was_swapped_in(tmp_path, plant):
    native = tmp_path / "native"
    native.mkdir()
    marker = native / MARKER
    victim = plant(marker, tmp_path)
    before = victim.read_bytes() if victim.exists() else None

    with _dir_fd(native) as native_fd:
        assert wr._write_marker(native_fd, marker) is False
        assert wr._managed(native_fd) is False

    if before is None:
        assert not os.path.lexists(victim)
    else:
        assert victim.read_bytes() == before


def test_marker_write_refuses_a_socket(tmp_path, monkeypatch):
    native = tmp_path / "native"
    native.mkdir()
    monkeypatch.chdir(native)  # bind relatively: tmp paths can exceed sun_path
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(MARKER)
        with _dir_fd(native) as native_fd:
            assert wr._write_marker(native_fd, native / MARKER) is False
    finally:
        server.close()
    assert stat.S_ISSOCK(os.lstat(native / MARKER).st_mode)


def test_marker_write_empties_an_old_text_marker_and_keeps_an_empty_one(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    marker = native / MARKER
    marker.write_text("managed by oh-my-gateway\n", encoding="utf-8")

    with _dir_fd(native) as native_fd:
        assert wr._write_marker(native_fd, marker) is True
        assert marker.stat().st_size == 0
        inode = marker.stat().st_ino
        assert wr._write_marker(native_fd, marker) is True
        assert wr._managed(native_fd) is True

    assert marker.stat().st_ino == inode


def test_symlinked_mirror_subdirectory_is_replaced_not_followed(tmp_path):
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "review" / "SKILL.md", "skill\n")
    ensure_workspace_resources(workspace)
    outside = tmp_path / "outside"
    _write(outside / "keep.md", "keep\n")
    before = _tree(outside)
    mirror = workspace / ".claude" / "skills" / "review"
    shutil.rmtree(mirror)
    mirror.symlink_to(outside, target_is_directory=True)
    _write(workspace / "skills" / "review" / "extra.md", "extra\n")

    ensure_workspace_resources(workspace)

    assert _tree(outside) == before
    assert mirror.is_dir() and not mirror.is_symlink()
    assert (mirror / "SKILL.md").read_text(encoding="utf-8") == "skill\n"
    assert (mirror / "extra.md").read_text(encoding="utf-8") == "extra\n"


def test_mirror_directory_swapped_for_a_symlink_mid_sync_is_not_followed(
    tmp_path, monkeypatch
):
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "review" / "SKILL.md", "skill\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    mirror = workspace / ".claude" / "skills" / "review"
    moved = mirror.with_name("review.moved")
    real = wr._materialize_file
    swapped = []

    def swap_then_materialize(src_dir_fd, dst_dir_fd, name, where, *rest):
        if not swapped:  # the walk already holds the mirror directory's fd
            mirror.rename(moved)
            mirror.symlink_to(outside, target_is_directory=True)
            swapped.append(name)
        return real(src_dir_fd, dst_dir_fd, name, where, *rest)

    monkeypatch.setattr(wr, "_materialize_file", swap_then_materialize)
    wr.materialize_workspace_resources(workspace)

    assert swapped == ["SKILL.md"]
    assert list(outside.iterdir()) == []
    # The link went into the directory the walk opened, wherever it is now.
    assert (moved / "SKILL.md").read_text(encoding="utf-8") == "skill\n"

    monkeypatch.undo()
    wr.materialize_workspace_resources(workspace)

    assert list(outside.iterdir()) == []
    assert not mirror.is_symlink()
    assert (mirror / "SKILL.md").read_text(encoding="utf-8") == "skill\n"
    assert not moved.exists()


def test_canonical_file_swapped_for_a_symlink_mid_sync_is_not_linked(
    tmp_path, monkeypatch
):
    workspace = _managed_workspace(tmp_path)
    source = workspace / "skills" / "review" / "SKILL.md"
    _write(source, "skill\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    real = wr._materialize_file
    swapped = []

    def swap_then_materialize(src_dir_fd, dst_dir_fd, name, where, *rest):
        source.unlink()
        source.symlink_to(secret)
        swapped.append(name)
        return real(src_dir_fd, dst_dir_fd, name, where, *rest)

    monkeypatch.setattr(wr, "_materialize_file", swap_then_materialize)
    wr.materialize_workspace_resources(workspace)

    assert swapped == ["SKILL.md"]
    assert os.stat(secret).st_nlink == 1
    assert not os.path.lexists(workspace / ".claude" / "skills" / "review" / "SKILL.md")


@pytest.mark.parametrize("links_refused", [False, True])
def test_materialize_file_never_links_or_copies_a_symlink_source(
    tmp_path, monkeypatch, links_refused
):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    (src / "SKILL.md").symlink_to(secret)
    if links_refused:
        monkeypatch.setattr(os, "link", _refuse_link)

    with _dir_fd(src) as src_fd, _dir_fd(dst) as dst_fd:
        assert (
            wr._materialize_file(src_fd, dst_fd, "SKILL.md", src / "SKILL.md") is False
        )

    assert list(dst.iterdir()) == []
    assert os.stat(secret).st_nlink == 1


def test_materialize_file_refuses_a_fifo_source_without_blocking(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    os.mkfifo(src / "SKILL.md")

    with _dir_fd(src) as src_fd, _dir_fd(dst) as dst_fd:
        result = _within_deadline(
            lambda: wr._materialize_file(src_fd, dst_fd, "SKILL.md", src / "SKILL.md"),
            src / "SKILL.md",
        )

    assert result is False
    assert list(dst.iterdir()) == []


def test_symlinked_claude_directory_is_never_entered(tmp_path):
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "review" / "SKILL.md", "skill\n")
    outside = tmp_path / "outside"
    _write(outside / "skills" / "stale.md", "stale\n")
    (outside / "skills" / MARKER).write_bytes(b"")
    before = _tree(outside)
    (workspace / ".claude").symlink_to(outside, target_is_directory=True)

    ensure_workspace_resources(workspace)

    assert _tree(outside) == before


def test_native_symlink_to_another_directory_is_still_refused(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "keep" / "SKILL.md", "keep\n")
    before = _tree(elsewhere)
    (workspace / ".claude").mkdir()
    native = workspace / ".claude" / "skills"
    native.symlink_to(elsewhere, target_is_directory=True)

    ensure_workspace_resources(workspace)

    assert native.is_symlink()
    assert _tree(elsewhere) == before


def test_symlinked_visible_root_is_neither_mirrored_nor_written(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "evil" / "SKILL.md", "evil\n")
    before = _tree(outside)
    (workspace / "skills").symlink_to(outside, target_is_directory=True)

    ensure_workspace_resources(workspace)

    assert _tree(outside) == before
    assert not (workspace / ".claude" / "skills").exists()


def test_stale_mirror_entries_of_every_kind_are_removed_without_following(tmp_path):
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "keep" / "SKILL.md", "keep\n")
    _write(workspace / "skills" / "doc" / "README.md", "doc\n")
    outside = tmp_path / "outside"
    _write(outside / "victim.md", "victim\n")
    before = _tree(outside)
    native = workspace / ".claude" / "skills"
    (native / "dir-link").symlink_to(outside, target_is_directory=True)
    (native / "file-link").symlink_to(outside / "victim.md")
    os.mkfifo(native / "pipe")
    _write(native / "stale" / "nested" / "old.md", "old\n")
    (native / "stale" / "nested" / "out").symlink_to(outside, target_is_directory=True)
    # Wrong types at desired names: a file where a directory belongs, and a
    # directory where a file belongs.
    (native / "keep").write_text("not a directory\n", encoding="utf-8")
    (native / "doc" / "README.md").mkdir(parents=True)

    _within_deadline(lambda: ensure_workspace_resources(workspace), native / "pipe")

    assert sorted(p.name for p in native.iterdir()) == [MARKER, "doc", "keep"]
    assert (native / "keep" / "SKILL.md").read_text(encoding="utf-8") == "keep\n"
    assert (native / "doc" / "README.md").read_text(encoding="utf-8") == "doc\n"
    assert _tree(outside) == before


def test_canonical_file_named_like_the_marker_is_never_mirrored_onto_it(tmp_path):
    """Linked onto the marker, the user's file would be what the next resolve
    truncates."""
    workspace = _managed_workspace(tmp_path)
    user_file = workspace / "skills" / MARKER
    user_file.write_text("user data\n", encoding="utf-8")

    ensure_workspace_resources(workspace)
    ensure_workspace_resources(workspace)

    marker = workspace / ".claude" / "skills" / MARKER
    assert user_file.read_text(encoding="utf-8") == "user data\n"
    assert marker.stat().st_size == 0
    assert marker.stat().st_nlink == 1
    assert not os.path.samestat(marker.stat(), user_file.stat())


def test_copy_fallback_matches_bytes_mode_and_mtime_when_links_are_refused(
    tmp_path, monkeypatch
):
    workspace = _managed_workspace(tmp_path)
    source = workspace / "skills" / "tool" / "run.sh"
    _write(source, "#!/bin/sh\necho hi\n")
    os.chmod(source, 0o750)
    os.utime(source, ns=(1_600_000_000_000_000_000, 1_650_000_000_123_456_789))
    monkeypatch.setattr(os, "link", _refuse_link)

    wr.materialize_workspace_resources(workspace)

    mirror = workspace / ".claude" / "skills" / "tool" / "run.sh"
    assert mirror.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(mirror.stat().st_mode) == 0o750
    assert mirror.stat().st_mtime_ns == source.stat().st_mtime_ns
    assert not os.path.samestat(mirror.stat(), source.stat())
    assert [p.name for p in mirror.parent.iterdir()] == ["run.sh"]  # no temp left

    _write(source, "#!/bin/sh\necho changed\n")
    wr.materialize_workspace_resources(workspace)

    assert mirror.read_bytes() == source.read_bytes()


def test_copy_fallback_drops_set_id_bits(tmp_path, monkeypatch):
    workspace = _managed_workspace(tmp_path)
    source = workspace / "skills" / "tool" / "run"
    _write(source, "binary\n")
    os.chmod(source, 0o4755)
    if not os.stat(source).st_mode & stat.S_ISUID:
        pytest.skip("this filesystem does not keep the setuid bit")
    monkeypatch.setattr(os, "link", _refuse_link)

    wr.materialize_workspace_resources(workspace)

    mirror = workspace / ".claude" / "skills" / "tool" / "run"
    assert mirror.read_bytes() == b"binary\n"
    assert stat.S_IMODE(mirror.stat().st_mode) == 0o755


def test_copy_fallback_keeps_holes(tmp_path, monkeypatch):
    workspace = _managed_workspace(tmp_path)
    source = workspace / "skills" / "big" / "sparse.bin"
    source.parent.mkdir(parents=True)
    size = 8 * 1024 * 1024
    with open(source, "wb") as fh:
        fh.truncate(size)
        fh.seek(1024 * 1024)
        fh.write(b"head")
        fh.seek(5 * 1024 * 1024)
        fh.write(b"tail")
    if os.stat(source).st_blocks * 512 >= size:
        pytest.skip("this filesystem does not keep holes")
    monkeypatch.setattr(os, "link", _refuse_link)

    wr.materialize_workspace_resources(workspace)

    mirror = workspace / ".claude" / "skills" / "big" / "sparse.bin"
    assert mirror.stat().st_size == size
    assert mirror.read_bytes() == source.read_bytes()
    assert mirror.stat().st_blocks * 512 < size // 2


def test_copy_always_advances_past_a_hole_punched_mid_copy(tmp_path, monkeypatch):
    """SEEK_HOLE returning the data offset itself (a hole punched between the two
    lseek calls) must not stall the copy loop."""
    source, copy = tmp_path / "source", tmp_path / "copy"
    source.write_bytes(b"x" * 10_000)
    real_lseek = os.lseek

    def racing_lseek(fd, pos, how):
        return pos if how == os.SEEK_HOLE else real_lseek(fd, pos, how)

    monkeypatch.setattr(os, "lseek", racing_lseek)
    with open(source, "rb") as src, open(copy, "wb") as out:
        worker = threading.Thread(
            target=wr._copy_data, args=(src.fileno(), out.fileno(), 10_000), daemon=True
        )
        worker.start()
        worker.join(5.0)
        stalled = worker.is_alive()
        monkeypatch.undo()  # a stalled loop then finishes with the real lseek
        worker.join(5.0)

    assert not stalled
    assert copy.stat().st_size == 10_000


@_not_root
def test_unreadable_skill_subtree_never_raises_and_the_rest_is_mirrored(
    tmp_path, lock_mode
):
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "locked" / "SKILL.md", "locked\n")
    ensure_workspace_resources(workspace)
    _write(workspace / "skills" / "ok" / "SKILL.md", "ok\n")
    lock_mode(workspace / "skills" / "locked")

    ensure_workspace_resources(workspace)

    native = workspace / ".claude" / "skills"
    assert (native / "ok" / "SKILL.md").read_text(encoding="utf-8") == "ok\n"
    # What the gateway cannot read is left stale, not emptied.
    assert (native / "locked" / "SKILL.md").read_text(encoding="utf-8") == "locked\n"


@_not_root
def test_unreadable_canonical_file_is_skipped_and_the_rest_mirrored(
    tmp_path, lock_mode
):
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "agents" / "secret.md", "secret\n")
    _write(workspace / "agents" / "open.md", "open\n")
    lock_mode(workspace / "agents" / "secret.md")

    ensure_workspace_resources(workspace)

    native = workspace / ".claude" / "agents"
    assert (native / "open.md").read_text(encoding="utf-8") == "open\n"
    assert not os.path.lexists(native / "secret.md")


@_not_root
@pytest.mark.parametrize(
    "locked",
    [
        ".claude",
        ".claude/skills",
        ".claude/skills/review",
        "skills",
        "skills/review",
        "skills/review/SKILL.md",
    ],
)
def test_unreadable_entries_never_raise_from_resolve_or_materialize(
    tmp_path, lock_mode, locked
):
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.resolve("alice", backend="claude")
    _write(workspace / "skills" / "review" / "SKILL.md", "skill\n")
    _write(workspace / "agents" / "reviewer.md", "agent\n")
    wr.materialize_workspace_resources(workspace)
    _write(workspace / "agents" / "second.md", "second\n")
    lock_mode(workspace / locked)

    assert manager.resolve("alice", backend="claude") == workspace
    wr.materialize_workspace_resources(workspace)
    ensure_workspace_resources(workspace)

    if locked != ".claude":
        native = workspace / ".claude" / "agents"
        assert (native / "second.md").read_text(encoding="utf-8") == "second\n"


@pytest.mark.parametrize(
    "public, internal",
    [
        ("prepare_workspace_resources", "_prepare"),
        ("materialize_workspace_resources", "_materialize"),
    ],
)
def test_public_entry_points_never_raise(
    tmp_path, monkeypatch, caplog, public, internal
):
    def boom(workspace):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(wr, internal, boom)
    with caplog.at_level(logging.WARNING, logger=wr.__name__):
        getattr(wr, public)(tmp_path)

    assert "unexpected failure" in caplog.text


def test_overdeep_trees_are_left_alone_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(wr, "_MAX_DEPTH", 3)
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "a" / "b" / "c" / "d" / "e" / "SKILL.md", "deep\n")
    _write(workspace / "skills" / "a" / "top.md", "top\n")
    stale = workspace / ".claude" / "skills" / "old" / "1" / "2" / "3" / "4"
    _write(stale / "x.md", "x\n")

    ensure_workspace_resources(workspace)

    native = workspace / ".claude" / "skills"
    assert (native / "a" / "top.md").read_text(encoding="utf-8") == "top\n"
    assert (native / "a" / "b" / "c").is_dir()
    assert not (native / "a" / "b" / "c" / "d").exists()
    # A stale tree deeper than the limit stays instead of being walked.
    assert (stale / "x.md").is_file()


# --- clearing mirrors before a one-time ACL grant, and inspecting them ---------


def test_clear_managed_mirrors_drops_mirror_names_and_materialize_restores_them(
    tmp_path,
):
    """The ACL grant skips files with more than one link; clearing the mirror
    first leaves each canonical file with one, and materializing re-links it."""
    workspace = _managed_workspace(tmp_path)
    skill = workspace / "skills" / "review" / "SKILL.md"
    agent = workspace / "agents" / "reviewer.md"
    _write(skill, "skill\n")
    _write(agent, "agent\n")
    ensure_workspace_resources(workspace)
    assert os.stat(skill).st_nlink == os.stat(agent).st_nlink == 2
    outside = tmp_path / "outside"
    _write(outside / "victim.md", "victim\n")
    before = _tree(outside)
    native = workspace / ".claude"
    (native / "skills" / "out").symlink_to(outside, target_is_directory=True)
    os.mkfifo(native / "agents" / "pipe")

    _within_deadline(
        lambda: wr.clear_managed_mirrors(workspace), native / "agents" / "pipe"
    )

    for kind in ("skills", "agents"):
        assert [p.name for p in (native / kind).iterdir()] == [MARKER]
        assert wr.is_managed_mirror(workspace, kind)
    assert skill.read_text(encoding="utf-8") == "skill\n"
    assert agent.read_text(encoding="utf-8") == "agent\n"
    assert os.stat(skill).st_nlink == os.stat(agent).st_nlink == 1
    assert _tree(outside) == before

    wr.materialize_workspace_resources(workspace)

    assert os.path.samestat(
        os.stat(skill), os.lstat(native / "skills" / "review" / "SKILL.md")
    )
    assert os.path.samestat(os.stat(agent), os.lstat(native / "agents" / "reviewer.md"))


def test_clear_managed_mirrors_leaves_unmanaged_native_dirs_alone(tmp_path):
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "visible" / "SKILL.md", "visible\n")
    # Operator-managed: no marker at all.
    _write(workspace / ".claude" / "skills" / "native" / "SKILL.md", "native\n")
    # A marker the gateway refuses (a second name) makes it unmanaged as well.
    _write(workspace / ".claude" / "agents" / "keep.md", "keep\n")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    os.link(victim, workspace / ".claude" / "agents" / MARKER)
    before = _tree(workspace)

    wr.clear_managed_mirrors(workspace)

    assert _tree(workspace) == before
    assert victim.read_text(encoding="utf-8") == "precious\n"


@pytest.mark.parametrize("linked", [".claude", ".claude/skills"])
def test_clear_managed_mirrors_never_follows_a_symlinked_claude_or_native_dir(
    tmp_path, linked
):
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "review" / "SKILL.md", "skill\n")
    outside = tmp_path / "outside"
    # What the link leads to looks exactly like a managed mirror.
    looks_managed = outside / "skills" if linked == ".claude" else outside
    _write(looks_managed / "stale.md", "stale\n")
    (looks_managed / MARKER).write_bytes(b"")
    before = _tree(outside)
    link = workspace / linked
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    wr.clear_managed_mirrors(workspace)

    assert _tree(outside) == before
    assert link.is_symlink()
    assert not wr.is_managed_mirror(workspace, "skills")


@_not_root
def test_clear_managed_mirrors_skips_what_it_cannot_remove(
    tmp_path, lock_mode, monkeypatch
):
    monkeypatch.setattr(wr, "_MAX_DEPTH", 2)
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "a" / "SKILL.md", "a\n")
    _write(workspace / "skills" / "b" / "SKILL.md", "b\n")
    ensure_workspace_resources(workspace)
    native = workspace / ".claude" / "skills"
    _write(native / "deep" / "1" / "2" / "3" / "x.md", "x\n")
    lock_mode(native / "a")

    wr.clear_managed_mirrors(workspace)

    # "a" cannot be listed and "deep" is deeper than the limit: both stay, the
    # rest is cleared, and nothing raised.
    assert sorted(p.name for p in native.iterdir()) == [MARKER, "a", "deep"]
    assert os.stat(workspace / "skills" / "b" / "SKILL.md").st_nlink == 1


def test_is_managed_mirror_applies_the_materialization_rule(tmp_path):
    workspace = _managed_workspace(tmp_path)
    assert wr.is_managed_mirror(workspace, "skills") is True
    assert wr.is_managed_mirror(workspace, "agents") is True
    # Only the two resource kinds, by name; never a path walked through links.
    for kind in ("plans", "", ".", "..", "skills/../agents", "/etc", None):
        assert wr.is_managed_mirror(workspace, kind) is False
    assert wr.is_managed_mirror(tmp_path / "missing", "skills") is False

    marker = workspace / ".claude" / "skills" / MARKER
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n", encoding="utf-8")
    marker.unlink()
    marker.symlink_to(victim)
    assert wr.is_managed_mirror(workspace, "skills") is False
    marker.unlink()
    os.link(victim, marker)
    assert wr.is_managed_mirror(workspace, "skills") is False
    marker.unlink()
    assert wr.is_managed_mirror(workspace, "skills") is False
    assert wr.is_managed_mirror(workspace, "agents") is True


@_not_root
@pytest.mark.parametrize(
    "locked, expected",
    [(".claude", (False, False)), (".claude/skills", (False, True))],
)
def test_is_managed_mirror_reads_an_unreadable_mirror_as_false(
    tmp_path, lock_mode, locked, expected
):
    workspace = _managed_workspace(tmp_path)
    lock_mode(workspace / locked)

    assert (
        wr.is_managed_mirror(workspace, "skills"),
        wr.is_managed_mirror(workspace, "agents"),
    ) == expected


def test_mirror_helpers_never_raise(tmp_path, monkeypatch, caplog):
    def boom(*args):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(wr, "_clear_managed_mirrors", boom)
    monkeypatch.setattr(wr, "_is_managed_mirror", boom)
    with caplog.at_level(logging.WARNING, logger=wr.__name__):
        wr.clear_managed_mirrors(tmp_path)
        assert wr.is_managed_mirror(tmp_path, "skills") is False

    failures = [r for r in caplog.records if r.name == wr.__name__ and r.exc_info]
    assert [str(r.exc_info[1]) for r in failures] == ["unexpected failure"] * 2


# --- warnings the session could repeat at will ---------------------------------


def _count(caplog, level: int, text: str) -> int:
    return sum(
        1 for r in caplog.records if r.levelno == level and text in r.getMessage()
    )


def test_a_refused_marker_warns_once_then_logs_at_debug(tmp_path, caplog, monkeypatch):
    """Every resolve (file-browser polls too) sees the planted marker again."""
    monkeypatch.setattr(wr, "_warned", set())
    workspace = _managed_workspace(tmp_path)
    marker = workspace / ".claude" / "skills" / MARKER
    marker.unlink()
    marker.symlink_to(tmp_path / "nowhere")
    other = tmp_path / "other"
    other.mkdir()
    ensure_workspace_resources(other)
    (other / ".claude" / "skills" / MARKER).unlink()
    (other / ".claude" / "skills" / MARKER).symlink_to(tmp_path / "nowhere")

    with caplog.at_level(logging.DEBUG, logger=wr.__name__):
        for _ in range(5):
            wr.prepare_workspace_resources(workspace)
        wr.prepare_workspace_resources(other)

    # Once for each workspace (the second one warns on its own), debug after.
    assert _count(caplog, logging.WARNING, "Refusing Claude resource marker") == 2
    assert _count(caplog, logging.WARNING, "leaving both unchanged") == 2
    assert _count(caplog, logging.DEBUG, "Refusing Claude resource marker") == 4
    assert _count(caplog, logging.DEBUG, "leaving both unchanged") == 4


def test_warned_reasons_are_a_bounded_set(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(wr, "_warned", set())
    monkeypatch.setattr(wr, "_WARNED_MAX", 3)

    with caplog.at_level(logging.DEBUG, logger=wr.__name__):
        for i in range(5):
            scope = (str(tmp_path / f"w{i}"), "skills")
            wr._warn(scope, "conflict", "conflict in %s", scope[0])
            wr._warn(scope, "conflict", "conflict in %s", scope[0])

    assert _count(caplog, logging.WARNING, "conflict in") == 5
    assert _count(caplog, logging.DEBUG, "conflict in") == 5
    assert len(wr._warned) <= 3


# --- one workspace at a time: clear -> grant -> materialize ---------------------


def _finishes(fn, timeout: float = 5.0) -> bool:
    """Run ``fn`` in a thread: whether it returned in time (its error re-raised)."""
    outcome = {}

    def run():
        try:
            fn()
        except BaseException as exc:  # re-raised in the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if "error" in outcome:
        raise outcome["error"]
    return not worker.is_alive()


@pytest.mark.parametrize(
    "call", ["materialize_workspace_resources", "clear_managed_mirrors"]
)
def test_mirror_work_from_another_thread_waits_for_the_workspace_lock(tmp_path, call):
    workspace = _managed_workspace(tmp_path)
    _write(workspace / "skills" / "review" / "SKILL.md", "skill\n")
    if call == "clear_managed_mirrors":
        ensure_workspace_resources(workspace)  # a mirror to clear
    mirrored = workspace / ".claude" / "skills" / "review" / "SKILL.md"
    before = mirrored.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    done = threading.Event()

    def work():
        getattr(wr, call)(workspace)
        done.set()

    worker = threading.Thread(target=work, daemon=True)
    with wr.workspace_lock(alias):  # another spelling of the same workspace
        worker.start()
        assert not done.wait(0.3)
        assert mirrored.exists() == before  # nothing re-linked or cleared yet
    assert done.wait(5.0)
    worker.join(5.0)

    assert mirrored.exists() != before
    assert wr._locks == {}


def test_the_lock_holder_can_clear_and_materialize_inside_it(tmp_path):
    """The grant sequence holds the lock across both calls: it is reentrant."""
    workspace = _managed_workspace(tmp_path)
    source = workspace / "skills" / "review" / "SKILL.md"
    _write(source, "skill\n")
    ensure_workspace_resources(workspace)
    links_seen_by_the_grant = []

    def grant_sequence():
        with wr.workspace_lock(workspace):
            wr.clear_managed_mirrors(workspace)
            links_seen_by_the_grant.append(os.stat(source).st_nlink)
            wr.materialize_workspace_resources(workspace)

    assert _finishes(grant_sequence)
    assert links_seen_by_the_grant == [1]
    assert os.stat(source).st_nlink == 2


def test_prepare_skips_instead_of_waiting_while_another_thread_holds_the_lock(
    tmp_path,
):
    """Resolution runs on the event loop: it must never wait for the lock."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    held, release = threading.Event(), threading.Event()

    def holder():
        with wr.workspace_lock(workspace):
            held.set()
            release.wait(10.0)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5.0)
    try:
        assert _finishes(lambda: wr.prepare_workspace_resources(workspace), 2.0)
        assert not (workspace / "skills").exists()  # skipped, not waited for
    finally:
        release.set()
        thread.join(5.0)

    with wr.workspace_lock(workspace):  # its own holder is not kept out
        wr.prepare_workspace_resources(workspace)
    assert (workspace / "skills").is_dir()
    assert (workspace / ".claude" / "skills" / MARKER).is_file()


def test_workspace_lock_never_raises_and_leaves_no_entries(tmp_path):
    for odd in (None, 42, tmp_path / "missing", "relative/path"):
        with wr.workspace_lock(odd):
            pass
    with pytest.raises(RuntimeError, match="the body's own error"):
        with wr.workspace_lock(tmp_path):
            raise RuntimeError("the body's own error")

    def take():
        with wr.workspace_lock(tmp_path):
            pass

    assert _finishes(take)  # released despite the error
    assert wr._locks == {}
