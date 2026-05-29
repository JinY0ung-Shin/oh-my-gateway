"""Per-user workspace isolation manager.

Resolves user identifiers to filesystem paths and manages temporary workspace
cleanup. Workspaces are empty scratch directories; per-backend configuration is
loaded from global/env sources (Claude from ``~/.claude`` and
``~/.claude/plugins``; OpenCode/Codex from their own config env vars), never
seeded into the workspace here.
"""

import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from src.constants import USER_WORKSPACES_DIR

logger = logging.getLogger(__name__)

_USER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class WorkspaceManager:
    """Manages per-user working directories.

    Parameters:
        base_path: Root directory for all user workspaces.
    """

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)

    def resolve(
        self,
        user: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> Path:
        """Return the workspace path for *user*, creating it if necessary.

        Named users use ``base_path/user/backend`` when *backend* is provided.
        Anonymous workspaces remain session-scoped ``_tmp_{uuid}`` directories.
        Workspaces are created empty — no configuration is seeded into them.
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

    def sweep_orphan_temp_workspaces(self, max_age_seconds: float) -> int:
        """Remove ``_tmp_*`` workspaces older than *max_age_seconds*.

        Anonymous workspaces are tied to in-memory sessions that do not survive a
        gateway restart, so their ``_tmp_`` directories would otherwise leak
        across restarts. This sweeps stale ones (typically at startup). Only
        directories whose name starts with ``_tmp_`` and whose mtime is older
        than the cutoff are removed, so freshly-created live workspaces and
        permanent named-user workspaces are never touched.

        Returns the number of directories removed.
        """
        if not self.base_path.exists():
            return 0
        cutoff = time.time() - max_age_seconds
        try:
            children = list(self.base_path.iterdir())
        except OSError:
            return 0
        removed = 0
        for child in children:
            if not child.name.startswith("_tmp_"):
                continue
            try:
                if child.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            self.cleanup_temp_workspace(child)
            removed += 1
        if removed:
            logger.info(
                "Swept %d orphaned temporary workspace(s) from %s", removed, self.base_path
            )
        return removed

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


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def _resolve_base_path() -> Path:
    """Determine the workspace base path.

    Uses ``USER_WORKSPACES_DIR`` when set. Otherwise a **stable** per-host temp
    directory (``<tmp>/oh-my-gateway-workspaces``), created on first use. A
    stable path (rather than a fresh ``mkdtemp`` per process) is what lets the
    startup orphan-sweep reclaim anonymous ``_tmp_`` workspaces left behind by a
    previous run.
    """
    if USER_WORKSPACES_DIR:
        return Path(USER_WORKSPACES_DIR)
    return Path(tempfile.gettempdir()) / "oh-my-gateway-workspaces"


workspace_manager = WorkspaceManager(base_path=_resolve_base_path())
