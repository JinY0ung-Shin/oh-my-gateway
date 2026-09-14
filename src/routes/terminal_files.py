"""Open Terminal-compatible file server over per-user Claude workspaces.

Implements the subset of the open-webui "Open Terminal" server HTTP contract that
the ``FileNav`` right-sidebar explorer uses, scoped to a single user's Claude
workspace, so the gateway can be registered in open-webui as a terminal
connection and its workspace browsed/edited exactly as the agent sees it.

Identity is read from ``WORKSPACE_USER_HEADER`` (default ``X-User-Email``) and
used whole so the file browser and agent resolve the same per-user workspace.
All paths are confined to that workspace. ``USER_WORKSPACE_QUOTA_MB`` optionally
adds a cumulative soft quota across all backend directories below one user root;
the existing ``WORKSPACE_UPLOAD_MAX_BYTES`` remains the independent per-file cap.
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
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from src.auth import auth_manager, security, verify_api_key
from src.constants import MAX_REQUEST_SIZE, WORKSPACE_UPLOAD_MAX_BYTES
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
    no_clobber: bool = False


class _ArchiveBody(BaseModel):
    paths: List[str]


router = APIRouter(tags=["workspace-files"])

_BACKEND = "claude"
_MAX_READ_BYTES = 5 * 1024 * 1024
_DEFAULT_USER_HEADER = "X-User-Email"
_MULTIPART_ENVELOPE_RESERVE = 8192

# The lock only serializes gateway file-API mutations in this process. Claude
# subprocesses and other gateway workers do not acquire it, hence "soft quota".
_QUOTA_LOCKS: dict[str, asyncio.Lock] = {}


def _max_upload_bytes() -> int:
    """Largest single file ``POST /files/upload`` will actually accept."""
    ceiling = min(WORKSPACE_UPLOAD_MAX_BYTES, MAX_REQUEST_SIZE - _MULTIPART_ENVELOPE_RESERVE)
    return max(0, ceiling)


def _user_header() -> str:
    return os.getenv("WORKSPACE_USER_HEADER", _DEFAULT_USER_HEADER)


def _hide_dotfiles() -> bool:
    return os.getenv("WORKSPACE_HIDE_DOTFILES", "false").strip().lower() == "true"


def _ensure_api_key() -> None:
    if not auth_manager.get_api_key():
        raise HTTPException(
            status_code=503,
            detail="workspace file browser is disabled: API_KEY is not configured",
        )


def _legacy_localpart_key() -> bool:
    if os.getenv("WORKSPACE_LEGACY_LOCALPART_KEY", "").strip().lower() != "true":
        return False
    logger.warning(
        "WORKSPACE_LEGACY_LOCALPART_KEY=true: workspaces are keyed on the identity "
        "localpart, so callers sharing one localpart share one workspace"
    )
    return True


def _workspace_key(request: Request) -> str:
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
    return workspace_root.resolve().parent


def _quota_lock(user_root: Path) -> asyncio.Lock:
    key = str(user_root.resolve())
    lock = _QUOTA_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _QUOTA_LOCKS[key] = lock
    return lock


def _raise_quota_http(exc: WorkspaceQuotaExceeded) -> None:
    raise HTTPException(status_code=507, detail=exc.as_detail())


def resolve_workspace_for_request(request: Request) -> Optional[Path]:
    user = _workspace_key(request)
    if not user:
        return None
    try:
        return workspace_manager.resolve(user, backend=_BACKEND)
    except ValueError:
        return None


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _resolve_in_root(root: Path, rel: str) -> Optional[Path]:
    try:
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    p = rel or "/"
    if p in ("/", ""):
        return root_resolved
    try:
        candidate = Path(p)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not _is_under(resolved, root_resolved):
                if _is_under(root_resolved, resolved):
                    return None
                resolved = (root_resolved / p.lstrip("/")).resolve()
        else:
            resolved = (root_resolved / p).resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if _is_under(resolved, root_resolved) else None


def _resolve_or_403(root: Path, rel: str) -> Path:
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
    await verify_api_key(request, credentials)
    _ensure_api_key()
    return {"features": {"terminal": False}}


@router.get("/files/openapi.json")
async def tool_specs(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
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

    def _scan() -> list:
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                if hide_dot and entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat()
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
        return entries

    return {"entries": await run_in_threadpool(_scan)}


@router.get("/files/search")
async def search_files(
    request: Request,
    query: str = "",
    limit: int = 50,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    root_resolved = root.resolve()

    q = query.strip().lower()
    if not q:
        return {"results": [], "truncated": False}
    limit = max(1, min(limit, 200))
    hide_dot = _hide_dotfiles()
    scan_cap = 1000

    def _search() -> dict:
        matches: List[dict] = []
        scan_capped = False
        for dirpath, dirnames, filenames in os.walk(root_resolved):
            if hide_dot:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dirnames.sort()
            base = Path(dirpath)
            candidates = [(d, True) for d in dirnames] + [(f, False) for f in sorted(filenames)]
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
                if len(matches) >= scan_cap:
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
        return {"results": matches[:limit], "truncated": scan_capped or len(matches) > limit}

    return await run_in_threadpool(_search)


@router.get("/files/digest")
async def file_digest(
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

    def _digest() -> tuple[str, int]:
        h = hashlib.sha256()
        size = 0
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
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, path if path.startswith("/") else f"/{path}")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, content_disposition_type="inline")


@router.post("/files/cwd")
async def set_cwd(
    request: Request,
    body: _PathBody,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    target = _resolve_or_403(root, body.path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")
    return {"cwd": body.path}


async def _write_upload(target: Path, data: bytes, no_clobber: bool) -> None:
    if no_clobber:
        def _write_exclusive() -> None:
            with open(target, "xb") as fh:
                fh.write(data)
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
    await verify_api_key(request, credentials)
    _ensure_api_key()
    root = _workspace_root(_require_user(request))
    dest_dir = _resolve_or_403(root, directory)
    if not dest_dir.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")

    name = os.path.basename(file.filename or "")
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = _resolve_or_403(root, f"{directory}/{name}")

    data = await file.read()
    ceiling = _max_upload_bytes()
    if len(data) > ceiling:
        raise HTTPException(status_code=413, detail=f"file exceeds the upload limit of {ceiling} bytes")

    # Critical compatibility fast-path: when cumulative quota is disabled, use
    # exactly the pre-quota write sequence. Besides avoiding a full-tree scan,
    # this preserves existing atomic-race behavior and tests.
    if workspace_quota_limit_bytes() <= 0:
        await _write_upload(target, data, no_clobber)
        return {"path": str(target), "size": len(data)}

    user_root = _user_root(root)
    async with _quota_lock(user_root):
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
        await _write_upload(target, data, no_clobber)
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
    if src.is_dir() and not src.is_symlink() and (dst == src or src in dst.parents):
        raise HTTPException(status_code=400, detail="cannot move a directory into itself")
    if body.no_clobber:
        try:
            await run_in_threadpool(_rename_noreplace, src, dst)
        except NotImplementedError:
            raise HTTPException(status_code=501, detail="atomic no-replace move is unavailable on this system")
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
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError) as exc:
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
    pass


def _copy_file_exclusive(src: Path, dst: Path) -> None:
    try:
        fd = os.open(str(src), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno == errno.ENXIO:
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
    try:
        if is_dir:
            if dst == src or src in dst.parents:
                raise HTTPException(status_code=400, detail="cannot copy a directory into itself")
            await run_in_threadpool(shutil.copytree, str(src), str(dst), symlinks=True)
        else:
            await run_in_threadpool(_copy_file_exclusive, src, dst)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="destination already exists")
    except _UnsupportedSourceError:
        raise HTTPException(status_code=400, detail="unsupported file type")
    except shutil.SpecialFileError:
        raise HTTPException(status_code=400, detail="directory contains unsupported special files")
    except shutil.Error:
        raise HTTPException(status_code=400, detail="directory contains entries that cannot be copied")


@router.post("/files/copy")
async def copy_entry(
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
    if dst.exists():
        raise HTTPException(status_code=409, detail="destination already exists")
    if not dst.parent.is_dir():
        raise HTTPException(status_code=404, detail="destination directory not found")
    is_dir = src.is_dir() and not src.is_symlink()
    if not is_dir and not src.is_file():
        raise HTTPException(status_code=400, detail="unsupported file type")

    # No quota configured: preserve the exact legacy mutation path and avoid
    # extra scans/threadpool hops.
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

    def _is_hidden(p: Path) -> bool:
        return hide_dot and any(
            part.startswith(".") for part in p.relative_to(root_resolved).parts
        )

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for t in targets:
                if t.is_dir():
                    for sub in t.rglob("*"):
                        if sub.is_file() and not sub.is_symlink() and not _is_hidden(sub):
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
