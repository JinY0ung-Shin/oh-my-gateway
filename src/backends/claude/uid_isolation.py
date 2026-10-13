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
  The shared secret (``GATEWAY_ISOLATION_TICKET_SECRET``) is established once at
  broker startup and handed to the gateway; it never reaches a session.

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
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

UID_MIN_ENV = "CLAUDE_ISOLATION_UID_MIN"
UID_MAX_ENV = "CLAUDE_ISOLATION_UID_MAX"
UID_REGISTRY_ENV = "CLAUDE_ISOLATION_UID_REGISTRY"
EPHEMERAL_UIDS_ENV = "CLAUDE_ISOLATION_EPHEMERAL_UIDS"
BROKER_UID_ENV = "CLAUDE_ISOLATION_BROKER_UID"
TICKET_SECRET_ENV = "GATEWAY_ISOLATION_TICKET_SECRET"
TICKET_ENV = "GATEWAY_ISOLATION_TICKET"
BROKER_SOCKET_ENV = "GATEWAY_ISOLATION_BROKER_SOCKET"

# A conservative default band well clear of system/app uids (app is 1000). An
# operator narrows or moves it to match the container's provisioned passwd/group
# entries (userland needs a passwd record for the uid — see the design §3 V).
_DEFAULT_UID_MIN = 20000
_DEFAULT_UID_MAX = 29999
# Top slice of the band reserved for anonymous/stateless runs (recycled in
# memory, never persisted) so they cannot exhaust the band — see the split in
# :func:`_stable_band` / :func:`_ephemeral_band`.
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


def reserved_uids() -> Set[int]:
    """uids a session must never be assigned: 0, the gateway app uid and the
    broker uid. Excluded from both allocators even if they fall inside the band
    (so a misconfigured band can never hand a session root, the gateway's own
    identity, or the broker's uid — the #241 review's band-overlap finding)."""
    out: Set[int] = {0}
    out.add(_int_env("APP_UID", 1000))
    out.add(_int_env(BROKER_UID_ENV, 21000))
    return out


def _ephemeral_count() -> int:
    lo, hi = uid_range()
    span = hi - lo + 1
    if span <= 1:
        return 0
    default = min(_DEFAULT_EPHEMERAL, max(1, span // 4))
    n = _int_env(EPHEMERAL_UIDS_ENV, default)
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


class UidRegistry:
    """A persisted ``user key -> uid`` map with sequential allocation.

    The map is JSON on disk so uids survive restarts (a resumed session must
    land on the same uid that owns its files). Allocation hands out the lowest
    free uid in the band, skips the reserved uids, and never reuses one still
    mapped. Thread-safe for the gateway's concurrent session creation. Only
    stable (named-user) keys live here — anonymous/stateless runs use the
    in-memory :class:`_EphemeralPool` so they cannot grow this file or exhaust
    the band.
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
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, uid in raw.items():
            if not (isinstance(key, str) and isinstance(uid, int)):
                continue
            if self._lo <= uid <= self._hi and uid not in self._reserved:
                self._map[key] = uid
                self._used.add(uid)

    def _persist(self) -> None:
        """Write the map durably. Raises ``UidAllocationError`` on failure so a
        new allocation fails closed rather than handing out a uid that would be
        lost on restart (a resumed session must land on the uid owning its
        files; a silently dropped write risks a different or reused uid)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._map), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            raise UidAllocationError(
                f"could not persist uid registry {self._path}: {exc}"
            ) from exc

    def resolve(self, key: str) -> int:
        """Return the uid for *key*, allocating the lowest free one if new."""
        with self._lock:
            existing = self._map.get(key)
            if existing is not None:
                return existing
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
                return uid
            raise UidAllocationError(
                f"stable uid band [{self._lo}, {self._hi}] is exhausted "
                f"({len(self._used)} in use); widen {UID_MIN_ENV}/{UID_MAX_ENV}"
            )


class _EphemeralPool:
    """In-memory ``key -> uid`` ring for anonymous/stateless runs.

    Hands out distinct uids from the ephemeral band until it is full, then
    recycles the least-recently-used mapping. Never persisted and never raises
    on exhaustion (it recycles), so the high-volume anonymous path — every
    ``/v1/agents/messages`` call, each anonymous ``/v1/responses`` session,
    admin try-runs, startup ``verify()`` — cannot grow a file or permanently
    exhaust the band. A recycled uid can at most be shared by more than
    ``size`` concurrent anonymous runs, which Landlock still confines to their
    own per-spawn workspace.
    """

    def __init__(self, lo: int, hi: int, reserved: Optional[Set[int]] = None) -> None:
        reserved = set(reserved or ())
        self._uids = [u for u in range(lo, hi + 1) if u not in reserved]
        self._lru: "OrderedDict[str, int]" = OrderedDict()
        self._lock = threading.Lock()

    def resolve(self, key: str) -> int:
        with self._lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                return self._lru[key]
            if not self._uids:
                raise UidAllocationError(
                    "no ephemeral uids available; widen the band or raise "
                    f"{EPHEMERAL_UIDS_ENV}"
                )
            if len(self._lru) < len(self._uids):
                uid = self._uids[len(self._lru)]
            else:
                _old, uid = self._lru.popitem(last=False)  # recycle LRU
            self._lru[key] = uid
            return uid


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


def get_registry() -> UidRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            lo, hi = _stable_band()
            _registry = UidRegistry(_registry_path(), lo, hi, reserved_uids())
        return _registry


def get_ephemeral_pool() -> _EphemeralPool:
    global _ephemeral_pool
    with _registry_lock:
        if _ephemeral_pool is None:
            lo, hi = _ephemeral_band()
            _ephemeral_pool = _EphemeralPool(lo, hi, reserved_uids())
        return _ephemeral_pool


def reset_registry_cache() -> None:
    """Drop the memoized registry/pool (tests, or a band reconfiguration)."""
    global _registry, _ephemeral_pool
    with _registry_lock:
        _registry = None
        _ephemeral_pool = None


def resolve_uid(key: str, *, persist: bool = True) -> int:
    """Map a key to its per-user uid.

    ``persist=True`` (a named user) uses the durable stable band; ``persist=
    False`` (an anonymous/stateless run) uses the recycled in-memory ephemeral
    band, so it can never grow the registry file or exhaust the band.
    """
    if persist:
        return get_registry().resolve(key)
    return get_ephemeral_pool().resolve(key)


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
    slo, shi = _stable_band()
    reserved = reserved_uids()
    stable = [u for u in range(slo, shi + 1) if u not in reserved]
    if not stable:
        errors.append(
            f"the stable uid band [{slo}, {shi}] has no allocatable uids after "
            f"reserving {sorted(reserved)}; widen {UID_MIN_ENV}/{UID_MAX_ENV} or "
            f"lower {EPHEMERAL_UIDS_ENV}."
        )
    broker_uid = _int_env(BROKER_UID_ENV, 21000)
    app_uid = _int_env("APP_UID", 1000)
    for name, val in (("broker", broker_uid), ("gateway app", app_uid)):
        if lo <= val <= hi:
            logger.warning(
                "the %s uid %d falls inside the per-user band [%d, %d]; it is "
                "excluded from allocation, but move the band clear of it",
                name,
                val,
                lo,
                hi,
            )
    return errors


# --------------------------------------------------------------------------
# Spawn ticket: HMAC-signed, single-use, short-lived.
# --------------------------------------------------------------------------


def _secret() -> Optional[bytes]:
    raw = (os.getenv(TICKET_SECRET_ENV) or "").strip()
    return raw.encode("utf-8") if raw else None


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
    sig, _, body = ticket.partition(".")
    if not sig or not body:
        raise ValueError("malformed ticket")
    expected = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad ticket signature")
    payload = json.loads(body)
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("ticket expired")
    nonce = payload.get("nonce")
    if seen_nonces is not None:
        if nonce in seen_nonces:
            raise ValueError("ticket already used")
        seen_nonces.add(nonce)
    return payload
