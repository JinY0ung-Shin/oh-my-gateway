"""Runtime-admin control for the workspace upload ceiling."""

import pytest

from src.constants import WORKSPACE_UPLOAD_MAX_BYTES
from src.routes import terminal_files
from src.runtime_config import runtime_config


@pytest.fixture(autouse=True)
def clean_runtime_override():
    runtime_config.reset_all()
    yield
    runtime_config.reset_all()


def test_upload_limit_is_an_editable_gateway_runtime_setting(monkeypatch):
    monkeypatch.setattr(terminal_files, "MAX_REQUEST_SIZE", 64 * 1024 * 1024)

    runtime_config.set("workspace_upload_max_bytes", 3 * 1024 * 1024)

    detail = runtime_config.get_all()["workspace_upload_max_bytes"]
    assert detail["value"] == 3 * 1024 * 1024
    assert detail["overridden"] is True
    assert terminal_files._max_upload_bytes() == 3 * 1024 * 1024


def test_request_body_boundary_still_caps_admin_upload_limit(monkeypatch):
    request_cap = 2 * 1024 * 1024
    monkeypatch.setattr(terminal_files, "MAX_REQUEST_SIZE", request_cap)

    runtime_config.set("workspace_upload_max_bytes", 16 * 1024 * 1024)

    assert terminal_files._max_upload_bytes() == (
        request_cap - terminal_files._MULTIPART_ENVELOPE_RESERVE
    )


def test_zero_runtime_upload_limit_disables_file_uploads(monkeypatch):
    monkeypatch.setattr(terminal_files, "MAX_REQUEST_SIZE", 64 * 1024 * 1024)

    runtime_config.set("workspace_upload_max_bytes", 0)

    assert terminal_files._max_upload_bytes() == 0


def test_reset_restores_startup_workspace_upload_limit(monkeypatch):
    monkeypatch.setattr(terminal_files, "MAX_REQUEST_SIZE", 64 * 1024 * 1024)
    runtime_config.set("workspace_upload_max_bytes", 1024)

    runtime_config.reset("workspace_upload_max_bytes")

    assert runtime_config.get("workspace_upload_max_bytes") == WORKSPACE_UPLOAD_MAX_BYTES
    assert terminal_files.WORKSPACE_UPLOAD_MAX_BYTES == WORKSPACE_UPLOAD_MAX_BYTES
