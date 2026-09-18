"""Per-user workspace isolation manager.

Resolves user identifiers to filesystem paths and manages temporary workspace
cleanup. Per-backend configuration is loaded from global/env sources (Claude from
``~/.claude`` and ``~/.claude/plugins``; OpenCode/Codex from their own config env
vars). Named Claude workspaces additionally expose user-editable ``skills/`` and
``agents/`` directories while a backend compatibility layer keeps Claude Code's
native ``.claude`` discovery paths wired to them.
"""

import errno
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
from src.env_utils import parse_workspace_initial_dirs

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


# Creation-only seeding has ONE publisher and publishes ATOMICALLY.
#
# Every earlier shape of this protocol leaked a partial layout through some
# interleaving, because more than one resolver was allowed to create state and
# the root became visible before its contents did. Two rules remove the whole
# class of bug rather than one instance of it:
#
# 1. An exclusive claim (``O_EXCL`` sibling file) picks a single publisher.
#    Losers create nothing — they wait for the claim holder.
# 2. The publisher builds the layout in a staging sibling and ``os.rename``s
#    it into place. A rename is atomic, so the root either does not exist or
#    is complete; there is no moment at which "root exists" can mean anything
#    but "finished" (or "from before this feature").
#
# Together they make the states a resolver can observe unambiguous:
# no root -> nobody has published yet (claim it, or wait for the holder);
# root    -> complete or legacy, never in flight, so never ours to seed —
#            which is also what keeps a starter folder the user deleted from
#            coming back.
#
# Crash recovery: a holder that dies before publishing leaves its claim (and an
# inert staging directory) behind. A waiter that sees the claim outlive
# ``_STALE_CLAIM_SECONDS`` without a root appearing takes it over. Two
# publishers are still safe: the second rename fails on the non-empty root and
# that publisher discards its staging copy.
_SEEDING_MARKER = ".oh-my-gateway-seeding"
_STAGING_PREFIX = ".oh-my-gateway-staging"
_STALE_CLAIM_SECONDS = 60.0
_WAIT_POLL_SECONDS = 0.01


def _seeding_claim(workspace: Path) -> Path:
    """The claim file for *workspace*, as a sibling so it can precede the root."""
    return workspace.parent / f"{_SEEDING_MARKER}-{workspace.name}"


def _initial_workspace_dirs() -> tuple[Path, ...]:
    """Safe relative directories to seed into a brand-new backend workspace.

    Parsing and validation live in :func:`src.env_utils.parse_workspace_initial_dirs`
    so the startup config check reaches the same verdict through the same code.
    """
    try:
        return parse_workspace_initial_dirs(os.getenv("WORKSPACE_INITIAL_DIRS"))
    except ValueError as exc:
        raise ValueError(f"Invalid WORKSPACE_INITIAL_DIRS: {exc}") from exc


def _claim_initialization(workspace: Path) -> bool:
    """Try to become the single publisher for *workspace*.

    Exclusive (``O_EXCL``): ``True`` only for the one caller that created the
    claim file. ``False`` means another resolver holds it and will publish; the
    caller must then wait for that and never build on its own — a second
    creator is exactly what made every earlier version of this protocol racy.

    Exclusivity also closes a race the claim alone does not: two resolvers can
    both observe "no root" before either claims. Without ``O_EXCL`` the second
    would publish a fresh claim over the workspace the first just *completed*
    and seed it again, recreating a starter folder the user may have deleted.
    The winner still re-checks the root after claiming (see ``resolve``), and
    because every root is published atomically, a root that appeared since the
    observation is complete and not this caller's to touch.

    The claim's parent is the user's aggregate root, created here so the claim
    can precede the workspace directory.
    """
    claim = _seeding_claim(workspace)
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        logger.warning(
            "Failed to publish workspace seeding claim %s", claim, exc_info=True
        )
        raise
    return True


def _release_claim(workspace: Path) -> None:
    """Drop the claim; absence means a concurrent resolver released it first."""
    try:
        _seeding_claim(workspace).unlink()
    except FileNotFoundError:
        pass


def _publish_workspace(workspace: Path, initial_dirs: tuple[Path, ...]) -> None:
    """Build the full layout beside the workspace, then rename it into place.

    Only the claim holder calls this. The rename is what makes the protocol
    sound: the root becomes visible complete, in one step, so no observer can
    ever see a half-seeded workspace. If the rename finds a non-empty root
    already there — possible only after a stale-claim takeover raced a slow
    but live holder — someone else published a complete layout first, and the
    staging copy is simply discarded.

    The claim is released last, on every path, so a failed build never leaves
    a claim that waiters would have to age out.
    """
    staging = workspace.parent / (
        f"{_STAGING_PREFIX}-{workspace.name}-{uuid.uuid4().hex}"
    )
    try:
        staging.mkdir(parents=True, exist_ok=False)
        for relative_dir in initial_dirs:
            (staging / relative_dir).mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging, workspace)
        except OSError as exc:
            published_by_other = (
                exc.errno in (errno.ENOTEMPTY, errno.EEXIST) and workspace.is_dir()
            )
            if not published_by_other:
                raise
            logger.info(
                "Workspace %s was published by a concurrent resolver; "
                "discarding this staging copy",
                workspace,
            )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _release_claim(workspace)


def _await_publication(workspace: Path) -> None:
    """Wait, as a claim loser, for the holder to publish the root.

    Returns once the root exists (complete, by construction), or once the claim
    has gone away or gone stale without a root — the holder released after a
    failed build, or died. The caller then re-observes and, if the root is
    still missing, competes for the claim itself. A stale claim is removed here
    so that competition can succeed.
    """
    claim = _seeding_claim(workspace)
    while not workspace.exists():
        try:
            age = time.time() - claim.stat().st_mtime
        except FileNotFoundError:
            return  # released without publishing; caller re-observes
        if age > _STALE_CLAIM_SECONDS:
            logger.warning(
                "Workspace seeding claim %s is %.0fs old with no root published; "
                "treating its holder as gone",
                claim,
                age,
            )
            _release_claim(workspace)
            return
        time.sleep(_WAIT_POLL_SECONDS)


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

        # Seeding is creation-only: configured starter folders are onboarding
        # defaults, not invariants, so one the user later deletes must not come
        # back on the next resolve. "Has this workspace been initialized" is
        # therefore load-bearing, and the protocol above makes the directory
        # itself a truthful answer: a root is either absent or complete.
        if not initial_dirs:
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            while not workspace.exists():
                if not _claim_initialization(workspace):
                    # Someone else is the publisher. Create nothing; wait for
                    # the root to appear (complete) or the claim to lapse.
                    _await_publication(workspace)
                    continue
                if workspace.exists():
                    # Appeared between our observation and our claim. Published
                    # atomically, so it is complete (or legacy) — not ours.
                    _release_claim(workspace)
                    break
                _publish_workspace(workspace, initial_dirs)
                break
            if not workspace.is_dir():
                # Preserve pathlib's behavior for a non-directory collision.
                raise FileExistsError(f"{workspace} exists and is not a directory")

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
