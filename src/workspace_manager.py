"""Per-user workspace isolation manager.

Resolves user identifiers to filesystem paths, syncs `.claude` configuration
templates from CLAUDE_CWD, and manages temporary workspace cleanup.
"""

import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_USER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class WorkspaceManager:
    """Manages per-user working directories.

    Parameters:
        base_path: Root directory for all user workspaces.
        template_source: Directory containing `.claude/` to copy into new workspaces.
            Typically the ``CLAUDE_CWD`` value.
    """

    def __init__(
        self,
        base_path: Path,
        template_source: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ):
        self.base_path = Path(base_path)
        self.template_source = Path(template_source) if template_source else None
        self.project_root = Path(project_root) if project_root else None

    def resolve(
        self,
        user: Optional[str] = None,
        sync_template: bool = False,
        backend: Optional[str] = None,
    ) -> Path:
        """Return the workspace path for *user*, creating it if necessary.

        Named users use ``base_path/user/backend`` when *backend* is provided.
        Anonymous workspaces remain session-scoped ``_tmp_{uuid}`` directories.
        """
        backend_name = self._sanitize_backend(backend)
        if user is not None:
            sanitized = self._sanitize(user)
            workspace = self.base_path / sanitized
            if backend_name:
                workspace = workspace / backend_name
        else:
            workspace = self.base_path / f"_tmp_{uuid.uuid4().hex}"

        workspace.mkdir(parents=True, exist_ok=True)

        if sync_template:
            self._sync_template(workspace, backend=backend_name)
            self._sync_project_files(workspace)

        return workspace

    def cleanup_temp_workspace(self, workspace: Path) -> None:
        """Remove a temporary workspace directory.

        Only directories whose name starts with ``_tmp_`` are removed.
        Permanent user workspaces are left untouched.
        """
        if not workspace.exists():
            return
        if not workspace.name.startswith("_tmp_"):
            logger.debug("Skipping cleanup of non-temporary workspace: %s", workspace)
            return
        shutil.rmtree(workspace, ignore_errors=True)
        logger.info("Cleaned up temporary workspace: %s", workspace)

    def _sanitize(self, user: str) -> str:
        """Validate and return *user* as a safe directory name.

        Raises ``ValueError`` for empty, too-long, or disallowed strings.
        """
        if not user:
            raise ValueError("User identifier must not be empty")
        if not _USER_PATTERN.match(user):
            raise ValueError(
                f"Invalid user identifier: {user!r}. Must match ^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,62}}$"
            )
        return user

    def _sanitize_backend(self, backend: Optional[str]) -> Optional[str]:
        """Validate and return a backend directory name."""
        if backend is None:
            return None
        if not backend or not _BACKEND_PATTERN.match(backend):
            raise ValueError(f"Invalid backend: {backend!r}. Must match ^[a-z][a-z0-9_-]{{0,31}}$")
        return backend

    _PROJECT_FILES = ("pyproject.toml", "uv.lock")

    def _remove_path(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def _replace_tree(self, src: Path, dst: Path) -> None:
        if dst.exists() or dst.is_symlink():
            self._remove_path(dst)
        shutil.copytree(src, dst)

    def _copy_template_file(self, name: str, workspace: Path) -> None:
        if self.template_source is None:
            return
        src = self.template_source / name
        if not src.is_file():
            return
        dst = workspace / name
        if dst.exists() or dst.is_symlink():
            self._remove_path(dst)
        shutil.copy2(src, dst)

    def _copy_template_dir(self, name: str, workspace: Path) -> bool:
        if self.template_source is None:
            return False
        src = self.template_source / name
        if not src.is_dir():
            return False
        self._replace_tree(src, workspace / name)
        return True

    def _ensure_real_dir_path(self, root: Path, dst: Path) -> None:
        current = root
        current.mkdir(parents=True, exist_ok=True)
        for part in dst.relative_to(root).parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_dir() and not current.is_symlink():
                    continue
                self._remove_path(current)
            current.mkdir()

    def _contains_symlink(self, path: Path) -> bool:
        return path.is_symlink() or any(child.is_symlink() for child in path.rglob("*"))

    def _mirror_skill_dirs(self, sources: tuple[Path, ...], dst: Path) -> None:
        for src in sources:
            if not src.is_dir():
                continue
            self._ensure_real_dir_path(dst.parent.parent, dst)
            for child in sorted(src.iterdir()):
                if not child.is_dir() or child.is_symlink():
                    continue
                if self._contains_symlink(child):
                    logger.debug("Skipping skill template with symlink: %s", child)
                    continue
                skill_file = child / "SKILL.md"
                if not skill_file.is_file() or skill_file.is_symlink():
                    continue
                target = dst / child.name
                if target.exists():
                    continue
                shutil.copytree(child, target)

    def _sync_template(self, workspace: Path, backend: Optional[str] = None) -> None:
        """Copy backend-native templates into *workspace*."""
        if self.template_source is None:
            return

        if backend == "codex":
            self._copy_template_dir(".agents", workspace)
            self._copy_template_file("AGENTS.md", workspace)
            skills_dst = workspace / ".agents" / "skills"
            if skills_dst.is_symlink() or not skills_dst.exists():
                self._mirror_skill_dirs((self.template_source / ".claude" / "skills",), skills_dst)
            logger.debug("Synced Codex template to %s", workspace)
            return

        if backend == "opencode":
            self._copy_template_dir(".opencode", workspace)
            skills_dst = workspace / ".opencode" / "skills"
            if skills_dst.is_symlink() or not skills_dst.exists():
                self._mirror_skill_dirs(
                    (
                        self.template_source / ".claude" / "skills",
                        self.template_source / ".agents" / "skills",
                    ),
                    skills_dst,
                )
            logger.debug("Synced OpenCode template to %s", workspace)
            return

        self._copy_template_dir(".claude", workspace)
        self._copy_template_file("CLAUDE.md", workspace)
        logger.debug("Synced Claude template to %s", workspace)

    def _sync_project_files(self, workspace: Path) -> None:
        """Symlink project files (pyproject.toml, uv.lock) into *workspace*.

        This allows ``uv run`` and other project-aware tools to work
        correctly from isolated workspace directories.
        """
        if self.project_root is None:
            return
        for name in self._PROJECT_FILES:
            src = self.project_root / name
            dst = workspace / name
            if not src.is_file():
                continue
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(src)
        logger.debug("Synced project files from %s to %s", self.project_root, workspace)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

import os  # noqa: E402
from src.constants import USER_WORKSPACES_DIR  # noqa: E402


def _resolve_base_path() -> Path:
    """Determine the workspace base path from environment."""
    if USER_WORKSPACES_DIR:
        return Path(USER_WORKSPACES_DIR)
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        return Path(claude_cwd)
    import tempfile

    return Path(tempfile.mkdtemp(prefix="claude_workspaces_"))


def _resolve_template_source() -> Optional[Path]:
    """Determine the agent template source directory."""
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        p = Path(claude_cwd)
        if p.is_dir():
            return p
    return None


def _resolve_project_root(base: Path) -> Optional[Path]:
    """Return the project root used to source ``pyproject.toml``/``uv.lock``.

    Prefers ``CLAUDE_CWD`` so isolated workspaces (e.g. ``/tmp/$USER``) still
    pick up project files. Falls back to walking up from *base*.
    """
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        p = Path(claude_cwd)
        if (p / "pyproject.toml").is_file():
            return p
    start = base.resolve()
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


_base_path = _resolve_base_path()

workspace_manager = WorkspaceManager(
    base_path=_base_path,
    template_source=_resolve_template_source(),
    project_root=_resolve_project_root(_base_path),
)
