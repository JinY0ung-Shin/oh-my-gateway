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
``.claude/agents``.  Rather than making those directories the source of truth, the
gateway keeps real Claude-native directories as mirrors of the visible resource
trees.  Regular files are hard-linked whenever the filesystem allows it (so edits
through either path immediately address the same inode); a normal copy is the
portable fallback.  Directories are real directories, avoiding Claude Code's
historically inconsistent subagent discovery through directory symlinks.

Existing workspaces are migrated lazily.  If only the legacy native directory
exists, it is moved to the visible location first and a managed mirror is created
in its place.  If both locations contain unmanaged data, neither side is modified:
choosing one would silently shadow or delete user data.

The mirror is best-effort.  Canonical user data is never deleted by this module;
failures only make the Claude compatibility view stale and are logged.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_RESOURCE_DIRS = ("skills", "agents")
_MANAGED_MARKER = ".oh-my-gateway-managed"


def _exists(path: Path) -> bool:
    """Like :meth:`Path.exists`, but also true for a broken symlink."""
    return path.exists() or path.is_symlink()


def _inside(path: Path, root: Path) -> bool:
    """Whether *path* resolves inside *root* (including *root* itself)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _real_directory(path: Path, root: Path) -> bool:
    """True for a non-symlink directory that resolves inside *root*."""
    return path.is_dir() and not path.is_symlink() and _inside(path, root)


def _managed(native: Path) -> bool:
    marker = native / _MANAGED_MARKER
    return native.is_dir() and not native.is_symlink() and marker.is_file()


def _remove_native_entry(path: Path) -> None:
    """Remove one entry from a managed mirror without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
    elif _exists(path):
        path.unlink(missing_ok=True)


def _iter_visible_entries(base: Path):
    """Yield safe canonical entries as ``(relative_path, source_path, is_dir)``.

    Symlinks are deliberately skipped.  A user-visible resource symlink can point
    outside the workspace, and mirroring it into a trusted ``.claude`` discovery
    path would turn that external target into project configuration.
    """
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                logger.warning("Skipping symlinked workspace resource entry %s", entry)
                continue
            rel = entry.relative_to(base)
            if entry.is_dir():
                yield rel, entry, True
                stack.append(entry)
            elif entry.is_file():
                yield rel, entry, False


def _same_file(source: Path, target: Path) -> bool:
    """Whether two existing files already reference the same inode."""
    try:
        return os.path.samestat(source.stat(), target.stat())
    except (OSError, ValueError):
        return False


def _link_or_copy(source: Path, target: Path) -> None:
    """Materialize *source* at *target*, preferring a hard link."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and not target.is_symlink() and _same_file(source, target):
        return
    if _exists(target):
        _remove_native_entry(target)
    try:
        os.link(source, target)
    except OSError:
        # Hard links may be unavailable on some mounted/container filesystems.
        shutil.copy2(source, target)


def _sync_managed_mirror(visible: Path, native: Path) -> None:
    """Make a gateway-managed Claude directory mirror the canonical tree."""
    desired_dirs: set[Path] = set()
    desired_files: dict[Path, Path] = {}
    for rel, source, is_dir in _iter_visible_entries(visible):
        if is_dir:
            desired_dirs.add(rel)
        else:
            desired_files[rel] = source
            desired_dirs.update(rel.parents)
    desired_dirs.discard(Path("."))

    # Remove stale mirror entries from the leaves upward.  The marker is gateway
    # metadata, not a canonical resource, and is always preserved.
    try:
        current = sorted(
            (p for p in native.rglob("*") if p.name != _MANAGED_MARKER),
            key=lambda p: len(p.relative_to(native).parts),
            reverse=True,
        )
    except OSError:
        current = []
    for path in current:
        rel = path.relative_to(native)
        if path.is_symlink():
            _remove_native_entry(path)
            continue
        if path.is_dir():
            if rel not in desired_dirs:
                try:
                    path.rmdir()
                except OSError:
                    # A concurrent sync or an unexpected entry can leave it non-empty.
                    pass
        elif rel not in desired_files:
            _remove_native_entry(path)

    for rel in sorted(desired_dirs, key=lambda p: len(p.parts)):
        target = native / rel
        if _exists(target) and (target.is_symlink() or not target.is_dir()):
            _remove_native_entry(target)
        target.mkdir(parents=True, exist_ok=True)

    for rel, source in desired_files.items():
        _link_or_copy(source, native / rel)


def _prepare_resource_dir(workspace: Path, claude_dir: Path, name: str) -> None:
    visible = workspace / name
    native = claude_dir / name
    visible_exists = _exists(visible)
    native_exists = _exists(native)

    if visible_exists and not _real_directory(visible, workspace):
        logger.warning(
            "Cannot prepare Claude workspace resource %s: visible path is not a safe directory",
            visible,
        )
        return

    # Upgrade the first version of this bridge, which used a directory symlink.
    # It is safe to replace only when it points at the canonical visible directory.
    if native_exists and native.is_symlink():
        try:
            points_to_visible = visible_exists and native.resolve() == visible.resolve()
        except (OSError, RuntimeError):
            points_to_visible = False
        if not points_to_visible:
            logger.warning(
                "Cannot prepare Claude workspace resource %s: native symlink has a different target",
                native,
            )
            return
        try:
            native.unlink()
        except OSError:
            logger.warning("Failed to replace Claude resource symlink %s", native, exc_info=True)
            return
        native_exists = False

    native_is_managed = native_exists and _managed(native)

    if not visible_exists:
        try:
            if native_exists and not native_is_managed:
                if not _real_directory(native, workspace):
                    logger.warning(
                        "Cannot migrate Claude workspace resource %s: native path is not a safe directory",
                        native,
                    )
                    return
                # One-time migration from legacy .claude/{skills,agents}.
                native.rename(visible)
                visible_exists = True
                native_exists = False
            else:
                visible.mkdir(parents=False, exist_ok=False)
                visible_exists = True
        except FileExistsError:
            visible_exists = _real_directory(visible, workspace)
        except OSError:
            logger.warning("Failed to create/migrate workspace resource %s", visible, exc_info=True)
            return

    if native_exists and not native_is_managed:
        # Both locations contain real, independently-managed data.  Never delete
        # or silently shadow either side.  Operators can merge once, then remove
        # the legacy native directory; the next resolve will create the mirror.
        logger.warning(
            "Claude workspace has both canonical %s and unmanaged native %s; "
            "leaving both unchanged to avoid data loss",
            visible,
            native,
        )
        return

    try:
        if not native_exists:
            native.mkdir(parents=False, exist_ok=False)
        marker = native / _MANAGED_MARKER
        marker.write_text(
            "Managed by oh-my-gateway. Edit the sibling workspace/%s directory instead.\n"
            % name,
            encoding="utf-8",
        )
        _sync_managed_mirror(visible, native)
    except OSError:
        logger.warning("Failed to materialize Claude workspace resource %s", name, exc_info=True)


def ensure_workspace_resources(workspace: Path) -> None:
    """Expose backend-neutral resources and maintain Claude-native mirrors.

    The function is idempotent and non-raising.  It is cheap when nothing changed:
    hard-linked files are detected by inode and left untouched.  It runs whenever a
    named Claude workspace is resolved, so a legacy ``.claude`` layout migrates
    lazily the first time that user returns after an upgrade.
    """
    workspace = Path(workspace)
    claude_dir = workspace / ".claude"

    try:
        if _exists(claude_dir):
            if not _real_directory(claude_dir, workspace):
                logger.warning(
                    "Cannot prepare Claude workspace resources: %s is not a safe directory",
                    claude_dir,
                )
                return
        else:
            claude_dir.mkdir(parents=False, exist_ok=False)
    except OSError:
        logger.warning("Failed to prepare Claude workspace resource root %s", claude_dir, exc_info=True)
        return

    for name in _RESOURCE_DIRS:
        _prepare_resource_dir(workspace, claude_dir, name)
