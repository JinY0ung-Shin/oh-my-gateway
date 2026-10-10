#!/usr/bin/env python3
"""Docker entrypoint for repairing writable bind mounts before startup."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path


DEFAULT_UID = 1000
DEFAULT_GID = 1000
DEFAULT_DATA_DIR = Path("/app/data")
DEFAULT_CLAUDE_HOME = Path("/home/app/.claude")
DEFAULT_CODEX_HOME = Path("/home/app/.codex")
DEFAULT_OPENCODE_HOME = Path("/home/app/.local/share/opencode")
DEFAULT_OPENCODE_CONFIG = Path("/home/app/.config/opencode")
DEFAULT_UV_CACHE_DIR = Path("/home/app/.cache/uv")
MYSQL_DATA_DIR_NAME = "mysql_data"


def _parse_id(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be positive, got {value}")
    return value


def _chown(path: Path, uid: int, gid: int) -> bool:
    """Chown ``path`` itself; return False when it sits on a read-only mount."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"warning: could not chown {path}: {exc}", file=sys.stderr)
        return True
    # Another hard link to a file the app user does not own can live outside
    # these trees, and changing this inode would change that file as well.
    if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1 and info.st_uid != uid:
        reason = f"{info.st_nlink} hard links, owned by uid {info.st_uid}"
        print(f"warning: skipping {path}: {reason}", file=sys.stderr)
        return True
    try:
        # These trees are writable by the app user, so a link found in them
        # must never redirect a root chown: change the link itself.
        os.chown(path, uid, gid, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"warning: could not chown {path}: {exc}", file=sys.stderr)
        return exc.errno != errno.EROFS
    return True


def _first_symlink(path: Path, *, anchor: Path) -> Path | None:
    """Return the first symlink from ``anchor`` (inclusive) down to ``path``."""
    current = anchor
    if current.is_symlink():
        return current
    for part in path.relative_to(anchor).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _prepare_dir(path: Path, *, anchor: Path) -> bool:
    """Create ``path`` below ``anchor`` unless a symlink sits on the way."""
    if path != anchor and anchor not in path.parents:
        print(f"warning: skipping {path}: not under {anchor}", file=sys.stderr)
        return False
    link = _first_symlink(path, anchor=anchor)
    if link is not None:
        print(f"warning: skipping {path}: {link} is a symlink", file=sys.stderr)
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"warning: skipping {path}: {exc}", file=sys.stderr)
        return False
    return True


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    # Stop at a read-only mount, and never walk from a link: os.walk() lists
    # a symlinked top directory's target even with followlinks=False.
    if not _chown(root, uid, gid) or root.is_symlink() or not root.is_dir():
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        writable_dirs = []
        for name in dirs:
            if _chown(current_path / name, uid, gid):
                writable_dirs.append(name)
        # Pruning keeps os.walk() out of read-only mounts below the root.
        dirs[:] = writable_dirs
        for name in files:
            _chown(current_path / name, uid, gid)


def _chown_path_with_parents(path: Path, *, stop: Path, uid: int, gid: int) -> None:
    try:
        path.relative_to(stop)
    except ValueError:
        _chown(path, uid, gid)
        return

    paths = [path]
    current = path
    while current != stop and current.parent != current:
        current = current.parent
        paths.append(current)
    for item in reversed(paths):
        _chown(item, uid, gid)


def prepare_writable_paths(
    *,
    uid: int,
    gid: int,
    data_dir: Path = DEFAULT_DATA_DIR,
    claude_home: Path = DEFAULT_CLAUDE_HOME,
    codex_home: Path = DEFAULT_CODEX_HOME,
    opencode_home: Path = DEFAULT_OPENCODE_HOME,
    opencode_config: Path = DEFAULT_OPENCODE_CONFIG,
    uv_cache_dir: Path = DEFAULT_UV_CACHE_DIR,
) -> None:
    """Ensure gateway-owned writable paths are usable by the app process."""
    data_dir = Path(data_dir)
    prompts_dir = data_dir / "prompts"
    claude_home = Path(claude_home)
    codex_home = Path(codex_home)
    opencode_home = Path(opencode_home)
    opencode_config = Path(opencode_config)
    uv_cache_dir = Path(uv_cache_dir)
    home_dir = claude_home.parent

    # Never create or chown through a link between an anchor (data_dir,
    # home_dir) and a repaired path; skip that path with a warning instead.
    data_ready = _prepare_dir(data_dir, anchor=data_dir)
    if data_ready:
        _prepare_dir(prompts_dir, anchor=data_dir)
    _prepare_dir(home_dir, anchor=home_dir)
    ready_home_paths = []
    for path in (claude_home, codex_home, opencode_home, opencode_config, uv_cache_dir):
        if _prepare_dir(path, anchor=home_dir):
            ready_home_paths.append(path)

    if data_ready and _chown(data_dir, uid, gid):
        for child in data_dir.iterdir():
            if child.name == MYSQL_DATA_DIR_NAME:
                continue
            if child == prompts_dir:
                _chown_tree(child, uid, gid)
            elif child.is_file() or child.is_symlink():
                _chown(child, uid, gid)

    for path in ready_home_paths:
        _chown_path_with_parents(path, stop=home_dir, uid=uid, gid=gid)
        _chown_tree(path, uid, gid)


def drop_privileges(uid: int, gid: int) -> None:
    """Switch from root to the runtime app uid/gid."""
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit("no command provided")

    uid = _parse_id("APP_UID", DEFAULT_UID)
    gid = _parse_id("APP_GID", DEFAULT_GID)

    if os.geteuid() == 0:
        prepare_writable_paths(uid=uid, gid=gid)
        drop_privileges(uid, gid)

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
