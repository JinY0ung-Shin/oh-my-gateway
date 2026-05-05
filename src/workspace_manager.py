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
            raise ValueError(
                f"Invalid backend: {backend!r}. Must match ^[a-z][a-z0-9_-]{{0,31}}$"
            )
        return backend

    _PROJECT_FILES = ("pyproject.toml", "uv.lock")

    def _sync_template(self, workspace: Path, backend: Optional[str] = None) -> None:
        """Copy backend template files into *workspace*."""
        _ = backend
        if self.template_source is None:
            return
        src = self.template_source / ".claude"
        if not src.is_dir():
            logger.debug("Template source .claude/ not found at %s, skipping sync", src)
            return
        dst = workspace / ".claude"
        if dst.exists():
            if dst.is_symlink():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        shutil.copytree(src, dst)
        logger.debug("Synced .claude/ template to %s", dst)

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
    """Determine the .claude template source directory."""
    claude_cwd = os.getenv("CLAUDE_CWD", "")
    if claude_cwd:
        p = Path(claude_cwd)
        if (p / ".claude").is_dir():
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
