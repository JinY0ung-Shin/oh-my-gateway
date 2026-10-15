"""Gateway client for the broker's non-spawn operations (uid isolation).

Under ``CLAUDE_ISOLATION_MODE=uid`` each session CLI runs as a session uid
``U``. What ``U`` writes mode 0600/0700 (its transcripts, ``mkstemp`` files,
the CLI's own temp root) the gateway's uid can neither read nor remove, and the
broker holds no ``CAP_DAC_OVERRIDE`` to do it either. So the resident broker
(``docker/isolation_broker.py``) does it *as* ``U``, in a forked child with
``U``'s file access (``docs/isolation-uid-ownership-design.md`` §3.3, §3.6,
§4.5):

* ``cleanup`` removes paths (``clear`` keeps a directory and empties it,
  ``remove`` takes the entry itself), expires old per-session state and wipes
  ``U``'s ``/dev/shm`` objects;
* ``transcript`` counts the external user turns of one ``<sid>.jsonl`` and
  answers only that count (or a negative code), never the content.

Wire protocol: the gateway uid connects to ``$GATEWAY_ISOLATION_BROKER_SOCKET``
(the broker pins the peer with ``SO_PEERCRED``, as for the launcher), sends one
header — a ``=I`` length, then JSON ``{"ticket": T, "argv": [], "env": {}}`` —
with no file descriptors, and reads exactly one ``=Ii`` ``(kind, value)``
record. ``T`` is a single-use signed op ticket
(:func:`uid_isolation.mint_op_ticket`) that carries the op and every path; the
broker verifies it in its resident parent and answers anything off its strict
schema with ``ST_REFUSED``.

Every call blocks for one round trip. Teardown paths never make it on the event
loop: ``uid_cleanup`` runs them on its reclaimer thread.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from typing import Iterable, List, Mapping, Optional, Tuple

from . import uid_isolation as ui
from .transcript_turns import TRANSCRIPT_UNREADABLE

# Record kinds, mirrored from docker/_landlock_rules.py: the broker never
# imports the gateway tree, so tests/test_broker_ops.py pins the parity.
ST_EXITED = 1  # cleanup ran; value 0 = every step succeeded, else 1
ST_SIGNALED = 2  # spawn-only: never an op's answer
ST_REFUSED = 3  # the broker refused the request (schema, ticket, blocked)
ST_TRANSCRIPT = 4  # transcript op; value = user turns, or a negative code

_LENGTH = struct.Struct("=I")
_RECORD = struct.Struct("=Ii")

# The broker parks a blocked cleanup at most this many seconds.
MAX_WAIT_SECONDS = 30
# The smallest ``expire.older_than`` the broker accepts.
MIN_EXPIRE_SECONDS = 3600
_EXPIRE_KEYS = frozenset({"older_than", "units", "nested"})


class BrokerOpError(RuntimeError):
    """A broker op produced no result.

    The broker refused it (a request off its schema, an unusable ticket, a uid
    that is live or being wiped), could not be reached, the link failed, timed
    out or closed before a full record, or it answered with a record of the
    wrong kind. No message carries the ticket.
    """


def available() -> bool:
    """Whether broker ops can be attempted: uid mode with a broker socket."""
    from . import landlock

    return landlock.effective_mode() == "uid" and bool(landlock.broker_socket_path())


def _path(value) -> str:
    """*value* as the absolute, normalized path string the broker accepts."""
    path = os.fspath(value)
    if not isinstance(path, str):
        raise ValueError(f"broker op paths must be str, not {type(path).__name__}")
    path = os.path.abspath(path)
    if "\0" in path or path == "/":
        raise ValueError(f"not a path a broker op may name: {path!r}")
    return path


def _paths(values: Iterable) -> List[str]:
    if isinstance(values, (str, bytes, os.PathLike)):
        raise ValueError("broker op path lists must be lists, not one path")
    return [_path(v) for v in values]


def _expire_field(expire: Mapping) -> dict:
    """The ``expire`` payload: ``older_than`` seconds plus the dir lists."""
    if not isinstance(expire, Mapping) or set(expire) - _EXPIRE_KEYS:
        raise ValueError(f"expire takes only {sorted(_EXPIRE_KEYS)}")
    older = expire.get("older_than")
    if type(older) is not int or older < MIN_EXPIRE_SECONDS:
        raise ValueError(f"expire.older_than must be an int >= {MIN_EXPIRE_SECONDS}")
    field: dict = {"older_than": older}
    for key in ("units", "nested"):
        if key in expire:
            field[key] = _paths(expire[key])
    return field


def _mint(op: str, uid: int, fields: dict) -> str:
    try:
        return ui.mint_op_ticket(op, uid=uid, **fields)
    except RuntimeError as exc:  # no ticket secret: the broker could verify none
        raise BrokerOpError(f"cannot mint a broker {op} ticket: {exc}") from exc


def _remaining(deadline: float, what: str) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise BrokerOpError(f"the broker {what} op timed out")
    return left


def _exchange(ticket: str, timeout: float, what: str) -> Tuple[int, int]:
    """Send one op header and read the broker's one ``(kind, value)`` record."""
    from . import landlock

    path = landlock.broker_socket_path()
    if not path:
        raise BrokerOpError(
            f"no broker socket configured ({ui.BROKER_SOCKET_ENV}) for the {what} op"
        )
    header = json.dumps({"ticket": ticket, "argv": [], "env": {}}).encode("utf-8")
    deadline = time.monotonic() + max(0.0, timeout)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_remaining(deadline, what))
        sock.connect(path)
        sock.sendall(_LENGTH.pack(len(header)) + header)
        data = b""
        while len(data) < _RECORD.size:
            sock.settimeout(_remaining(deadline, what))
            chunk = sock.recv(_RECORD.size - len(data))
            if not chunk:
                raise BrokerOpError(
                    f"the broker closed the link before answering the {what} op"
                )
            data += chunk
    except socket.timeout as exc:
        raise BrokerOpError(f"the broker {what} op timed out") from exc
    except OSError as exc:
        raise BrokerOpError(
            f"the broker {what} op failed: {exc.strerror or exc}"
        ) from exc
    finally:
        sock.close()
    return _RECORD.unpack(data)


def _unexpected(what: str, uid: int, kind: int, value: int) -> BrokerOpError:
    if kind == ST_REFUSED:
        return BrokerOpError(f"the broker refused the {what} op for uid {uid}")
    return BrokerOpError(
        f"the broker answered the {what} op for uid {uid} with record "
        f"({kind}, {value})"
    )


def cleanup(
    uid: int,
    *,
    workspace="",
    clear: Iterable = (),
    remove: Iterable = (),
    shm: bool = False,
    wait: int = 0,
    expire: Optional[Mapping] = None,
    timeout: float = 60.0,
) -> bool:
    """Run a ``cleanup`` op as *uid*.

    *workspace* (``""`` or a path) is the live-check key: while a live session
    of *uid* runs in it — or, with *shm*, any live session of *uid* — the
    broker refuses, or parks the request up to *wait* seconds (0..30). It then
    empties every *clear* directory, removes every *remove* entry, applies
    *expire* (``{"older_than": seconds, "units": [...], "nested": [...]}``)
    and, with *shm*, removes *uid*'s ``/dev/shm`` entries. Paths leave here
    absolute and normalized.

    ``True`` when every step succeeded, ``False`` when one failed (the broker
    logs which). Raises :class:`BrokerOpError` when the op did not run to a
    result — refused, unreachable, an I/O error, *timeout* (+ *wait*) seconds
    passed, the link closed early, any other record — and ``ValueError`` for
    arguments the broker would refuse anyway.
    """
    if type(wait) is not int or not 0 <= wait <= MAX_WAIT_SECONDS:
        raise ValueError(f"wait must be an int in [0, {MAX_WAIT_SECONDS}]")
    fields = {
        "workspace": _path(workspace) if workspace else "",
        "wait": wait,
        "clear": _paths(clear),
        "remove": _paths(remove),
        "shm": bool(shm),
    }
    if expire is not None:
        fields["expire"] = _expire_field(expire)
    ticket = _mint(ui.OP_CLEANUP, uid, fields)
    kind, value = _exchange(ticket, timeout + wait, "cleanup")
    if kind == ST_EXITED:
        return value == 0
    raise _unexpected("cleanup", uid, kind, value)


def read_transcript(uid: int, path, cwd, *, timeout: float = 30.0) -> int:
    """Count the external user turns of the transcript at *path*, as *uid*.

    The broker applies ``transcript_turns.count_transcript_turns`` against
    *cwd* (the requester's workspace) and answers the count, or
    ``TRANSCRIPT_MISSING`` / ``_MISMATCH`` / ``_CORRUPT`` / ``_UNREADABLE``.
    Raises :class:`BrokerOpError` when it produced no such answer, and
    ``ValueError`` for a *path* that is not a ``.jsonl`` file name.
    """
    transcript = _path(path)
    if not transcript.endswith(".jsonl"):
        raise ValueError(f"not a transcript path: {transcript!r}")
    fields = {"path": transcript, "cwd": _path(cwd)}
    ticket = _mint(ui.OP_TRANSCRIPT, uid, fields)
    kind, value = _exchange(ticket, timeout, "transcript")
    if kind == ST_TRANSCRIPT and value >= TRANSCRIPT_UNREADABLE:
        return value
    raise _unexpected("transcript", uid, kind, value)
