"""Gateway side of uid-per-user isolation (issue #241, PR-C).

This is the ``uid`` mode of ``CLAUDE_ISOLATION_MODE`` (see ``landlock.py`` for
the mode knob and the Landlock ruleset the broker applies after the switch).
Where ``landlock`` mode has the launcher confine itself in place, ``uid`` mode
adds a privileged **broker** that forks each CLI as a per-user uid before the
Landlock step, so a session also gets DAC and signal isolation from other users.

The gateway body holds **no privilege**. This module is the unprivileged part:

* **uid mapping** — a user key (the same resolved key ``WorkspaceManager`` uses)
  maps to a stable numeric uid in ``[CLAUDE_ISOLATION_UID_MIN, _MAX]`` via a
  persisted registry, so the same user resumes onto the same uid and files stay
  owned consistently. Allocation refuses when the range is exhausted (never
  reuses a live mapping, never collides two keys onto one uid).
* **spawn ticket** — per spawn the gateway mints a single-use, HMAC-signed
  ticket ``{uid, workspace, ro, rw, env allowlist, nonce, exp}`` and puts it in
  ``options.env``; the launcher stub presents it to the broker over the spawn
  socket (``docker/landlock_launcher.py`` uid branch), and the broker verifies
  the signature, the nonce (single-use) and the expiry before switching uid.
  The shared secret (``GATEWAY_ISOLATION_TICKET_SECRET``) is generated once by
  the entrypoint, which forks the broker with it and hands the gateway its copy
  through an inherited pipe (``GATEWAY_ISOLATION_TICKET_SECRET_FD``) instead of
  the environment the image CMD's earlier steps inherit. The gateway reads and
  closes the pipe at startup, or moves an operator-set variable out of its
  environment (:func:`capture_ticket_secret`), so no child inherits it; it
  never reaches a session.

The actual uid switch, the Landlock-after-switch and the broker's fork/watcher/
reaper live in ``docker/isolation_broker.py`` and are only exercisable with
privilege (container), but the mapping and ticket logic here are pure and unit
tested. Nothing in this module requires privilege.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

UID_MIN_ENV = "CLAUDE_ISOLATION_UID_MIN"
UID_MAX_ENV = "CLAUDE_ISOLATION_UID_MAX"
UID_REGISTRY_ENV = "CLAUDE_ISOLATION_UID_REGISTRY"
EPHEMERAL_UIDS_ENV = "CLAUDE_ISOLATION_EPHEMERAL_UIDS"
BROKER_UID_ENV = "CLAUDE_ISOLATION_BROKER_UID"
TICKET_SECRET_ENV = "GATEWAY_ISOLATION_TICKET_SECRET"
# The inherited pipe the image entrypoint hands the secret over in instead
# (docker/entrypoint.py hand_off_ticket_secret; read by capture_ticket_secret).
TICKET_SECRET_FD_ENV = "GATEWAY_ISOLATION_TICKET_SECRET_FD"
TICKET_ENV = "GATEWAY_ISOLATION_TICKET"
BROKER_SOCKET_ENV = "GATEWAY_ISOLATION_BROKER_SOCKET"

# A conservative default band well clear of system/app uids (app is 1000). An
# operator narrows or moves it to match the container's provisioned passwd/group
# entries (userland needs a passwd record for the uid — see the design §3 V).
_DEFAULT_UID_MIN = 20000
_DEFAULT_UID_MAX = 29999
# The broker's own dedicated uid, outside the default band (the entrypoint's
# default must match: docker/entrypoint.py).
_DEFAULT_BROKER_UID = 19999
# Top slice of the band reserved for anonymous/stateless runs (leased in memory
# for as long as the run's workspace lives, never persisted) so they cannot
# exhaust the band — see the split in :func:`_stable_band` /
# :func:`_ephemeral_band` and :class:`_EphemeralPool`.
_DEFAULT_EPHEMERAL = 256

_TICKET_TTL_SECONDS = 120  # a ticket is presented within one spawn; keep it short


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring %s=%r: not an integer; using %d", name, raw, default)
        return default


def uid_range() -> Tuple[int, int]:
    """The ``[min, max]`` uid band for per-user allocation (inclusive)."""
    lo = _int_env(UID_MIN_ENV, _DEFAULT_UID_MIN)
    hi = _int_env(UID_MAX_ENV, _DEFAULT_UID_MAX)
    return lo, hi


# The broker's gid: the gateway's gid unless the operator sets one
# (docker/entrypoint.py launch_isolation_broker).
BROKER_GID_ENV = "CLAUDE_ISOLATION_BROKER_GID"


def reserved_uids() -> Set[int]:
    """uids a session must never be assigned: 0, the gateway app uid and gid,
    and the broker uid and gid. Excluded from both allocators even if they fall
    inside the band (so a misconfigured band can never hand a session root, the
    gateway's own identity, or the broker's — the #241 review's band-overlap
    finding).

    The same ids the broker's ``_authorize`` refuses (docker/isolation_broker.py:
    0, ``APP_UID``, ``APP_GID`` and its own uid/euid/gid/egid). A session's gid
    is its uid, so the broker refuses a uid equal to one of those gids too; the
    allocator used to hand such a uid out and persist it, and that user was then
    refused at every spawn (PR #244 follow-up).
    """
    app_gid = _int_env("APP_GID", 1000)
    out: Set[int] = {0}
    out.add(_int_env("APP_UID", 1000))
    out.add(app_gid)
    out.add(_int_env(BROKER_UID_ENV, _DEFAULT_BROKER_UID))
    out.add(_int_env(BROKER_GID_ENV, app_gid))
    return out


# The most uids the ephemeral slice may take. It bounds how many anonymous runs
# can be live at once (far more than one gateway process runs), and with it the
# pool's memory: every released uid stays queued until the never-used ones run
# out. validate_bands() warns when CLAUDE_ISOLATION_EPHEMERAL_UIDS asks for more.
_MAX_EPHEMERAL = 65536


def _ephemeral_count() -> int:
    lo, hi = uid_range()
    span = hi - lo + 1
    if span <= 1:
        return 0
    default = min(_DEFAULT_EPHEMERAL, max(1, span // 4))
    n = min(_int_env(EPHEMERAL_UIDS_ENV, default), _MAX_EPHEMERAL)
    # Always leave at least one uid for the stable (named-user) band.
    return max(0, min(n, span - 1))


def _stable_band() -> Tuple[int, int]:
    lo, hi = uid_range()
    return lo, hi - _ephemeral_count()


def _ephemeral_band() -> Tuple[int, int]:
    lo, hi = uid_range()
    e = _ephemeral_count()
    return hi - e + 1, hi


class UidAllocationError(RuntimeError):
    """Raised when no uid can be assigned (range exhausted or invalid)."""


class UidBandExhausted(UidAllocationError):
    """Every uid of the stable (named-user) band is assigned.

    Unlike a momentary shortage this lasts until an operator widens the band:
    a named user keeps their uid for good (no reclamation yet), so a retry can
    never succeed. The request path reports it as such, not "retry shortly".
    """


class UidRegistry:
    """A persisted ``user key -> uid`` map with sequential allocation.

    The map is JSON on disk so uids survive restarts (a resumed session must
    land on the same uid that owns its files). Allocation hands out the lowest
    free uid in the band, skips the reserved uids, and never reuses one still
    mapped. Thread-safe for the gateway's concurrent session creation. Only
    stable (named-user) keys live here — anonymous/stateless runs use the
    in-memory :class:`_EphemeralPool` so they cannot grow this file or use up
    the stable band.

    Fail-closed on a damaged file (#241 review): a registry that exists but
    cannot be read or parsed raises :class:`UidAllocationError` instead of
    loading as empty — an empty map would hand the lowest free uids out again,
    giving a returning user a different uid (losing their files) or another
    user's former uid. Entries that cannot be honored (malformed, outside the
    band, reserved, or sharing a uid with another key) are kept on disk, their
    uids are never handed to anyone else (the ephemeral pool excludes them too,
    see :func:`get_ephemeral_pool`), their keys refuse to resolve, and uid-mode
    startup refuses (:func:`registry_errors`) until an operator fixes the
    registry or the band.
    """

    def __init__(
        self, path: Path, lo: int, hi: int, reserved: Optional[Set[int]] = None
    ) -> None:
        self._path = path
        self._lo = lo
        self._hi = hi
        self._reserved = set(reserved or ())
        self._lock = threading.Lock()
        self._map: Dict[str, int] = {}
        self._used: set[int] = set()
        # Raw entries kept verbatim on persist but never resolved (see _load),
        # and why each one cannot be honored (a _HELD_REASONS key).
        self._held: Dict[str, object] = {}
        self._held_reason: Dict[str, str] = {}
        # How many uids the band can hand out, and whether the "nearly full"
        # warning is out for the current crossing (see _check_fill).
        self._capacity = max(0, hi - lo + 1) - sum(
            1 for u in self._reserved if lo <= u <= hi
        )
        self._fill_warned = False
        self._load()
        self._check_fill()

    def _load(self) -> None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # first start: nothing allocated yet
        except (OSError, UnicodeDecodeError) as exc:
            raise UidAllocationError(
                f"cannot read uid registry {self._path}: {exc}; refusing to "
                "allocate uids rather than reassign them"
            ) from exc
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError) as exc:
            # RecursionError: a deeply nested document, which would otherwise
            # escape registry_errors() as a traceback instead of this error.
            raise UidAllocationError(
                f"uid registry {self._path} is corrupt ({exc}); refusing to "
                "allocate uids rather than reassign them. Restore it from a "
                "backup, or remove it deliberately to start a fresh mapping."
            ) from exc
        if not isinstance(raw, dict):
            raise UidAllocationError(
                f"uid registry {self._path} is not a JSON object; refusing to "
                "allocate uids rather than reassign them"
            )

        def _is_uid(val: object) -> bool:
            return isinstance(val, int) and not isinstance(val, bool)

        owners: Dict[int, int] = {}
        for uid in raw.values():
            if _is_uid(uid):
                owners[uid] = owners.get(uid, 0) + 1
        for key, uid in raw.items():
            if not _is_uid(uid):
                reason = "malformed"
            elif uid in self._reserved:
                reason = "reserved"
            elif not self._lo <= uid <= self._hi:
                reason = "outside"
            elif owners[uid] != 1:
                reason = "shared"
            else:
                self._map[key] = uid
                self._used.add(uid)
                continue
            self._held[key] = uid
            self._held_reason[key] = reason
            if _is_uid(uid):
                self._used.add(uid)  # never hand a held uid to another key
        if self._held:
            logger.warning(
                "uid registry %s: %d entr%s cannot be honored (%s); those "
                "users' spawns are refused until the registry or the band is fixed",
                self._path,
                len(self._held),
                "y" if len(self._held) == 1 else "ies",
                self.describe_held(),
            )

    # Why a registry entry cannot be honored; filled in with the band and the
    # reserved ids by describe_held().
    _HELD_REASONS = {
        "reserved": (
            "on a reserved id (root, the gateway's uid/gid or the broker's "
            "uid/gid: {reserved}), which the broker refuses"
        ),
        "outside": "outside the stable band [{lo}, {hi}]",
        "shared": "sharing a uid with another user",
        "malformed": "not an integer uid",
    }

    def _held_reason_text(self, reason: str) -> str:
        return self._HELD_REASONS[reason].format(
            reserved=sorted(self._reserved), lo=self._lo, hi=self._hi
        )

    # What an operator does about each reason (registry_errors).
    _HELD_REMEDIES = {
        "reserved": (
            "an entry on a reserved id cannot work, since the broker refuses it "
            "as a session uid or gid: restore APP_UID/APP_GID/"
            f"{BROKER_UID_ENV}/{BROKER_GID_ENV} if one of them changed since the "
            "entry was made, otherwise give that user a free uid of the band or "
            "remove the entry"
        ),
        "outside": (
            f"restore {UID_MIN_ENV}/{UID_MAX_ENV}/{EPHEMERAL_UIDS_ENV}, or move "
            "the entry into the stable band (and chown that user's files)"
        ),
        "shared": "give each of those users a uid of its own",
        "malformed": "fix or remove the entry",
    }

    # How many uids describe_held() names per reason before "and N more".
    _HELD_UIDS_SHOWN = 10

    def held_reasons(self) -> List[str]:
        """The reasons held entries have, in :attr:`_HELD_REASONS` order."""
        with self._lock:
            present = set(self._held_reason.values())
        return [reason for reason in self._HELD_REASONS if reason in present]

    def describe_held(self) -> str:
        """Why the held entries cannot be honored, counted per reason with the
        uids involved (never the user keys: the log is not the registry), e.g.
        ``1 on a reserved id (…) (uid 1000); 2 outside the stable band
        [20000, 29743] (uids 30001, 30002)``."""
        with self._lock:
            counts: Dict[str, int] = {}
            uids: Dict[str, Set[int]] = {}
            for key, reason in self._held_reason.items():
                counts[reason] = counts.get(reason, 0) + 1
                uid = self._held[key]
                if reason != "malformed" and isinstance(uid, int):
                    uids.setdefault(reason, set()).add(uid)
        parts = []
        for reason in self._HELD_REASONS:
            if reason not in counts:
                continue
            text = f"{counts[reason]} {self._held_reason_text(reason)}"
            listed = sorted(uids.get(reason, ()))
            if listed:
                shown = ", ".join(str(u) for u in listed[: self._HELD_UIDS_SHOWN])
                more = len(listed) - self._HELD_UIDS_SHOWN
                tail = f" and {more} more" if more > 0 else ""
                plural = "s" if len(listed) > 1 else ""
                text += f" (uid{plural} {shown}{tail})"
            parts.append(text)
        return "; ".join(parts)

    def _persist(self) -> None:
        """Write the map durably. Raises ``UidAllocationError`` on failure so a
        new allocation fails closed rather than handing out a uid that would be
        lost on restart (a resumed session must land on the uid owning its
        files; a silently dropped write risks a different or reused uid).

        The temp file is fsynced before the atomic replace and the directory
        after it, so a crash cannot leave a truncated registry behind (which
        :meth:`_load` would then refuse). Held entries are written back
        unchanged. Mode 0600: the map names every user.

        Only a failure *before* the replace raises: once the new map is in
        place the allocation is on disk, so a failing directory fsync is
        logged instead — rolling the grant back then would free a uid the file
        already assigns, and the next allocation could hand it to someone else.
        """
        data: Dict[str, object] = dict(self._held)
        data.update(self._map)
        payload = json.dumps(data, sort_keys=True).encode("utf-8")
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            raise UidAllocationError(
                f"could not persist uid registry {self._path}: {exc}"
            ) from exc
        try:
            dfd = os.open(
                self._path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            logger.warning(
                "uid registry %s was replaced but its directory could not be "
                "fsynced (%s); the allocation stands",
                self._path,
                exc,
            )

    # Warn once the stable band is this full: a named user's uid is never
    # reclaimed yet, and a full band refuses every new named user.
    _FILL_WARN_PERCENT = 90

    def _check_fill(self) -> None:
        """Warn once when the band crosses ``_FILL_WARN_PERCENT`` used (not on
        every allocation after that; again only after it dropped below)."""
        if self._capacity <= 0:
            return
        used = sum(
            1
            for u in self._used
            if self._lo <= u <= self._hi and u not in self._reserved
        )
        full = used * 100 >= self._capacity * self._FILL_WARN_PERCENT
        if full and not self._fill_warned:
            logger.warning(
                "uid registry %s: %d of the %d uids in the stable band [%d, %d] "
                "are assigned to named users. They are never reclaimed, and a "
                "full band refuses every new named user; widen %s/%s before it "
                "fills up.",
                self._path,
                used,
                self._capacity,
                self._lo,
                self._hi,
                UID_MIN_ENV,
                UID_MAX_ENV,
            )
        self._fill_warned = full

    def claimed_uids(self) -> Set[int]:
        """Every uid the registry file accounts for, honored or held."""
        with self._lock:
            return set(self._used)

    def held_count(self) -> int:
        """How many registry entries cannot be honored under the current band."""
        with self._lock:
            return len(self._held)

    def resolve(self, key: str) -> int:
        """Return the uid for *key*, allocating the lowest free one if new."""
        with self._lock:
            existing = self._map.get(key)
            if existing is not None:
                return existing
            if key in self._held:
                reason = self._held_reason_text(self._held_reason[key])
                raise UidAllocationError(
                    f"the uid registry entry for this user ({self._held[key]!r}) "
                    f"cannot be honored ({reason}); refusing to assign a "
                    "different uid. Fix the registry or the band."
                )
            for uid in range(self._lo, self._hi + 1):
                if uid in self._used or uid in self._reserved:
                    continue
                self._map[key] = uid
                self._used.add(uid)
                try:
                    self._persist()
                except UidAllocationError:
                    # roll back the in-memory grant so a retry can try again
                    self._map.pop(key, None)
                    self._used.discard(uid)
                    raise
                self._check_fill()
                return uid
            raise UidBandExhausted(
                f"stable uid band [{self._lo}, {self._hi}] is exhausted "
                f"({len(self._used)} in use); widen {UID_MIN_ENV}/{UID_MAX_ENV}"
            )


class _EphemeralPool:
    """In-memory uid leases on the ephemeral band, for anonymous/stateless runs.

    A run's key (its absolute workspace path: ``landlock._uid_spawn_env`` keys
    an anonymous spawn on ``_absolute(cwd)``) holds one uid from its first
    spawn until :meth:`release`, which ``WorkspaceManager.cleanup_temp_workspace``
    calls when the run's workspace is removed. Every spawn of the run (the
    session, a re-created client, its slash preflight) gets that lease, and no
    other key gets the uid while it is held. Sharing a uid between two live
    runs would give up the DAC split of ``/dev/shm`` and, below Landlock ABI 6,
    the signal scoping (#244 review), so the lease follows the workspace rather
    than one client: a session keeps its uid across re-created clients, and
    the workspace (with the lease) goes only after the run's CLIs are stopped.
    The session manager kills a CLI that outlived its capped disconnect, and a
    session that ends while its client is still starting leaves the removal to
    the request creating that client. Gaps remain: a ``/v1/agents/messages``
    or admin try-run CLI is killed after its disconnect only when the caller
    went away, so a normal-path disconnect that times out ends the lease with
    that CLI possibly still running; a delete that lands during the slash
    preflight a request runs before creating its client ends the lease under
    that short catalog CLI; and in uid mode a killed CLI's own processes are
    the broker's to reap, within about 5 s. A new key gets a free uid:
    never-used ones first, then the one released longest ago, which keeps a
    just-released uid out of use far longer than such stragglers live.

    The pool's size bounds how many anonymous runs can be live at once, not
    how many can run over time. When every uid is leased, the leases whose
    workspace no longer exists are reclaimed first, which recovers a release
    that never came. If none is free, the spawn fails with
    :class:`UidAllocationError` (on ``/v1/responses`` a retryable 503) instead
    of sharing a live uid. Never persisted, so the high-volume anonymous path
    (every ``/v1/agents/messages`` call, each anonymous ``/v1/responses``
    session, admin try-runs, startup ``verify()`` and the uid probe) cannot
    grow a file. The broker is the authoritative backstop for the gaps above:
    it refuses a uid that a live supervisor still holds for another workspace,
    and any uid an orphan still holds (``isolation_broker._Broker._check_uid_free``).
    """

    def __init__(self, lo: int, hi: int, reserved: Optional[Set[int]] = None) -> None:
        # The band is never listed up front (a list of a huge band once ran the
        # gateway out of memory at its first anonymous run): only the excluded
        # uids inside it are kept, plus each released uid until it is handed
        # out again — at most the pool's size, which _MAX_EPHEMERAL caps.
        self._lo, self._hi = lo, hi
        self._reserved = frozenset(u for u in (reserved or ()) if lo <= u <= hi)
        self._capacity = max(0, hi - lo + 1) - len(self._reserved)
        # Hand-out order: the never-used uids ascending (a cursor), then the
        # released ones oldest first (they join the back of the deque).
        self._next = lo
        self._released: deque[int] = deque()
        self._leases: Dict[str, int] = {}
        self._lock = threading.Lock()

    def resolve(self, key: str) -> int:
        """The uid leased to *key*, leasing a free one when it holds none."""
        with self._lock:
            uid = self._leases.get(key)
            if uid is not None:
                return uid
            if self._capacity <= 0:
                raise UidAllocationError(
                    "no ephemeral uids available; widen the band or raise "
                    f"{EPHEMERAL_UIDS_ENV}"
                )
            uid = self._take_free_locked()
            if uid is None:
                self._reclaim_stale_locked()
                uid = self._take_free_locked()
            if uid is None:
                raise UidAllocationError(
                    f"all {self._capacity} ephemeral uids are leased to live "
                    "anonymous runs; retry once one finishes, or raise "
                    f"{EPHEMERAL_UIDS_ENV}"
                )
            self._leases[key] = uid
            return uid

    def release(self, key: str) -> Optional[int]:
        """End *key*'s lease; its uid is handed out again last. Returns the
        released uid, ``None`` when *key* held no lease."""
        with self._lock:
            uid = self._leases.pop(key, None)
            if uid is not None:
                self._released.append(uid)
            return uid

    def is_free(self, uid: int) -> bool:
        """Whether *uid* is a pool uid that no run holds a lease on."""
        with self._lock:
            return (
                self._lo <= uid <= self._hi
                and uid not in self._reserved
                and uid not in self._leases.values()
            )

    def _take_free_locked(self) -> Optional[int]:
        while self._next <= self._hi:
            uid = self._next
            self._next += 1
            if uid not in self._reserved:
                return uid
        if self._released:
            return self._released.popleft()
        return None

    def _reclaim_stale_locked(self) -> None:
        for key in [k for k in self._leases if _lease_workspace_gone(k)]:
            uid = self._leases.pop(key)
            self._released.append(uid)
            logger.warning(
                "Reclaimed ephemeral uid %d from %s: its workspace is gone but "
                "the lease was never released",
                uid,
                key,
            )


def _lease_workspace_gone(key: str) -> bool:
    """Whether the workspace an ephemeral lease is keyed on no longer exists.

    Only a missing absolute path counts. A key that names no path is freed by
    its release alone, and a path that cannot be checked (EACCES, EIO) keeps
    its lease: handing a live run's uid to another run is what leases prevent.
    """
    if not os.path.isabs(key):
        return False
    try:
        os.lstat(key)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


_registry: Optional[UidRegistry] = None
_ephemeral_pool: Optional[_EphemeralPool] = None
_registry_lock = threading.Lock()


def _registry_path() -> Path:
    raw = (os.getenv(UID_REGISTRY_ENV) or "").strip()
    if raw:
        return Path(raw)
    # Default under the gateway's /app/data volume (durable, writable), not
    # /app/src/data (code tree): parents[3] is /app for this module.
    return Path(__file__).resolve().parents[3] / "data" / "isolation_uids.json"


# The registry has one writer. Each gateway process allocates from its own
# in-memory copy, and nothing re-reads the file, so two processes on one
# registry (``uvicorn --workers``, replicas sharing the data volume) would hand
# two users the same uid and overwrite each other's grants. The process that
# loads the registry holds an exclusive lock on ``<registry>.lock`` until it
# exits, and any other process refuses uid mode (PR #244 follow-up).
_REGISTRY_LOCK_SUFFIX = ".lock"
# (absolute registry path, open lock fd) of the lock this process holds.
_held_registry_lock: Optional[Tuple[str, int]] = None


def _registry_lock_path(path: Path) -> Path:
    return path.with_name(path.name + _REGISTRY_LOCK_SUFFIX)


def _hold_registry_lock(path: Path) -> None:
    """Take the process-lifetime lock for the registry at *path* (call under
    ``_registry_lock``). A no-op when this process already holds it.

    Raises :class:`UidAllocationError` when another process holds it, or when
    the lock cannot be taken at all: refusing uid mode beats risking a second
    writer.

    The file is opened read-write, and read-only only when that is refused
    (an existing lock file the gateway can only read): ``flock`` needs no write
    access locally, but NFS emulates it with a POSIX write lock, which does.
    Creating the file needs a writable directory. ``O_NONBLOCK`` plus a
    regular-file check keep a planted FIFO from hanging startup. ``flock`` only
    excludes processes that share its lock manager: replicas on other hosts
    sharing the registry over a filesystem that keeps it node-local (NFS
    mounted ``local_lock=flock`` or ``nolock``) are not detected.
    """
    import errno
    import fcntl
    import stat

    global _held_registry_lock
    key = os.path.abspath(path)
    if _held_registry_lock is not None:
        if _held_registry_lock[0] == key:
            return
        _release_registry_lock()
    lock_path = _registry_lock_path(path)
    flags = os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock_path, os.O_RDWR | flags, 0o600)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise
            fd = os.open(lock_path, os.O_RDONLY | flags, 0o600)
    except OSError as exc:
        hint = ""
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            hint = (
                " Make the registry directory writable by the gateway, or create "
                "the lock file once (any content) where the gateway can read it."
            )
        raise UidAllocationError(
            f"cannot open the uid registry lock {lock_path}: {exc}; refusing uid "
            "mode rather than risk a second process allocating from the "
            f"registry.{hint}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UidAllocationError(
                f"the uid registry lock {lock_path} is not a regular file; "
                "refusing uid mode. Remove it so the gateway can create one."
            )
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except UidAllocationError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise UidAllocationError(
                f"another gateway process holds the uid registry {path} (lock "
                f"{lock_path}). uid mode supports one gateway process per "
                "registry — no uvicorn --workers, no replicas sharing it: each "
                "process would allocate from its own copy and give two users the "
                "same uid. Stop the other process."
            ) from exc
        hint = ""
        if exc.errno == errno.EBADF:
            hint = (
                " An exclusive lock over NFS needs a lock file the gateway can "
                "write."
            )
        raise UidAllocationError(
            f"cannot lock the uid registry ({lock_path}: {exc}); refusing uid "
            "mode rather than risk a second process allocating from the "
            f"registry.{hint}"
        ) from exc
    _held_registry_lock = (key, fd)


def _release_registry_lock() -> None:
    global _held_registry_lock
    held, _held_registry_lock = _held_registry_lock, None
    if held is not None:
        os.close(held[1])  # closing the only fd drops the flock


def get_registry() -> UidRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            path = _registry_path()
            _hold_registry_lock(path)
            lo, hi = _stable_band()
            try:
                _registry = UidRegistry(path, lo, hi, reserved_uids())
            except BaseException:
                _release_registry_lock()
                raise
        return _registry


def get_ephemeral_pool() -> _EphemeralPool:
    """The anonymous/stateless uid pool.

    Excludes every uid the persisted registry accounts for — not only the
    reserved ones: after a band change a named user's uid (now *held*) can sit
    inside the ephemeral band, and handing it to an anonymous run would give
    that run the named user's files. The registry is loaded first (outside the
    lock — :func:`get_registry` takes it too), so a damaged registry fails the
    anonymous path closed as well.
    """
    global _ephemeral_pool
    pool = _ephemeral_pool
    if pool is not None:
        return pool
    claimed = get_registry().claimed_uids()
    with _registry_lock:
        if _ephemeral_pool is None:
            lo, hi = _ephemeral_band()
            _ephemeral_pool = _EphemeralPool(lo, hi, reserved_uids() | claimed)
        return _ephemeral_pool


def release_ephemeral(key: str) -> None:
    """End an anonymous run's uid lease (see :class:`_EphemeralPool`).

    Called once the run is over and its workspace removed, with the key its
    spawns leased under (the absolute workspace path). A no-op when no
    anonymous run leased a uid in this process (the pool is created on the
    first lease, so the other modes never build it) and for a key that holds
    no lease.
    """
    pool = _ephemeral_pool
    if pool is not None:
        pool.release(key)


def reset_registry_cache() -> None:
    """Drop the memoized registry/pool (tests, or a band reconfiguration) and
    the registry lock this process holds; the next use takes it again."""
    global _registry, _ephemeral_pool
    with _registry_lock:
        _registry = None
        _ephemeral_pool = None
        _release_registry_lock()


def resolve_uid(key: str, *, persist: bool = True) -> int:
    """Map a key to its per-user uid.

    ``persist=True`` (a named user) uses the durable stable band; ``persist=
    False`` (an anonymous/stateless run) leases a uid of the in-memory
    ephemeral band until :func:`release_ephemeral`, so it can never grow the
    registry file, and two live runs never share a uid.
    """
    if persist:
        return get_registry().resolve(key)
    return get_ephemeral_pool().resolve(key)


def existing_uid(key: str) -> Optional[int]:
    """The durable uid already registered for the named user *key*, else ``None``.

    Never allocates and never writes the registry, so a caller can ask before
    deciding to spawn as the user at all (the slash-command preflight, which
    runs before the request is validated). A held entry — one the band cannot
    honor — raises the same :class:`UidAllocationError` as
    :meth:`UidRegistry.resolve`, and so does a registry that cannot be loaded.
    """
    registry = get_registry()
    with registry._lock:
        uid = registry._map.get(key)
        held = key in registry._held
    if held:
        # resolve() refuses a held key before it could allocate anything.
        return registry.resolve(key)
    return uid


def registry_errors() -> List[str]:
    """Startup errors for an unusable uid registry (empty list means OK).

    Loads the registry once so a damaged file refuses startup in uid mode
    instead of failing each named user's first spawn later. Entries the current
    band cannot honor (e.g. after narrowing it or growing the ephemeral slice)
    are an error too: their users would be refused at every spawn.
    """
    try:
        registry = get_registry()
    except UidAllocationError as exc:
        return [str(exc)]
    held = registry.held_count()
    if held:
        remedies = "; ".join(
            UidRegistry._HELD_REMEDIES[reason] for reason in registry.held_reasons()
        )
        return [
            f"the uid registry {_registry_path()} holds {held} entr"
            f"{'y' if held == 1 else 'ies'} the current settings cannot honor "
            f"({registry.describe_held()}); those users would be refused at every "
            f"spawn. To fix: {remedies}. Edit the registry only while the gateway "
            "is stopped."
        ]
    return []


# The largest id a session may get: (uid_t)-1 = 4294967295 tells setresuid/
# setresgid to leave an id unchanged, so a session "switched" to it would keep
# the broker's. Mirrored by docker/isolation_broker.py _MAX_SESSION_ID.
_MAX_ID = 4294967294


def _allocatable(lo: int, hi: int, reserved: Set[int]) -> int:
    """How many uids of ``[lo, hi]`` are not *reserved*: counted, never listed
    (a band can be huge)."""
    if hi < lo:
        return 0
    return (hi - lo + 1) - sum(1 for u in reserved if lo <= u <= hi)


def validate_bands() -> List[str]:
    """Startup errors for an unusable uid band (empty message list means OK)."""
    lo, hi = uid_range()
    errors: List[str] = []
    if hi < lo:
        errors.append(
            f"{UID_MIN_ENV}={lo} is greater than {UID_MAX_ENV}={hi}; the uid band "
            "is empty."
        )
        return errors
    if lo <= 0:
        errors.append(
            f"{UID_MIN_ENV}={lo} must be >= 1 so a session is never mapped to uid 0 "
            "(root)."
        )
    if hi > _MAX_ID:
        errors.append(
            f"{UID_MAX_ENV}={hi} is above {_MAX_ID}, the largest valid uid: "
            "(uid_t)-1 = 4294967295 makes setresuid/setresgid leave the id "
            "unchanged."
        )
    slo, shi = _stable_band()
    reserved = reserved_uids()
    if _allocatable(slo, shi, reserved) <= 0:
        errors.append(
            f"the stable uid band [{slo}, {shi}] has no allocatable uids after "
            f"reserving {sorted(reserved)}; widen {UID_MIN_ENV}/{UID_MAX_ENV} or "
            f"lower {EPHEMERAL_UIDS_ENV}."
        )
    # Parsed quietly: _int_env already warns about a non-integer value.
    try:
        asked = int((os.getenv(EPHEMERAL_UIDS_ENV) or "").strip() or "0")
    except ValueError:
        asked = 0
    if asked > _MAX_EPHEMERAL:
        logger.warning(
            "%s=%d is above the maximum %d; the ephemeral slice uses %d uids (the "
            "most anonymous runs one gateway can have live at once)",
            EPHEMERAL_UIDS_ENV,
            asked,
            _MAX_EPHEMERAL,
            _ephemeral_count(),
        )
    # Anonymous/stateless runs (every /v1/agents/messages call, anonymous
    # sessions) draw from the ephemeral band; an empty one would fail each of
    # them at spawn time instead of refusing startup with the reason.
    elo, ehi = _ephemeral_band()
    if _ephemeral_count() <= 0 or _allocatable(elo, ehi, reserved) <= 0:
        errors.append(
            f"the ephemeral uid band (top {EPHEMERAL_UIDS_ENV} uids of "
            f"[{lo}, {hi}]) has no allocatable uids after reserving "
            f"{sorted(reserved)}; anonymous/stateless runs could not be assigned "
            f"a uid. Widen the band or raise {EPHEMERAL_UIDS_ENV} (>= 1)."
        )
    app_gid = _int_env("APP_GID", 1000)
    ids: Dict[int, List[str]] = {}
    for name, val in (
        ("broker uid", _int_env(BROKER_UID_ENV, _DEFAULT_BROKER_UID)),
        ("broker gid", _int_env(BROKER_GID_ENV, app_gid)),
        ("gateway app uid", _int_env("APP_UID", 1000)),
        ("gateway app gid", app_gid),
    ):
        ids.setdefault(val, []).append(name)
    for val, names in sorted(ids.items()):
        if lo <= val <= hi:
            logger.warning(
                "the %s %d falls inside the per-user band [%d, %d]; it is "
                "excluded from allocation, but move the band clear of it",
                "/".join(names),
                val,
                lo,
                hi,
            )
    return errors


# --------------------------------------------------------------------------
# Spawn ticket: HMAC-signed, single-use, short-lived.
# --------------------------------------------------------------------------


_captured_secret: Optional[bytes] = None

# docker/entrypoint.py refuses a longer secret (MAX_TICKET_SECRET_BYTES), so a
# pipe holding more is not one it filled.
_SECRET_FD_MAX_BYTES = 4096


def _parse_secret_fd(raw: str) -> Optional[Tuple[int, int, int]]:
    """``<fd>:<st_dev>:<st_ino>`` as ``docker/entrypoint.py`` writes it, else
    ``None``. The entrypoint never leaves the pipe on a stdio number."""
    parts = raw.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        fd, dev, ino = (int(part) for part in parts)
    except ValueError:
        return None
    if fd <= 2 or dev < 0 or ino < 0:
        return None
    return fd, dev, ino


def _read_secret_fd() -> Optional[bytes]:
    """The secret in the entrypoint's pipe (``TICKET_SECRET_FD_ENV``), else ``None``.

    Pops the variable in every case. Only the entrypoint's own pipe is read
    (without blocking) and closed: the variable names its device and inode as
    well as its number, because a process can inherit the variable without the
    descriptor. A ``uvicorn --workers``/``--reload`` worker is spawned with only
    the parent's environment, and under that number it may hold a pipe of its
    own (multiprocessing's parent sentinel) — reading and closing that would
    break the worker and blame a consumed secret. Any other descriptor is left
    alone, and so is a stdio number.
    """
    import stat

    raw = os.environ.pop(TICKET_SECRET_FD_ENV, None)
    if raw is None or not raw.strip():
        return None
    parsed = _parse_secret_fd(raw)
    if parsed is None:
        logger.warning(
            "Ignoring %s=%r: not <fd>:<dev>:<inode> with a descriptor above 2",
            TICKET_SECRET_FD_ENV,
            raw,
        )
        return None
    fd, dev, ino = parsed
    try:
        st = os.fstat(fd)
    except OSError as exc:
        logger.warning("Ignoring %s=%r: %s", TICKET_SECRET_FD_ENV, raw, exc)
        return None
    if not stat.S_ISFIFO(st.st_mode) or (st.st_dev, st.st_ino) != (dev, ino):
        logger.warning(
            "Ignoring %s=%r: descriptor %d is not the entrypoint's pipe (only the "
            "variable was inherited, as by a uvicorn --workers/--reload worker), "
            "so it is left alone and uid mode has no ticket secret here",
            TICKET_SECRET_FD_ENV,
            raw,
            fd,
        )
        return None
    data = b""
    try:
        os.set_blocking(fd, False)
        while len(data) <= _SECRET_FD_MAX_BYTES:
            try:
                chunk = os.read(fd, _SECRET_FD_MAX_BYTES + 1 - len(data))
            except BlockingIOError:
                break  # drained; some process still holds a write end
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        logger.warning(
            "Could not read the ticket secret from %s=%d: %s",
            TICKET_SECRET_FD_ENV,
            fd,
            exc,
        )
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(data) > _SECRET_FD_MAX_BYTES:
        logger.warning(
            "Ignoring the ticket secret pipe %s=%d: it holds more than %d bytes",
            TICKET_SECRET_FD_ENV,
            fd,
            _SECRET_FD_MAX_BYTES,
        )
        return None
    try:
        secret = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.warning(
            "Ignoring the ticket secret pipe %s=%d: not UTF-8",
            TICKET_SECRET_FD_ENV,
            fd,
        )
        return None
    if not secret:
        logger.warning(
            "The ticket secret pipe %s=%d was empty: another process may have "
            "read it first, and uid mode cannot mint broker tickets without it",
            TICKET_SECRET_FD_ENV,
            fd,
        )
        return None
    return secret.encode("utf-8")


def capture_ticket_secret() -> None:
    """Take the ticket secret into module memory, out of every child's reach.

    Two sources; the environment variable wins (an operator's value, tests, a
    gateway started without the image entrypoint):

    * ``GATEWAY_ISOLATION_TICKET_SECRET`` is popped from ``os.environ``. Left
      there, every child that copies ``os.environ`` (the frozen codex/opencode
      clients, startup probes, anything not routed through the per-spawn
      blanking) would inherit it and could mint a valid ticket for any uid or
      workspace (#241 review).
    * Otherwise the pipe the image entrypoint hands over
      (``GATEWAY_ISOLATION_TICKET_SECRET_FD``, ``docker/entrypoint.py``
      ``hand_off_ticket_secret``) is read and closed, and that variable popped.
      It is drained and closed even when the variable above wins.

    The entrypoint moves an operator-set variable into the pipe too, so under it
    the exec-time ``/proc/<gateway>/environ`` holds only the descriptor number.
    Without the entrypoint the exec-time copy of the variable remains: the
    non-dumpable policy (``GATEWAY_NON_DUMPABLE``, on by default) keeps it
    root-only for the serving process, but not for a ``uvicorn --workers`` /
    ``--reload`` supervisor. Confined sessions are kept off both by the uid
    split (uid mode) or Landlock's ptrace scoping.

    The steps the image CMD runs before uvicorn inherit the descriptor; Python
    ``subprocess`` children (git, ``claude plugin``) do not, since they close
    inherited descriptors. A step that reads it by accident consumes the
    secret: the pipe is then empty here, this warns, and uid mode refuses to
    start for lack of a secret. That catches mistakes, not intent: a step that
    means harm can read the pipe and write the secret back through
    ``/proc/self/fd`` unnoticed — but it runs as the app uid, which can already
    rewrite the gateway's code under ``/app``, so the pipe is hygiene (no
    environ copy, no inheritance by default), not a boundary. Called once at
    startup, before any spawn; idempotent.
    """
    global _captured_secret
    raw = (os.environ.pop(TICKET_SECRET_ENV, None) or "").strip()
    from_pipe = _read_secret_fd()
    if raw:
        _captured_secret = raw.encode("utf-8")
    elif from_pipe:
        _captured_secret = from_pipe


def reset_captured_secret() -> None:
    """Forget a captured secret (tests)."""
    global _captured_secret
    _captured_secret = None


def ticket_secret() -> Optional[bytes]:
    """The ticket HMAC secret: the environment value if set, else the one
    :func:`capture_ticket_secret` took into memory (from the environment or the
    entrypoint's pipe); ``None`` when neither."""
    raw = (os.getenv(TICKET_SECRET_ENV) or "").strip()
    if raw:
        return raw.encode("utf-8")
    return _captured_secret


def _secret() -> Optional[bytes]:
    return ticket_secret()


def mint_ticket(
    *,
    uid: int,
    workspace: str,
    ro: List[str],
    rw: List[str],
    env_allow: List[str],
    ttl: int = _TICKET_TTL_SECONDS,
) -> str:
    """Mint a signed, single-use ticket string for ``options.env``.

    Raises ``RuntimeError`` when no shared secret is configured — the broker
    could not verify an unsigned ticket, so the gateway must not spawn.
    """
    secret = _secret()
    if secret is None:
        raise RuntimeError(
            f"{TICKET_SECRET_ENV} is not set; cannot mint a broker ticket. The "
            "entrypoint must generate it and share it with the gateway and broker."
        )
    payload = {
        "uid": uid,
        "workspace": workspace,
        "ro": ro,
        "rw": rw,
        "env_allow": env_allow,
        "nonce": secrets.token_hex(16),
        "exp": int(time.time()) + max(1, ttl),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"{sig}.{body.decode('utf-8')}"


def verify_ticket(ticket: str, *, seen_nonces: Optional[set] = None) -> Dict:
    """Verify a ticket's signature, expiry and (optionally) single use.

    Returns the payload dict, or raises ``ValueError``. ``seen_nonces`` is the
    broker's record of spent nonces; pass it to enforce single use.
    """
    secret = _secret()
    if secret is None:
        raise ValueError("no ticket secret configured")
    # Same refusals as the broker's copy (docker/isolation_broker.py), so the
    # two never disagree: tickets are minted ASCII, so anything else is
    # malformed rather than a TypeError out of compare_digest.
    if not isinstance(ticket, str) or not ticket.isascii():
        raise ValueError("malformed ticket")
    sig, _, body = ticket.partition(".")
    if not sig or not body:
        raise ValueError("malformed ticket")
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode("ascii"), expected.encode("ascii")):
        raise ValueError("bad ticket signature")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("ticket payload is not an object")
    exp, nonce = payload.get("exp"), payload.get("nonce")
    if type(exp) is not int or not isinstance(nonce, str) or not nonce:
        raise ValueError("ticket lacks an integer exp or a nonce")
    if exp < int(time.time()):
        raise ValueError("ticket expired")
    if seen_nonces is not None:
        if nonce in seen_nonces:
            raise ValueError("ticket already used")
        seen_nonces.add(nonce)
    return payload
