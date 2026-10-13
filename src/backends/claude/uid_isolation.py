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
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

UID_MIN_ENV = "CLAUDE_ISOLATION_UID_MIN"
UID_MAX_ENV = "CLAUDE_ISOLATION_UID_MAX"
UID_REGISTRY_ENV = "CLAUDE_ISOLATION_UID_REGISTRY"
TICKET_SECRET_ENV = "GATEWAY_ISOLATION_TICKET_SECRET"
TICKET_ENV = "GATEWAY_ISOLATION_TICKET"
BROKER_SOCKET_ENV = "GATEWAY_ISOLATION_BROKER_SOCKET"

# A conservative default band well clear of system/app uids (app is 1000). An
# operator narrows or moves it to match the container's provisioned passwd/group
# entries (userland needs a passwd record for the uid — see the design §3 V).
_DEFAULT_UID_MIN = 20000
_DEFAULT_UID_MAX = 29999

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


class UidAllocationError(RuntimeError):
    """Raised when no uid can be assigned (range exhausted or invalid)."""


class UidRegistry:
    """A persisted ``user key -> uid`` map with sequential allocation.

    The map is JSON on disk so uids survive restarts (a resumed session must
    land on the same uid that owns its files). Allocation hands out the lowest
    free uid in the band and never reuses one still mapped. Thread-safe for the
    gateway's concurrent session creation.
    """

    def __init__(self, path: Path, lo: int, hi: int) -> None:
        self._path = path
        self._lo = lo
        self._hi = hi
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
            if self._lo <= uid <= self._hi:
                self._map[key] = uid
                self._used.add(uid)

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._map), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("Could not persist uid registry %s: %s", self._path, exc)

    def resolve(self, key: str) -> int:
        """Return the uid for *key*, allocating the lowest free one if new."""
        with self._lock:
            existing = self._map.get(key)
            if existing is not None:
                return existing
            for uid in range(self._lo, self._hi + 1):
                if uid not in self._used:
                    self._map[key] = uid
                    self._used.add(uid)
                    self._persist()
                    return uid
            raise UidAllocationError(
                f"uid range [{self._lo}, {self._hi}] is exhausted "
                f"({len(self._used)} in use); widen {UID_MIN_ENV}/{UID_MAX_ENV}"
            )


_registry: Optional[UidRegistry] = None
_registry_lock = threading.Lock()


def _registry_path() -> Path:
    raw = (os.getenv(UID_REGISTRY_ENV) or "").strip()
    if raw:
        return Path(raw)
    # Default beside the gateway's data dir (same place other durable state lives).
    return Path(__file__).resolve().parents[2] / "data" / "isolation_uids.json"


def get_registry() -> UidRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            lo, hi = uid_range()
            _registry = UidRegistry(_registry_path(), lo, hi)
        return _registry


def resolve_uid(key: str) -> int:
    """Map a user key to its stable per-user uid (see :class:`UidRegistry`)."""
    return get_registry().resolve(key)


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
