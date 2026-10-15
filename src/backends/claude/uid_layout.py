"""Per-uid config dirs and workspace access for uid isolation (#241 follow-up).

``CLAUDE_ISOLATION_MODE=uid`` runs every session CLI as a session uid ``U``.
Two things the gateway owns must then become usable by ``U`` without handing
the gateway's own state over (``docs/isolation-uid-ownership-design.md``):

* the **workspace** stays owned by the gateway (app) uid; before each spawn the
  gateway grants ``U`` access to it with a POSIX ACL (design α, §3.1);
* the **config dir**: ``U`` gets its own ``P = <CLAUDE_ISOLATION_CONFIG_BASE>/<U>``
  (``CLAUDE_CONFIG_DIR`` of its CLI), app-owned, readable by ``U``, writable only
  where the CLI keeps per-user state, with the shared assets linked in from the
  gateway's own config dir ``S`` (§4.1).

This module answers "which layout does this session use" without allocating
anything (:func:`session_uid`, :func:`session_layout`, :func:`projects_root`)
and prepares a layout before a spawn (:func:`prepare_spawn`). Everything a
session uid locked away from the gateway (its 0600/0700 files) is removed or
read as that uid by the broker (``broker_ops``, ``uid_cleanup``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

from src import claude_config_paths as cp
from src.claude_config_paths import ConfigLayout

logger = logging.getLogger(__name__)

# The per-uid dirs the session uid writes, relative to its config dir ``P``.
# The gateway creates them (owned by app, opened to U with an ACL); the CLI
# keeps its transcripts, hook env files, task lists, plugin data and global
# config (``state/.claude.json``, linked as ``P/.claude.json``) there.
WRITABLE_DIRS = ("projects", "session-env", "tasks", "plugins/data", "state")


def enabled() -> bool:
    """Whether per-uid config dirs are in force: uid mode with a config base."""
    from . import landlock

    return landlock.effective_mode() == "uid" and cp.config_base() is not None


def session_uid(user: Optional[str], workspace) -> Optional[int]:
    """The session uid a spawn for (*user*, *workspace*) runs as, if it has one.

    Never allocates: a named user's durable uid from the registry, an anonymous
    run's ephemeral lease (keyed on the absolute workspace path). ``None`` when
    per-uid config dirs are off, or no uid was assigned yet (or the registry
    cannot honor the user's entry).
    """
    if not enabled():
        return None
    from . import uid_isolation as ui

    if user:
        try:
            return ui.existing_uid(user)
        except ui.UidAllocationError:
            return None
    if workspace is None:
        return None
    return ui.leased_uid(os.path.abspath(str(workspace)))


def session_layout(user: Optional[str], workspace) -> ConfigLayout:
    """The config layout of (*user*, *workspace*); the shared one when the
    session has no uid (see :func:`session_uid`)."""
    return cp.layout(session_uid(user, workspace))


def projects_root(user: Optional[str], workspace) -> Optional[Path]:
    """The ``projects/`` root that holds this session's transcripts.

    * per-uid config dirs off: the shared config dir's (today's behavior);
    * on, and the session has a uid: ``P(uid)/projects``;
    * on, a named user with no uid yet: the legacy config dir's
      (``CLAUDE_ISOLATION_MIGRATE_FROM``, default the shared one), whose
      transcripts that user's first spawn copies over — ``None`` when the copy
      is off;
    * on, an anonymous run with no lease: ``None`` (it cannot have one).
    """
    if not enabled():
        return cp.projects_root()
    uid = session_uid(user, workspace)
    if uid is not None:
        return cp.layout(uid).projects
    if user:
        legacy = cp.legacy_config_dir()
        return None if legacy is None else legacy / "projects"
    return None


def ephemeral_clear_targets(uid: int) -> List[str]:
    """The per-uid dirs whose contents go when an ephemeral lease of *uid* ends.

    Absolute paths of :data:`WRITABLE_DIRS` in ``P(uid)``; empty without a
    config base. The next anonymous run that leases *uid* must find none of
    the previous run's transcripts, hook env files, task lists, plugin data
    or global config there.
    """
    config = cp.user_config_dir(uid)
    if config is None:
        return []
    return [os.path.join(str(config), rel) for rel in WRITABLE_DIRS]
