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
  belongs to the client rendering the tree, and hiding them here also blocks
  writes to the workspace's agent-resource directories.
- ``WORKSPACE_HIDE_CLAUDE_PREFIX`` — when true, path components whose names start
  with ``.claude`` are hidden/blocked by the file API (for example ``.claude``,
  ``.claude_images``, ``.claude-local``). Other dotfiles stay visible. Default **false**.
- ``USER_WORKSPACE_QUOTA_MB`` — optional cumulative quota for a named user's whole
  ``<base>/<user>`` tree, across backend directories. ``0``/unset = unlimited.

Concurrency:
- Filesystem work (directory scans, file reads/writes, deletes, zip builds) runs
  in the threadpool via ``run_in_threadpool``. FileNav polls ``/files/list``
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
"""

import asyncio
import ctypes
import errno
import hashlib
import io
import logging
import mimetypes
import os
import shutil
import stat as stat_module
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
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
    out of that surface. The agent process itself is unaffected.
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
        with os.scandir(target) as it:
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
        with target.open("rb") as fh:
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

    if target.stat().st_size > _MAX_READ_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large to preview (> {_MAX_READ_BYTES} bytes); use download",
        )

    data = await run_in_threadpool(target.read_bytes)
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
    return FileResponse(target, media_type=media, filename=target.name)


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
    return FileResponse(target, media_type=media, content_disposition_type="inline")


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


async def _write_uploaded_file(target: Path, data: bytes, no_clobber: bool) -> None:
    """Preserve the historical upload write/no-clobber semantics."""
    if no_clobber:

        def _write_exclusive() -> None:
            with open(target, "xb") as f:
                f.write(data)

        try:
            await run_in_threadpool(_write_exclusive)
        except FileExistsError:
            raise HTTPException(status_code=409, detail="destination already exists")
    else:
        await run_in_threadpool(target.write_bytes, data)


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
        await _write_uploaded_file(target, data, no_clobber)
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
        await _write_uploaded_file(target, data, no_clobber)
    return {"path": str(target), "size": len(data)}


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
    target.mkdir(parents=True, exist_ok=True)
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
    is_dir = target.is_dir() and not target.is_symlink()
    # rmtree over a large workspace subtree can take seconds — keep it off the loop.
    if is_dir:
        await run_in_threadpool(shutil.rmtree, target)
    else:
        await run_in_threadpool(target.unlink)
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
    if body.no_clobber:
        # No check-then-move: rename(2) replaces an existing destination by
        # design, so only the kernel can enforce no-replace atomically.
        # _rename_noreplace is renameat2(RENAME_NOREPLACE); when the primitive
        # is unavailable we fail closed (501) instead of silently falling back
        # to a replace-capable move.
        try:
            await run_in_threadpool(_rename_noreplace, src, dst)
        except NotImplementedError:
            raise HTTPException(
                status_code=501,
                detail="atomic no-replace move is unavailable on this system",
            )
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise HTTPException(status_code=409, detail="destination already exists")
            raise
    else:
        await run_in_threadpool(shutil.move, str(src), str(dst))
    return {"source": body.source, "destination": body.destination}


_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Atomic no-replace rename via Linux ``renameat2(RENAME_NOREPLACE)``.

    ``os.rename``/``shutil.move`` replace an existing destination by design,
    so any exists-check followed by a move is a TOCTOU, however small the
    window. Raises ``NotImplementedError`` when the primitive cannot give the
    guarantee (missing symbol, unsupported filesystem, cross-device) — callers
    must fail closed, never fall back to a replace-capable move.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as exc:  # pragma: no cover — non-Linux
        raise NotImplementedError("renameat2 unavailable") from exc
    ret = renameat2(
        ctypes.c_int(_AT_FDCWD),
        os.fsencode(str(src)),
        ctypes.c_int(_AT_FDCWD),
        os.fsencode(str(dst)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if ret != 0:
        err = ctypes.get_errno()
        if err in (errno.ENOSYS, errno.EINVAL, errno.EXDEV):
            raise NotImplementedError(os.strerror(err))
        raise OSError(err, os.strerror(err), str(src), None, str(dst))


class _UnsupportedSourceError(Exception):
    """Copy source is not a regular file (FIFO/socket/device) — refused."""


def _copy_file_exclusive(src: Path, dst: Path) -> None:
    """Copy a REGULAR file, failing with ``FileExistsError`` if ``dst`` exists.

    ``open(dst, "xb")`` (O_CREAT|O_EXCL) makes the no-clobber promise a
    filesystem guarantee instead of a check-then-copy: a destination that
    appears after validation loses the race to us or we lose it to them, but
    nobody's file is overwritten either way.

    The source is opened with ``O_NONBLOCK`` and validated via ``fstat`` on
    the open fd: a plain ``open(src, "rb")`` on a FIFO would block the worker
    until a writer appears (exhausting the shared threadpool), and checking
    the type before opening would just be another TOCTOU.
    """
    try:
        fd = os.open(str(src), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # e.g. a socket file
            raise _UnsupportedSourceError(str(src)) from exc
        raise
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise _UnsupportedSourceError(str(src))
        os.set_blocking(fd, True)
        with open(dst, "xb") as fdst, os.fdopen(os.dup(fd), "rb") as fsrc:
            shutil.copyfileobj(fsrc, fdst)
    finally:
        os.close(fd)
    shutil.copystat(str(src), str(dst))


async def _perform_copy(src: Path, dst: Path, is_dir: bool) -> None:
    """Perform the historical copy operation and preserve its error contract."""
    try:
        if is_dir:
            # Copying a directory into its own subtree would recurse forever.
            if dst == src or src in dst.parents:
                raise HTTPException(
                    status_code=400, detail="cannot copy a directory into itself"
                )
            # symlinks=True copies links as links instead of following them — a
            # link inside the tree may point outside the workspace root, and
            # following it here would duplicate foreign content into the
            # workspace. dirs_exist_ok stays False, so copytree's own mkdir
            # refuses a destination that appeared after validation.
            await run_in_threadpool(shutil.copytree, str(src), str(dst), symlinks=True)
        else:
            await run_in_threadpool(_copy_file_exclusive, src, dst)
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
    # on the opened fd inside _copy_file_exclusive.
    if not is_dir and not src.is_file():
        raise HTTPException(status_code=400, detail="unsupported file type")

    # Quota-disabled deployments retain the exact historical path: no extra
    # scan, lock, or threadpool call before the copy.
    if workspace_quota_limit_bytes() <= 0:
        await _perform_copy(src, dst, is_dir)
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
            await _perform_copy(src, dst, is_dir)

    return {
        "source": body.source,
        "destination": body.destination,
        "type": "directory" if is_dir else "file",
    }


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

    # Walking the tree and deflating can take seconds on big workspaces — keep
    # the whole zip build off the event loop.
    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for t in targets:
                if t.is_dir():
                    for sub in t.rglob("*"):
                        if (
                            sub.is_file()
                            and not sub.is_symlink()
                            and not _is_hidden(sub)
                        ):
                            zf.write(sub, arcname=str(sub.relative_to(root_resolved)))
                elif t.is_file() and not t.is_symlink():
                    zf.write(t, arcname=str(t.relative_to(root_resolved)))
        return buf.getvalue()

    payload = await run_in_threadpool(_build_zip)
    return StreamingResponse(
        iter([payload]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )