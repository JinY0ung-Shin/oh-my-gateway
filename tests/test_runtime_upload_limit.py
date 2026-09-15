"""Runtime-admin control for the workspace upload ceiling."""

from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.requests import Request

from src import concurrency_middleware
from src.constants import MAX_REQUEST_SIZE, WORKSPACE_UPLOAD_MAX_BYTES
from src.routes import terminal_files
from src.routes import terminal_files as tf
from src.runtime_config import (
    WORKSPACE_UPLOAD_MULTIPART_RESERVE,
    get_workspace_upload_max_bytes,
    get_workspace_upload_request_max_bytes,
    runtime_config,
)


@pytest.fixture(autouse=True)
def clean_runtime_override():
    runtime_config.reset_all()
    yield
    runtime_config.reset_all()


def test_upload_limit_is_an_editable_gateway_runtime_setting():
    runtime_config.set("workspace_upload_max_bytes", 3 * 1024 * 1024)

    detail = runtime_config.get_all()["workspace_upload_max_bytes"]
    assert detail["value"] == 3 * 1024 * 1024
    assert detail["overridden"] is True
    assert terminal_files._max_upload_bytes() == 3 * 1024 * 1024


def test_upload_request_boundary_uses_the_same_runtime_setting(monkeypatch):
    runtime_config.set("workspace_upload_max_bytes", 16 * 1024 * 1024)
    monkeypatch.setattr(
        concurrency_middleware.auth_manager, "has_api_auth", lambda: True
    )
    monkeypatch.setattr(
        concurrency_middleware.auth_manager,
        "authenticate_gateway_key",
        lambda token: (token == "test-key", None),
    )

    expected = 16 * 1024 * 1024 + WORKSPACE_UPLOAD_MULTIPART_RESERVE
    assert get_workspace_upload_request_max_bytes() == expected
    assert (
        concurrency_middleware._request_body_limit(
            {
                "type": "http",
                "method": "POST",
                "path": "/files/upload",
                "headers": [(b"authorization", b"Bearer test-key")],
            }
        )
        == expected
    )


def test_generic_requests_keep_their_existing_request_cap():
    runtime_config.set("workspace_upload_max_bytes", 64 * 1024 * 1024)

    assert (
        concurrency_middleware._request_body_limit(
            {"type": "http", "method": "POST", "path": "/v1/responses"}
        )
        == MAX_REQUEST_SIZE
    )
    assert (
        concurrency_middleware._request_body_limit(
            {"type": "http", "method": "PUT", "path": "/anything"}
        )
        == MAX_REQUEST_SIZE
    )


def test_zero_runtime_upload_limit_disables_file_bytes_but_keeps_envelope_room():
    runtime_config.set("workspace_upload_max_bytes", 0)

    assert terminal_files._max_upload_bytes() == 0
    assert get_workspace_upload_request_max_bytes() == WORKSPACE_UPLOAD_MULTIPART_RESERVE


@pytest.mark.asyncio
async def test_zero_runtime_upload_limit_rejects_even_empty_file(
    tmp_path: Path, monkeypatch
):
    """Admin value 0 is a hard upload kill switch, including zero-byte files."""
    runtime_config.set("workspace_upload_max_bytes", 0)
    workspace = tmp_path / "alice" / "claude"
    workspace.mkdir(parents=True)

    async def allow_auth(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tf, "verify_api_key", allow_auth)
    monkeypatch.setattr(tf, "_ensure_api_key", lambda: None)
    monkeypatch.setattr(
        tf.workspace_manager,
        "resolve",
        lambda user, backend=None: workspace,
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/files/upload",
            "headers": [(b"x-user-email", b"alice")],
        }
    )
    upload = UploadFile(filename="empty.bin", file=BytesIO(b""))

    with pytest.raises(HTTPException) as exc_info:
        await tf.upload_file(request=request, file=upload, credentials=None)

    assert exc_info.value.status_code == 413
    assert not (workspace / "empty.bin").exists()


def test_negative_runtime_upload_limit_is_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        runtime_config.set("workspace_upload_max_bytes", -1)


def test_reset_restores_startup_workspace_upload_limit():
    runtime_config.set("workspace_upload_max_bytes", 1024)
    runtime_config.reset("workspace_upload_max_bytes")

    assert runtime_config.get("workspace_upload_max_bytes") == WORKSPACE_UPLOAD_MAX_BYTES
    assert terminal_files._max_upload_bytes() == WORKSPACE_UPLOAD_MAX_BYTES


def test_zero_limit_disables_uploads_and_is_published(monkeypatch):
    """``0`` is a real answer: uploads off, and ``/files/limits`` says so.

    A client sizes its picker against the published number, so a disabled
    deployment must publish 0 rather than a value whose every use ends in a 413.
    """
    runtime_config.set("workspace_upload_max_bytes", 0)
    try:
        assert get_workspace_upload_max_bytes() == 0
        assert tf._max_upload_bytes() == 0
        # The request boundary leaves multipart envelope room. The route owns
        # the final file-byte contract and rejects even a zero-byte UploadFile.
        assert (
            get_workspace_upload_request_max_bytes()
            == WORKSPACE_UPLOAD_MULTIPART_RESERVE
        )
    finally:
        runtime_config.reset_all()


def test_negative_limit_is_rejected():
    with pytest.raises(ValueError):
        runtime_config.set("workspace_upload_max_bytes", -1)
