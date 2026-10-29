"""Which gateway process holds a session's CLI (``docs/zero-downtime-deploy.md`` §5.2).

One ``flock(LOCK_EX)`` per session id on ``data/session-owners/<id>.lock``, taken
when the session enters this process's :class:`~src.session_manager.SessionManager`
and released once its teardown has stopped the CLI. Both colors share ``data/``
(same host, same kernel), so the lock spans containers, and a dead process's
locks are dropped by the kernel. The file holds the owner's instance name and
boot id for whoever finds it locked.

Lock files are removed by their holder before it unlocks; an acquirer re-checks
after locking that the path still names the inode it locked (the usual
lock-file protocol), so an unlink never leaves two holders on two inodes.

Without ``GATEWAY_INSTANCE`` every call is a no-op that answers "this process".
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from fastapi import HTTPException

from src import instance_registry

logger = logging.getLogger(__name__)


class SessionOwnedElsewhere(HTTPException):
    """The session cannot run here right now; the request must be re-resolved.

    Raised before any work of the request has started (admission, or a session
    lock that a hand-off poisoned), so re-sending the request elsewhere is safe.
    An ``HTTPException`` (421 + ``x-gateway-reroute``) so the routes' existing
    ``except HTTPException: raise`` paths pass it through untouched and the
    handoff middleware can re-decide; it never reaches a client of a working
    blue/green pair.
    """

    def __init__(self, session_id: str = "", reason: str = "") -> None:
        super().__init__(
            status_code=421,
            detail={
                "error": {
                    "message": f"Session is not owned by this gateway instance ({reason}); retry",
                    "type": "api_error",
                    "code": "session_rerouted",
                }
            },
            headers={"x-gateway-reroute": "1", "retry-after": "1"},
        )
        self.session_id = session_id
        self.reason = reason


class SessionLock(asyncio.Lock):
    """``Session.lock`` that a hand-off can poison.

    After :meth:`poison` every acquisition — including requests already waiting
    on the lock when the session was handed over — raises
    :class:`SessionOwnedElsewhere` instead of running a turn on a session this
    process no longer owns.
    """

    _poisoned: bool = False

    def poison(self) -> None:
        self._poisoned = True

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    async def acquire(self) -> bool:  # type: ignore[override]
        await super().acquire()
        if self._poisoned:
            super().release()
            raise SessionOwnedElsewhere(reason="handed off")
        return True


def owners_dir() -> Path:
    return instance_registry.instances_dir().parent / "session-owners"


def _safe_name(session_id: str) -> str:
    if not session_id or "/" in session_id or session_id.startswith("."):
        raise ValueError(f"invalid session id {session_id!r}")
    return session_id


class OwnerLocks:
    """This process's held owner locks. Thread-safe (the session manager calls it
    under its own ``threading.Lock``)."""

    def __init__(self) -> None:
        self._held: Dict[str, Tuple[int, Set[int]]] = {}
        self._mutex = threading.Lock()

    def acquire(self, session_id: str, token: object) -> bool:
        """Take *session_id* for *token* (a session object). ``False`` when another
        process holds it. Several tokens of this process share one lock (an old
        incarnation still tearing down beside a rehydrated one)."""
        if not instance_registry.enabled():
            return True
        name = _safe_name(session_id)
        with self._mutex:
            held = self._held.get(name)
            if held is not None:
                held[1].add(id(token))
                return True
            directory = owners_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{name}.lock"
            for _ in range(8):
                fd = os.open(
                    str(path),
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o644,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    return False
                try:
                    same = os.fstat(fd).st_ino == os.stat(path).st_ino
                except FileNotFoundError:
                    same = False
                if not same:  # its holder removed it after we opened it
                    os.close(fd)
                    continue
                os.ftruncate(fd, 0)
                owner = f"{instance_registry.instance_name()}\n{instance_registry.BOOT_ID}\n"
                os.write(fd, owner.encode("ascii"))
                self._held[name] = (fd, {id(token)})
                return True
            raise OSError(errno.EAGAIN, f"owner lock {path} kept being replaced")

    def release(self, session_id: str, token: object) -> None:
        """Drop *token*'s hold; the lock goes when the last token of this process
        lets go. Never raises."""
        if not instance_registry.enabled():
            return
        with self._mutex:
            held = self._held.get(session_id)
            if held is None:
                return
            fd, tokens = held
            tokens.discard(id(token))
            if tokens:
                return
            del self._held[session_id]
            try:
                os.unlink(owners_dir() / f"{session_id}.lock")
            except OSError:
                pass
            os.close(fd)

    def holds(self, session_id: str) -> bool:
        with self._mutex:
            return session_id in self._held

    def owner(self, session_id: str) -> Optional[str]:
        """The instance holding *session_id*: this one's name, another live one's,
        ``""`` when it is held but the holder is not readable yet, or None when
        nobody holds it."""
        if not instance_registry.enabled():
            return None
        try:
            name = _safe_name(session_id)
        except ValueError:
            return None
        if self.holds(name):
            return instance_registry.instance_name()
        path = owners_dir() / f"{name}.lock"
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                content = os.pread(fd, 256, 0).decode("ascii", "replace")
                return content.split("\n", 1)[0].strip()
            return None
        finally:
            os.close(fd)


owner_locks = OwnerLocks()
