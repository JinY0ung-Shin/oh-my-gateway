"""Open Terminal-compatible file server over per-user Claude workspaces.

Implements the subset of the open-webui "Open Terminal" server HTTP contract that
the ``FileNav`` right-sidebar explorer uses, scoped to a single user's Claude
workspace, so the gateway can be registered in open-webui as a terminal
connection and its workspace browsed/edited exactly as the agent sees it.

Contract (what FileNav calls):
- ``GET  /api/config``                -> ``{"features": {"terminal": false}}`` (handshake)
- ``GET  /files/cwd``                 -> ``{"cwd": "/"}`` (workspace root is the virtual "/")
- ``POST /files/cwd``  {path}         -> ``{"cwd": path}`` (validate; cwd is client-tracked)
- ``GET  /files/list?directory=<p>``  -> ``{"entries": [{name,type,size,modified}]}``
- ``GET  /files/search?query=<q>``    -> ``{"results": [{path,name,type,size,modified}], "truncated"}``
- ``GET  /files/digest?path=<p>``     -> ``{"path","size","sha256"}`` (content identity)
- ``GET  /files/read?path=<p>``       -> text ``{path,total_lines,content}`` | raw bytes (binary)
- ``GET  /files/view?path=<p>``       -> raw bytes (download)
- ``GET  /files/serve/<path>``        -> raw bytes, inline (HTML iframe preview; relative assets)
- ``POST /files/upload?directory=<p>``-> ``{path,size}`` (also how "new file" is created)
- ``POST /files/mkdir``  {path}       -> ``{path}``
- ``DELETE /files/delete?path=<p>``   -> ``{path,type}``
- ``POST /files/move``  {source,destination} -> ``{source,destination}``
- ``POST /files/copy``  {source,destination} -> ``{source,destination,type}``
- ``POST /files/archive`` {paths}     -> zip stream

Identity: read from a configurable, vendor-neutral header (``WORKSPACE_USER_HEADER``,
default ``X-User-Email``) and used WHOLE — the same value ``/v1/responses`` keys its
workspace on — so the explorer resolves the very files the agent wrote. The caller
(e.g. open-webui) forwards the user's identity under that header name; on open-webui
set ``FORWARD_USER_INFO_HEADER_USER_EMAIL`` to the same name so the two agree (no code
coupling to the caller's product).

This router used to key the workspace on the identity's *localpart* (everything
before ``@``). That collapsed distinct principals: ``alice@a.com``, ``alice@b.com``
and bare ``alice`` are three identities and were one directory, so any of them could
list, read, overwrite and delete the others' files, and ``/v1/agent-resources``
reported the others' private skills and subagents. It also disagreed with
``/v1/responses``, which never truncated — the file browser and the agent could end
up in different workspaces for one and the same caller. The whole identity is now
the key. Set ``WORKSPACE_LEGACY_LOCALPART_KEY=true`` to restore the old truncation
while migrating an existing deployment's directories; it re-opens the collision, so
it logs a warning on every resolve.

Config:
- ``WORKSPACE_USER_HEADER`` — inbound identity header name (default ``X-User-Email``).
- ``WORKSPACE_LEGACY_LOCALPART_KEY`` — when true, key the workspace on the identity's
  localpart as releases before this one did. Insecure (see above); migration only.
- ``WORKSPACE_HIDE_DOTFILES`` — when true, dot-prefixed entries are neither
  listed nor accessible. Default **false**: hiding is a presentation choice that
  belongs to the client rendering the tree, and this switch reaches every
  dotfile the user may legitimately want to edit (``.env``, ``.gitignore``, a
  tool's own dotdir), writes included.
- ``WORKSPACE_HIDE_CLAUDE_PREFIX`` — when true, path components whose names start
  with ``.claude`` are hidden/blocked by the file API (for example ``.claude``,
  ``.claude_images``, ``.claude-local``). Other dotfiles stay visible. Default **false**.

Which one an operator wants follows from the workspace layout. A workspace keeps
its skills and subagents at the canonical ``skills/`` and ``agents/`` roots, and
the gateway maintains a managed ``.claude/{skills,agents}`` mirror of them for
Claude Code's own discovery (see ``backends.claude.workspace_resources``). So
``WORKSPACE_HIDE_CLAUDE_PREFIX`` removes the *duplicate* from the file manager
while the copy the user actually edits stays listable and writable;
``WORKSPACE_HIDE_DOTFILES`` reaches that mirror too, but only on its way to
hiding everything else as well. Neither switch touches the agent process: it
reads the workspace directly, not through this API.
- ``USER_WORKSPACE_QUOTA_MB`` — optional cumulative quota for a named user's whole
  ``<base>/<user>`` tree, across backend directories. ``0``/unset = unlimited.
- ``ARCHIVE_MAX_CONCURRENT`` (default 4) / ``ARCHIVE_MAX_PER_USER`` (default 2) —
  concurrent ``/files/archive`` streams gateway-wide and per user; over either
  limit the request is refused with 429 + ``Retry-After`` before any producer
  thread starts.

Concurrency:
- Filesystem work (directory scans, file reads/writes, deletes, the archive
  walk) runs in the threadpool via ``run_in_threadpool``; ``/files/archive``
  deflates in a dedicated producer thread and streams the zip (``_stream_zip``). FileNav polls ``/files/list``
  continuously for every connected user; done synchronously that I/O would
  block the gateway event loop and stall everything else it serves
  (``/v1/responses`` streams, terminal websockets).
- When cumulative quota is enabled, quota-growing file-API mutations are
  serialized per user within this process so concurrent uploads/copies cannot
  both pass the same preflight. Agent subprocesses and other gateway workers are
  outside that lock; this is a soft application quota, not a filesystem quota.

Security:
- Gateway API authentication (``API_KEY`` or ``USER_API_KEYS``) MUST be configured;
  otherwise ``verify_api_key`` is a no-op and these endpoints would expose every
  user's files unauthenticated, so we fail closed here.
- Every path (read AND write) is confined to the single workspace root via
  ``_resolve_or_403`` (``Path.resolve()`` collapses ``..`` and resolves symlinks
  before the containment check); anything above/outside the root is a 403.
  Uploaded filenames are reduced to a basename. No extra roots (never
  ``~/.claude`` etc.).
- A path check alone races the agent and the terminal, which keep changing the
  workspace: a file or a directory above it swapped for a symlink after
  ``_resolve_or_403`` would be followed out of the workspace by a later
  open-by-path. So no route opens, creates, renames or deletes a workspace path
  by its full path. Each pins the resolved root as an ``O_NOFOLLOW`` directory
  fd and works from it one component at a time with ``O_NOFOLLOW``
  (``_open_file_beneath``, ``_parent_beneath``, ``*at`` syscalls via
  ``dir_fd=``); a symlink met on the way is a swap and is refused (404 for a
  read, 409 for a write), and a non-regular file is refused without blocking.
  ``/files/archive`` reads its members the same way, from the producer thread,
  with the root's identity checked against the walk.
"""

import asyncio
import concurrent.futures
import ctypes
import errno
import fcntl
import hashlib
import logging
import mimetypes
import os
import shutil
import stat as stat_module
import threading
import time
import uuid
import weakref
import zipfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette.background import BackgroundTask
from pydantic import BaseModel

from src.auth import auth_manager, security, verify_api_key
from src.runtime_config import get_workspace_upload_max_bytes
from src.workspace_manager import workspace_manager
from src.workspace_quota import (
    WorkspaceQuotaExceeded,
    copy_growth_bytes,
    ensure_growth_fits,
    quota_snapshot,
    workspace_quota_limit_bytes,
)

logger = logging.getLogger(__name__)


class _PathBody(BaseModel):
    path: str


class _MoveBody(BaseModel):
    source: str
    destination: str
    # Opt-in atomic no-overwrite for move: the default (False) keeps the
    # historical FileNav contract where move silently replaces the
    # destination. A file manager that resolves name conflicts client-side
    # sets True so a file appearing between its listing and the move gets a
    # 409 instead of being clobbered (copy already refuses unconditionally).
    no_clobber: bool = False


class _ArchiveBody(BaseModel):
    paths: List[str]


router = APIRouter(tags=["workspace-files"])

_BACKEND = "claude"
# Cap in-band text previews; larger files must be fetched via /files/view.
_MAX_READ_BYTES = 5 * 1024 * 1024

# Name of the inbound header carrying the user identity. Kept generic and
# configurable (no hard dependency on the caller's product) — the frontend just
# has to forward the user's identity under this name. Its value keys the workspace
# WHOLE, matching how ``/v1/responses`` keys ``body.user``. Default is deliberately
# vendor-neutral.
_DEFAULT_USER_HEADER = "X-User-Email"

# Per-process serialization for quota-increasing file API mutations. This does
# not turn the application quota into an OS/filesystem hard quota.
#
# Entries are reference-counted rather than cached: a gateway serves an unbounded
# set of named users over its lifetime, so a plain dict keyed by user root only
# ever grows. Counting is exact instead of policy-based (LRU, TTL) because the
# only unsafe eviction is removing a lock someone still holds or awaits, and a
# refcount answers that question directly — no tuning, and no window where two
# callers hold different lock objects for the same user.
_QUOTA_LOCKS: dict[str, "_QuotaLockEntry"] = {}


@dataclass
class _QuotaLockEntry:
    """One user's mutation lock plus the number of callers holding or awaiting it."""

    lock: asyncio.Lock
    users: int = 0


# Fail closed when the quota scope cannot be determined: charging a user for a
# root we cannot identify is worse than refusing the quota-dependent call.
_QUOTA_SCOPE_UNAVAILABLE = {
    "error": {
        "message": "Workspace storage quota scope could not be determined.",
        "type": "service_unavailable",
        "code": "workspace_quota_accounting_unavailable",
    }
}


def _max_upload_bytes() -> int:
    """Largest single file ``POST /files/upload`` will actually accept.

    The runtime-config value is the single source of truth. The ASGI request
    boundary uses that same value plus multipart envelope room, so this route,
    ``/files/limits`` and an admin-edited limit cannot drift apart.

    ``0`` is a real answer, not a failure: it means this deployment accepts no
    workspace uploads at all, and saying so is the point of publishing the
    number. A client that sizes its picker against ``/files/limits`` reports
    "uploads unavailable" instead of offering a control whose every use ends in
    a 413.
    """
    return max(0, get_workspace_upload_max_bytes())


def _user_header() -> str:
    return os.getenv("WORKSPACE_USER_HEADER", _DEFAULT_USER_HEADER)


def _hide_dotfiles() -> bool:
    """When true, dot-prefixed entries are neither listed nor accessible.

    Defaults to **false**: hiding dotfiles protects nothing here — the agent
    itself reads and writes them freely through Bash/Read within the same
    workspace, and the real guards are gateway API auth plus root confinement.
    What it *did* do was make the workspace's agent-resource directories
    unreachable over ``/files/*`` (a 404 on any dot-prefixed component,
    including writes), which silently breaks clients that install skills/subagents
    through this API. Hiding is presentation, so it belongs to the client that
    renders the tree — see the Finder-style "show hidden items" toggle in
    ChatDRAGON's files panel. Set this to ``true`` to restore server-side hiding
    for a deployment that wants it.
    """
    return os.getenv("WORKSPACE_HIDE_DOTFILES", "false").strip().lower() == "true"


def _hide_claude_prefix() -> bool:
    """Hide workspace path components whose names start with ``.claude``.

    This is intentionally narrower than ``WORKSPACE_HIDE_DOTFILES``: deployments
    can keep ordinary dotfiles visible/editable in the file manager while keeping
    Claude-owned/project-scoped paths such as ``.claude`` and ``.claude_images``
    out of that surface. In particular it hides the gateway's managed
    ``.claude/{skills,agents}`` mirror without touching the canonical
    ``skills/``/``agents/`` roots the user edits. The agent process itself is
    unaffected — it reads the workspace directly, not through this API.

    Prefix, not exact match: the sibling paths a deployment wants gone
    (``.claude_images``, ``.claude-local``) are not under ``.claude/``.
    """
    return os.getenv("WORKSPACE_HIDE_CLAUDE_PREFIX", "false").strip().lower() == "true"


def _hidden_name(name: str, *, hide_dotfiles: bool, hide_claude_prefix: bool) -> bool:
    """Whether one path component is hidden by the current workspace policy."""
    return (hide_dotfiles and name.startswith(".")) or (
        hide_claude_prefix and name.startswith(".claude")
    )


def _hidden_relative_path(
    relative: Path, *, hide_dotfiles: bool, hide_claude_prefix: bool
) -> bool:
    """Whether any component of a workspace-relative path is hidden."""
    return any(
        _hidden_name(
            part,
            hide_dotfiles=hide_dotfiles,
            hide_claude_prefix=hide_claude_prefix,
        )
        for part in relative.parts
    )


def _ensure_api_key() -> None:
    """Fail closed unless gateway API auth is configured."""
    if not auth_manager.has_api_auth():
        raise HTTPException(
            status_code=503,
            detail="workspace file browser is disabled: gateway API auth is not configured",
        )


def _legacy_localpart_key() -> bool:
    """Opt back into the pre-fix localpart workspace key (migration only).

    Truncating the identity at ``@`` maps every principal sharing a localpart onto
    one workspace, so this is a known cross-user isolation hole. It stays reachable
    only so an existing deployment can stage a directory migration, and it says so
    on every resolve rather than failing quietly into the old behaviour.
    """
    if os.getenv("WORKSPACE_LEGACY_LOCALPART_KEY", "").strip().lower() != "true":
        return False
    logger.warning(
        "WORKSPACE_LEGACY_LOCALPART_KEY=true: workspaces are keyed on the identity "
        "localpart, so callers sharing one localpart share one workspace"
    )
    return True


def _workspace_key(request: Request) -> str:
    """The caller's identity as the workspace key ("" when the header is absent)."""
    identity = (request.headers.get(_user_header()) or "").strip()
    if _legacy_localpart_key():
        return identity.split("@")[0]
    return identity


def _require_user(request: Request) -> str:
    user = _workspace_key(request)
    if not user:
        raise HTTPException(status_code=400, detail="missing user identity header")
    return user


def _workspace_root(user: str) -> Path:
    try:
        return workspace_manager.resolve(user, backend=_BACKEND)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid user identity")


def _user_root(workspace_root: Path) -> Path:
    """Aggregate quota root for a named user (parent of the backend directory).

    The shape is verified rather than assumed. ``workspace_manager.resolve``
    returns ``<base>/<user>/<backend-dir>`` only while a backend name is passed;
    without one it returns ``<base>/<user>``, and this function's ``parent``
    would then be the shared base holding **every** user's workspace. Quota would
    silently become a global figure charged to each user individually — a wrong
    answer that no test would notice because the numbers stay plausible.

    Today ``_BACKEND`` is a module constant so that cannot happen, which is
    exactly why the coupling deserves a check rather than a comment: the failure
    arrives whenever someone changes the caller, not when they change this line.
    The agent-side hook (``workspace_sandbox._quota_user_root``) already
    validates the same shape; this keeps the HTTP mutation path from being the
    weaker of the two.
    """
    resolved = Path(workspace_root).resolve()
    try:
        relative = resolved.relative_to(workspace_manager.base_path.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail=_QUOTA_SCOPE_UNAVAILABLE)
    if len(relative.parts) != 2 or relative.parts[0].startswith("_tmp_"):
        raise HTTPException(status_code=503, detail=_QUOTA_SCOPE_UNAVAILABLE)
    return resolved.parent


@asynccontextmanager
async def _quota_lock(user_root: Path):
    """Hold this user's quota-mutation lock, dropping the entry when idle.

    The reference count is taken **before** awaiting the lock, so a second
    caller arriving while the first holds it finds the same entry and shares the
    same lock — the entry can only be removed once nobody is inside or waiting.
    Everything here runs on one event loop and no ``await`` sits between the
    lookup and the increment, so the count cannot be observed mid-update.
    """
    key = str(user_root.resolve())
    entry = _QUOTA_LOCKS.get(key)
    if entry is None:
        entry = _QuotaLockEntry(lock=asyncio.Lock())
        _QUOTA_LOCKS[key] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        # The identity check matters if a test (or a future caller) cleared the
        # registry while this request was in flight: dropping a newer entry that
        # other callers are already sharing would split the lock in two.
        if entry.users <= 0 and _QUOTA_LOCKS.get(key) is entry:
            del _QUOTA_LOCKS[key]


def _raise_quota_http(exc: WorkspaceQuotaExceeded) -> None:
    raise HTTPException(status_code=507, detail=exc.as_detail())


def resolve_workspace_for_request(request: Request) -> Optional[Path]:
    """The caller's workspace directory, or ``None`` when it can't be keyed.

    Same identity header and workspace mapping as the file browser, but soft:
    endpoints that merely *describe* a workspace (e.g. the agent-resource
    catalog) should degrade to "no project scope" rather than 400 when the
    header is absent. Never creates the directory.
    """
    user = _workspace_key(request)
    if not user:
        return None
    try:
        return workspace_manager.resolve(user, backend=_BACKEND)
    except ValueError:
        return None


def _is_under(path: Path, base: Path) -> bool:
    """True when *path* is *base* or nested inside it."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _resolve_in_root(root: Path, rel: str) -> Optional[Path]:
    """Resolve *rel* and confine it to *root*; ``None`` if it escapes the root.

    ``Path.resolve()`` collapses ``..`` and follows symlinks, so an escape via
    either is caught by the containment check. This is containment only —
    dotfile hiding is applied separately so callers can distinguish "outside
    your workspace" (403) from "hidden/not found" (404).

    The explorer echoes the real cwd, so most paths arrive absolute and under
    the root. An absolute path that is an *ancestor* of the root (breadcrumb
    navigation above the workspace) is rejected outright; any other stray
    leading-slash path is reinterpreted as workspace-relative.
    """
    try:
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    p = rel or "/"
    if p in ("/", ""):
        return root_resolved  # workspace root (virtual "/")
    try:
        candidate = Path(p)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not _is_under(resolved, root_resolved):
                if _is_under(root_resolved, resolved):
                    return None  # ancestor of the root -> above-workspace nav
                resolved = (root_resolved / p.lstrip("/")).resolve()
        else:
            resolved = (root_resolved / p).resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if _is_under(resolved, root_resolved) else None


def _lexical_relative_path(root: Path, rel: str) -> Path:
    """Return the requested workspace-relative path without resolving symlinks.

    Hidden-path policy is lexical as well as target-based.  A request such as
    ``/.claude_link/file`` must not become visible merely because
    ``.claude_link`` resolves to a non-hidden directory inside the workspace.

    Absolute paths below the real workspace root are made relative to that root.
    Other leading-slash paths use the same virtual-root interpretation as
    ``_resolve_in_root``.  The caller still performs resolved containment
    separately; this helper is policy input, not a security boundary.
    """
    p = rel or "/"
    if p in ("/", ""):
        return Path(".")
    candidate = Path(p)
    if not candidate.is_absolute():
        return candidate
    try:
        return candidate.relative_to(root.resolve())
    except ValueError:
        return Path(p.lstrip("/"))


def _resolve_or_403(root: Path, rel: str) -> Path:
    """Resolve within the workspace root or raise.

    - Outside the root -> **403** ("outside your workspace"), so the explorer can
      warn the user that navigation there isn't allowed.
    - A component hidden by workspace policy -> **404**, so hidden entries stay
      invisible rather than advertising that something is blocked.

    The returned path may not exist yet (callers that create paths check as
    needed); callers reading/listing must still verify existence.
    """
    target = _resolve_in_root(root, rel)
    if target is None:
        raise HTTPException(
            status_code=403,
            detail="Access denied: this path is outside your workspace.",
        )
    hide_dot = _hide_dotfiles()
    hide_claude = _hide_claude_prefix()
    if hide_dot or hide_claude:
        requested_relative = _lexical_relative_path(root, rel)
        resolved_relative = target.relative_to(root.resolve())
        if _hidden_relative_path(
            requested_relative,
            hide_dotfiles=hide_dot,
            hide_claude_prefix=hide_claude,
        ) or _hidden_relative_path(
            resolved_relative,
            hide_dotfiles=hide_dot,
            hide_claude_prefix=hide_claude,
        ):
            raise HTTPException(status_code=404, detail="not found")
    return target


# --- opening workspace paths without following a swapped symlink -------------
# ``_resolve_or_403`` checks a path, but the agent and the terminal keep
# changing the workspace while a request runs: a file or a directory above it
# swapped for a symlink after the check would make a later open-by-path follow
# it out of the workspace. So the routes never open, create, rename or delete a
# workspace path by its full path. They pin the workspace root as a directory fd
# and descend from it one component at a time with ``O_NOFOLLOW``, using the
# components of the already-resolved path (a legitimate in-workspace symlink was
# resolved away by ``_resolve_or_403``, so any symlink met on the way down is a
# swap and is refused with ``_PathChanged``).


class _PathChanged(Exception):
    """A path stopped leading to what was checked: a symlink, or another type."""


# Kept for the archive code and its tests.
_UnsafeArchiveMember = _PathChanged

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: a file swapped for a FIFO must not park a worker in open(); it is
# refused by the S_ISREG check right after.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# What openat() reports when O_NOFOLLOW meets a symlink, or a directory on the
# way down is no longer a directory.
_SWAPPED_ERRNOS = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)


def _pin_root(root: Path, identity: Optional[tuple] = None) -> int:
    """Open the (already resolved) workspace root as a directory fd.

    ``O_NOFOLLOW`` refuses a root that became a symlink since it was resolved;
    ``identity`` (``(st_dev, st_ino)`` taken earlier) refuses one that was
    replaced by another directory.
    """
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in _SWAPPED_ERRNOS:
            raise _PathChanged("workspace root changed during the request") from exc
        raise
    try:
        st = os.fstat(fd)
        if identity is not None and (st.st_dev, st.st_ino) != identity:
            raise _PathChanged("workspace root changed during the request")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _pinned_root(root: Path, target: Path):
    """Yield ``(root_fd, parts)``: the pinned root and ``target``'s components.

    ``target`` is a ``_resolve_or_403`` result, i.e. a real path under the root.
    """
    real_root = root.resolve()
    try:
        parts = list(target.relative_to(real_root).parts)
    except ValueError as exc:
        raise _PathChanged("workspace root changed during the request") from exc
    fd = _pin_root(real_root)
    try:
        yield fd, parts
    finally:
        os.close(fd)


def _openat_checked(name: str, flags: int, dir_fd: int, mode: int = 0o777) -> int:
    try:
        return os.open(name, flags, mode, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in _SWAPPED_ERRNOS:
            raise _PathChanged(name) from exc
        raise


def _open_dir_beneath(root_fd: int, parts: List[str]) -> int:
    """A new fd for the directory ``parts`` beneath ``root_fd``; caller closes."""
    fd = os.dup(root_fd)
    try:
        for name in parts:
            next_fd = _openat_checked(name, _DIR_FLAGS, fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_file_beneath(root_fd: int, parts: List[str]):
    """Open the regular file ``parts`` beneath ``root_fd`` for reading.

    Returns ``(file, stat)``. Raises ``_PathChanged`` for a symlink anywhere on
    the way or a non-regular file, ``FileNotFoundError`` when something is gone.
    """
    if not parts:
        raise _PathChanged("not a file")
    dir_fd = _open_dir_beneath(root_fd, parts[:-1])
    try:
        fd = _openat_checked(parts[-1], _FILE_FLAGS, dir_fd)
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise _PathChanged(parts[-1])
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb"), st
    except BaseException:
        os.close(fd)
        raise


def _read_beneath(root: Path, target: Path):
    """``_open_file_beneath`` for a request: ``(file, stat)`` or a 404."""
    try:
        with _pinned_root(root, target) as (root_fd, parts):
            return _open_file_beneath(root_fd, parts)
    except (_PathChanged, FileNotFoundError):
        raise HTTPException(status_code=404, detail="file not found")


async def _file_response_beneath(root: Path, target: Path, **kwargs) -> FileResponse:
    """A ``FileResponse`` for the file opened by ``_read_beneath``.

    ``FileResponse`` opens its path again when it sends (and per Range request).
    Pointing it at ``/proc/self/fd/<n>`` re-opens the already-checked open file
    instead of whatever the workspace path names by then; the descriptor is
    closed once the response is done.
    """
    fh, st = await run_in_threadpool(_read_beneath, root, target)
    try:
        return FileResponse(
            f"/proc/self/fd/{fh.fileno()}",
            stat_result=st,
            background=BackgroundTask(fh.close),
            **kwargs,
        )
    except BaseException:
        fh.close()
        raise


def _path_changed_409() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="the path changed while the request ran; try again",
    )


@contextmanager
def _parent_beneath(root: Path, target: Path):
    """Yield ``(dir_fd, name)``: ``target``'s directory opened beneath the root.

    Every write route creates, renames or deletes ``name`` relative to this fd
    (``dir_fd=``), never through the full path.
    """
    with _pinned_root(root, target) as (root_fd, parts):
        if not parts:
            raise _PathChanged("the workspace root itself")
        dir_fd = _open_dir_beneath(root_fd, parts[:-1])
    try:
        yield dir_fd, parts[-1]
    finally:
        os.close(dir_fd)


def _open_new_or_regular(dir_fd: int, name: str, flags: int) -> int:
    """``openat`` for writing that refuses symlinks, FIFOs and devices."""
    try:
        fd = _openat_checked(
            name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd, 0o666
        )
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # a FIFO without a reader, a socket
            raise _PathChanged(name) from exc
        raise
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise _PathChanged(name)
        os.set_blocking(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


@router.get("/api/config")
async def terminal_config(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Handshake: advertise a read-only, no-terminal file server."""
    await verify_api_key(request, credentials)
    _ensure_api_key()
    return {"features": {"terminal": False}}


@router.get("/files/openapi.json")
async def tool_specs(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Empty OpenAPI so open-webui exposes ZERO LLM tools for this connection.

    A terminal connection's ``path`` (default ``/openapi.json``) is fetched by
    open-webui, and every operation in it becomes an LLM-callable tool (whose
    callables it then builds — and can fail to serialize with a manifold/pipe
    model). This gateway is a file *browser*, not a tool provider — point the
    connection ``path`` here so no tools are built. The FileNav sidebar calls
    ``/files/*`` directly and is unaffected.
    """
    await verify_api_key(request, credentials)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Oh My Gateway Workspace Files", "version": "1.0.0"},
        "paths": {},
    }


@router.get("/files/limits")
async def get_limits(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Publish upload and cumulative workspace limits for file-manager clients."""
    await verify_api_key(request, credentials)
    _ensure_api_key()
    return {
        "max_upload_bytes": _max_upload_bytes(),
        "workspace_quota_bytes": workspace_quota_limit_bytes(),
    }


@router.get("/files/quota")
async def get_quota(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Return current aggregate quota usage for the authenticated named user.

    Usage is measured even when no limit is configured. "How much am I using?"
    is a real question without an enforced ceiling, and a client that renders a
    workspace needs the answer either way; reporting 0 to save a scan would
    publish a number that is simply wrong. The "no scan unless configured" rule
    belongs to the mutation paths (upload/copy), where the walk buys nothing.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    snapshot = await run_in_threadpool(quota_snapshot, _user_root(root))
    return snapshot.as_dict()


@router.get("/files/cwd")
async def get_cwd(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    # Report the real workspace path so the explorer's breadcrumb matches the
    # paths the agent uses (e.g. what MEMORY.md references).
    return {"cwd": str(root.resolve())}


@router.get("/files/list")
async def list_files(
    request: Request,
    directory: str = "/",
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, directory)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    hide_dot = _hide_dotfiles()
    hide_claude = _hide_claude_prefix()

    # Run the directory scan off the event loop: FileNav polls this endpoint
    # continuously across all users, and a synchronous scandir would stall
    # every other request (chat streams, terminal websockets) on the loop.
    def _scan() -> list:
        entries = []
        # Scan the directory fd reached beneath the root, not the path: a
        # directory swapped for a symlink after the check is refused (404)
        # instead of listing wherever it points.
        try:
            with _pinned_root(root, target) as (root_fd, parts):
                dir_fd = _open_dir_beneath(root_fd, parts)
        except (_PathChanged, FileNotFoundError):
            raise HTTPException(status_code=404, detail="directory not found")
        try:
            with os.scandir(dir_fd) as it:
                for entry in it:
                    if _hidden_name(
                        entry.name,
                        hide_dotfiles=hide_dot,
                        hide_claude_prefix=hide_claude,
                    ):
                        continue
                    try:
                        st = entry.stat()  # follow symlinks; broken links are skipped
                    except OSError:
                        continue
                    entries.append(
                        {
                            "name": entry.name,
                            "type": (
                                "directory" if stat_module.S_ISDIR(st.st_mode) else "file"
                            ),
                            "size": st.st_size,
                            "modified": int(st.st_mtime),
                        }
                    )
        finally:
            os.close(dir_fd)
        entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
        return entries

    return {"entries": await run_in_threadpool(_scan)}


@router.get("/files/search")
async def search_files(
    request: Request,
    query: str = "",
    limit: int = 50,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Recursive filename search under the workspace root.

    Case-insensitive substring match on entry names. Hidden entries follow
    the same rule as listing (dot-prefixed components, or only names starting
    with ``.claude`` when that narrower switch is enabled, are pruned), symlinks
    are skipped like the archive walk, and results are capped
    at ``limit`` (1-200) with a ``truncated`` flag. Name-prefix matches sort
    before substring matches, shallower paths before deeper ones.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    root_resolved = root.resolve()

    q = query.strip().lower()
    if not q:
        return {"results": [], "truncated": False}
    limit = max(1, min(limit, 200))

    hide_dot = _hide_dotfiles()
    hide_claude = _hide_claude_prefix()
    _SCAN_CAP = 1000  # stop collecting beyond this many matches

    # The recursive walk is the most expensive scan this router does — run it
    # in the threadpool like the other filesystem work so it cannot stall the
    # event loop.
    def _search() -> dict:
        matches: List[dict] = []
        scan_capped = False

        for dirpath, dirnames, filenames in os.walk(root_resolved):
            if hide_dot or hide_claude:
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not _hidden_name(
                        d,
                        hide_dotfiles=hide_dot,
                        hide_claude_prefix=hide_claude,
                    )
                ]
            dirnames.sort()
            base = Path(dirpath)
            candidates = [(d, True) for d in dirnames] + [
                (f, False) for f in sorted(filenames)
            ]
            for name, is_dir in candidates:
                if _hidden_name(
                    name,
                    hide_dotfiles=hide_dot,
                    hide_claude_prefix=hide_claude,
                ):
                    continue
                if q not in name.lower():
                    continue
                p = base / name
                if p.is_symlink():
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                matches.append(
                    {
                        "path": str(p),
                        "name": name,
                        "type": "directory" if is_dir else "file",
                        "size": st.st_size,
                        "modified": int(st.st_mtime),
                    }
                )
                if len(matches) >= _SCAN_CAP:
                    scan_capped = True
                    break
            if scan_capped:
                break

        matches.sort(
            key=lambda e: (
                not e["name"].lower().startswith(q),
                e["path"].count("/"),
                e["name"].lower(),
            )
        )
        truncated = scan_capped or len(matches) > limit
        return {"results": matches[:limit], "truncated": truncated}

    return await run_in_threadpool(_search)


@router.get("/files/digest")
async def file_digest(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Content identity for one file: equality of ``sha256`` means equal bytes.

    ``modified``/``st_mtime`` cannot carry this, at any resolution. A timestamp
    says when a write happened, not what the bytes are: filesystem granularity
    can coalesce two writes, and a metadata-preserving writer can restore an old
    value with ``os.utime``. A caller that pins a file's revision — ChatDRAGON
    pins chat attachments so a later overwrite cannot be accepted as the evidence
    an earlier turn read — needs equality to actually imply sameness, so it needs
    the content.

    This deliberately does NOT live on ``/files/list`` or ``/files/search``:
    hashing every entry would make a directory listing cost the size of the
    directory. It is asked for one file at a time, at the moment a caller pins
    or re-checks that file.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    def _digest() -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
        # Streamed: pinning a revision must not depend on the file fitting in memory.
        fh, _ = _read_beneath(root, target)
        with fh:
            while chunk := fh.read(1024 * 1024):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    digest, size = await run_in_threadpool(_digest)
    return {"path": path, "size": size, "sha256": digest}


@router.get("/files/read")
async def read_file(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    def _read() -> bytes:
        fh, st = _read_beneath(root, target)
        with fh:
            if st.st_size > _MAX_READ_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"file too large to preview (> {_MAX_READ_BYTES} bytes); "
                        "use download"
                    ),
                )
            return fh.read(_MAX_READ_BYTES + 1)

    data = await run_in_threadpool(_read)
    if len(data) > _MAX_READ_BYTES:  # grew after the fstat
        raise HTTPException(
            status_code=413,
            detail=f"file too large to preview (> {_MAX_READ_BYTES} bytes); use download",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Binary: return raw bytes so FileNav renders a preview / placeholder.
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(content=data, media_type=media)

    return {"path": path, "total_lines": text.count("\n") + 1, "content": text}


@router.get("/files/view")
async def view_file(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return await _file_response_beneath(
        root, target, media_type=media, filename=target.name
    )


@router.get("/files/serve/{path:path}")
async def serve_file(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Serve a file inline for in-browser preview.

    FileNav previews HTML documents via ``<iframe src=".../files/serve/<path>">``
    (path-based, unlike the query-based ``/files/view`` download endpoint) so
    that relative references inside the document — ``./style.css``, images,
    scripts — resolve to sibling files through this same route. The leading
    slash of the absolute workspace path is consumed by the URL, so re-anchor
    before resolving; confinement and dotfile hiding are the same as every
    other endpoint.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, path if path.startswith("/") else f"/{path}")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return await _file_response_beneath(
        root, target, media_type=media, content_disposition_type="inline"
    )


# ---------------------------------------------------------------------------
# Write operations (all confined to the workspace root by _resolve_or_403)
# ---------------------------------------------------------------------------


@router.post("/files/cwd")
async def set_cwd(
    request: Request,
    body: _PathBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Validate a directory; cwd itself is tracked client-side."""
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, body.path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")
    return {"cwd": body.path}


async def _write_uploaded_file(
    root: Path, target: Path, data: bytes, no_clobber: bool
) -> None:
    """Preserve the historical upload write/no-clobber semantics.

    The file is created (``O_EXCL`` for no-clobber) or truncated beneath the
    pinned root, so a destination or a directory above it swapped for a
    symlink is refused rather than written through.
    """

    def _write() -> None:
        flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if no_clobber else 0)
        with _parent_beneath(root, target) as (dir_fd, name):
            fd = _open_new_or_regular(dir_fd, name, flags)
        try:
            if not no_clobber:
                os.ftruncate(fd, 0)
            _write_all(fd, data)
        finally:
            os.close(fd)

    try:
        await run_in_threadpool(_write)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="destination already exists")
    except _PathChanged:
        raise _path_changed_409()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="directory not found")


@router.post("/files/upload")
async def upload_file(
    request: Request,
    directory: str = "/",
    no_clobber: bool = False,
    file: UploadFile = File(...),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Write one uploaded file into the workspace.

    Default keeps the historical contract: same name overwrites (this is how
    "save" works through the proxy chain). ``no_clobber=true`` is the opt-in
    for file managers dropping OS files: creation is O_EXCL, so a destination
    appearing after the client's listing gets a 409 instead of being replaced
    — same contract as ``/files/copy`` and move's ``no_clobber``.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    dest_dir = _resolve_or_403(root, directory)
    if not dest_dir.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    # Reduce the client filename to a basename so it can't carry path segments.
    name = os.path.basename(file.filename or "")
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = _resolve_or_403(root, f"{directory}/{name}")

    data = await file.read()
    # Defence in depth against the request boundary: middleware bounds the raw
    # multipart request with this same runtime file ceiling plus envelope room;
    # this final check measures the actual file bytes, so boundary and route
    # cannot disagree about the operator-configured limit.
    ceiling = _max_upload_bytes()
    if ceiling == 0 or len(data) > ceiling:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the upload limit of {ceiling} bytes",
        )

    # Keep quota-disabled deployments on the exact historical mutation path:
    # no extra tree scan, lock, or threadpool hop.
    if workspace_quota_limit_bytes() <= 0:
        await _write_uploaded_file(root, target, data, no_clobber)
        return {"path": str(target), "size": len(data)}

    user_root = _user_root(root)
    async with _quota_lock(user_root):
        if no_clobber and target.exists():
            # Preserve the historical conflict contract before quota accounting:
            # an already-existing destination is a 409, not a quota-dependent 507.
            # O_EXCL in _write_uploaded_file remains the race-proof final check if
            # the destination appears after this fast path.
            raise HTTPException(status_code=409, detail="destination already exists")

        reclaimed = 0
        if not no_clobber:
            try:
                if target.is_file():
                    reclaimed = target.stat().st_size
            except OSError:
                reclaimed = 0
        try:
            await run_in_threadpool(
                lambda: ensure_growth_fits(
                    user_root,
                    added_bytes=len(data),
                    reclaimed_bytes=reclaimed,
                )
            )
        except WorkspaceQuotaExceeded as exc:
            _raise_quota_http(exc)
        await _write_uploaded_file(root, target, data, no_clobber)
    return {"path": str(target), "size": len(data)}


# --- conditional write (ChatDRAGON #213) --------------------------------------
# The workspace is written by more than this API: the agent's tools and the
# terminal write files directly, and the gateway may run as several processes.
# A client saving a draft it started from an older body must never silently
# replace a change made underneath it. ``POST /files/write_if`` is a separate,
# versioned endpoint on purpose: a gateway that predates it answers 404 before
# touching anything, so a caller can fail closed instead of having an
# unconditional upload ignore an unknown parameter.
#
# Protocol, for ``expected_sha256`` (the SHA-256 of the body the draft started
# from):
#
# 1. A per-path ``flock`` on a lock file under the user root serialises every
#    conditional writer across gateway processes (and hosts sharing the
#    volume).
# 2. The new body is written to a hidden temp file beside the target.
# 3. Compare: the target must hash to the expected value.
# 4. Claim: ``rename(target -> .claim)`` atomically takes the current inode off
#    the path. The claimed bytes are hashed again -- a write that landed between
#    the compare and the claim is detected here, and the claimed file is put
#    back (or, if the path was already recreated, kept as a visible conflict
#    copy) -> 409.
# 5. Install: ``link(tmp -> target)`` never replaces an existing path, so a
#    writer that recreated the path after the claim wins and we answer 409.
# 6. A writer that opened the file before the claim and keeps writing through
#    its descriptor writes into the claimed inode; if the claimed bytes no
#    longer match after install, they are kept as a visible conflict copy and
#    the response names it. Nothing an external writer produced is discarded.
_CAS_LOCK_DIR = ".gateway-cas-locks"


class _WriteConflict(Exception):
    def __init__(self, exists: bool, preserved: Optional[Path] = None):
        super().__init__("conditional write conflict")
        self.exists = exists
        self.preserved = preserved


def _cas_after_compare(target: Path) -> None:
    """Test seam: right after the compare, before the claim."""


def _cas_before_install(target: Path) -> None:
    """Test seam: after the claim, right before the new body is linked in."""


_NOT_REGULAR = "not-a-regular-file"


def _sha256_at(dir_fd: int, name: str) -> Optional[str]:
    """SHA-256 of ``name`` in ``dir_fd``; ``None`` if absent.

    Never follows a symlink and never blocks on a FIFO: anything but a regular
    file hashes to a value no expected digest can match.
    """
    digest = hashlib.sha256()
    try:
        fd = _openat_checked(name, _FILE_FLAGS, dir_fd)
    except FileNotFoundError:
        return None
    except (_PathChanged, OSError):
        return _NOT_REGULAR
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            return _NOT_REGULAR
        os.set_blocking(fd, True)
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _link_at(dir_fd: int, src: str, dst: str) -> None:
    os.link(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)


def _preserve_copy(dir_fd: int, claim: str, target: Path) -> Path:
    """Keep a claimed external version under a visible, non-clobbering name."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(100):
        tag = f".conflict-{stamp}" + (f"-{n}" if n else "")
        dest = target.with_name(f"{target.stem}{tag}{target.suffix}")
        try:
            _link_at(dir_fd, claim, dest.name)
        except FileExistsError:
            continue
        _unlink_quietly(dir_fd, claim)
        return dest
    raise OSError("could not preserve the conflicting version")


def _cas_write_locked(
    dir_fd: int, target: Path, data: bytes, expected: str
) -> Optional[Path]:
    """The conditional write, entirely relative to the target's directory fd."""
    name = target.name
    token = uuid.uuid4().hex
    tmp = f".{name}.cas-{token}.tmp"
    claim = f".{name}.cas-{token}.claim"
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o666,
        dir_fd=dir_fd,
    )
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)
    try:
        current = _sha256_at(dir_fd, name)
        if current != expected:
            raise _WriteConflict(exists=current is not None)
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
            os.chmod(tmp, stat_module.S_IMODE(mode), dir_fd=dir_fd)
        except OSError:
            pass
        _cas_after_compare(target)
        try:
            os.rename(name, claim, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileNotFoundError:
            raise _WriteConflict(exists=False)
        if _sha256_at(dir_fd, claim) != expected:
            # Changed between the compare and the claim: put the external
            # version back where it was.
            try:
                _link_at(dir_fd, claim, name)
            except FileExistsError:
                raise _WriteConflict(
                    exists=True, preserved=_preserve_copy(dir_fd, claim, target)
                )
            _unlink_quietly(dir_fd, claim)
            raise _WriteConflict(exists=True)
        _cas_before_install(target)
        try:
            _link_at(dir_fd, tmp, name)
        except FileExistsError:
            # Recreated after the claim: that writer wins. The claimed inode is
            # the expected version unless a descriptor kept writing into it.
            if _sha256_at(dir_fd, claim) != expected:
                raise _WriteConflict(
                    exists=True, preserved=_preserve_copy(dir_fd, claim, target)
                )
            _unlink_quietly(dir_fd, claim)
            raise _WriteConflict(exists=True)
        if _sha256_at(dir_fd, claim) != expected:
            return _preserve_copy(dir_fd, claim, target)
        _unlink_quietly(dir_fd, claim)
        return None
    finally:
        _unlink_quietly(dir_fd, tmp)
        if _exists_at(dir_fd, claim) and not _exists_at(dir_fd, name):
            # An unexpected error between claim and install: never leave the
            # path empty because of us. (A claim is otherwise never deleted on
            # an error path -- losing nothing beats tidiness.)
            try:
                _link_at(dir_fd, claim, name)
                _unlink_quietly(dir_fd, claim)
            except FileExistsError:
                pass


def _cas_write(
    lock_root: Path,
    target: Path,
    data: bytes,
    expected: str,
    root: Optional[Path] = None,
) -> Optional[Path]:
    """Lock, then write relative to ``target``'s directory.

    With ``root`` (the route), that directory is reached beneath the pinned
    workspace root, so a directory swapped for a symlink is refused instead of
    written into; without it (direct callers) the directory is opened by path.
    """
    lock_dir = lock_root / _CAS_LOCK_DIR
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / (hashlib.sha256(str(target).encode()).hexdigest() + ".lock")
    with open(lock_path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if root is not None:
                with _parent_beneath(root, target) as (dir_fd, _):
                    return _cas_write_locked(dir_fd, target, data, expected)
            dir_fd = os.open(target.parent, _DIR_FLAGS)
            try:
                return _cas_write_locked(dir_fd, target, data, expected)
            finally:
                os.close(dir_fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@router.post("/files/write_if")
async def write_if_unchanged(
    request: Request,
    expected_sha256: str,
    directory: str = "/",
    file: UploadFile = File(...),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Replace one file only if it still hashes to ``expected_sha256``.

    409 ``{"code": "save_conflict", "exists", "preserved"}`` leaves the file
    as the other writer left it; a 200 may carry ``preserved`` when an
    external version written through an already-open descriptor was kept
    beside the file. See "conditional write" above for the protocol.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    dest_dir = _resolve_or_403(root, directory)
    if not dest_dir.is_dir():
        # The file's folder is gone, so the file the draft started from is gone:
        # that is a conflict, not a 404 -- callers read 404/405 from this route
        # as "this gateway has no conditional write" and fail closed.
        raise HTTPException(
            status_code=409,
            detail={"code": "save_conflict", "exists": False, "preserved": None},
        )
    name = os.path.basename(file.filename or "")
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = _resolve_or_403(root, f"{directory}/{name}")
    expected = expected_sha256.lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise HTTPException(status_code=400, detail="invalid expected_sha256")
    data = await file.read()
    ceiling = _max_upload_bytes()
    if ceiling == 0 or len(data) > ceiling:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the upload limit of {ceiling} bytes",
        )
    # Lock files live under the user root (beside the backend directories, not
    # inside the agent's workspace). A workspace outside the standard layout falls
    # back to its own root rather than refusing the save.
    try:
        lock_root = _user_root(root)
    except HTTPException:
        lock_root = root

    def _rel(path: Optional[Path]) -> Optional[str]:
        return "/" + str(path.relative_to(root)) if path is not None else None

    async def _write() -> Optional[Path]:
        try:
            return await run_in_threadpool(
                _cas_write, lock_root, target, data, expected, root
            )
        except _PathChanged:
            raise _path_changed_409()
        except _WriteConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "save_conflict",
                    "exists": exc.exists,
                    "preserved": _rel(exc.preserved),
                },
            )

    if workspace_quota_limit_bytes() <= 0:
        preserved = await _write()
    else:
        user_root = _user_root(root)
        async with _quota_lock(user_root):
            try:
                reclaimed = target.stat().st_size if target.is_file() else 0
            except OSError:
                reclaimed = 0
            try:
                await run_in_threadpool(
                    lambda: ensure_growth_fits(
                        user_root, added_bytes=len(data), reclaimed_bytes=reclaimed
                    )
                )
            except WorkspaceQuotaExceeded as exc:
                _raise_quota_http(exc)
            preserved = await _write()
    return {"path": str(target), "size": len(data), "preserved": _rel(preserved)}


@router.post("/files/mkdir")
async def make_dir(
    request: Request,
    body: _PathBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, body.path)
    if target == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")

    def _mkdirs() -> None:
        # mkdir -p one component at a time beneath the pinned root: a
        # component swapped for a symlink is refused, never created through.
        with _pinned_root(root, target) as (root_fd, parts):
            fd = os.dup(root_fd)
        try:
            for name in parts:
                try:
                    os.mkdir(name, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = _openat_checked(name, _DIR_FLAGS, fd)
                os.close(fd)
                fd = next_fd
        finally:
            os.close(fd)

    try:
        await run_in_threadpool(_mkdirs)
    except _PathChanged:
        raise _path_changed_409()
    return {"path": str(target)}


@router.delete("/files/delete")
async def delete_entry(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, path)
    if target == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")

    def _delete() -> bool:
        # Unlink/rmtree the entry relative to its directory fd beneath the
        # root, so a directory above it swapped for a symlink cannot redirect
        # the delete outside the workspace. rmtree(dir_fd=) is the fd-based,
        # symlink-safe walk.
        with _parent_beneath(root, target) as (dir_fd, name):
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat_module.S_ISDIR(st.st_mode):
                shutil.rmtree(name, dir_fd=dir_fd)
                return True
            os.unlink(name, dir_fd=dir_fd)
            return False

    # rmtree over a large workspace subtree can take seconds — keep it off the loop.
    try:
        is_dir = await run_in_threadpool(_delete)
    except _PathChanged:
        raise _path_changed_409()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    return {"path": path, "type": "directory" if is_dir else "file"}


@router.post("/files/move")
async def move_entry(
    request: Request,
    body: _MoveBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    src = _resolve_or_403(root, body.source)
    dst = _resolve_or_403(root, body.destination)
    if src == root.resolve() or dst == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not src.exists():
        raise HTTPException(status_code=404, detail="source not found")
    if not dst.parent.is_dir():
        raise HTTPException(status_code=404, detail="destination directory not found")
    # Same rule as copy: a directory cannot move into its own subtree. Without
    # this, the user error surfaces as renameat2's EINVAL (mapped to 501) or
    # shutil.Error (500) instead of a precise 400.
    if src.is_dir() and not src.is_symlink() and (dst == src or src in dst.parents):
        raise HTTPException(status_code=400, detail="cannot move a directory into itself")
    def _move() -> None:
        # Both ends are renamed relative to their directory fds beneath the
        # pinned root (renameat/renameat2), never through full paths: a
        # directory on either side swapped for a symlink cannot take the
        # entry, or the overwrite, outside the workspace.
        with _parent_beneath(root, src) as (src_dir, src_name), _parent_beneath(
            root, dst
        ) as (dst_dir, dst_name):
            if body.no_clobber:
                # No check-then-move: rename(2) replaces an existing destination
                # by design, so only the kernel can enforce no-replace
                # atomically. _rename_noreplace is renameat2(RENAME_NOREPLACE);
                # when the primitive is unavailable we fail closed (501)
                # instead of silently falling back to a replace-capable move.
                _rename_noreplace((src_dir, src_name), (dst_dir, dst_name))
                return
            _move_at(src_dir, src_name, dst_dir, dst_name, src, dst)

    try:
        await run_in_threadpool(_move)
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="atomic no-replace move is unavailable on this system",
        )
    except _PathChanged:
        raise _path_changed_409()
    except OSError as exc:
        if body.no_clobber and exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
            raise HTTPException(status_code=409, detail="destination already exists")
        raise
    return {"source": body.source, "destination": body.destination}


def _move_at(
    src_dir: int, src_name: str, dst_dir: int, dst_name: str, src: Path, dst: Path
) -> None:
    """``shutil.move`` semantics on directory fds.

    An existing destination directory receives the source inside it (unless it
    is the source itself); anything else is replaced by the rename. A
    cross-device move (a mount inside the workspace) keeps the historical
    ``shutil.move`` copy fallback.
    """
    src_st = os.stat(src_name, dir_fd=src_dir, follow_symlinks=False)
    try:
        dst_st = os.stat(dst_name, dir_fd=dst_dir, follow_symlinks=False)
    except FileNotFoundError:
        dst_st = None
    into = None
    if (
        dst_st is not None
        and stat_module.S_ISDIR(dst_st.st_mode)
        and (dst_st.st_dev, dst_st.st_ino) != (src_st.st_dev, src_st.st_ino)
    ):
        into = _openat_checked(dst_name, _DIR_FLAGS, dst_dir)
    try:
        if into is not None:
            try:
                os.stat(src_name, dir_fd=into, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise shutil.Error(
                    f"Destination path '{dst / src_name}' already exists"
                )
            target_dir, target_name = into, src_name
        else:
            target_dir, target_name = dst_dir, dst_name
        try:
            os.rename(
                src_name, target_name, src_dir_fd=src_dir, dst_dir_fd=target_dir
            )
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(str(src), str(dst))
    finally:
        if into is not None:
            os.close(into)


_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


def _rename_noreplace(src, dst) -> None:
    """Atomic no-replace rename via Linux ``renameat2(RENAME_NOREPLACE)``.

    ``os.rename``/``shutil.move`` replace an existing destination by design,
    so any exists-check followed by a move is a TOCTOU, however small the
    window. Raises ``NotImplementedError`` when the primitive cannot give the
    guarantee (missing symbol, unsupported filesystem, cross-device) — callers
    must fail closed, never fall back to a replace-capable move.

    Each side is a path, or a ``(dir_fd, name)`` pair resolved relative to
    that directory fd.
    """
    src_dir, src_name = src if isinstance(src, tuple) else (_AT_FDCWD, str(src))
    dst_dir, dst_name = dst if isinstance(dst, tuple) else (_AT_FDCWD, str(dst))
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as exc:  # pragma: no cover — non-Linux
        raise NotImplementedError("renameat2 unavailable") from exc
    ret = renameat2(
        ctypes.c_int(src_dir),
        os.fsencode(src_name),
        ctypes.c_int(dst_dir),
        os.fsencode(dst_name),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if ret != 0:
        err = ctypes.get_errno()
        if err in (errno.ENOSYS, errno.EINVAL, errno.EXDEV):
            raise NotImplementedError(os.strerror(err))
        raise OSError(err, os.strerror(err), src_name, None, dst_name)


class _UnsupportedSourceError(Exception):
    """Copy source is not a regular file (FIFO/socket/device) — refused."""


def _copy_meta(src_fd: int, dst_fd: int, st: os.stat_result) -> None:
    """``shutil.copystat`` between open descriptors: mode, times, xattrs."""
    os.fchmod(dst_fd, stat_module.S_IMODE(st.st_mode))
    for name in getattr(os, "listxattr", lambda _fd: [])(src_fd):
        try:
            os.setxattr(dst_fd, name, os.getxattr(src_fd, name))
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EACCES):
                raise
    os.utime(dst_fd, ns=(st.st_atime_ns, st.st_mtime_ns))


def _copy_file_at(src_dir: int, src_name: str, dst_dir: int, dst_name: str) -> None:
    """Copy a REGULAR file, failing with ``FileExistsError`` if ``dst`` exists.

    ``O_CREAT|O_EXCL`` makes the no-clobber promise a filesystem guarantee
    instead of a check-then-copy: a destination that appears after validation
    loses the race to us or we lose it to them, but nobody's file is
    overwritten either way.

    Both ends are opened relative to directory fds beneath the pinned root,
    with ``O_NOFOLLOW``. The source is opened with ``O_NONBLOCK`` and validated
    via ``fstat`` on the open fd: a plain open of a FIFO would block the worker
    until a writer appears (exhausting the shared threadpool), and checking the
    type before opening would just be another TOCTOU.
    """
    try:
        fd = _openat_checked(src_name, _FILE_FLAGS, src_dir)
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # e.g. a socket file
            raise _UnsupportedSourceError(src_name) from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise _UnsupportedSourceError(src_name)
        os.set_blocking(fd, True)
        out = os.open(
            dst_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o666,
            dir_fd=dst_dir,
        )
        try:
            while chunk := os.read(fd, 1024 * 1024):
                _write_all(out, chunk)
            _copy_meta(fd, out, st)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _copytree_at(src_fd: int, dst_dir: int, dst_name: str, errors: list) -> None:
    """``shutil.copytree(symlinks=True)`` between directory fds.

    The destination directory is created exclusively (an existing one is a
    ``FileExistsError``); symlinks are copied as links; every directory is
    entered with ``O_NOFOLLOW``, so a subdirectory swapped for a symlink during
    the copy is recorded as an error instead of being copied through. Entries
    that cannot be copied are collected in ``errors`` like ``copytree`` does.
    """
    os.mkdir(dst_name, 0o777, dir_fd=dst_dir)
    out_fd = _openat_checked(dst_name, _DIR_FLAGS, dst_dir)
    try:
        with os.scandir(src_fd) as it:
            names = [entry.name for entry in it]
        for name in names:
            try:
                st = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
                if stat_module.S_ISLNK(st.st_mode):
                    os.symlink(os.readlink(name, dir_fd=src_fd), name, dir_fd=out_fd)
                elif stat_module.S_ISDIR(st.st_mode):
                    sub = _openat_checked(name, _DIR_FLAGS, src_fd)
                    try:
                        _copytree_at(sub, out_fd, name, errors)
                    finally:
                        os.close(sub)
                else:
                    _copy_file_at(src_fd, name, out_fd, name)
            except (OSError, _PathChanged, _UnsupportedSourceError) as why:
                errors.append((name, name, str(why)))
        _copy_meta(src_fd, out_fd, os.fstat(src_fd))
    finally:
        os.close(out_fd)


def _copy_beneath(root: Path, src: Path, dst: Path, is_dir: bool) -> None:
    with _parent_beneath(root, src) as (src_dir, src_name), _parent_beneath(
        root, dst
    ) as (dst_dir, dst_name):
        if not is_dir:
            _copy_file_at(src_dir, src_name, dst_dir, dst_name)
            return
        src_fd = _openat_checked(src_name, _DIR_FLAGS, src_dir)
        try:
            errors: list = []
            _copytree_at(src_fd, dst_dir, dst_name, errors)
        finally:
            os.close(src_fd)
        if errors:
            raise shutil.Error(errors)


async def _perform_copy(root: Path, src: Path, dst: Path, is_dir: bool) -> None:
    """Perform the historical copy operation and preserve its error contract."""
    try:
        if is_dir:
            # Copying a directory into its own subtree would recurse forever.
            if dst == src or src in dst.parents:
                raise HTTPException(
                    status_code=400, detail="cannot copy a directory into itself"
                )
            # Links are copied as links instead of being followed — a link
            # inside the tree may point outside the workspace root, and
            # following it here would duplicate foreign content into the
            # workspace. The destination directory is created exclusively, so
            # one that appeared after validation is refused.
            await run_in_threadpool(_copy_beneath, root, src, dst, True)
        else:
            await run_in_threadpool(_copy_beneath, root, src, dst, False)
    except _PathChanged:
        raise _path_changed_409()
    except FileExistsError:
        raise HTTPException(status_code=409, detail="destination already exists")
    except _UnsupportedSourceError:
        raise HTTPException(status_code=400, detail="unsupported file type")
    except shutil.SpecialFileError:
        # copytree hit a named pipe inside the tree (shutil.copyfile refuses).
        raise HTTPException(
            status_code=400, detail="directory contains unsupported special files"
        )
    except shutil.Error:
        # copytree's aggregate error — some entries could not be copied
        # (special files, unreadable entries). Client-visible, not a 500.
        raise HTTPException(
            status_code=400, detail="directory contains entries that cannot be copied"
        )


@router.post("/files/copy")
async def copy_entry(
    request: Request,
    body: _MoveBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Duplicate a file or directory inside the workspace.

    Unlike ``move``, the destination must not already exist: the file-manager
    client resolves name conflicts itself (VS Code-style ``name copy.ext``), so
    a collision reaching this endpoint is a race we refuse rather than resolve
    by silently overwriting someone's file. The refusal is enforced at
    creation time (O_EXCL for files, ``copytree``'s exclusive mkdir for
    directories) — the early ``dst.exists()`` check below is only a fast path
    for a clear error message.
    """
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    src = _resolve_or_403(root, body.source)
    dst = _resolve_or_403(root, body.destination)
    if src == root.resolve() or dst == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not src.exists():
        raise HTTPException(status_code=404, detail="source not found")
    if dst.exists():
        raise HTTPException(status_code=409, detail="destination already exists")
    if not dst.parent.is_dir():
        raise HTTPException(status_code=404, detail="destination directory not found")
    is_dir = src.is_dir() and not src.is_symlink()
    # Preserve validation precedence before any quota/accounting work. An invalid
    # self/subtree directory copy is a deterministic 400 regardless of whether
    # cumulative quota is enabled or whether the workspace is near its limit.
    if is_dir and (dst == src or src in dst.parents):
        raise HTTPException(status_code=400, detail="cannot copy a directory into itself")
    # Only regular files and directories are copyable — a FIFO/socket/device
    # in the workspace must be a deterministic 4xx, not a blocked worker. This
    # is a fast path for the error message; the race-proof check is the fstat
    # on the opened fd inside _copy_file_at.
    if not is_dir and not src.is_file():
        raise HTTPException(status_code=400, detail="unsupported file type")

    # Quota-disabled deployments retain the exact historical path: no extra
    # scan, lock, or threadpool call before the copy.
    if workspace_quota_limit_bytes() <= 0:
        await _perform_copy(root, src, dst, is_dir)
    else:
        user_root = _user_root(root)
        async with _quota_lock(user_root):
            added_bytes = await run_in_threadpool(copy_growth_bytes, src)
            try:
                await run_in_threadpool(
                    lambda: ensure_growth_fits(user_root, added_bytes=added_bytes)
                )
            except WorkspaceQuotaExceeded as exc:
                _raise_quota_http(exc)
            await _perform_copy(root, src, dst, is_dir)

    return {
        "source": body.source,
        "destination": body.destination,
        "type": "directory" if is_dir else "file",
    }


# --- streaming zip (issue #188 stage 3) --------------------------------------
# ``/files/archive`` used to deflate the whole selection into one in-memory
# buffer, so gateway RSS grew with the archive. The zip is now produced by a
# worker thread writing to an unseekable sink (``zipfile`` then emits data
# descriptors instead of seeking back to patch local headers) and handed to the
# response a chunk at a time through a bounded queue: at most
# ``_ZIP_QUEUE_DEPTH`` chunks of ~``_ZIP_CHUNK_BYTES`` are ever buffered. A
# client disconnect sets the cancel flag; the producer checks it on every write
# and while waiting for queue space, so it stops promptly and the ``with
# open(...)`` inside ``ZipFile.write`` closes the file being read.
_ZIP_CHUNK_BYTES = 256 * 1024
_ZIP_QUEUE_DEPTH = 4
_ZIP_PUT_POLL_SECONDS = 0.1
_ZIP_DONE = object()


# --- archive admission control -----------------------------------------------
# Each streaming archive owns one producer thread plus up to
# ``_ZIP_QUEUE_DEPTH`` buffered chunks for as long as its consumer lingers, and
# that thread lives outside ``run_in_threadpool``'s capacity limiter. Without a
# cap, N slow (or never-read) archive connections pin N compression threads and
# ~N MiB of queue. A request therefore takes a slot -- one global, one per
# user -- after its validation succeeds and before any thread exists or any
# response byte is sent; over the limit it is refused with 429 and no thread.
#
# A slot is held by two parties: the response body (the async generator) and,
# once started, the producer thread. It frees when both have let go, so the
# number of live producer threads -- and of queues still holding chunks -- never
# exceeds the configured limits. Every party release is idempotent: the
# generator's ``finally``, the producer's ``finally``, and a ``weakref.finalize``
# on the generator (a body that is never iterated never runs its ``finally``).
_DEFAULT_ARCHIVE_MAX_CONCURRENT = 4
_DEFAULT_ARCHIVE_MAX_PER_USER = 2
_ARCHIVE_RETRY_AFTER_SECONDS = 5


def _positive_int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("%s=%r is not a positive integer; using %d", name, raw, default)
        return default
    return value


def _archive_max_concurrent() -> int:
    return _positive_int_env("ARCHIVE_MAX_CONCURRENT", _DEFAULT_ARCHIVE_MAX_CONCURRENT)


def _archive_max_per_user() -> int:
    return _positive_int_env("ARCHIVE_MAX_PER_USER", _DEFAULT_ARCHIVE_MAX_PER_USER)


class _ArchiveSlotParty:
    """One holder's claim on a slot; releasing it twice is a no-op."""

    def __init__(self, slot: "_ArchiveSlot"):
        self._slot = slot
        self._released = False

    def release(self) -> None:
        self._slot._drop_party(self)


class _ArchiveSlot:
    """A granted admission. Returns itself to the pool when every party is gone."""

    def __init__(self, admission: "_ArchiveAdmission", user: str):
        self._admission = admission
        self._user = user
        self._parties: set = set()
        self._freed = False

    def party(self) -> _ArchiveSlotParty:
        with self._admission._lock:
            if self._freed:
                raise RuntimeError("archive slot already released")
            p = _ArchiveSlotParty(self)
            self._parties.add(p)
            return p

    @property
    def freed(self) -> bool:
        return self._freed

    def _drop_party(self, p: _ArchiveSlotParty) -> None:
        with self._admission._lock:
            if p._released:
                return
            p._released = True
            self._parties.discard(p)
            if self._parties or self._freed:
                return
            self._freed = True
            self._admission._free_locked(self._user)


class _ArchiveAdmission:
    """Global + per-user counters of archive streams currently holding a slot."""

    def __init__(self) -> None:
        # A threading lock, not an asyncio one: producer threads and GC
        # finalizers release slots off the event loop.
        self._lock = threading.Lock()
        self._total = 0
        self._per_user: dict[str, int] = {}

    def try_acquire(self, user: str):
        """Return ``(slot, first_party)`` or raise a 429 ``HTTPException``."""
        max_total = _archive_max_concurrent()
        max_user = _archive_max_per_user()
        with self._lock:
            if self._per_user.get(user, 0) >= max_user:
                scope, limit = "user", max_user
            elif self._total >= max_total:
                scope, limit = "global", max_total
            else:
                self._total += 1
                self._per_user[user] = self._per_user.get(user, 0) + 1
                slot = _ArchiveSlot(self, user)
                first = _ArchiveSlotParty(slot)
                slot._parties.add(first)
                return slot, first
        logger.info("archive: refused (%s limit %d reached)", scope, limit)
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Too many archive downloads in progress; retry shortly.",
                "type": "rate_limit",
                "code": "archive_concurrency_limit",
                "scope": scope,
                "limit": limit,
            },
            headers={"Retry-After": str(_ARCHIVE_RETRY_AFTER_SECONDS)},
        )

    def _free_locked(self, user: str) -> None:
        self._total -= 1
        left = self._per_user.get(user, 0) - 1
        if left > 0:
            self._per_user[user] = left
        else:
            self._per_user.pop(user, None)

    def snapshot(self) -> tuple[int, dict]:
        with self._lock:
            return self._total, dict(self._per_user)


_ARCHIVE_ADMISSION = _ArchiveAdmission()


class _ZipCancelled(Exception):
    """Raised inside the producer once the consumer has gone away."""


class _ZipStreamSink:
    """Unseekable, write-only file object feeding zip bytes to the event loop.

    Deliberately has no ``tell``/``seek`` so ``zipfile`` takes its streaming
    path (data descriptors).
    """

    def __init__(self, loop, queue: "asyncio.Queue", cancel: threading.Event):
        self._loop = loop
        self._queue = queue
        self._cancel = cancel
        self._buf = bytearray()
        self._dead = False

    def abandon(self) -> None:
        """Swallow further writes so the producer can close ``ZipFile`` quietly."""
        self._dead = True
        self._buf.clear()

    def write(self, data) -> int:
        if self._dead:
            return len(data)
        if self._cancel.is_set():
            raise _ZipCancelled()
        self._buf += data
        if len(self._buf) >= _ZIP_CHUNK_BYTES:
            self._emit()
        return len(data)

    def flush(self) -> None:
        # zipfile flushes after every member; batching continues regardless so
        # chunks stay ~_ZIP_CHUNK_BYTES instead of one tiny chunk per file.
        if not self._dead and self._cancel.is_set():
            raise _ZipCancelled()

    def finish(self) -> None:
        if self._buf:
            self._emit()
        self.put(_ZIP_DONE)

    def _emit(self) -> None:
        chunk = bytes(self._buf)
        self._buf.clear()
        self.put(chunk)

    def put(self, item) -> None:
        """Blocking, cancellable put onto the bounded asyncio queue."""
        if self._cancel.is_set():
            raise _ZipCancelled()
        try:
            fut = asyncio.run_coroutine_threadsafe(self._queue.put(item), self._loop)
        except RuntimeError as exc:  # event loop closed under us
            raise _ZipCancelled() from exc
        while True:
            try:
                fut.result(timeout=_ZIP_PUT_POLL_SECONDS)
                return
            except concurrent.futures.TimeoutError:
                if self._cancel.is_set():
                    fut.cancel()
                    raise _ZipCancelled()
            except concurrent.futures.CancelledError as exc:
                raise _ZipCancelled() from exc


def _open_archive_root(root: Path, identity: Optional[tuple]) -> int:
    """Open the workspace root as a directory fd, refusing a swapped root."""
    return _pin_root(root, identity)


def _open_archive_member(root_fd: int, arcname: str):
    """Open one archive member beneath ``root_fd`` (see ``_open_file_beneath``)."""
    parts = arcname.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _UnsafeArchiveMember(arcname)
    return _open_file_beneath(root_fd, parts)


def _archive_zipinfo(arcname: str, st: os.stat_result) -> zipfile.ZipInfo:
    """``ZipInfo.from_file`` for an already-open member (from its fstat)."""
    date_time = time.localtime(st.st_mtime)[0:6]
    if date_time[0] < 1980:  # the zip format cannot express earlier dates
        date_time = (1980, 1, 1, 0, 0, 0)
    zinfo = zipfile.ZipInfo(arcname, date_time)
    zinfo.external_attr = (st.st_mode & 0xFFFF) << 16
    zinfo.file_size = st.st_size
    zinfo.compress_type = zipfile.ZIP_DEFLATED
    return zinfo


def _produce_zip(
    root: Path,
    arcnames: List[str],
    sink: _ZipStreamSink,
    party: Optional[_ArchiveSlotParty] = None,
    root_identity: Optional[tuple] = None,
) -> None:
    zf = None
    root_fd = None
    try:
        root_fd = _open_archive_root(root, root_identity)
        zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED)
        for arcname in arcnames:
            # Open (and fstat) before the member header goes out, so a skip
            # leaves the archive well-formed.
            try:
                src, st = _open_archive_member(root_fd, arcname)
            except FileNotFoundError:
                # Removed between the pre-stream walk and now.
                logger.info("archive: skipping vanished file %s", arcname)
                continue
            except _UnsafeArchiveMember:
                logger.warning(
                    "archive: skipping %s, it is no longer a plain file inside "
                    "the workspace",
                    arcname,
                )
                continue
            with src:
                # Same size hint as ``ZipFile.write`` so zip64 is chosen up front.
                with zf.open(_archive_zipinfo(arcname, st), "w") as dest:
                    shutil.copyfileobj(src, dest, 1024 * 8)
        zf.close()
        sink.finish()
    except _ZipCancelled:
        logger.debug("archive: producer stopped, client went away")
    except BaseException as exc:  # surface to the consumer, then stop
        logger.warning("archive: zip stream failed mid-response: %s", exc)
        try:
            sink.put(exc)
        except _ZipCancelled:
            pass
    finally:
        # On the abort paths, close the half-written ZipFile into a dead sink so
        # its ``__del__`` does not later try to write a central directory.
        sink.abandon()
        if zf is not None:
            try:
                zf.close()
            except Exception:  # best effort; the stream is already gone
                pass
        if root_fd is not None:
            os.close(root_fd)
        if party is not None:
            party.release()


async def _stream_zip(
    root: Path,
    arcnames: List[str],
    party: Optional[_ArchiveSlotParty] = None,
    root_identity: Optional[tuple] = None,
):
    """Yield the zip of ``arcnames`` (paths relative to ``root``) chunk by chunk.

    Members are opened beneath ``root`` by ``_open_archive_member``, never by
    path; ``root_identity`` (``(st_dev, st_ino)`` from the walk) refuses a root
    that was swapped since. ``party`` is the response body's claim on an
    admission slot; the producer thread takes its own claim before it starts,
    and each releases on exit.
    """
    try:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_ZIP_QUEUE_DEPTH)
        cancel = threading.Event()
        sink = _ZipStreamSink(loop, queue, cancel)
        worker_party = party._slot.party() if party is not None else None
        worker = threading.Thread(
            target=_produce_zip,
            args=(root, arcnames, sink, worker_party, root_identity),
            name="archive-zip-producer",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            if worker_party is not None:
                worker_party.release()
            raise
        try:
            while True:
                item = await queue.get()
                if item is _ZIP_DONE:
                    return
                if isinstance(item, BaseException):
                    # Headers are already sent; raising aborts the response so
                    # the client sees a truncated (invalid) download, not a
                    # "good" zip.
                    raise item
                yield item
        finally:
            cancel.set()
            # Free a producer blocked on a full queue so it sees the flag at once.
            while not queue.empty():
                queue.get_nowait()
    finally:
        if party is not None:
            party.release()


@router.post("/files/archive")
async def archive_entries(
    request: Request,
    body: _ArchiveBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    root_resolved = root.resolve()

    targets = []
    for p in body.paths:
        t = _resolve_or_403(root, p)
        if not t.exists():
            raise HTTPException(status_code=404, detail=f"not found: {p}")
        targets.append(t)

    hide_dot = _hide_dotfiles()
    hide_claude = _hide_claude_prefix()

    def _is_hidden(p: Path) -> bool:
        # Same rule as listing/_resolve_or_403. Keeps downloads consistent with
        # the browser view and prevents hidden ``.claude*`` subtrees from being
        # swept back in through a recursive directory archive.
        return _hidden_relative_path(
            p.relative_to(root_resolved),
            hide_dotfiles=hide_dot,
            hide_claude_prefix=hide_claude,
        )

    # Admission covers the archive's whole expensive lifecycle -- the recursive
    # walk, the producer thread and the response -- and comes right after the
    # cheap checks above (auth, sandbox, top-level existence), so a 401/403/404
    # never holds a slot and an over-limit request is refused before it walks a
    # single directory.
    _, party = _ARCHIVE_ADMISSION.try_acquire(_workspace_key(request))
    # The walk thread holds its own claim: if this request is cancelled while
    # the walk runs, the slot is only freed once the walk has actually stopped.
    walk_party = party._slot.party()

    # Walk the selection BEFORE the response starts (off the event loop): a walk
    # failure is still a plain error response, never a 200 with a broken zip.
    # These checks only choose what to archive; the producer re-opens every
    # member beneath the root fd without following symlinks, so a swap after
    # the walk cannot pull in anything outside the workspace.
    def _collect() -> tuple:
        try:
            root_st = os.stat(root_resolved)
            arcnames = []
            for t in targets:
                if t.is_dir():
                    for sub in t.rglob("*"):
                        if sub.is_file() and not sub.is_symlink() and not _is_hidden(sub):
                            arcnames.append(sub.relative_to(root_resolved).as_posix())
                elif t.is_file() and not t.is_symlink():
                    arcnames.append(t.relative_to(root_resolved).as_posix())
            return arcnames, (root_st.st_dev, root_st.st_ino)
        finally:
            walk_party.release()

    try:
        arcnames, root_identity = await run_in_threadpool(_collect)
    except BaseException:
        # Walk failed or the request was cancelled: nothing will stream, so the
        # response's claim goes too (the walk thread drops its own on exit).
        party.release()
        raise
    body_iter = _stream_zip(root_resolved, arcnames, party, root_identity)
    # A body Starlette never iterates (client gone before streaming starts)
    # never runs its ``finally``; the finalizer returns the slot instead.
    weakref.finalize(body_iter, party.release)
    return StreamingResponse(
        body_iter,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )