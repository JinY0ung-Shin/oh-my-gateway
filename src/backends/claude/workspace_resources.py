"""Claude compatibility layer for user-visible workspace resources.

ChatDRAGON users edit skills and subagents through the workspace file manager.  The
user-facing paths should therefore be backend-neutral and copyable as
``skills/...`` and ``agents/...`` rather than exposing Claude Code's implementation
layout under ``.claude``.

Claude Code still discovers project resources from ``.claude/skills`` and
``.claude/agents``.  Named Claude workspaces bridge those native paths to the
user-visible directories with relative symlinks::

    workspace/
      skills/                 # canonical, user-visible
      agents/                 # canonical, user-visible
      .claude/
        skills -> ../skills   # Claude compatibility only
        agents -> ../agents

For an existing workspace that only has the legacy native directory, the directory
is moved to the visible location first and the compatibility link is installed in
its place.  No resource contents are copied or duplicated.

This module is deliberately filesystem-only and best-effort.  A malformed or
operator-managed layout must not make the whole workspace unusable; conflicts are
left untouched and logged instead of deleting or overwriting user data.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_RESOURCE_DIRS = ("skills", "agents")


def _exists(path: Path) -> bool:
    """Like ``Path.exists`` but also true for a broken symlink."""
    return path.exists() or path.is_symlink()


def _inside(path: Path, root: Path) -> bool:
    """Whether *path* resolves inside *root* (including *root* itself)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _bridge_resource_dir(workspace: Path, claude_dir: Path, name: str) -> None:
    visible = workspace / name
    native = claude_dir / name
    visible_exists = _exists(visible)
    native_exists = _exists(native)

    # A user/operator-created visible symlink could point outside the workspace.
    # Never make Claude follow it through a trusted project-resource path.
    if visible_exists and (visible.is_symlink() or not visible.is_dir()):
        logger.warning(
            "Cannot bridge Claude workspace resource %s: visible path is not a real directory",
            visible,
        )
        return
    if visible_exists and not _inside(visible, workspace):
        logger.warning(
            "Cannot bridge Claude workspace resource %s: path resolves outside workspace",
            visible,
        )
        return

    if native_exists and native.is_symlink():
        try:
            if native.resolve() == visible.resolve() and visible_exists:
                return
        except (OSError, RuntimeError):
            pass
        logger.warning(
            "Cannot bridge Claude workspace resource %s: native symlink has a different target",
            native,
        )
        return

    if visible_exists and native_exists:
        # Both are real operator/user data.  Choosing either side would silently
        # shadow the other in Claude, so preserve both and require manual cleanup.
        logger.warning(
            "Claude workspace has both %s and %s; leaving both unchanged to avoid data loss",
            visible,
            native,
        )
        return

    try:
        if native_exists:
            if not native.is_dir() or not _inside(native, workspace):
                logger.warning(
                    "Cannot migrate Claude workspace resource %s: native path is not a safe directory",
                    native,
                )
                return
            # One-time migration from the old user-visible .claude layout.
            native.rename(visible)
        elif not visible_exists:
            visible.mkdir(parents=False, exist_ok=False)

        # Relative target keeps a workspace movable and resolves entirely inside
        # the same workspace root: .claude/<name> -> ../<name>.
        native.symlink_to(Path("..") / name, target_is_directory=True)
    except FileExistsError:
        # Another request may have created the same bridge concurrently.
        if native.is_symlink() and visible.is_dir():
            try:
                if native.resolve() == visible.resolve():
                    return
            except (OSError, RuntimeError):
                pass
        logger.warning("Claude workspace resource bridge raced at %s", native)
    except OSError:
        logger.warning("Failed to prepare Claude workspace resource %s", name, exc_info=True)


def ensure_workspace_resources(workspace: Path) -> None:
    """Expose ``skills/`` and ``agents/`` while keeping Claude-native discovery.

    The function is idempotent and non-raising.  It is intended to run whenever a
    named Claude workspace is resolved, which also means an old ``.claude`` layout
    is migrated lazily the first time that user returns after an upgrade.
    """
    workspace = Path(workspace)
    claude_dir = workspace / ".claude"

    try:
        if _exists(claude_dir):
            # Following an operator-provided .claude symlink could mutate files
            # outside the workspace while installing the resource bridge.
            if claude_dir.is_symlink() or not claude_dir.is_dir():
                logger.warning(
                    "Cannot prepare Claude workspace resources: %s is not a real directory",
                    claude_dir,
                )
                return
            if not _inside(claude_dir, workspace):
                logger.warning(
                    "Cannot prepare Claude workspace resources: %s resolves outside workspace",
                    claude_dir,
                )
                return
        else:
            claude_dir.mkdir(parents=False, exist_ok=False)
    except OSError:
        logger.warning("Failed to prepare Claude workspace resource root %s", claude_dir, exc_info=True)
        return

    for name in _RESOURCE_DIRS:
        _bridge_resource_dir(workspace, claude_dir, name)
