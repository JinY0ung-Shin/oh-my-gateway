#!/usr/bin/env python3
"""Docker entrypoint for repairing writable bind mounts before startup."""

from __future__ import annotations

import os
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


def _chown(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except FileNotFoundError:
        return
    except PermissionError as exc:
        print(f"warning: could not chown {path}: {exc}", file=sys.stderr)


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    _chown(root, uid, gid)
    if not root.is_dir():
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        for name in dirs:
            _chown(current_path / name, uid, gid)
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

    data_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    opencode_home.mkdir(parents=True, exist_ok=True)
    opencode_config.mkdir(parents=True, exist_ok=True)
    uv_cache_dir.mkdir(parents=True, exist_ok=True)

    _chown(data_dir, uid, gid)
    for child in data_dir.iterdir():
        if child.name == MYSQL_DATA_DIR_NAME:
            continue
        if child == prompts_dir:
            _chown_tree(child, uid, gid)
        elif child.is_file() or child.is_symlink():
            _chown(child, uid, gid)

    for path in (claude_home, codex_home, opencode_home, opencode_config, uv_cache_dir):
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
