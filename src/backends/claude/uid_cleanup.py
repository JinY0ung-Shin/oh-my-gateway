"""Cleanup and transcript reads that need the session uid (uid isolation).

With per-uid config dirs (``uid_layout.enabled()``: ``CLAUDE_ISOLATION_MODE=uid``
plus ``CLAUDE_ISOLATION_CONFIG_BASE``) every session CLI runs as a session uid
``U`` with its own config dir ``P(U)``. The gateway's uid keeps access to what
``U`` creates with the default modes (the workspace ACL), but not to what ``U``
writes 0600/0700 — transcripts, ``mkstemp`` files, the CLI's temp root — so
those parts go through the broker, which acts as ``U`` (``broker_ops``;
``docs/isolation-uid-ownership-design.md`` §3.3, §3.6, §4.5):

* :func:`reclaim_temp_workspace` — an anonymous run's workspace, the writable
  dirs of ``P(U)``, ``U``'s ``/dev/shm`` objects and its SysV / POSIX IPC
  objects (:func:`ipc_leftovers`) are wiped before its ephemeral uid lease
  ends. The next anonymous run that leases ``U`` must find nothing of this
  one, so the lease is kept until the wipe is verified;
* :func:`remove_session_dirs`, :func:`remove_transcript` — the gateway removes
  what it can and hands what it could not to the broker;
* :func:`transcript_exists`, :func:`read_transcript_turns` — a transcript the
  gateway may not open is counted by the broker, which answers only the count;
* :func:`retention_sweep` — the ``cleanupPeriodDays`` expiry the CLI runs only
  for a uid whose CLI starts, run for every named user's ``P``.

In every other mode — and in uid mode without a config base — each helper does
exactly what the gateway did before (and never calls the broker).

Teardowns never wait for the broker on the event loop: their broker work is a
job on the :class:`Reclaimer` threads, retried with backoff. The transcript
reads are synchronous, bounded by the op timeout, and rare (the gateway reads a
transcript itself whenever it may); ``SessionManager.get_session_async`` runs
them off the event loop.
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

# Delays, in seconds, before a failed job runs again. One that still fails
# after the last is given up with a warning, unless its submitter says it is
# still needed (``submit(..., keep=...)``): then it is parked and tried again
# every PARKED_RETRY_SECONDS for as long as that holds.
BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
PARKED_RETRY_SECONDS = 300.0
# How many jobs run at once: one the broker parks (a live CLI of its uid) must
# not hold every other lease release back.
RECLAIMER_WORKERS = 4
# A synchronous wipe (the uid startup probe) retries this long at most.
_SYNC_BUDGET_SECONDS = 30.0
# How long the broker may park a workspace wipe while a CLI of the uid lives.
_WIPE_WAIT_SECONDS = 10
_OP_TIMEOUT_SECONDS = 75.0  # above the broker cleaner's 60 s alarm
_DEFAULT_RETENTION_DAYS = 30
_SHM_DIR = "/dev/shm"
# Where the kernel lists the SysV IPC objects and the POSIX message queues
# (module level, so tests can point them at fake tables).
_SYSVIPC_DIR = "/proc/sysvipc"
_MQUEUE_DIR = "/dev/mqueue"
# The tables of /proc/sysvipc and the name of each one's id column.
_SYSVIPC_TABLES = (("shm", "shmid"), ("sem", "semid"), ("msg", "msqid"))
# The ``perms`` bit of a shared memory segment IPC_RMID marked destroyed while
# a process still had it attached (it goes when the last one detaches).
_SHM_DEST = 0o1000
# What ``Path.is_file()`` reads as "no such file" rather than raising.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


# ---------------------------------------------------------------------------
# The reclaimer threads
# ---------------------------------------------------------------------------


class _Job:
    __slots__ = (
        "fn",
        "keep",
        "attempts",
        "error",
        "parked",
        "warned",
        "running",
        "seq",
    )

    def __init__(
        self, fn: Callable[[], bool], keep: Optional[Callable[[], bool]]
    ) -> None:
        self.fn = fn
        self.keep = keep
        self.attempts = 0
        self.error: Optional[str] = None
        self.parked = False  # past its backoff, retried every parked interval
        self.warned = False  # logged that it is parked
        self.running = False
        self.seq = -1  # its live entry in the due heap (older ones are stale)


class Reclaimer:
    """A small pool of daemon threads that runs cleanup jobs, retried with backoff.

    A job is ``(key, fn)``: ``fn()`` returns ``True`` when it is done, and
    ``False`` (or raises) when it has to run again, after the next delay of
    *backoff*. After the last delay it is given up with a warning, unless its
    *keep* callable (:meth:`submit`) still answers ``True``: then it is parked
    and tried again every *parked_retry* seconds for as long as that holds (a
    workspace whose root and uid lease are still there).

    A key is pending from :meth:`submit` until its job is done or dropped, and
    submitting a pending key again queues nothing (a parked job is tried at
    once instead), so a teardown that runs twice queues one job. A key never
    runs twice at once; up to *workers* jobs of different keys do, oldest due
    first. The threads start on the first submit.
    """

    def __init__(
        self,
        backoff: Sequence[float] = BACKOFF_SECONDS,
        *,
        workers: int = RECLAIMER_WORKERS,
        parked_retry: float = PARKED_RETRY_SECONDS,
        name: str = "uid-reclaimer",
    ) -> None:
        self._backoff = tuple(float(delay) for delay in backoff)
        self._workers = max(1, int(workers))
        self._parked_retry = float(parked_retry)
        self._name = name
        self._cond = threading.Condition()
        self._pending: Dict[Hashable, _Job] = {}
        self._due: List[Tuple[float, int, Hashable]] = []
        self._seq = itertools.count()
        self._threads: List[threading.Thread] = []
        self._stopped = False

    def submit(
        self,
        key: Hashable,
        fn: Callable[[], bool],
        *,
        keep: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Queue *fn* under *key*; ``False`` when *key* is pending already (a
        parked job then runs at once) or the reclaimer was stopped.

        *keep* is asked when *fn* still fails after the last backoff delay:
        ``True`` parks the job instead of giving it up.
        """
        with self._cond:
            if self._stopped:
                logger.warning("Dropped uid cleanup job %s: the reclaimer stopped", key)
                return False
            job = self._pending.get(key)
            if job is not None:
                if job.parked and not job.running:
                    # Asked for again: try it now, and :meth:`drain` waits for
                    # that try (a failure parks it again).
                    job.parked = False
                    self._schedule_locked(key, job, 0.0)
                return False
            job = _Job(fn, keep)
            self._pending[key] = job
            self._schedule_locked(key, job, 0.0)
            self._start_locked()
            return True

    def pending(self) -> int:
        """How many jobs are queued, waiting for a retry, running or parked."""
        with self._cond:
            return len(self._pending)

    def parked(self) -> int:
        """How many jobs are parked (see :meth:`submit`)."""
        with self._cond:
            return sum(1 for job in self._pending.values() if job.parked)

    def is_pending(self, key: Hashable) -> bool:
        with self._cond:
            return key in self._pending

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait until every job is done, given up or parked (``True``), at most
        *timeout* seconds.

        Waiting includes the backoff of jobs that are retried; a parked job
        waiting for its next try does not count. ``False`` once the time is up,
        or when the reclaimer stopped with such jobs left.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._busy_locked():
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
        """Stop the threads after the jobs they run; pending jobs are dropped."""
        with self._cond:
            self._stopped = True
            dropped = len(self._pending)
            self._cond.notify_all()
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        if dropped:
            logger.warning("uid reclaimer stopped with %d cleanup job(s) left", dropped)

    def _busy_locked(self) -> bool:
        return any(job.running or not job.parked for job in self._pending.values())

    def _start_locked(self) -> None:
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self._workers:
            thread = threading.Thread(
                target=self._run,
                name=f"{self._name}-{len(self._threads)}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def _schedule_locked(self, key: Hashable, job: _Job, delay: float) -> None:
        job.seq = next(self._seq)
        heapq.heappush(self._due, (time.monotonic() + delay, job.seq, key))
        self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                key = self._next_due_locked()
                if key is None:
                    return
                job = self._pending[key]
                job.running = True
            try:
                done = bool(job.fn())
                if not done:
                    job.error = "leftovers remained"
            except Exception as exc:  # a job's failure must not end the thread
                logger.debug("uid cleanup job %s failed", key, exc_info=True)
                job.error = f"{type(exc).__name__}: {exc}"
                done = False
            keep = False
            if not done and job.keep is not None and job.attempts >= len(self._backoff):
                try:
                    keep = bool(job.keep())
                except Exception:
                    logger.debug("uid cleanup job %s: keep failed", key, exc_info=True)
            with self._cond:
                job.running = False
                self._finish_locked(key, job, done, keep)
                self._cond.notify_all()

    def _next_due_locked(self) -> Optional[Hashable]:
        while not self._stopped:
            while self._due:
                _due, seq, key = self._due[0]
                job = self._pending.get(key)
                if job is not None and job.seq == seq and not job.running:
                    break
                heapq.heappop(self._due)  # stale: done, dropped or rescheduled
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

    def _finish_locked(self, key: Hashable, job: _Job, done: bool, keep: bool) -> None:
        if done:
            del self._pending[key]
            if job.warned:
                logger.info(
                    "uid cleanup job %s finished after %d attempts",
                    key,
                    job.attempts + 1,
                )
            return
        if job.attempts < len(self._backoff):
            delay = self._backoff[job.attempts]
            job.attempts += 1
            self._schedule_locked(key, job, delay)
            return
        job.attempts += 1
        if keep:
            if not job.warned:
                job.warned = True
                logger.warning(
                    "uid cleanup job %s still fails after %d attempts (%s); "
                    "retrying every %.0f s",
                    key,
                    job.attempts,
                    job.error or "it kept failing",
                    self._parked_retry,
                )
            job.parked = True
            self._schedule_locked(key, job, self._parked_retry)
            return
        del self._pending[key]
        logger.warning(
            "Gave up on uid cleanup job %s after %d attempts (%s)",
            key,
            job.attempts,
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
    ``/dev/shm`` and IPC objects, removed by the broker as ``uid``, then the
    rest by the gateway — ends with the lease's release, and only once it is
    verified; a failed one keeps the lease and is retried (:class:`Reclaimer`),
    and once past its backoff it is parked and tried again every
    :data:`PARKED_RETRY_SECONDS` while the workspace (and its lease) is there.
    *sync* runs it here, retrying for at most ~30 s: only the uid startup
    probe, which has to know before it reports. Never raises.
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
            leased = ui.leased_uid(key) is not None
            reclaimer().submit(
                ("ws", key),
                lambda: _wipe_workspace(key, uid),
                keep=lambda: _wipe_still_needed(key, uid, leased),
            )
    except Exception:
        logger.warning(
            "Could not clean up temporary workspace %s", workspace, exc_info=True
        )


def _wipe_still_needed(key: str, uid: int, leased: bool) -> bool:
    """Whether a wipe that keeps failing is worth parking: its workspace is
    still there and, for a leased run, so is the lease (a released or
    reclaimed one is the pool's business: ``uid_layout.ensure_clean_ephemeral``
    wipes a reused uid before its next run)."""
    from . import uid_isolation as ui

    if not os.path.lexists(key):
        return False
    return ui.leased_uid(key) == uid if leased else True


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
    """What the uid still has in its per-uid dirs, ``/dev/shm`` and IPC."""
    left: List[str] = ipc_leftovers(uid)
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


def ipc_leftovers(uid: int) -> List[str]:
    """The SysV IPC objects and POSIX message queues a run on *uid* left
    behind, described.

    A run's SysV shared memory segments, semaphore sets and message queues
    outlive it (``/proc/sysvipc/{shm,sem,msg}``: the objects whose ``cuid`` is
    *uid*, :func:`_sysvipc_objects`), and so do its POSIX message queues (the
    ``/dev/mqueue`` entries it owns, where that is mounted); the broker's shm
    step removes them as the uid. Each table is read by its header line. A
    table that cannot be read or parsed counts as a leftover (fail closed); a
    missing one (no such kernel support, no such mount) as none. Never raises.
    """
    left: List[str] = []
    for name, id_column in _SYSVIPC_TABLES:
        path = os.path.join(_SYSVIPC_DIR, name)
        try:
            with open(path, encoding="ascii", errors="replace") as table:
                left.extend(_sysvipc_objects(table, name, id_column, uid, path))
        except FileNotFoundError:
            continue
        except OSError:
            left.append(path)
    try:
        with os.scandir(_MQUEUE_DIR) as entries:
            for entry in entries:
                try:
                    if entry.stat(follow_symlinks=False).st_uid == uid:
                        left.append(entry.path)
                except FileNotFoundError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        left.append(_MQUEUE_DIR)
    return left


def _sysvipc_objects(
    table, name: str, id_column: str, uid: int, path: str
) -> List[str]:
    """The objects of one ``/proc/sysvipc`` *table* a run on *uid* left
    behind, read by the column names of its header line: the ones *uid*
    created, whoever owns them now (``IPC_SET`` can hand an object over; its
    creator keeps the right to remove it).

    Not counted: an object another uid created and handed to *uid* — that uid
    can hand one over at any time, the next run's included, so it is no
    previous run's state, and counting it would let any session hold *uid*'s
    lease for good (the broker's sweep still removes it, as its owner) — and a
    segment already marked destroyed (``SHM_DEST``): IPC_RMID took its key, and
    it lives on only while another uid's process keeps it attached."""
    header = table.readline().split()
    try:
        columns = [header.index(c) for c in (id_column, "cuid", "perms")]
    except ValueError:
        return [f"{path} (unrecognized header)"]
    found: List[str] = []
    for line in table:
        fields = line.split()
        if not fields:
            continue
        try:
            ident, creator = (int(fields[i]) for i in columns[:2])
            perms = int(fields[columns[2]], 8)
        except (IndexError, ValueError):
            return found + [f"{path} (unparsable line)"]
        if creator != uid or (name == "shm" and perms & _SHM_DEST):
            continue
        found.append(f"SysV {name} {ident}")
    return found


# ---------------------------------------------------------------------------
# Per-session dirs and transcripts
# ---------------------------------------------------------------------------


def _present(path) -> bool:
    """Whether *path* may still exist: like ``os.path.lexists``, but a path the
    gateway cannot look at (a session uid's 0700 dir above it) counts as there
    — only the broker, as that uid, can tell."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


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
            if layout.per_uid and _present(path):
                remains.append(str(path))
    uid = layout.uid
    if remains and uid is not None:
        reclaimer().submit(
            ("dirs", uid, session_id), lambda: _remove_as_uid(uid, remains, guard)
        )


def _remove_as_uid(
    uid: int, paths: Sequence[str], guard: Optional[Callable[[], bool]] = None
) -> bool:
    """Have the broker remove *paths* as *uid*; ``True`` once none is left.

    The broker's success is taken as is: under a session uid's 0700 dir the
    gateway cannot check for itself (:func:`_present`).
    """
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
    left = [p for p in paths if _present(p)]
    if not left:
        return True
    done = broker_ops.cleanup(uid, remove=[_broker_path(p) for p in left])
    for path in left:
        # An entry the uid emptied but could not unlink from a parent only the
        # gateway may write.
        try:
            os.rmdir(path)
        except OSError:
            pass
    return done or not any(_present(p) for p in left)


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
    left = [str(p) for p in (transcript, sidecar) if _present(p)]
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
    """The broker's count for a transcript the gateway may not open.

    ``TranscriptUnreadable`` when there is no broker to ask, the broker refused
    or could not read it either (``TRANSCRIPT_UNREADABLE``: it is there, so
    not a "not found"); ``TranscriptTemporarilyUnavailable`` when the broker
    could not be reached or did not answer in time (a retry may succeed).
    """
    from src.session_manager import TranscriptTemporarilyUnavailable
    from src.session_manager import TranscriptUnreadable

    from . import broker_ops
    from . import uid_layout
    from .transcript_turns import TRANSCRIPT_UNREADABLE

    uid = uid_layout.session_layout(user, workspace).uid
    if uid is None or not broker_ops.available():
        raise TranscriptUnreadable(path) from cause
    try:
        turns = broker_ops.read_transcript(uid, _broker_path(path), workspace)
    except broker_ops.BrokerOpError as exc:
        if exc.transient:
            raise TranscriptTemporarilyUnavailable(path) from exc
        raise TranscriptUnreadable(path) from exc
    if turns == TRANSCRIPT_UNREADABLE:
        raise TranscriptUnreadable(path) from RuntimeError(
            "the broker could not read it as the session uid either"
        )
    return turns


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


def _open_transcript(path: Path):
    """*path* opened for reading as UTF-8 text with universal newlines, and its
    ``fstat``. ``O_NONBLOCK``: a FIFO planted in a transcript's place opens at
    once (and is then refused as not a regular file) instead of hanging the
    reader. Raises ``OSError``."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        return os.fdopen(fd, "r", encoding="utf-8", newline=None), st
    except BaseException:
        os.close(fd)
        raise


def read_transcript_turns(session_id: str, *, user, workspace) -> int:
    """``transcript_turns.count_transcript_turns`` of this session's transcript.

    The gateway reads it itself when it may; else (EACCES, a session uid's
    0600 transcript) the broker counts it as the session uid. Returns a count
    or a negative ``TRANSCRIPT_*`` code. Raises
    ``session_manager.TranscriptUnreadable`` when neither may read it, or when
    it is larger than ``transcript_turns.MAX_TRANSCRIPT_BYTES`` (the session
    uid may write its own transcript: it is never read into memory unbounded,
    and no line past ``MAX_LINE_CHARS`` either — that one counts as corrupt);
    ``session_manager.TranscriptTemporarilyUnavailable`` when the broker did
    not answer.
    """
    from src.session_manager import TranscriptUnreadable

    from . import transcript_turns as tt

    path = _transcript_path(session_id, user, workspace)
    if path is None:
        return tt.TRANSCRIPT_MISSING
    try:
        opened, st = _open_transcript(path)
    except (FileNotFoundError, NotADirectoryError):
        return tt.TRANSCRIPT_MISSING
    except PermissionError as exc:
        return _broker_turns(path, user, workspace, exc)
    except OSError:
        return tt.TRANSCRIPT_UNREADABLE
    with opened as lines:
        if not stat.S_ISREG(st.st_mode):
            return tt.TRANSCRIPT_MISSING
        if st.st_size > tt.MAX_TRANSCRIPT_BYTES:
            raise TranscriptUnreadable(path) from RuntimeError(
                f"it is {st.st_size} bytes, more than the "
                f"{tt.MAX_TRANSCRIPT_BYTES} a transcript is read up to"
            )
        try:
            return tt.count_transcript_turns(
                tt.bounded_lines(lines, tt.MAX_LINE_CHARS), str(workspace)
            )
        except UnicodeDecodeError:
            return tt.TRANSCRIPT_CORRUPT
        except OSError:
            return tt.TRANSCRIPT_UNREADABLE


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
    mtime is older than the period. Per-uid layouts only, and only with the
    ``user`` setting source (``CLAUDE_SETTING_SOURCES``), the one the CLI's
    own cleanup needs; 0 otherwise.
    Returns how many uids were swept; logs the failures. Blocks for one broker
    round trip per uid: run it off the event loop.
    """
    from src import claude_config_paths as cp

    from . import broker_ops
    from . import uid_isolation as ui
    from . import uid_layout

    if not uid_layout.enabled():
        return 0
    from .client import _get_setting_sources

    if "user" not in _get_setting_sources():
        # The CLI loads cleanupPeriodDays (and runs its own cleanup) only with
        # the user setting source; without it nothing ever expired these, so
        # neither does the gateway (it would start deleting transcripts the
        # shared config dir kept, migrated old ones first).
        logger.debug("uid retention sweep skipped: no user setting source")
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
