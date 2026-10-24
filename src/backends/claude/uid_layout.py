"""Per-uid config dirs and workspace access for uid isolation (#241 follow-up).

``CLAUDE_ISOLATION_MODE=uid`` runs every session CLI as a session uid ``U``.
Two things the gateway owns must then become usable by ``U`` without handing
the gateway's own state over (``docs/isolation-uid-ownership-design.md``):

* the **workspace** stays owned by the gateway (app) uid; before each spawn the
  gateway grants ``U`` access to it with a POSIX ACL (design α, §3.1): access
  ``u::rwx, u:U:rwx, g::---, m::rwx, o::---`` and the default ACL
  ``d:u::rwx, d:u:U:rwx, d:u:<app>:rwx, d:g::---, d:m::rwx, d:o::---``, so what
  either side creates there stays reachable by the other unless it is made
  0600/0700 on purpose. An existing workspace's tree is granted once
  (§3.7, :func:`grant_workspace`);
* the **config dir**: ``U`` gets its own ``P = <CLAUDE_ISOLATION_CONFIG_BASE>/<U>``
  (``CLAUDE_CONFIG_DIR`` of its CLI), owned by app, readable by ``U``, writable
  only where the CLI keeps per-user state (:data:`WRITABLE_DIRS`), with the
  shared assets linked in from the gateway's own config dir ``S`` (§4.1,
  :func:`ensure_user_config`). A named user's transcripts and memory from the
  shared config dir are copied over on their first spawn (:func:`migrate_legacy`).

This module answers "which layout does this session use" without allocating
anything (:func:`session_uid`, :func:`session_layout`, :func:`projects_root`)
and prepares a layout before a spawn (:func:`prepare_spawn`). What a session uid
locks away from the gateway (its 0600/0700 files) is removed or read as that uid
by the broker (``broker_ops``, ``uid_cleanup``).

Every step here runs as the gateway uid, unconfined, on trees a session uid can
write: everything below the config base and the workspace is reached from
directory fds without following a symlink, and only entries the gateway owns
are changed.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from src import claude_config_paths as cp
from src.claude_config_paths import ConfigLayout

logger = logging.getLogger(__name__)

# The per-uid dirs the session uid writes, relative to its config dir ``P``.
# The gateway creates them (owned by app, opened to U with an ACL); the CLI
# keeps its transcripts, hook env files, task lists, plugin data and global
# config (``state/.claude.json``, linked as ``P/.claude.json``) there.
WRITABLE_DIRS = ("projects", "session-env", "tasks", "plugins/data", "state")

# ``P/<name> -> S/<name>``: the shared assets a session reads from the
# gateway's own config dir. Kept equal to the guard's and Landlock's shared
# names (``workspace_sandbox._SHARED_CLAUDE_ASSETS`` minus ``plugins``, which is
# a real per-uid dir here, plus ``landlock._LANDLOCK_ONLY_ASSETS``) by
# tests/test_uid_layout.py, plus the user settings files the CLI loads.
SHARED_LINKS = (
    "skills",
    "agents",
    "commands",
    "output-styles",
    "CLAUDE.md",
    "rules",
    "hooks",
    "workflows",
    "settings.json",
    "settings.local.json",
)
# ``P/plugins/<name> -> S/plugins/<name>``. ``plugins`` itself is a real dir:
# the CLI mkdirs ``plugins/data/<id>`` before every plugin hook and skips the
# hook when that fails, so a shared link would silently disable every hook
# (design §4.2, measured). No ``cache`` link: the registry names each plugin's
# absolute install path in S, and the CLI's cache GC would only try to write S.
PLUGIN_LINKS = ("installed_plugins.json", "known_marketplaces.json", "marketplaces")
# The CLI's global config is ``<CLAUDE_CONFIG_DIR>/.claude.json``; it writes it
# with a lock and a rename in ``P``, which ``U`` cannot write, and then falls
# back to writing through the link (measured), so the link points into the
# writable ``state/``.
GLOBAL_CONFIG = ".claude.json"
_GLOBAL_CONFIG_TARGET = "state/.claude.json"

# Marker beside a named workspace (in its app-owned parent) recording the uid
# its existing tree was granted to (grant_workspace), and the list of migrated
# project dirs in ``P`` (migrate_legacy). Both live where the session uid
# cannot write.
_TREE_MARKER_PREFIX = ".oh-my-gateway-acl-"
_MIGRATED_MARKER = ".oh-my-gateway-migrated"
# In P: whose config dir it is — the digest of the named user's key, written
# once its first clean start passed (:func:`_claim`).
_OWNER_MARKER = ".oh-my-gateway-owner"

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_DIR_NOFOLLOW = _DIR | os.O_NOFOLLOW
_FILE_NOFOLLOW = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
)


# Where POSIX shared memory objects live; tests point it elsewhere (the test
# uid is the developer's, whose own /dev/shm objects must never be touched).
_SHM_DIR = "/dev/shm"


def _workspace_refused():
    from .landlock import WorkspaceRefused

    return WorkspaceRefused


def refused(message: str) -> Exception:
    """A ``landlock.WorkspaceRefused`` for a layout this module cannot prepare.

    The request paths answer it with the non-retryable 500
    ``workspace_refused`` (an operator has to fix the layout); the details are
    logged here, never sent to the caller.
    """
    logger.error("uid isolation: %s", message)
    return _workspace_refused()(f"refusing an isolated spawn: {message}")


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
        if user and workspace is not None:
            legacy = _unmigrated_legacy_projects(uid, workspace)
            if legacy is not None:
                return legacy
        return cp.layout(uid).projects
    if user:
        legacy = cp.legacy_config_dir()
        return None if legacy is None else legacy / "projects"
    return None


def _unmigrated_legacy_projects(uid: int, workspace) -> Optional[Path]:
    """The legacy ``projects`` root while this workspace's project dir is
    still only there, else ``None``.

    The legacy copy runs on a spawn in that workspace (:func:`migrate_user`).
    A named user who got a uid in one workspace may still resume a session of
    another (the legacy ``<base>/<user>`` layout): until a spawn there copied
    its project dir, its transcripts are only in the legacy config dir, and
    looking in ``P`` alone answered 404 before any spawn could copy them.
    """
    from . import workspace_sandbox as ws

    legacy = cp.legacy_config_dir()
    config = cp.user_config_dir(uid)
    if legacy is None or config is None:
        return None
    name = ws._project_dir_name(str(workspace))
    try:
        os.lstat(config / "projects" / name)
        return None  # P has it: authoritative
    except OSError:
        pass
    try:
        p_fd = os.open(os.path.realpath(config), _DIR_NOFOLLOW)
    except OSError:
        p_fd = -1
    if p_fd >= 0:
        try:
            if name in _migrated(p_fd):
                return None
        finally:
            os.close(p_fd)
    candidate = legacy / "projects"
    try:
        if stat.S_ISDIR(os.lstat(candidate / name).st_mode):
            return candidate
    except OSError:
        pass
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


# --------------------------------------------------------------------------
# Preparing a spawn
# --------------------------------------------------------------------------

_uid_locks: Dict[int, threading.Lock] = {}
_uid_locks_guard = threading.Lock()


def _uid_lock(uid: int) -> threading.Lock:
    """One lock per session uid: a named user's concurrent spawns prepare the
    same ``P`` and workspace, and the one-time steps must run once."""
    with _uid_locks_guard:
        lock = _uid_locks.get(uid)
        if lock is None:
            lock = _uid_locks[uid] = threading.Lock()
        return lock


def _app_uid() -> int:
    return os.geteuid()


def _mkdir_at(dir_fd: int, name: str, mode: int) -> int:
    """``name`` below *dir_fd* as a directory the gateway owns, opened.

    Made when missing; never followed when it is a symlink (ELOOP), and
    refused when something else or another uid's dir sits there.
    """
    try:
        os.mkdir(name, mode, dir_fd=dir_fd)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, _DIR_NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        raise refused(f"{name!r} is not a directory the gateway can use ({exc})")
    if os.fstat(fd).st_uid != _app_uid():
        os.close(fd)
        raise refused(f"{name!r} is not owned by the gateway uid")
    return fd


def _acl_equal(current, wanted) -> bool:
    return current is not None and sorted(current) == sorted(wanted)


def _set_acl(fd: int, entries, *, default: bool = False) -> None:
    """Set an ACL unless it is already exactly that (no ctime churn)."""
    from . import posix_acl

    if _acl_equal(posix_acl.get_acl(fd, default=default), entries):
        return
    posix_acl.set_acl(fd, entries, default=default)


def _grant_readable(fd: int, uid: int) -> None:
    """``u::rwx, u:U:r-x, g::---, m::r-x, o::---``: U lists and enters, never
    writes (it must not swap the shared links in ``P``)."""
    from . import posix_acl

    _set_acl(fd, posix_acl.session_dir_access(uid, write=False))


def _grant_writable(fd: int, uid: int) -> None:
    """The workspace-root ACL (access + default) on one writable dir."""
    from . import posix_acl

    _set_acl(fd, posix_acl.session_dir_access(uid, write=True))
    _set_acl(fd, posix_acl.session_dir_default(uid, _app_uid()), default=True)


_warned: Set[Tuple] = set()


def _warn_once(key: Tuple, msg: str, *args) -> None:
    if key in _warned:
        return
    if len(_warned) > 1024:
        _warned.clear()
    _warned.add(key)
    logger.warning(msg, *args)


def _ensure_link(dir_fd: int, name: str, target: str) -> None:
    """``name -> target`` below *dir_fd* (only the gateway writes there).

    A link to the wrong place is replaced; a real file or dir in its place is
    left alone with a warning (never delete what someone put there).
    """
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISLNK(st.st_mode):
            _warn_once(
                ("link", target, name),
                "uid isolation: %s in a per-uid config dir is not the link to %s "
                "the layout expects; leaving it as it is",
                name,
                target,
            )
            return
        if os.readlink(name, dir_fd=dir_fd) == target:
            return
        os.unlink(name, dir_fd=dir_fd)
    try:
        os.symlink(target, name, dir_fd=dir_fd)
    except FileExistsError:
        pass  # a concurrent prepare of the same uid (held off by _uid_lock)


def _open_config_base() -> int:
    """The config base, resolved and opened (created 0711 when missing)."""
    base = cp.config_base()
    if base is None:
        raise refused(f"{cp.CONFIG_BASE_ENV} is not set")
    real = os.path.realpath(base)
    try:
        os.makedirs(real, mode=0o711, exist_ok=True)
        return os.open(real, _DIR)
    except OSError as exc:
        raise refused(f"the per-uid config base {real} is unusable ({exc})")


def ensure_user_config(layout: ConfigLayout) -> None:
    """Make ``P = layout.config_dir`` the per-uid config dir of ``layout.uid``.

    Idempotent; runs before every per-uid spawn (a few syscalls when nothing
    changed). Raises ``WorkspaceRefused`` (:func:`refused`) when it cannot.
    """
    from . import posix_acl

    uid = layout.uid
    if uid is None:
        return
    shared = os.path.realpath(layout.shared_dir)
    base_fd = _open_config_base()
    try:
        p_fd = _mkdir_at(base_fd, str(uid), 0o750)
        try:
            _grant_readable(p_fd, uid)
            for name in SHARED_LINKS:
                _ensure_link(p_fd, name, os.path.join(shared, name))
            _ensure_link(p_fd, GLOBAL_CONFIG, _GLOBAL_CONFIG_TARGET)
            for rel in ("projects", "session-env", "tasks", "state"):
                fd = _mkdir_at(p_fd, rel, 0o770)
                try:
                    _grant_writable(fd, uid)
                    if rel == "state":
                        _ensure_global_config(fd)
                finally:
                    os.close(fd)
            plugins_fd = _mkdir_at(p_fd, "plugins", 0o750)
            try:
                _grant_readable(plugins_fd, uid)
                for name in PLUGIN_LINKS:
                    target = os.path.join(shared, "plugins", name)
                    _ensure_link(plugins_fd, name, target)
                data_fd = _mkdir_at(plugins_fd, "data", 0o770)
                try:
                    _grant_writable(data_fd, uid)
                finally:
                    os.close(data_fd)
            finally:
                os.close(plugins_fd)
        finally:
            os.close(p_fd)
    except posix_acl.AclUnsupported as exc:
        raise refused(
            f"the per-uid config base does not support POSIX ACLs ({exc}); uid "
            "mode needs them"
        )
    except OSError as exc:
        raise refused(f"the per-uid config dir of uid {uid} is unusable ({exc})")
    finally:
        os.close(base_fd)


def _ensure_global_config(state_fd: int) -> None:
    """``state/.claude.json`` as ``{}`` when missing, so the CLI's fallback
    write through ``P/.claude.json`` has a target (created 0666: under the
    dir's default ACL, U and the gateway both get read + write)."""
    try:
        fd = os.open(
            GLOBAL_CONFIG,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o666,
            dir_fd=state_fd,
        )
    except FileExistsError:
        return
    try:
        os.write(fd, b"{}\n")
    finally:
        os.close(fd)


# Anonymous workspaces whose tree this process granted (they never outlive it),
# by resolved path → uid.
_granted_anonymous: Dict[str, int] = {}
_GRANTED_ANONYMOUS_MAX = 4096
# (workspace, uid) whose tree grant this process tried and could not complete
# (errors or the entry cap): not re-walked on every spawn, retried on restart.
_tree_attempted: Set[Tuple[str, int]] = set()


def grant_workspace(workspace: str, uid: int, *, named: bool) -> None:
    """Open the resolved *workspace* root to *uid* (design §3.1).

    The root keeps its owner (the gateway); its access and default ACL are set
    on every spawn — idempotent, and the only way the gateway's own later
    ``chmod`` could have narrowed them. Never chmod the root after this: the
    group bits are the ACL mask. The tree that existed before the first grant
    (app-owned 0755/0644 from the off/landlock modes) is granted once
    (``posix_acl.grant_tree``): a default ACL is inherited only at creation.
    Raises ``WorkspaceRefused`` when it cannot.
    """
    from . import posix_acl

    try:
        fd = os.open(workspace, _DIR_NOFOLLOW)
    except OSError as exc:
        raise refused(f"cannot open the workspace {workspace} ({exc})")
    try:
        if os.fstat(fd).st_uid != _app_uid():
            raise refused(f"the workspace {workspace} is not owned by the gateway uid")
        _grant_writable(fd, uid)
        attempt = (workspace, uid)
        if attempt not in _tree_attempted and not _tree_granted(workspace, uid, named):
            from . import workspace_resources

            started = time.monotonic()
            # grant_tree skips files with several names, and every file of the
            # gateway's .claude/{skills,agents} mirrors is a hard link of its
            # canonical file: empty the mirrors first so the user's own skills
            # and agents are granted, then rebuild them (the ACL is per inode).
            # Under the workspace's mirror lock: a concurrent materialize (another
            # request's create_client or slash preflight) re-linking between the
            # clear and the grant would leave those files ungranted for good.
            # Every caller runs in a worker thread or at startup: the wait is
            # unbounded.
            with workspace_resources.workspace_lock(Path(workspace)):
                if named:
                    workspace_resources.clear_managed_mirrors(Path(workspace))
                stats = posix_acl.grant_tree(fd, uid, _app_uid(), owner=_app_uid())
                if named:
                    workspace_resources.materialize_workspace_resources(
                        Path(workspace)
                    )
            took = time.monotonic() - started
            complete = not stats.truncated and not stats.errors
            log = logger.info if complete else logger.warning
            log(
                "uid isolation: granted the existing tree of %s to uid %d in "
                "%.2fs (%d dirs, %d files, %d skipped, %d errors%s)",
                workspace,
                uid,
                took,
                stats.dirs,
                stats.files,
                stats.skipped,
                stats.errors,
                ", truncated" if stats.truncated else "",
            )
            # Recorded only when complete, so a gateway restart tries again;
            # within this process it is not re-walked on every spawn.
            if complete:
                _mark_tree_granted(workspace, uid, named)
            elif len(_tree_attempted) < _GRANTED_ANONYMOUS_MAX:
                _tree_attempted.add(attempt)
    except posix_acl.AclUnsupported as exc:
        raise refused(
            f"the workspace {workspace} does not support POSIX ACLs ({exc}); uid "
            "mode needs them"
        )
    except OSError as exc:
        raise refused(f"cannot grant the workspace {workspace} to uid {uid} ({exc})")
    finally:
        os.close(fd)


def _tree_marker(workspace: str) -> Tuple[str, str]:
    parent, name = os.path.split(workspace.rstrip(os.sep))
    return parent, _TREE_MARKER_PREFIX + name


def _tree_granted(workspace: str, uid: int, named: bool) -> bool:
    if not named:
        return _granted_anonymous.get(workspace) == uid
    parent, marker = _tree_marker(workspace)
    try:
        pfd = os.open(parent, _DIR_NOFOLLOW)
    except OSError:
        return False
    try:
        fd = os.open(marker, _FILE_NOFOLLOW, dir_fd=pfd)
    except OSError:
        return False
    finally:
        os.close(pfd)
    try:
        return os.read(fd, 64).strip() == str(uid).encode()
    except OSError:
        return False
    finally:
        os.close(fd)


def _mark_tree_granted(workspace: str, uid: int, named: bool) -> None:
    if not named:
        if len(_granted_anonymous) >= _GRANTED_ANONYMOUS_MAX:
            _granted_anonymous.clear()
        _granted_anonymous[workspace] = uid
        return
    parent, marker = _tree_marker(workspace)
    try:
        _write_file_at(parent, marker, f"{uid}\n".encode())
    except OSError as exc:
        _warn_once(
            ("tree-marker", workspace),
            "uid isolation: could not record the grant of %s (%s); it is redone "
            "on the next spawn",
            workspace,
            exc,
        )


def _write_file_at(directory: str, name: str, data: bytes) -> None:
    """Replace ``directory/name`` atomically and durably (a temp name, fsync,
    a rename, fsync of the dir), never following a symlink; mode 0644, owned
    by the gateway. A crash leaves the old file or the new one, never an
    empty one: a marker read back empty would mean something else."""
    dfd = os.open(directory, _DIR_NOFOLLOW)
    try:
        tmp = f".{name}.tmp-{os.getpid()}"
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=dfd,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        os.fsync(dfd)
    finally:
        os.close(dfd)


# --------------------------------------------------------------------------
# Ephemeral uids: a leased uid's config dir must be empty before it is used
# --------------------------------------------------------------------------

# uid -> the lease key whose spawn found P(uid) clean (this process).
_clean_for: Dict[int, str] = {}


_PRISTINE_GLOBAL_CONFIG = (b"{}", b"")


def _pristine_global_config(state_dir: str) -> bool:
    """Whether ``state/.claude.json`` is still the ``{}`` the gateway wrote
    (a run's CLI writes its whole global config there, cwd history included)."""
    try:
        dfd = os.open(state_dir, _DIR_NOFOLLOW)
    except OSError:
        return False
    try:
        fd = os.open(GLOBAL_CONFIG, _FILE_NOFOLLOW, dir_fd=dfd)
    except OSError:
        return False
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 16:
            return False
        return os.read(fd, 32).strip() in _PRISTINE_GLOBAL_CONFIG
    except OSError:
        return False
    finally:
        os.close(fd)


def _has_leftovers(uid: int) -> bool:
    """Whether ``P(uid)``'s writable dirs or ``/dev/shm`` hold anything of a
    previous run (the gateway can list them: they are its own dirs). The
    gateway's own untouched ``state/.claude.json`` does not count."""
    for path in ephemeral_clear_targets(uid):
        try:
            entries = os.listdir(path)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if os.path.basename(path) == "state" and entries == [GLOBAL_CONFIG]:
            if _pristine_global_config(path):
                continue
        if entries:
            return True
    try:
        with os.scandir(_SHM_DIR) as shm:
            for entry in shm:
                try:
                    if entry.stat(follow_symlinks=False).st_uid == uid:
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    # SysV shm/sem/msg objects and POSIX message queues outlive a run too, and
    # Landlock mediates none of them: the next caller on this uid could attach.
    from .uid_cleanup import ipc_leftovers

    return bool(ipc_leftovers(uid))


def ensure_clean_ephemeral(uid: int, key: str) -> None:
    """Make sure the run leasing *uid* under *key* starts clean.

    For an ephemeral uid on every new lease; for a named user's uid until its
    config dir is claimed for that user (:func:`_owner_state`): a band change
    can hand a named user a uid an anonymous run used.

    A lease is released only after the broker wiped ``P(uid)`` and the uid's
    ``/dev/shm`` objects (``uid_cleanup``), but a previous gateway process may
    have died before that: its leftovers would otherwise be the next anonymous
    caller's to read. Checked once per (uid, key); leftovers are wiped as
    *uid* through the broker before the spawn, and a wipe that fails refuses
    the spawn retryably (``UidAllocationError``: the run is not given a uid
    that holds another run's data).
    """
    if _clean_for.get(uid) == key:
        return
    if _has_leftovers(uid):
        from . import broker_ops
        from . import uid_isolation as ui
        from .uid_cleanup import _broker_path

        logger.warning(
            "uid isolation: uid %d still holds a previous run's state; wiping it "
            "before reuse",
            uid,
        )
        targets = [
            _broker_path(p) for p in ephemeral_clear_targets(uid) if os.path.isdir(p)
        ]
        try:
            done = broker_ops.cleanup(uid, clear=targets, shm=True, wait=10)
        except Exception as exc:
            raise ui.UidAllocationError(
                f"could not wipe the previous run's state of uid {uid}: {exc}"
            ) from exc
        if not done or _has_leftovers(uid):
            raise ui.UidAllocationError(
                f"the previous run's state of uid {uid} is not fully removed; "
                "retry shortly"
            )
    _clean_for[uid] = key


def _owner_digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest().encode()


_DIGEST = re.compile(rb"[0-9a-f]{64}")


def _owner_state(uid: int, key: str) -> str:
    """Whom ``P(uid)`` belongs to, for a named user's *key*: ``"mine"`` (the
    marker holds this key), ``"unclaimed"`` (no marker, no P yet, or another
    key's marker: whatever is there is no data of this user's) — or
    ``WorkspaceRefused`` when the marker cannot be read or holds no digest
    (empty, cut short): such a marker must not make the gateway wipe the
    user's own transcripts. Removing it by hand redoes the clean start.

    A ``P`` made before this marker existed (builds of the uid layout before
    it) reads as unclaimed: its user's first spawn wipes it."""
    config = cp.user_config_dir(uid)
    if config is None:
        raise refused(f"{cp.CONFIG_BASE_ENV} is not set")
    try:
        dfd = os.open(os.path.realpath(config), _DIR_NOFOLLOW)
    except FileNotFoundError:
        return "unclaimed"
    except OSError as exc:
        raise refused(f"the per-uid config dir of uid {uid} is unusable ({exc})")
    try:
        fd = os.open(_OWNER_MARKER, _FILE_NOFOLLOW, dir_fd=dfd)
    except FileNotFoundError:
        return "unclaimed"
    except OSError as exc:
        raise refused(f"cannot read the owner of uid {uid}'s config dir ({exc})")
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != _app_uid():
            raise refused(f"the owner marker of uid {uid}'s config dir is not ours")
        data = os.read(fd, 128).strip()
    except OSError as exc:
        raise refused(f"cannot read the owner of uid {uid}'s config dir ({exc})")
    finally:
        os.close(fd)
    if not _DIGEST.fullmatch(data):
        raise refused(
            f"the owner marker of uid {uid}'s config dir is malformed; remove "
            f"{_OWNER_MARKER} there to redo its clean start"
        )
    return "mine" if data == _owner_digest(key) else "unclaimed"


def _claim(uid: int, key: str) -> None:
    """Record ``P(uid)`` as *key*'s, after its clean start passed and before
    any data of the user lands there. A failure refuses the spawn: without
    the marker, the next spawn would take the user's data for a previous run's.
    A previous owner's migration record goes first (it names their projects)."""
    config = cp.user_config_dir(uid)
    if config is None:
        raise refused(f"{cp.CONFIG_BASE_ENV} is not set")
    real = os.path.realpath(config)
    try:
        dfd = os.open(real, _DIR_NOFOLLOW)
        try:
            os.unlink(_MIGRATED_MARKER, dir_fd=dfd)
        except FileNotFoundError:
            pass
        finally:
            os.close(dfd)
        _write_file_at(real, _OWNER_MARKER, _owner_digest(key) + b"\n")
    except OSError as exc:
        raise refused(f"cannot record the owner of uid {uid}'s config dir ({exc})")


def _unclaim(uid: int) -> None:
    """Drop the owner marker of a uid an ephemeral run leases (the registry
    gave it up): a named user it goes to later starts clean again."""
    config = cp.user_config_dir(uid)
    if config is None:
        return
    try:
        dfd = os.open(os.path.realpath(config), _DIR_NOFOLLOW)
    except OSError:
        return
    try:
        os.unlink(_OWNER_MARKER, dir_fd=dfd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise refused(f"cannot clear the owner of uid {uid}'s config dir ({exc})")
    finally:
        os.close(dfd)


# --------------------------------------------------------------------------
# Legacy migration: a named user's transcripts from the shared config dir
# --------------------------------------------------------------------------

_COPY_MAX_ENTRIES = 50000
_COPY_MAX_BYTES = 2 << 30
# A legacy tree is session-written: no deeper than this (recursion), no file
# with another name elsewhere (its current content would be copied over).
_COPY_MAX_DEPTH = 64


class _CopyBudget:
    def __init__(self) -> None:
        self.entries = 0
        self.bytes = 0
        self.truncated = False

    def take(self, size: int = 0) -> bool:
        self.entries += 1
        self.bytes += size
        ok = self.entries <= _COPY_MAX_ENTRIES and self.bytes <= _COPY_MAX_BYTES
        if not ok:
            self.truncated = True
        return ok


def _copy_file_at(src_dir: int, name: str, dst_dir: int, budget: _CopyBudget) -> bool:
    """Copy the regular file *name*; never through a symlink, never over an
    existing destination. Returns whether it was copied."""
    try:
        sfd = os.open(name, _FILE_NOFOLLOW, dir_fd=src_dir)
    except OSError:
        return False
    try:
        st = os.fstat(sfd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            return False
        if not budget.take(st.st_size):
            return False
        try:
            dfd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o666,
                dir_fd=dst_dir,
            )
        except FileExistsError:
            return False
        try:
            while True:
                chunk = os.read(sfd, 1 << 20)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(dfd, view) :]
            os.utime(dfd, ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(dfd)
        return True
    finally:
        os.close(sfd)


def _copy_tree_at(
    src_dir: int, name: str, dst_dir: int, budget: _CopyBudget, depth: int = 0
) -> None:
    """Copy the directory *name* recursively (regular files and dirs only),
    at most :data:`_COPY_MAX_DEPTH` levels deep."""
    if depth > _COPY_MAX_DEPTH:
        budget.truncated = True
        return
    try:
        sfd = os.open(name, _DIR_NOFOLLOW, dir_fd=src_dir)
    except OSError:
        return
    try:
        if not budget.take():
            return
        try:
            os.mkdir(name, 0o777, dir_fd=dst_dir)
        except FileExistsError:
            pass
        try:
            dfd = os.open(name, _DIR_NOFOLLOW, dir_fd=dst_dir)
        except OSError:
            return
        try:
            for entry in sorted(os.listdir(sfd)):
                try:
                    st = os.stat(entry, dir_fd=sfd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    _copy_tree_at(sfd, entry, dfd, budget, depth + 1)
                elif stat.S_ISREG(st.st_mode):
                    _copy_file_at(sfd, entry, dfd, budget)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def _transcript_owner_ok(src_dir: int, name: str, workspaces: List[str]) -> int:
    """``count_transcript_turns`` of the legacy transcript *name* against the
    workspace's spellings: the first non-mismatch result, else MISMATCH. The
    file was session-written: one larger than ``MAX_TRANSCRIPT_BYTES`` is
    UNREADABLE (not copied), and lines are read through ``bounded_lines``."""
    from .transcript_turns import (
        MAX_TRANSCRIPT_BYTES,
        TRANSCRIPT_CORRUPT,
        TRANSCRIPT_MISMATCH,
        TRANSCRIPT_UNREADABLE,
        bounded_lines,
        count_transcript_turns,
    )

    result = TRANSCRIPT_MISMATCH
    for workspace in workspaces:
        try:
            fd = os.open(name, _FILE_NOFOLLOW, dir_fd=src_dir)
        except OSError:
            return TRANSCRIPT_UNREADABLE
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_nlink > 1
                or st.st_size > MAX_TRANSCRIPT_BYTES
            ):
                os.close(fd)
                return TRANSCRIPT_UNREADABLE
            with os.fdopen(fd, "r", encoding="utf-8", newline=None) as fh:
                try:
                    result = count_transcript_turns(bounded_lines(fh), workspace)
                except UnicodeDecodeError:
                    return TRANSCRIPT_CORRUPT
        except OSError:
            return TRANSCRIPT_UNREADABLE
        if result != TRANSCRIPT_MISMATCH:
            return result
    return result


def _migrated(p_fd: int) -> Set[str]:
    try:
        fd = os.open(_MIGRATED_MARKER, _FILE_NOFOLLOW, dir_fd=p_fd)
    except OSError:
        return set()
    try:
        data = os.read(fd, 1 << 20)
    except OSError:
        return set()
    finally:
        os.close(fd)
    return set(data.decode("utf-8", "replace").split())


# (uid, project name) whose migration raised in this process.
_migrate_failed: Set[Tuple[int, str]] = set()


def _name_is_shared(name: str, workspace: str) -> bool:
    """Whether another workspace under the workspace base maps to the same
    ``projects/`` name (``a.b`` and ``a_b``), so files in that legacy project
    dir with no owner of their own (``memory/``) may be someone else's."""
    from src.workspace_manager import workspace_manager

    from . import workspace_sandbox as ws

    base = os.path.realpath(workspace_manager.base_path)
    mine = os.path.realpath(workspace)
    try:
        users = os.listdir(base)
    except OSError:
        return True  # cannot tell: treat it as shared
    for user in users:
        top = os.path.join(base, user)
        candidates = [top]
        try:
            candidates += [
                os.path.join(top, d)
                for d in os.listdir(top)
                if os.path.isdir(os.path.join(top, d))
            ]
        except OSError:
            pass
        for candidate in candidates:
            if candidate != mine and ws._project_dir_name(candidate) == name:
                return True
    return False


def migrate_legacy(user: str, uid: int, workspace: str) -> None:
    """Copy a named user's ``projects/<name>`` from the legacy config dir once.

    Before uid mode, every session CLI kept its transcripts, subagent logs and
    auto memory in the shared config dir. The first per-uid spawn of a user
    copies what is theirs into ``P(uid)/projects/<name>`` (design §3.7): the
    transcripts whose every recorded ``cwd`` is this workspace (the
    ``projects/`` names are lossy: other users' transcripts can share the dir),
    their sidecar dirs, and ``memory/`` only when no transcript in the dir
    belongs to anyone else. Copies are made by the gateway (0666 under the
    dir's default ACL: the session uid reads and appends them). The source is
    left in place (rollback). Recorded in ``P``, so it runs once per project
    dir; best effort: a failure is logged and the spawn goes on.
    """
    legacy = cp.legacy_config_dir()
    config = cp.user_config_dir(uid)
    if legacy is None or config is None:
        return
    from . import workspace_sandbox as ws

    name = ws._project_dir_name(workspace)
    attempt = (uid, name)
    if attempt in _migrate_failed:
        return  # it failed in this process: no retry on every spawn
    try:
        p_fd = os.open(os.path.realpath(config), _DIR_NOFOLLOW)
    except OSError:
        return
    try:
        done = _migrated(p_fd)
        if name in done:
            return
        copied = _copy_project(legacy, name, p_fd, workspace)
        if copied is None:
            return  # the legacy dir is unreadable now: try again next time
        _write_file_at(
            os.path.realpath(config),
            _MIGRATED_MARKER,
            "\n".join(sorted(done | {name})).encode() + b"\n",
        )
        if copied:
            logger.info(
                "uid isolation: migrated %d legacy transcript(s) of user %r into "
                "the per-uid config dir of uid %d",
                copied,
                user,
                uid,
            )
    except Exception as exc:  # best effort: never fails the spawn
        if len(_migrate_failed) < _GRANTED_ANONYMOUS_MAX:
            _migrate_failed.add(attempt)
        _warn_once(
            ("migrate", uid, name),
            "uid isolation: could not migrate the legacy transcripts of uid %d "
            "(retried after a restart): %r",
            uid,
            exc,
        )
    finally:
        os.close(p_fd)


def _copy_project(legacy: Path, name: str, p_fd: int, workspace: str) -> Optional[int]:
    """Copy ``<legacy>/projects/<name>`` into ``P/projects/<name>``; how many
    transcripts were copied, ``None`` when the source cannot be read."""
    from .transcript_turns import TRANSCRIPT_MISMATCH

    try:
        projects_fd = os.open(os.path.join(os.path.realpath(legacy), "projects"), _DIR)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    try:
        try:
            src = os.open(name, _DIR_NOFOLLOW, dir_fd=projects_fd)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return 0  # not a real dir: nothing of this user's to copy
            return None
    finally:
        os.close(projects_fd)
    try:
        entries = sorted(os.listdir(src))
        spellings = list(dict.fromkeys([workspace, os.path.abspath(workspace)]))
        sids: List[str] = []
        mismatch = False
        for entry in entries:
            if not entry.endswith(".jsonl"):
                continue
            turns = _transcript_owner_ok(src, entry, spellings)
            if turns == TRANSCRIPT_MISMATCH:
                mismatch = True
            elif turns >= 0:
                sids.append(entry[: -len(".jsonl")])
        if not sids and (mismatch or "memory" not in entries):
            return 0
        dst_parent = os.open("projects", _DIR_NOFOLLOW, dir_fd=p_fd)
        try:
            try:
                os.mkdir(name, 0o777, dir_fd=dst_parent)
            except FileExistsError:
                pass
            dst = os.open(name, _DIR_NOFOLLOW, dir_fd=dst_parent)
        finally:
            os.close(dst_parent)
        budget = _CopyBudget()
        copied = 0
        try:
            for sid in sids:
                if _copy_file_at(src, sid + ".jsonl", dst, budget):
                    copied += 1
                if sid in entries:
                    _copy_tree_at(src, sid, dst, budget)
            # memory/ has no recorded owner: only when nothing else may own it.
            if (
                not mismatch
                and "memory" in entries
                and not _name_is_shared(name, workspace)
            ):
                _copy_tree_at(src, "memory", dst, budget)
        finally:
            os.close(dst)
        if budget.truncated:
            logger.warning(
                "uid isolation: the legacy project dir %s exceeded the copy budget; "
                "only part of it was migrated",
                name,
            )
        return copied
    finally:
        os.close(src)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


async def to_thread_to_completion(func, /, *args, **kwargs):
    """``asyncio.to_thread``, except that a cancelled caller first waits for
    the thread to finish; the cancellation then propagates as usual.

    A spawn's preparation runs in a thread (it may wait for a broker wipe):
    it leases an anonymous run's uid, grants the workspace and builds the
    layout. Cancelling the await does not stop the thread, and the caller's
    cleanup (removing the workspace, releasing the lease) would otherwise run
    beside it — the thread could then lease again for a workspace that is
    gone. A failure the thread raises after the cancellation is logged."""
    future = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.wait({future})
            except asyncio.CancelledError:
                pass  # cancelled again: the thread still has to finish
        if not future.cancelled() and future.exception() is not None:
            logger.debug(
                "uid isolation: %s failed after its caller was cancelled: %r",
                getattr(func, "__name__", func),
                future.exception(),
            )
        raise


def prepare_spawn(
    uid: int,
    workspace: str,
    *,
    user: Optional[str],
    key: str,
) -> ConfigLayout:
    """Make *uid*'s config dir and the resolved *workspace* ready for a spawn.

    Runs before every per-uid spawn (sessions, the slash preflight, ``verify()``
    and the startup probe): first the clean start (:func:`_start_clean`, *key*
    being the lease key of an anonymous run or ``named:<user>``), then the
    per-uid config dir (:func:`ensure_user_config`), the workspace grant
    (:func:`grant_workspace`) and, for a named user, the one-time legacy
    migration (:func:`migrate_legacy`). Returns the spawn's layout. Raises
    ``WorkspaceRefused`` for a layout it cannot prepare and
    ``UidAllocationError`` for a uid that is not clean yet.
    """
    layout = cp.layout(uid)
    if not layout.per_uid:
        raise refused(f"{cp.CONFIG_BASE_ENV} is not set")
    with _uid_lock(uid):
        _start_clean(uid, layout, user=user, key=key)
        grant_workspace(workspace, uid, named=bool(user))
        if user:
            migrate_legacy(user, uid, workspace)
    return layout


def _start_clean(
    uid: int, layout: ConfigLayout, *, user: Optional[str], key: str
) -> None:
    """The clean start and config dir of a spawn, under the uid's lock.

    An anonymous run's ephemeral uid is checked on every new lease. A named
    user's uid until its config dir is claimed for that user: a band change
    can hand a named user a uid an anonymous run used, and the check must
    survive a wipe that failed (the registry keeps the uid, the client
    retries) and a restart — so it is decided by the marker in ``P``
    (:func:`_owner_state`), not by whether the registry had the uid. The clean
    check runs first (a wipe empties ``state/`` too, and the layout puts the
    pristine global config back); the claim is recorded before anything of
    the user's lands in ``P``.
    """
    if not user:
        ensure_clean_ephemeral(uid, key)
        ensure_user_config(layout)
        _unclaim(uid)
        return
    claimed = _owner_state(uid, key) == "mine"
    if not claimed:
        ensure_clean_ephemeral(uid, key)
    ensure_user_config(layout)
    if not claimed:
        _claim(uid, key)


def migrate_user(user: Optional[str], workspace) -> None:
    """For ``create_client``, in a worker thread: a named *user*'s one-time
    steps ahead of the spawn's synchronous option build.

    Gives the user their uid and config dir, grants the workspace (its whole
    existing tree, the first time) and runs the legacy migration *before*
    ``create_client`` asks whether the session has a transcript (a migrated one
    makes it ``--resume``). The spawn's own :func:`prepare_spawn` then finds
    all of it done. The workspace is checked exactly as a spawn checks it
    (``landlock._confined_workspace``). No-op unless per-uid config dirs are
    on. Allocates the user's durable uid (the create path is a spawn); raises
    like :func:`prepare_spawn`."""
    if not user or workspace is None or not enabled():
        return
    from . import landlock
    from . import uid_isolation as ui

    uid = ui.resolve_uid(user, persist=True)
    layout = cp.layout(uid)
    resolved = landlock._confined_workspace(
        landlock._absolute(workspace),
        Path(os.path.realpath(layout.config_dir)),
        layout=layout,
    )
    with _uid_lock(uid):
        _start_clean(uid, layout, user=user, key=f"named:{user}")
        grant_workspace(resolved, uid, named=True)
        migrate_legacy(user, uid, resolved)


# --------------------------------------------------------------------------
# The shared tree S
# --------------------------------------------------------------------------

# What of S a session uid reads through its links: S itself and these entries.
_SHARED_FILES = ("settings.json", "settings.local.json", "CLAUDE.md")
_SHARED_PLUGIN_FILES = ("installed_plugins.json", "known_marketplaces.json")


def ensure_shared_readable() -> List[str]:
    """At uid-mode startup: make the shared tree's entry points readable by
    session uids, and return what it changed.

    ``S`` carries no ACL, so plain mode bits decide: ``S``, its asset dirs and
    ``plugins/`` get ``o+rx``, its settings and registry files ``o+r`` — only
    entries the gateway owns, never through a symlink, never deeper (what the
    CLI and git write below them is readable by default). The admin settings
    writer and the CLI's plugin commands may write these 0600.
    """
    shared = os.path.realpath(cp.config_dir())
    changed: List[str] = []

    def widen(dir_fd: Optional[int], name: str, label: str) -> None:
        """Add ``o+r`` (and ``o+x`` for a dir) to an entry the gateway owns."""
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode) or st.st_uid != _app_uid():
            return
        bits = 0o055 if stat.S_ISDIR(st.st_mode) else 0o044
        if st.st_mode & bits == bits:
            return
        try:
            os.chmod(name, stat.S_IMODE(st.st_mode) | bits, dir_fd=dir_fd)
        except (OSError, NotImplementedError):
            return
        changed.append(label)

    widen(None, shared, shared)
    try:
        sfd = os.open(shared, _DIR)
    except OSError:
        return changed
    try:
        for name in (*SHARED_LINKS, "plugins"):
            widen(sfd, name, f"{shared}/{name}")
        try:
            pfd = os.open("plugins", _DIR_NOFOLLOW, dir_fd=sfd)
        except OSError:
            pfd = -1
        if pfd >= 0:
            try:
                for name in (*_SHARED_PLUGIN_FILES, "marketplaces"):
                    widen(pfd, name, f"{shared}/plugins/{name}")
            finally:
                os.close(pfd)
    finally:
        os.close(sfd)
    if changed:
        logger.info(
            "uid isolation: made shared entries readable by session uids: %s",
            ", ".join(changed),
        )
    return changed


def shared_tree_problems(uid: int) -> List[str]:
    """Why session *uid* cannot read the shared tree (empty when it can).

    Mode bits only — ``S`` carries no ACL, so they are what the kernel checks
    (with the session's gid equal to its uid and no supplementary groups).
    """
    shared = os.path.realpath(cp.config_dir())
    if not os.path.isdir(shared):
        return []
    from .landlock import _first_unsearchable, _dac_allows, _describe_mode

    blocked = _first_unsearchable(Path(shared), uid, uid)
    if blocked is not None:
        return [
            f"a session uid cannot reach the shared config dir {shared} "
            f"({blocked}): sessions would run without the shared settings, "
            "skills and plugins. Put CLAUDE_CONFIG_DIR outside /home/app on a "
            "path every uid can search."
        ]
    problems = []
    for rel in (*_SHARED_FILES, *(f"plugins/{n}" for n in _SHARED_PLUGIN_FILES)):
        path = Path(shared) / rel
        try:
            if path.is_file() and not _dac_allows(path, uid, uid, os.R_OK):
                problems.append(
                    f"a session uid cannot read the shared {path} "
                    f"({_describe_mode(path)})"
                )
        except OSError:
            continue
    return problems
