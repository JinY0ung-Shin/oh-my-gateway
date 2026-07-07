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
- ``GET  /files/read?path=<p>``       -> text ``{path,total_lines,content}`` | raw bytes (binary)
- ``GET  /files/view?path=<p>``       -> raw bytes (download)
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
- ``WORKSPACE_HIDE_DOTFILES`` — when true (default), dot-prefixed entries are
  neither listed nor accessible.

Security:
- ``API_KEY`` MUST be configured; otherwise ``verify_api_key`` is a no-op and
  these endpoints would expose every user's files unauthenticated, so we fail
  closed here.
- Every path (read AND write) is confined to the single workspace root via
  ``_safe_resolve`` (``Path.resolve()`` collapses ``..`` and resolves symlinks
  before the containment check). Uploaded filenames are reduced to a basename.
  No extra roots (never ``~/.claude`` etc.).
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
    """When true (default), dot-prefixed entries are neither listed nor accessible."""
    return os.getenv("WORKSPACE_HIDE_DOTFILES", "true").strip().lower() == "true"


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


def _safe_resolve(root: Path, rel: str) -> Optional[Path]:
    """Resolve *rel* (relative to the workspace root) and confine it to that root.

    ``rel`` is treated as workspace-relative; ``"/"`` or ``""`` is the root.
    ``Path.resolve()`` collapses ``..`` and follows symlinks, so an escape via
    either is caught by the ``relative_to`` containment check. When dotfiles are
    hidden, any path with a dot-prefixed component is also rejected (so a hidden
    entry can't be reached by typing its path). Returns ``None`` on any escape,
    hidden-path, or resolution error.
    """
    rel = (rel or "/").lstrip("/")
    candidate = root / rel if rel else root
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError:
        return None
    if _hide_dotfiles() and any(part.startswith(".") for part in relative.parts):
        return None
    return resolved


def _rel(root: Path, target: Path) -> str:
    """Workspace-relative path (leading '/') for a resolved *target*."""
    return "/" + str(target.relative_to(root.resolve()))


@router.get("/api/config")
async def terminal_config(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Handshake: advertise a read-only, no-terminal file server."""
    await verify_api_key(request, credentials)
    _ensure_api_key()
    return {"features": {"terminal": False}}


@router.get("/files/cwd")
async def get_cwd(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    _require_user(request)  # require identity even though the root is virtual "/"
    return {"cwd": "/"}


@router.get("/files/list")
async def list_files(
    request: Request,
    directory: str = "/",
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _safe_resolve(root, directory)
    if target is None or not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    hide_dot = _hide_dotfiles()
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
                    "type": "directory" if stat_module.S_ISDIR(st.st_mode) else "file",
                    "size": st.st_size,
                    "modified": int(st.st_mtime),
                }
            )
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    return {"entries": entries}


@router.get("/files/read")
async def read_file(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _safe_resolve(root, path)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    if target.stat().st_size > _MAX_READ_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large to preview (> {_MAX_READ_BYTES} bytes); use download",
        )

    data = target.read_bytes()
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
    target = _safe_resolve(root, path)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, filename=target.name)


# ---------------------------------------------------------------------------
# Write operations (all confined to the workspace root by _safe_resolve)
# ---------------------------------------------------------------------------


@router.post("/files/cwd")
async def set_cwd(
    request: Request,
    body: _PathBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Validate a directory; cwd itself is tracked client-side (root is virtual)."""
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _safe_resolve(root, body.path)
    if target is None or not target.is_dir():
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
    dest_dir = _safe_resolve(root, directory)
    if dest_dir is None or not dest_dir.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    # Reduce the client filename to a basename so it can't carry path segments.
    name = os.path.basename(file.filename or "")
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = _safe_resolve(root, f"{directory}/{name}")
    if target is None:
        raise HTTPException(status_code=400, detail="invalid path")

    data = await file.read()
    target.write_bytes(data)
    return {"path": _rel(root, target), "size": len(data)}


@router.post("/files/mkdir")
async def make_dir(
    request: Request,
    body: _PathBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _safe_resolve(root, body.path)
    if target is None or target == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    target.mkdir(parents=True, exist_ok=True)
    return {"path": _rel(root, target)}


@router.delete("/files/delete")
async def delete_entry(
    request: Request,
    path: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _safe_resolve(root, path)
    if target is None or target == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    is_dir = target.is_dir() and not target.is_symlink()
    if is_dir:
        shutil.rmtree(target)
    else:
        target.unlink()
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
    src = _safe_resolve(root, body.source)
    dst = _safe_resolve(root, body.destination)
    if src is None or dst is None or src == root.resolve() or dst == root.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not src.exists():
        raise HTTPException(status_code=404, detail="source not found")
    if not dst.parent.is_dir():
        raise HTTPException(status_code=404, detail="destination directory not found")
    shutil.move(str(src), str(dst))
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
        t = _safe_resolve(root, p)
        if t is None or not t.exists():
            raise HTTPException(status_code=404, detail=f"not found: {p}")
        targets.append(t)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in targets:
            if t.is_dir():
                for sub in t.rglob("*"):
                    if sub.is_file() and not sub.is_symlink():
                        zf.write(sub, arcname=str(sub.relative_to(root_resolved)))
            elif t.is_file() and not t.is_symlink():
                zf.write(t, arcname=str(t.relative_to(root_resolved)))
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="archive.zip"'},
    )
