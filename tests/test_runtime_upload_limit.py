"""Runtime-admin control for the workspace upload ceiling."""

import pytest

from src import concurrency_middleware
from src.constants import MAX_REQUEST_SIZE, WORKSPACE_UPLOAD_MAX_BYTES
from src.routes import terminal_files
from src.runtime_config import (
    WORKSPACE_UPLOAD_MULTIPART_RESERVE,
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


def test_upload_request_boundary_uses_the_same_runtime_setting():
    runtime_config.set("workspace_upload_max_bytes", 16 * 1024 * 1024)

    expected = 16 * 1024 * 1024 + WORKSPACE_UPLOAD_MULTIPART_RESERVE
    assert get_workspace_upload_request_max_bytes() == expected
    assert (
        concurrency_middleware._request_body_limit(
            {"type": "http", "method": "POST", "path": "/files/upload"}
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


def test_negative_runtime_upload_limit_is_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        runtime_config.set("workspace_upload_max_bytes", -1)


def test_reset_restores_startup_workspace_upload_limit():
    runtime_config.set("workspace_upload_max_bytes", 1024)
    runtime_config.reset("workspace_upload_max_bytes")

    assert runtime_config.get("workspace_upload_max_bytes") == WORKSPACE_UPLOAD_MAX_BYTES
    assert terminal_files._max_upload_bytes() == WORKSPACE_UPLOAD_MAX_BYTES
