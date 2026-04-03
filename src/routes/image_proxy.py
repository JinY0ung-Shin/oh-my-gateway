"""Proxy routes for the external image server."""

import os
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import Response, JSONResponse, StreamingResponse

from src.constants import IMAGE_SERVER_BASE, IMAGE_INTERNAL_SECRET, IMAGE_TLS_VERIFY

logger = logging.getLogger(__name__)

router = APIRouter(tags=["image_proxy"])

PASS_HEADERS = frozenset({
    "content-type",
    "content-length",
    "cache-control",
    "etag",
    "last-modified",
})


def _require_image_server() -> None:
    if not IMAGE_SERVER_BASE:
        raise HTTPException(503, "IMAGE_SERVER_BASE is not configured")


@router.get("/api/get_image_list")
async def get_image_list(
    filename: str = Query(..., description="Path of an image; its directory is used to list siblings"),
    background_tasks: BackgroundTasks = None,
):
    _require_image_server()
    upstream_url = f"{IMAGE_SERVER_BASE.rstrip('/')}/api/get_image_list"
    folder = os.path.dirname(filename)
    params = {"folder": folder}
    headers = {
        "X-From-Chat": "true",
        "x-internal-secret": IMAGE_INTERNAL_SECRET,
    }

    client = httpx.AsyncClient(
        verify=IMAGE_TLS_VERIFY,
        timeout=30.0,
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", upstream_url, params=params, headers=headers)
        r = await client.send(req, stream=True)

        if r.status_code != 200:
            body = await r.aread()
            await r.aclose()
            await client.aclose()
            return Response(
                content=body,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "text/plain"),
                headers={"Cross-Origin-Resource-Policy": "cross-origin"},
            )

        content_type = r.headers.get("content-type", "")
        if "application/json" in content_type or content_type.endswith("+json"):
            body = await r.aread()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body

            def _prefix(x: str) -> str:
                return f"{folder.rstrip('/')}/{str(x).lstrip('/')}"

            if isinstance(data, list):
                prefixed = [_prefix(x) for x in data]
            elif isinstance(data, dict):
                for key in ("images", "data", "items"):
                    if isinstance(data.get(key), list):
                        data[key] = [_prefix(x) for x in data[key]]
                prefixed = data
            else:
                prefixed = data

            await r.aclose()
            await client.aclose()
            return JSONResponse(
                content=prefixed,
                headers={"Cross-Origin-Resource-Policy": "cross-origin"},
            )

        # Non-JSON: stream through
        media_type = r.headers.get("content-type", "application/octet-stream")
        out_headers = {
            k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS
        }
        out_headers["Cross-Origin-Resource-Policy"] = "cross-origin"

        async def iterator():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                if background_tasks is None:
                    await r.aclose()
                    await client.aclose()

        if background_tasks is not None:
            background_tasks.add_task(r.aclose)
            background_tasks.add_task(client.aclose)

        return StreamingResponse(
            iterator(), media_type=media_type, headers=out_headers
        )

    except httpx.HTTPError as e:
        try:
            await client.aclose()
        except Exception:
            pass
        raise HTTPException(502, f"Upstream connection error: {e}")


@router.get("/api/get_image")
async def get_image(
    filename: str = Query(..., description="Image filename"),
    folder: str = Query("", description="Folder path"),
    background_tasks: BackgroundTasks = None,
):
    _require_image_server()
    upstream_url = f"{IMAGE_SERVER_BASE.rstrip('/')}/api/get_image"
    if folder and not folder.startswith("/"):
        folder = "/" + folder
    full_path = os.path.join(folder, filename) if folder else filename
    params = {"filename": full_path, "folder": ""}
    headers = {
        "X-From-Chat": "true",
        "x-internal-secret": IMAGE_INTERNAL_SECRET,
    }

    client = httpx.AsyncClient(
        verify=IMAGE_TLS_VERIFY,
        timeout=30.0,
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", upstream_url, params=params, headers=headers)
        r = await client.send(req, stream=True)

        if r.status_code != 200:
            body = await r.aread()
            await r.aclose()
            await client.aclose()
            return Response(
                content=body,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "text/plain"),
                headers={"Cross-Origin-Resource-Policy": "cross-origin"},
            )

        media_type = r.headers.get("content-type", "application/octet-stream")
        out_headers = {
            k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS
        }
        out_headers["Cross-Origin-Resource-Policy"] = "cross-origin"

        async def iterator():
            async for chunk in r.aiter_bytes():
                yield chunk

        if background_tasks is not None:
            background_tasks.add_task(r.aclose)
            background_tasks.add_task(client.aclose)

        return StreamingResponse(
            iterator(), media_type=media_type, headers=out_headers
        )

    except httpx.HTTPError as e:
        try:
            await client.aclose()
        except Exception:
            pass
        raise HTTPException(502, f"Upstream connection error: {e}")
