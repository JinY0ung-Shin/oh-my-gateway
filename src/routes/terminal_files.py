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
- ``GET  /files/read?path=<p>``       -> text ``{path,total_lines,content}`` | raw bytes (binary)
- ``GET  /files/view?path=<p>``       -> raw bytes (download)
- ``GET  /files/serve/<path>``        -> raw bytes, inline (HTML iframe preview; relative assets)
- ``POST /files/upload?directory=<p>``-> ``{path,size}`` (also how "new file" is created)
- ``POST /files/mkdir``  {path}       -> ``{path}``
- ``DELETE /files/delete?path=<p>``   -> ``{path,type}``
- ``POST /files/move``  {source,destination} -> ``{source,destination}``
- ``POST /files/archive`` {paths}     -> zip stream

Identity: read from a configurable, vendor-neutral header (``WORKSPACE_USER_HEADER``,
default ``X-User-Email``) and take its localpart — the same value the pipe uses to
key ``/v1/responses`` workspaces — so the explorer resolves the very files the
agent wrote. The caller (e.g. open-webui) forwards the user's identity under that
header name; on open-webui set ``FORWARD_USER_INFO_HEADER_USER_EMAIL`` to the same
name so the two agree (no code coupling to the caller's product).

Config:
- ``WORKSPACE_USER_HEADER`` — inbound identity header name (default ``X-User-Email``).
- ``WORKSPACE_HIDE_DOTFILES`` — when true, dot-prefixed entries are neither
  listed nor accessible. Default **false**: hiding is a presentation choice that
  belongs to the client rendering the tree, and hiding them here also blocks
  writes to the workspace's agent-resource directories.

Concurrency:
- Filesystem work (directory scans, file reads/writes, deletes, zip builds) runs
  in the threadpool via ``run_in_threadpool``. FileNav polls ``/files/list``
  continuously for every connected user; done synchronously that I/O would
  block the gateway event loop and stall everything else it serves
  (``/v1/responses`` streams, terminal websockets).

Security:
- ``API_KEY`` MUST be configured; otherwise ``verify_api_key`` is a no-op and
  these endpoints would expose every user's files unauthenticated, so we fail
  closed here.
- Every path (read AND write) is confined to the single workspace root via
  ``_resolve_or_403`` (``Path.resolve()`` collapses ``..`` and resolves symlinks
  before the containment check); anything above/outside the root is a 403.
  Uploaded filenames are reduced to a basename. No extra roots (never
  ``~/.claude`` etc.).
"""

import io
import mimetypes
import os
import shutil
import stat as stat_module
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.auth import auth_manager, security, verify_api_key
from src.workspace_manager import workspace_manager


class _PathBody(BaseModel):
    path: str


class _MoveBody(BaseModel):
    source: str
    destination: str


class _ArchiveBody(BaseModel):
    paths: List[str]

router = APIRouter(tags=["workspace-files"])

_BACKEND = "claude"
# Cap in-band text previews; larger files must be fetched via /files/view.
_MAX_READ_BYTES = 5 * 1024 * 1024

# Name of the inbound header carrying the user identity. Kept generic and
# configurable (no hard dependency on the caller's product) — the frontend just
# has to forward the user's identity under this name. Its value is treated as an
# email/identity whose localpart (before '@') keys the workspace, matching how
# the pipe derives body.user. Default is deliberately vendor-neutral.
_DEFAULT_USER_HEADER = "X-User-Email"


def _user_header() -> str:
    return os.getenv("WORKSPACE_USER_HEADER", _DEFAULT_USER_HEADER)


def _hide_dotfiles() -> bool:
    """When true, dot-prefixed entries are neither listed nor accessible.

    Defaults to **false**: hiding dotfiles protects nothing here — the agent
    itself reads and writes them freely through Bash/Read within the same
    workspace, and the real guards are ``API_KEY`` plus root confinement. What it
    *did* do was make the workspace's agent-resource directories unreachable over
    ``/files/*`` (a 404 on any dot-prefixed component, including writes), which
    silently breaks clients that install skills/subagents through this API. Hiding
    is presentation, so it belongs to the client that renders the tree — see the
    Finder-style "show hidden items" toggle in ChatDRAGON's files panel. Set this
    to ``true`` to restore server-side hiding for a deployment that wants it.
    """
    return os.getenv("WORKSPACE_HIDE_DOTFILES", "false").strip().lower() == "true"


def _ensure_api_key() -> None:
    """Fail closed unless an API key is configured (verify_api_key no-ops without one)."""
    if not auth_manager.get_api_key():
        raise HTTPException(
            status_code=503,
            detail="workspace file browser is disabled: API_KEY is not configured",
        )


def _require_user(request: Request) -> str:
    # Workspace key = identity localpart (strip from '@'), matching how the pipe
    # derives body.user. Falls back to the raw value when there is no '@'.
    identity = (request.headers.get(_user_header()) or "").strip()
    user = identity.split("@")[0]
    if not user:
        raise HTTPException(status_code=400, detail="missing user identity header")
    return user


def _workspace_root(user: str) -> Path:
    try:
        return workspace_manager.resolve(user, backend=_BACKEND)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid user identity")


def resolve_workspace_for_request(request: Request) -> Optional[Path]:
    """The caller's workspace directory, or ``None`` when it can't be keyed.

    Same identity header and workspace mapping as the file browser, but soft:
    endpoints that merely *describe* a workspace (e.g. the agent-resource
    catalog) should degrade to "no project scope" rather than 400 when the
    header is absent. Never creates the directory.
    """
    identity = (request.headers.get(_user_header()) or "").strip()
    user = identity.split("@")[0]
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


def _resolve_or_403(root: Path, rel: str) -> Path:
    """Resolve within the workspace root or raise.

    - Outside the root -> **403** ("outside your workspace"), so the explorer can
      warn the user that navigation there isn't allowed.
    - A dot-prefixed (hidden) component, when hiding is on -> **404**, so hidden
      entries stay invisible rather than advertising that something is blocked.

    The returned path may not exist yet (callers that create paths check as
    needed); callers reading/listing must still verify existence.
    """
    target = _resolve_in_root(root, rel)
    if target is None:
        raise HTTPException(
            status_code=403,
            detail="Access denied: this path is outside your workspace.",
        )
    if _hide_dotfiles():
        relative = target.relative_to(root.resolve())
        if any(part.startswith(".") for part in relative.parts):
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

    # Run the directory scan off the event loop: FileNav polls this endpoint
    # continuously across all users, and a synchronous scandir would stall
    # every other request (chat streams, terminal websockets) on the loop.
    def _scan() -> list:
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                if hide_dot and entry.name.startswith("."):
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
    the same rule as listing (dot-prefixed components pruned while hiding is
    on), symlinks are skipped like the archive walk, and results are capped
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
    _SCAN_CAP = 1000  # stop collecting beyond this many matches

    # The recursive walk is the most expensive scan this router does — run it
    # in the threadpool like the other filesystem work so it cannot stall the
    # event loop.
    def _search() -> dict:
        matches: List[dict] = []
        scan_capped = False

        for dirpath, dirnames, filenames in os.walk(root_resolved):
            if hide_dot:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dirnames.sort()
            base = Path(dirpath)
            candidates = [(d, True) for d in dirnames] + [
                (f, False) for f in sorted(filenames)
            ]
            for name, is_dir in candidates:
                if hide_dot and name.startswith("."):
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


@router.post("/files/upload")
async def upload_file(
    request: Request,
    directory: str = "/",
    file: UploadFile = File(...),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
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
    await run_in_threadpool(target.write_bytes, data)
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
    await run_in_threadpool(shutil.move, str(src), str(dst))
    return {"source": body.source, "destination": body.destination}


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

    def _is_hidden(p: Path) -> bool:
        # Same rule as listing/_resolve_or_403: any dot-prefixed component
        # relative to the workspace root is hidden. Keeps downloads consistent
        # with the browser view — e.g. ``.claude`` never ends up in the zip.
        return hide_dot and any(
            part.startswith(".") for part in p.relative_to(root_resolved).parts
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
