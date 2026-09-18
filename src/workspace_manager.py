"""Per-user workspace isolation manager.

Resolves user identifiers to filesystem paths and manages temporary workspace
cleanup. Per-backend configuration is loaded from global/env sources (Claude from
``~/.claude`` and ``~/.claude/plugins``; OpenCode/Codex from their own config env
vars). Named Claude workspaces additionally expose user-editable ``skills/`` and
``agents/`` directories while a backend compatibility layer keeps Claude Code's
native ``.claude`` discovery paths wired to them.
"""

import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from src.constants import USER_WORKSPACES_DIR

logger = logging.getLogger(__name__)

# The workspace key is the caller's WHOLE identity. ``@`` is allowed because the
# identity header is routinely an email (the default header name is
# ``X-User-Email``): keying on the localpart alone would map ``alice@a.com`` and
# ``alice@b.com`` — two different principals — onto one directory. ``@`` is inert
# as a path component (no traversal, no separator); containment is enforced
# independently by the callers' root confinement.
_USER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._@-]{0,126}$")
_BACKEND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Every known backend owns its default directory name. Keep these names reserved
# even when a backend is disabled: workspaces persist across configuration changes,
# and allowing Claude to claim (for example) ``codex`` today would make a later
# ``BACKENDS=claude,codex`` deployment silently merge two backend workspaces.
_BACKEND_WORKSPACE_NAMES = frozenset({"claude", "opencode", "codex"})


def _legacy_localpart_key_enabled() -> bool:
    """Whether all named workspace consumers should use the pre-fix localpart key.

    This compatibility switch is intentionally resolved here, at the single
    workspace-path authority. Applying it only in ``/files/*`` would make the file
    browser use ``alice`` while ``/v1/responses`` used ``alice@example.com`` — the
    same cross-surface split issue #188 fixed. The mode remains insecure because
    different principals that share a localpart collide; it exists for migration
    only and warns on every named resolve while enabled.
    """
    return os.getenv("WORKSPACE_LEGACY_LOCALPART_KEY", "").strip().lower() == "true"


def _initial_workspace_dirs() -> tuple[Path, ...]:
    """Parse safe relative directories to seed into a brand-new backend workspace.

    ``WORKSPACE_INITIAL_DIRS`` is a comma-separated list of workspace-relative
    directory paths. Nested paths and dot-directories are allowed, but absolute
    paths and traversal components are rejected so deployment configuration can
    never escape the workspace root.
    """
    raw = os.getenv("WORKSPACE_INITIAL_DIRS", "")
    if not raw.strip():
        return ()

    result: list[Path] = []
    seen: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or not path.parts or any(part == ".." for part in path.parts):
            raise ValueError(
                f"Invalid WORKSPACE_INITIAL_DIRS entry: {value!r}. "
                "Entries must be relative workspace paths without '..'."
            )
        normalized = Path(*path.parts)
        key = normalized.as_posix()
        if key not in seen:
            result.append(normalized)
            seen.add(key)
    return tuple(result)


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
        ``CLAUDE_WORKSPACE_DIR`` may override only the filesystem directory name
        used for the ``claude`` backend; the backend identifier itself remains
        unchanged. Anonymous workspaces remain session-scoped ``_tmp_{uuid}``
        directories.

        On the first creation of a named backend workspace,
        ``WORKSPACE_INITIAL_DIRS`` may seed configurable top-level or nested
        directories. The seed is intentionally creation-only: deleting one later
        does not make it reappear on the next resolve.

        Named Claude workspaces get top-level ``skills/`` and ``agents/`` resource
        directories. Claude's native ``.claude/{skills,agents}`` paths are an
        internal compatibility view maintained by the Claude backend, so file
        manager users can work with backend-neutral paths. Resolve only prepares
        those roots; recursive mirror refresh is deferred until an SDK operation
        so polling file-browser calls do not repeatedly scan resource trees.

        ``WORKSPACE_LEGACY_LOCALPART_KEY=true`` is a migration-only compatibility
        mode. It is applied here rather than in an HTTP route so every consumer
        (Responses, file routes, agent resources, and direct manager users) resolves
        the same workspace key.
        """
        backend_name = self._sanitize_backend(backend)
        if user is not None:
            workspace_dir_name = self._workspace_dir_name(backend_name)
            workspace_key = user
            if _legacy_localpart_key_enabled():
                workspace_key = user.split("@", 1)[0]
                logger.warning(
                    "WORKSPACE_LEGACY_LOCALPART_KEY=true: workspaces are keyed on "
                    "the identity localpart, so callers sharing one localpart share "
                    "one workspace"
                )
            sanitized = self._sanitize(workspace_key)
            workspace = self.base_path / sanitized
            if workspace_dir_name:
                workspace = workspace / workspace_dir_name
        else:
            workspace = self.base_path / f"_tmp_{uuid.uuid4().hex}"

        # Validate seed configuration before creating anything. Otherwise a bad
        # entry could leave behind an empty workspace that subsequent resolves
        # would treat as pre-existing and therefore never seed.
        initial_dirs = (
            _initial_workspace_dirs()
            if user is not None and backend_name is not None
            else ()
        )

        # Only the process that creates the final backend directory seeds it.
        # This matters semantically: configured starter folders are onboarding
        # defaults, not invariants. If a user later deletes one, resolving the
        # workspace again must not recreate it.
        created = False
        try:
            workspace.mkdir(parents=True, exist_ok=False)
            created = True
        except FileExistsError:
            # Preserve pathlib's previous behavior for a non-directory collision
            # while treating an already-created directory (including a concurrent
            # creator) as an existing workspace.
            if not workspace.is_dir():
                raise

        if created and initial_dirs:
            for relative_dir in initial_dirs:
                (workspace / relative_dir).mkdir(parents=True, exist_ok=True)

        if user is not None and backend_name == "claude":
            # Import lazily so this generic path manager does not import the Claude
            # SDK/backend stack for Codex/OpenCode or plain workspace callers.
            from src.backends.claude.workspace_resources import prepare_workspace_resources

            prepare_workspace_resources(workspace)

        return workspace

    def cleanup_temp_workspace(self, workspace: Path) -> None:
        """Remove a temporary workspace directory.

        Only directories whose name starts with ``_tmp_`` are removed.
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
                f"Invalid user identifier: {user!r}. Must match {_USER_PATTERN.pattern}"
            )
        return user

    def _sanitize_backend(self, backend: Optional[str]) -> Optional[str]:
        """Validate and return a backend directory name."""
        if backend is None:
            return None
        if not backend or not _BACKEND_PATTERN.match(backend):
            raise ValueError(f"Invalid backend: {backend!r}. Must match ^[a-z][a-z0-9_-]{{0,31}}$")
        return backend

    def _workspace_dir_name(self, backend: Optional[str]) -> Optional[str]:
        """Return the filesystem directory name for a backend.

        Backend identifiers are part of routing semantics and must remain stable.
        ``CLAUDE_WORKSPACE_DIR`` therefore aliases only the on-disk directory used
        by the ``claude`` backend. Empty/unset values preserve the default name.
        The override is validated with the same single-component rules as backend
        names and may not claim another backend's reserved workspace name.
        """
        if backend != "claude":
            return backend

        override = os.getenv("CLAUDE_WORKSPACE_DIR", "").strip()
        if not override:
            return backend

        try:
            workspace_dir = self._sanitize_backend(override)
        except ValueError as exc:
            raise ValueError(
                f"Invalid CLAUDE_WORKSPACE_DIR: {override!r}. "
                "Must match ^[a-z][a-z0-9_-]{0,31}$"
            ) from exc

        if workspace_dir != backend and workspace_dir in _BACKEND_WORKSPACE_NAMES:
            raise ValueError(
                f"Invalid CLAUDE_WORKSPACE_DIR: {override!r} collides with the "
                f"{workspace_dir!r} backend workspace directory"
            )
        return workspace_dir


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
