"""Cleanup and transcript reads that need the session uid (uid isolation).

With per-uid config dirs (``uid_layout.enabled()``: ``CLAUDE_ISOLATION_MODE=uid``
plus ``CLAUDE_ISOLATION_CONFIG_BASE``) every session CLI runs as a session uid
``U`` with its own config dir ``P(U)``. The gateway's uid keeps access to what
``U`` creates with the default modes (the workspace ACL), but not to what ``U``
writes 0600/0700 — transcripts, ``mkstemp`` files, the CLI's temp root — so
those parts go through the broker, which acts as ``U`` (``broker_ops``;
``docs/isolation-uid-ownership-design.md`` §3.3, §3.6, §4.5):

* :func:`reclaim_temp_workspace` — an anonymous run's workspace, the writable
  dirs of ``P(U)`` and ``U``'s ``/dev/shm`` objects are wiped before its
  ephemeral uid lease ends. The next anonymous run that leases ``U`` must find
  nothing of this one, so the lease is kept until the wipe is verified;
* :func:`remove_session_dirs`, :func:`remove_transcript` — the gateway removes
  what it can and hands what it could not to the broker;
* :func:`transcript_exists`, :func:`read_transcript_turns` — a transcript the
  gateway may not open is counted by the broker, which answers only the count;
* :func:`retention_sweep` — the ``cleanupPeriodDays`` expiry the CLI runs only
  for a uid whose CLI starts, run for every named user's ``P``.

In every other mode — and in uid mode without a config base — each helper does
exactly what the gateway did before (and never calls the broker).

Teardowns never wait for the broker on the event loop: their broker work is a
job on the :class:`Reclaimer` thread, retried with backoff. The transcript reads
are synchronous, bounded by the op timeout, and rare: the gateway reads a
transcript itself whenever it may.
"""

from __future__ import annotations

import errno
import heapq
import itertools
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Hashable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# A session id becomes one path component: the rule landlock._SESSION_ID_RE
# applies when it creates these dirs (tests/test_uid_cleanup.py pins the two
# equal).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Delays, in seconds, before a failed job runs again; one that still fails
# after the last is given up with a warning.
BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
# A synchronous wipe (the uid startup probe, verify()) retries this long at most.
_SYNC_BUDGET_SECONDS = 30.0
# How long the broker may park a workspace wipe while a CLI of the uid lives.
_WIPE_WAIT_SECONDS = 10
_OP_TIMEOUT_SECONDS = 60.0
_DEFAULT_RETENTION_DAYS = 30
_SHM_DIR = "/dev/shm"
# What ``Path.is_file()`` reads as "no such file" rather than raising.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


# ---------------------------------------------------------------------------
# The reclaimer thread
# ---------------------------------------------------------------------------


class _Job:
    __slots__ = ("fn", "attempts", "error")

    def __init__(self, fn: Callable[[], bool]) -> None:
        self.fn = fn
        self.attempts = 0
        self.error: Optional[str] = None


class Reclaimer:
    """One daemon thread that runs cleanup jobs, retried with backoff.

    A job is ``(key, fn)``: ``fn()`` returns ``True`` when it is done, and
    ``False`` (or raises) when it has to run again, after the next delay of
    *backoff*; after the last delay it is given up with a warning. A key is
    pending from :meth:`submit` until its job is done or given up, and
    submitting a pending key again is a no-op, so a teardown that runs twice
    queues one job. Jobs run one at a time, oldest due first; the thread starts
    on the first submit.
    """

    def __init__(
        self, backoff: Sequence[float] = BACKOFF_SECONDS, *, name: str = "uid-reclaimer"
    ) -> None:
        self._backoff = tuple(float(delay) for delay in backoff)
        self._name = name
        self._cond = threading.Condition()
        self._pending: Dict[Hashable, _Job] = {}
        self._due: List[Tuple[float, int, Hashable]] = []
        self._seq = itertools.count()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    def submit(self, key: Hashable, fn: Callable[[], bool]) -> bool:
        """Queue *fn* under *key*; ``False`` when *key* is pending already (or
        the reclaimer was stopped)."""
        with self._cond:
            if self._stopped:
                logger.warning("Dropped uid cleanup job %s: the reclaimer stopped", key)
                return False
            if key in self._pending:
                return False
            self._pending[key] = _Job(fn)
            heapq.heappush(self._due, (time.monotonic(), next(self._seq), key))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name=self._name, daemon=True
                )
                self._thread.start()
            self._cond.notify_all()
            return True

    def pending(self) -> int:
        """How many jobs are queued, waiting for a retry, or running."""
        with self._cond:
            return len(self._pending)

    def is_pending(self, key: Hashable) -> bool:
        with self._cond:
            return key in self._pending

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait until no job is pending (``True``), at most *timeout* seconds.

        Waiting includes the backoff of jobs that are retried. ``False`` once
        the time is up, or when the reclaimer stopped with jobs pending.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._pending:
                if self._stopped:
                    return False
                if deadline is None:
                    self._cond.wait()
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread after the job it runs; pending jobs are dropped."""
        with self._cond:
            self._stopped = True
            dropped = len(self._pending)
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if dropped:
            logger.warning("uid reclaimer stopped with %d cleanup job(s) left", dropped)

    def _run(self) -> None:
        while True:
            with self._cond:
                key = self._next_due_locked()
                if key is None:
                    return
                job = self._pending[key]
            try:
                done = bool(job.fn())
                if not done:
                    job.error = "leftovers remained"
            except Exception as exc:  # a job's failure must not end the thread
                logger.debug("uid cleanup job %s failed", key, exc_info=True)
                job.error = f"{type(exc).__name__}: {exc}"
                done = False
            with self._cond:
                self._finish_locked(key, job, done)
                self._cond.notify_all()

    def _next_due_locked(self) -> Optional[Hashable]:
        while not self._stopped:
            if not self._due:
                self._cond.wait()
                continue
            due, _seq, key = self._due[0]
            left = due - time.monotonic()
            if left > 0:
                self._cond.wait(left)
                continue
            heapq.heappop(self._due)
            return key
        return None

    def _finish_locked(self, key: Hashable, job: _Job, done: bool) -> None:
        if done:
            del self._pending[key]
            return
        if job.attempts < len(self._backoff):
            delay = self._backoff[job.attempts]
            job.attempts += 1
            heapq.heappush(self._due, (time.monotonic() + delay, next(self._seq), key))
            return
        del self._pending[key]
        logger.warning(
            "Gave up on uid cleanup job %s after %d attempts (%s)",
            key,
            job.attempts + 1,
            job.error or "it kept failing",
        )


_reclaimer: Optional[Reclaimer] = None
_reclaimer_lock = threading.Lock()


def reclaimer() -> Reclaimer:
    """The process's reclaimer (created on first use; its thread on first job)."""
    global _reclaimer
    with _reclaimer_lock:
        if _reclaimer is None:
            _reclaimer = Reclaimer()
        return _reclaimer


def pending() -> int:
    """Jobs the process's reclaimer still holds; 0 when it was never used."""
    current = _reclaimer
    return 0 if current is None else current.pending()


def drain(timeout: Optional[float] = None) -> bool:
    """:meth:`Reclaimer.drain` of the process's reclaimer, if it has one."""
    current = _reclaimer
    return True if current is None else current.drain(timeout)


def _broker_path(path) -> str:
    """*path* as a broker op names it: absolute, with its parent resolved.

    The broker reaches every target from ``/`` without following a symlink, so
    a target under a base reached through a link (an operator's symlinked
    workspace base or config base) would fail on every attempt. The last
    component stays as it is: a link planted in its place is still refused by
    the broker rather than followed here.
    """
    head, tail = os.path.split(os.path.abspath(os.fspath(path)))
    return os.path.join(os.path.realpath(head), tail)


def _run_inline(key: Hashable, fn: Callable[[], bool], deadline: float) -> bool:
    """Run *fn* here with the reclaimer's retries, until *deadline*."""
    backoff = BACKOFF_SECONDS
    error = "it kept failing"
    for attempt in range(len(backoff) + 1):
        try:
            if fn():
                return True
            error = "leftovers remained"
        except Exception as exc:
            logger.debug("uid cleanup job %s failed", key, exc_info=True)
            error = f"{type(exc).__name__}: {exc}"
        if attempt == len(backoff):
            break
        delay = backoff[attempt]
        if time.monotonic() + delay >= deadline:
            break
        time.sleep(delay)
    logger.warning("Could not finish uid cleanup job %s (%s)", key, error)
    return False


# ---------------------------------------------------------------------------
# Anonymous workspaces and their uid lease
# ---------------------------------------------------------------------------


def reclaim_temp_workspace(workspace, *, sync: bool = False) -> None:
    """The per-uid branch of ``WorkspaceManager.cleanup_temp_workspace``.

    The uid to wipe as is the ephemeral lease keyed on the absolute workspace
    path. Without one (a workspace a previous gateway process left: its leases
    died with it) it is the single ephemeral uid the workspace root's ACL names
    that nothing holds now — no lease, no registry entry. Without either the
    gateway removes what its own uid can, as in the other modes.

    The wipe — workspace, the writable dirs of ``P(uid)`` and ``uid``'s
    ``/dev/shm`` objects, removed by the broker as ``uid``, then the rest by
    the gateway — ends with the lease's release, and only once it is verified;
    a failed one keeps the lease and is retried (:class:`Reclaimer`). *sync*
    runs it here, retrying for at most ~30 s (the uid startup probe and
    ``verify()``, which own their workspace). Never raises.
    """
    try:
        from . import uid_isolation as ui

        key = os.path.abspath(os.fspath(workspace))
        uid = ui.leased_uid(key)
        if uid is None and os.path.lexists(key):
            uid = _orphan_uid(key)
        if uid is None:
            from src.workspace_manager import remove_temp_workspace

            remove_temp_workspace(Path(workspace))
            return
        if sync:
            deadline = time.monotonic() + _SYNC_BUDGET_SECONDS
            _run_inline(
                ("ws", key),
                lambda: _wipe_workspace(key, uid, deadline=deadline),
                deadline,
            )
        else:
            reclaimer().submit(("ws", key), lambda: _wipe_workspace(key, uid))
    except Exception:
        logger.warning(
            "Could not clean up temporary workspace %s", workspace, exc_info=True
        )


def _acl_named_users(path: str) -> List[int]:
    """The named-user entries of the access ACL of the directory *path*."""
    from . import posix_acl

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return [int(uid) for uid in posix_acl.named_users(fd)]
    finally:
        os.close(fd)


def _orphan_uid(key: str) -> Optional[int]:
    """The ephemeral uid an unleased workspace was granted to, if exactly one."""
    from . import uid_isolation as ui

    try:
        named = _acl_named_users(key)
    except Exception:
        logger.debug("Could not read the ACL of %s", key, exc_info=True)
        return None
    candidates = sorted(
        {
            uid
            for uid in named
            if ui.in_ephemeral_band(uid)
            and ui.lease_holder(uid) is None
            and not ui.registry_holds(uid)
        }
    )
    if len(candidates) > 1:
        logger.warning(
            "Temporary workspace %s is granted to several ephemeral uids %s; "
            "the gateway removes only what its own uid can",
            key,
            candidates,
        )
    return candidates[0] if len(candidates) == 1 else None


def _wipe_workspace(key: str, uid: int, *, deadline: Optional[float] = None) -> bool:
    """One attempt at wiping an anonymous run's state as *uid*; ``True`` = done.

    While *uid* is this run's (leased on *key*, or on nothing any more), the
    broker empties the workspace and the writable dirs of ``P(uid)`` and
    removes ``uid``'s ``/dev/shm`` entries, parked while a CLI of the uid still
    runs. Leased to another run since (the uid of a workspace a previous
    process left, handed out again), ``P(uid)`` and ``/dev/shm`` are that run's:
    only the workspace is emptied. The gateway then removes the root and
    whatever else is its own. The lease ends only when the root is gone and,
    if the broker reported a failed step, ``P(uid)`` and ``/dev/shm`` hold
    nothing of the uid's any more.
    """
    from . import broker_ops
    from . import uid_isolation as ui
    from . import uid_layout

    holder = ui.lease_holder(uid)
    own = holder is None or holder == key
    timeout = _OP_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = max(1.0, deadline - time.monotonic() - _WIPE_WAIT_SECONDS)
    exists = os.path.lexists(key)
    workspace = _broker_path(key)
    if own:
        targets = [
            t for t in uid_layout.ephemeral_clear_targets(uid) if os.path.lexists(t)
        ]
        ok = broker_ops.cleanup(
            uid,
            workspace=workspace,
            wait=_WIPE_WAIT_SECONDS,
            clear=([workspace] if exists else []) + [_broker_path(t) for t in targets],
            shm=True,
            timeout=timeout,
        )
        if not ok:
            leftovers = _leftovers(uid, targets)
            if leftovers:
                # The root stays meanwhile: a lease whose workspace is gone
                # may be reclaimed by the pool, leftovers and all.
                logger.debug("Wipe of uid %d left %s", uid, leftovers)
                return False
    elif exists:
        broker_ops.cleanup(uid, clear=[workspace], timeout=timeout)
    shutil.rmtree(key, ignore_errors=True)
    if os.path.lexists(key):
        return False
    ui.release_ephemeral(key)  # a no-op without a lease
    logger.info("Cleaned up temporary workspace: %s", key)
    return True


def _leftovers(uid: int, targets: Sequence[str]) -> List[str]:
    """What the uid still has in its per-uid dirs and ``/dev/shm``."""
    left: List[str] = []
    for target in targets:
        try:
            with os.scandir(target) as entries:
                if next(entries, None) is not None:
                    left.append(target)
        except FileNotFoundError:
            continue
        except OSError:
            left.append(target)
    try:
        with os.scandir(_SHM_DIR) as entries:
            for entry in entries:
                try:
                    if entry.stat(follow_symlinks=False).st_uid == uid:
                        left.append(entry.path)
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    except OSError:
        left.append(_SHM_DIR)
    return left


# ---------------------------------------------------------------------------
# Per-session dirs and transcripts
# ---------------------------------------------------------------------------


def _config_dir(layout) -> Optional[Path]:
    """The resolved config dir the session dirs of *layout* live in."""
    if not layout.per_uid:
        # The shared dir keeps its strict rule ($HOME unset and unoverridden:
        # none), as the gateway resolved it before per-uid layouts.
        from . import workspace_sandbox as ws

        return ws._claude_home()
    try:
        return layout.config_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def remove_session_dirs(
    session_id: Optional[str],
    *,
    user: Optional[str] = None,
    workspace=None,
    guard: Optional[Callable[[], bool]] = None,
) -> None:
    """Remove a finished session's ``session-env/<id>`` (and ``tasks/<id>``).

    ``landlock.remove_session_dirs`` as it was, in the session's own config
    dir: ``session-env/<id>`` always and, under confinement, ``tasks/<id>``
    too; the id validated as at creation; nothing removed through a symlink in
    its place or above it, and links inside removed, not followed. Called once
    the CLI is dead and the session is finished (see
    ``SessionManager._release_session_dirs``).

    With per-uid layouts a named session's dirs are in ``P(uid)``; what the
    gateway's uid could not remove there goes to the broker on the reclaimer
    thread. *guard*, when given, is asked before each of those attempts and
    stops them once it answers ``False`` (a live session uses the id again).
    An anonymous session's dirs go with its lease (:func:`reclaim_temp_workspace`).
    Never raises.
    """
    if not session_id or not _SESSION_ID_RE.match(session_id):
        return
    try:
        _remove_session_dirs(session_id, user, workspace, guard)
    except Exception:
        logger.debug(
            "Could not remove the dirs of session %s", session_id, exc_info=True
        )


def _remove_session_dirs(session_id, user, workspace, guard) -> None:
    from . import landlock
    from . import uid_layout

    if uid_layout.enabled() and not user:
        return
    layout = uid_layout.session_layout(user, workspace)
    config = _config_dir(layout)
    if config is None:
        return
    kinds = (
        landlock._SESSION_DIR_KINDS if landlock.confine_enabled() else ("session-env",)
    )
    remains: List[str] = []
    for kind in kinds:
        path = config / kind / session_id
        try:
            if os.path.realpath(path) != str(path) or not path.is_dir():
                continue
            shutil.rmtree(path)
        except OSError:
            logger.debug("Could not remove %s", path, exc_info=True)
            if layout.per_uid and os.path.lexists(path):
                remains.append(str(path))
    uid = layout.uid
    if remains and uid is not None:
        reclaimer().submit(
            ("dirs", uid, session_id), lambda: _remove_as_uid(uid, remains, guard)
        )


def _remove_as_uid(
    uid: int, paths: Sequence[str], guard: Optional[Callable[[], bool]] = None
) -> bool:
    """Have the broker remove *paths* as *uid*; ``True`` once none is left."""
    from . import broker_ops

    if guard is not None:
        try:
            free = bool(guard())
        except Exception:
            logger.debug("Session dir guard failed", exc_info=True)
            free = False
        if not free:
            logger.info("Not removing %s: a live session uses them again", list(paths))
            return True
    left = [p for p in paths if os.path.lexists(p)]
    if not left:
        return True
    broker_ops.cleanup(uid, remove=[_broker_path(p) for p in left])
    for path in left:
        # An entry the uid emptied but could not unlink from a parent only the
        # gateway may write.
        try:
            os.rmdir(path)
        except OSError:
            pass
    return not any(os.path.lexists(p) for p in left)


def _transcript_path(session_id: str, user, workspace) -> Optional[Path]:
    """``session_manager._session_jsonl_path`` (it honors the test seam)."""
    from src import session_manager

    return session_manager._session_jsonl_path(session_id, workspace, user=user)


def remove_transcript(
    session_id: Optional[str],
    *,
    user: Optional[str] = None,
    workspace=None,
    path=None,
) -> None:
    """Remove a stateless run's transcript, its ``<id>/`` sidecar dir and the
    project dir they leave empty.

    *path* is the transcript when the caller computed it already (the
    stateless routes, through their own seam); else it is derived from
    (*user*, *workspace*). A permission error is a warning: the stateless
    contract promises the transcript is gone. With per-uid layouts an
    anonymous run's transcript goes with its lease (:func:`reclaim_temp_workspace`)
    and what the gateway could not remove of a named session's goes to the
    broker on the reclaimer thread. Never raises.
    """
    if not session_id or (path is None and not workspace):
        return
    try:
        _remove_transcript(session_id, user, workspace, path)
    except Exception:
        logger.debug("Stateless agent transcript cleanup failed", exc_info=True)


def _remove_transcript(session_id, user, workspace, path) -> None:
    from . import uid_layout

    enabled = uid_layout.enabled()
    if enabled and not user:
        return
    transcript = (
        Path(path)
        if path is not None
        else _transcript_path(session_id, user, workspace)
    )
    if transcript is None:
        return
    uid = uid_layout.session_layout(user, workspace).uid if enabled else None
    per_uid = uid is not None
    sidecar = transcript.parent / session_id
    try:
        transcript.unlink(missing_ok=True)
        shutil.rmtree(sidecar, ignore_errors=True)
        # Anonymous workspaces get unique encoded project directories. Remove
        # the now-empty directory, but never disturb unexpected sibling files.
        try:
            transcript.parent.rmdir()
        except OSError:
            pass
    except PermissionError as exc:
        if not per_uid:
            # Not silent: the stateless contract promises the transcript is gone.
            logger.warning(
                "Stateless agent transcript %s could not be removed: %s. Under "
                "CLAUDE_ISOLATION_MODE=uid the session uid owns it; the broker "
                "removes it as that uid only with per-uid config dirs "
                "(CLAUDE_ISOLATION_CONFIG_BASE).",
                transcript,
                exc.strerror or exc,
            )
            return
    except OSError:
        logger.debug("Stateless agent transcript cleanup failed", exc_info=True)
        return
    if uid is None:
        return
    left = [str(p) for p in (transcript, sidecar) if os.path.lexists(p)]
    if left:
        project = transcript.parent
        reclaimer().submit(
            ("transcript", uid, session_id),
            lambda: _remove_transcript_as_uid(uid, left, project),
        )


def _remove_transcript_as_uid(uid: int, paths: Sequence[str], project: Path) -> bool:
    done = _remove_as_uid(uid, paths)
    try:
        project.rmdir()
    except OSError:
        pass
    return done


def _broker_turns(path: Path, user, workspace, cause: BaseException) -> int:
    """The broker's count for a transcript the gateway may not open."""
    from src.session_manager import TranscriptUnreadable

    from . import broker_ops
    from . import uid_layout

    uid = uid_layout.session_layout(user, workspace).uid
    if uid is None or not broker_ops.available():
        raise TranscriptUnreadable(path) from cause
    try:
        return broker_ops.read_transcript(uid, _broker_path(path), workspace)
    except broker_ops.BrokerOpError as exc:
        raise TranscriptUnreadable(path) from exc


def transcript_exists(session_id: str, *, user, workspace) -> bool:
    """Whether the CLI wrote a transcript for this session (resume or start).

    Raises ``session_manager.TranscriptUnreadable`` when the gateway may not
    even look and the broker cannot answer for it.
    """
    from .transcript_turns import TRANSCRIPT_MISSING

    path = _transcript_path(session_id, user, workspace)
    if path is None:
        return False
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except PermissionError as exc:
        return _broker_turns(path, user, workspace, exc) != TRANSCRIPT_MISSING
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return False
        raise


def read_transcript_turns(session_id: str, *, user, workspace) -> int:
    """``transcript_turns.count_transcript_turns`` of this session's transcript.

    The gateway reads it itself when it may; else (EACCES, a session uid's
    0600 transcript) the broker counts it as the session uid. Returns a count
    or a negative ``TRANSCRIPT_*`` code; raises
    ``session_manager.TranscriptUnreadable`` when neither may read it.
    """
    from .transcript_turns import (
        TRANSCRIPT_CORRUPT,
        TRANSCRIPT_MISSING,
        TRANSCRIPT_UNREADABLE,
        count_transcript_turns,
    )

    path = _transcript_path(session_id, user, workspace)
    if path is None:
        return TRANSCRIPT_MISSING
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return TRANSCRIPT_MISSING
        with path.open("r", encoding="utf-8", newline=None) as lines:
            try:
                return count_transcript_turns(lines, str(workspace))
            except UnicodeDecodeError:
                return TRANSCRIPT_CORRUPT
    except (FileNotFoundError, NotADirectoryError):
        return TRANSCRIPT_MISSING
    except PermissionError as exc:
        return _broker_turns(path, user, workspace, exc)
    except OSError:
        return TRANSCRIPT_UNREADABLE


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def _retention_days() -> int:
    """``cleanupPeriodDays`` of the shared ``settings.json`` (an int >= 1), else 30."""
    from src import claude_config_paths as cp

    try:
        with open(cp.config_dir() / "settings.json", encoding="utf-8") as f:
            days = json.load(f).get("cleanupPeriodDays")
    except (OSError, ValueError, AttributeError):
        return _DEFAULT_RETENTION_DAYS
    if type(days) is int and days >= 1:
        return days
    return _DEFAULT_RETENTION_DAYS


def retention_sweep() -> int:
    """Expire every named user's old per-uid state, as that user's uid.

    The CLI's own ``cleanupPeriodDays`` sweep runs only when a CLI of the uid
    starts, so a user who never comes back would keep their transcripts,
    ``session-env`` and ``tasks`` forever. For each named uid with a config
    dir, the broker removes the entries of ``session-env`` and ``tasks`` and
    the per-project entries of ``projects`` (never ``memory``) whose newest
    mtime is older than the period. Per-uid layouts only (0 otherwise).
    Returns how many uids were swept; logs the failures. Blocks for one broker
    round trip per uid: run it off the event loop.
    """
    from src import claude_config_paths as cp

    from . import broker_ops
    from . import uid_isolation as ui
    from . import uid_layout

    if not uid_layout.enabled():
        return 0
    older_than = _retention_days() * 86400
    swept = 0
    for uid in ui.named_uids():
        config = cp.user_config_dir(uid)
        if config is None or not config.is_dir():
            continue
        units = [
            _broker_path(config / k)
            for k in ("session-env", "tasks")
            if (config / k).is_dir()
        ]
        projects = config / "projects"
        nested = [_broker_path(projects)] if projects.is_dir() else []
        if not units and not nested:
            continue
        expire = {"older_than": older_than, "units": units, "nested": nested}
        try:
            if broker_ops.cleanup(uid, expire=expire):
                swept += 1
            else:
                logger.warning(
                    "Retention sweep of uid %d left expired entries (the broker "
                    "logs which)",
                    uid,
                )
        except Exception as exc:
            logger.warning("Retention sweep of uid %d failed: %s", uid, exc)
    return swept
