"""Gateway instance identity and liveness for blue/green deploys.

Two gateway containers (``gateway-blue`` / ``gateway-green``) share ``./data``
during a swap (``docs/zero-downtime-deploy.md``). Each one that sets
``GATEWAY_INSTANCE`` registers itself here:

* ``<instances dir>/<name>.json`` — name, boot id, internal URL, capabilities.
* ``<instances dir>/<name>.lock`` — an ``flock(LOCK_EX)`` held for the life of the
  process. The kernel drops it when the process dies, so "file present, lock
  free" means "that instance is gone": liveness never depends on a heartbeat.

With ``GATEWAY_INSTANCE`` unset (a plain single-container deploy) nothing is
registered and every helper answers as if this were the only gateway.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from src.constants import DEFAULT_PORT

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

CAPABILITIES = ("handoff", "drain")

# This process's identity: fixed at import so every module sees the same value.
BOOT_ID = uuid.uuid4().hex

_lock_fd: Optional[int] = None


@dataclass(frozen=True)
class Instance:
    name: str
    boot_id: str
    url: str


def instance_name() -> str:
    """This gateway's color, or ``""`` when blue/green is not in use."""
    name = os.getenv("GATEWAY_INSTANCE", "").strip().lower()
    if name and not _NAME.fullmatch(name):
        raise ValueError(f"GATEWAY_INSTANCE={name!r} must match {_NAME.pattern}")
    return name


def enabled() -> bool:
    return bool(instance_name())


def instances_dir() -> Path:
    raw = os.getenv("GATEWAY_INSTANCE_DIR", "").strip()
    return Path(raw) if raw else _DATA_DIR / "instances"


def instance_url(name: Optional[str] = None) -> str:
    """Internal URL other instances use to reach this one (the compose service name)."""
    name = name or instance_name()
    raw = os.getenv("GATEWAY_INSTANCE_URL", "").strip()
    if raw and name == instance_name():
        return raw.rstrip("/")
    return f"http://gateway-{name}:{DEFAULT_PORT}"


def _lock_path(name: str) -> Path:
    return instances_dir() / f"{name}.lock"


def _record_path(name: str) -> Path:
    return instances_dir() / f"{name}.json"


def register() -> None:
    """Take this instance's liveness lock and publish its record. Idempotent.

    Refuses (``RuntimeError``) when another live process already holds the same
    name — two containers configured as the same color would route sessions to
    each other's boot ids.
    """
    global _lock_fd
    name = instance_name()
    if not name or _lock_fd is not None:
        return
    directory = instances_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(_lock_path(name)), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            f"GATEWAY_INSTANCE={name!r} is already running (lock {_lock_path(name)} held)"
        ) from None
    _lock_fd = fd
    record = {
        "name": name,
        "boot_id": BOOT_ID,
        "url": instance_url(name),
        "pid": os.getpid(),
        "capabilities": list(CAPABILITIES),
    }
    tmp = directory / f".{name}.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, _record_path(name))
    logger.info("Registered gateway instance %s (boot %s)", name, BOOT_ID[:8])


def unregister() -> None:
    """Drop the liveness lock (lifespan shutdown). The record file stays: with the
    lock free it already reads as "gone"."""
    global _lock_fd
    if _lock_fd is not None:
        os.close(_lock_fd)
        _lock_fd = None


def is_alive(name: str) -> bool:
    """True while some process holds ``name``'s liveness lock."""
    if name == instance_name() and _lock_fd is not None:
        return True
    try:
        fd = os.open(str(_lock_path(name)), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        return False
    finally:
        os.close(fd)


def lookup(name: str) -> Optional[Instance]:
    """The live instance called ``name``, or None when it is not running."""
    if not is_alive(name):
        return None
    try:
        record = json.loads(_record_path(name).read_text(encoding="utf-8"))
        return Instance(name=name, boot_id=str(record["boot_id"]), url=str(record["url"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def other_live_instances() -> List[str]:
    """Names of live instances other than this one (empty without blue/green)."""
    me = instance_name()
    try:
        names = sorted(p.stem for p in instances_dir().glob("*.lock"))
    except OSError:
        return []
    return [n for n in names if n != me and _NAME.fullmatch(n) and is_alive(n)]


def shared_secret() -> bytes:
    """HMAC key shared by every instance on this ``data/`` (created once, 0600)."""
    path = instances_dir().parent / "instance_secret"
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:  # another instance won the race
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(value)
    return bytes.fromhex(value)


def describe() -> Dict[str, object]:
    """``/health`` fields; empty without blue/green."""
    if not enabled():
        return {}
    return {"instance": instance_name(), "boot_id": BOOT_ID, "capabilities": list(CAPABILITIES)}
