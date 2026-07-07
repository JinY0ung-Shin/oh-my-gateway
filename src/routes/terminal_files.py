"""Open Terminal-compatible, read-only file server over per-user Claude workspaces.

Implements the subset of the open-webui "Open Terminal" server HTTP contract that
the ``FileNav`` right-sidebar explorer needs to BROWSE and PREVIEW files, scoped
to a single user's Claude workspace. It is **read-only**: no write/exec endpoints
are served, so the gateway can be registered in open-webui as a terminal
connection and its workspace browsed exactly as the agent sees it.

Contract (what FileNav calls):
- ``GET /api/config``                 -> ``{"features": {"terminal": false}}`` (handshake)
- ``GET /files/cwd``                  -> ``{"cwd": "/"}`` (workspace root is the virtual "/")
- ``GET /files/list?directory=<p>``   -> ``{"entries": [{name,type,size,modified}]}``
- ``GET /files/read?path=<p>``        -> text ``{path,total_lines,content}`` | raw bytes (binary)
- ``GET /files/view?path=<p>``        -> raw bytes (download)

Identity: the open-webui backend proxy forwards the standard user-info headers
(when ``ENABLE_FORWARD_USER_INFO_HEADERS`` is on); we read ``X-OpenWebUI-User-Email``
and take its localpart — the same value the pipe uses to key ``/v1/responses``
workspaces — so the explorer resolves the very files the agent wrote.

Security:
- ``API_KEY`` MUST be configured; otherwise ``verify_api_key`` is a no-op and
  these endpoints would expose every user's files unauthenticated, so we fail
  closed here.
- Every path is confined to the single workspace root via ``_safe_resolve``
  (``Path.resolve()`` collapses ``..`` and follows symlinks before the
  containment check). No extra roots (never ``~/.claude`` etc.).
"""

import mimetypes
import os
import stat as stat_module
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials

from src.auth import auth_manager, security, verify_api_key
from src.workspace_manager import workspace_manager

router = APIRouter(tags=["workspace-files"])

_EMAIL_HEADER = "x-openwebui-user-email"
_BACKEND = "claude"
# Cap in-band text previews; larger files must be fetched via /files/view.
_MAX_READ_BYTES = 5 * 1024 * 1024


def _ensure_api_key() -> None:
    """Fail closed unless an API key is configured (verify_api_key no-ops without one)."""
    if not auth_manager.get_api_key():
        raise HTTPException(
            status_code=503,
            detail="workspace file browser is disabled: API_KEY is not configured",
        )


def _require_user(request: Request) -> str:
    # Workspace key = email localpart (strip from '@'), matching how the pipe
    # derives body.user. Falls back to the raw value when there is no '@'.
    email = (request.headers.get(_EMAIL_HEADER) or "").strip()
    user = email.split("@")[0]
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
    either is caught by the ``relative_to`` containment check. Returns ``None``
    on any escape or resolution error.
    """
    rel = (rel or "/").lstrip("/")
    candidate = root / rel if rel else root
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return resolved


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

    entries = []
    with os.scandir(target) as it:
        for entry in it:
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
