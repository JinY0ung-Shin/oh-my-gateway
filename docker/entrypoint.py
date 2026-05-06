#!/usr/bin/env python3
"""Docker entrypoint for repairing writable bind mounts before startup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


DEFAULT_UID = 1000
DEFAULT_GID = 1000
DEFAULT_DATA_DIR = Path("/app/data")
DEFAULT_CLAUDE_HOME = Path("/home/app/.claude")
MYSQL_DATA_DIR_NAME = "mysql_data"

DEFAULT_AUTO_MEMORY_DIR = "/workspace/memory"


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


def ensure_auto_memory_settings(
    claude_home: Path,
    *,
    auto_memory_dir: str,
    uid: int,
    gid: int,
) -> None:
    """Merge ``autoMemoryDirectory`` into ``<claude_home>/settings.json``.

    Claude Code's auto-memory writes go to ``~/.claude/projects/<slug>/memory/``
    by default, which the SDK's sensitive-file guardrail then auto-denies.  The
    only way to redirect those writes to a non-sensitive path is the
    ``autoMemoryDirectory`` setting in *user-scope* (``~/.claude/settings.json``)
    or *managed-policy* settings - ``Edit``/``Write`` PreToolUse hooks cannot
    override the guardrail, and project/local settings are explicitly ignored
    for this key by design.

    This runs at every container start so the setting survives volume
    recreation. We merge into any existing settings.json rather than
    overwriting so user-added keys (e.g. plugins, model overrides) are
    preserved.  Set ``CLAUDE_AUTO_MEMORY_DIR=`` (empty) to skip; set to a
    custom path to override the default.
    """
    if not auto_memory_dir:
        return
    settings_path = claude_home / "settings.json"
    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text())
            if not isinstance(existing, dict):
                print(
                    f"warning: {settings_path} is not a JSON object; replacing",
                    file=sys.stderr,
                )
                existing = {}
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"warning: could not read {settings_path}: {exc}; replacing",
                file=sys.stderr,
            )
            existing = {}

    if existing.get("autoMemoryDirectory") == auto_memory_dir:
        return  # Already in sync, no rewrite needed.

    existing["autoMemoryDirectory"] = auto_memory_dir
    claude_home.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    _chown(settings_path, uid, gid)

    # Ensure the destination directory exists and is owned by the app uid.
    target = Path(auto_memory_dir)
    if not target.is_absolute():
        # Pathological case (relative path); skip preparation rather than
        # creating something next to the entrypoint.
        return
    target.mkdir(parents=True, exist_ok=True)
    _chown_tree(target, uid, gid)


def prepare_writable_paths(
    *,
    uid: int,
    gid: int,
    data_dir: Path = DEFAULT_DATA_DIR,
    claude_home: Path = DEFAULT_CLAUDE_HOME,
    auto_memory_dir: str = DEFAULT_AUTO_MEMORY_DIR,
) -> None:
    """Ensure gateway-owned writable paths are usable by the app process."""
    data_dir = Path(data_dir)
    prompts_dir = data_dir / "prompts"
    claude_home = Path(claude_home)
    home_dir = claude_home.parent

    data_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)

    _chown(data_dir, uid, gid)
    for child in data_dir.iterdir():
        if child.name == MYSQL_DATA_DIR_NAME:
            continue
        if child == prompts_dir:
            _chown_tree(child, uid, gid)
        elif child.is_file() or child.is_symlink():
            _chown(child, uid, gid)

    _chown(home_dir, uid, gid)
    _chown_tree(claude_home, uid, gid)

    ensure_auto_memory_settings(
        claude_home,
        auto_memory_dir=auto_memory_dir,
        uid=uid,
        gid=gid,
    )


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
    auto_memory_dir = os.environ.get("CLAUDE_AUTO_MEMORY_DIR", DEFAULT_AUTO_MEMORY_DIR).strip()

    if os.geteuid() == 0:
        prepare_writable_paths(uid=uid, gid=gid, auto_memory_dir=auto_memory_dir)
        drop_privileges(uid, gid)

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
