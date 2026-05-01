"""Residual coverage for small OpenCode modules."""

import pytest

from src.backends import opencode as opencode_pkg
from src.backends.opencode import auth as opencode_auth
from src.backends.opencode import config as opencode_config
from src.backends.opencode import constants as opencode_constants


# ---------------------------------------------------------------------------
# __init__.py — lazy attribute access and registration
# ---------------------------------------------------------------------------


def test_pkg_lazy_attr_returns_client_class():
    cls = opencode_pkg.OpenCodeClient
    from src.backends.opencode.client import OpenCodeClient

    assert cls is OpenCodeClient


def test_pkg_lazy_attr_returns_auth_provider_class():
    cls = opencode_pkg.OpenCodeAuthProvider
    from src.backends.opencode.auth import OpenCodeAuthProvider

    assert cls is OpenCodeAuthProvider


def test_pkg_lazy_attr_raises_for_unknown_name():
    with pytest.raises(AttributeError, match="no attribute 'Bogus'"):
        opencode_pkg.Bogus  # noqa: B018


def test_register_uses_default_backend_registry_and_logs_failure(monkeypatch, caplog):
    """Default registry path: OpenCodeClient instantiation fails -> logs error."""

    class FakeRegistry:
        descriptors = []

        @classmethod
        def register_descriptor(cls, descriptor):
            cls.descriptors.append(descriptor)

        @classmethod
        def register(cls, name, instance):
            raise AssertionError("should not register live client when ctor raises")

    def boom(*_args, **_kwargs):
        raise RuntimeError("ctor exploded")

    monkeypatch.setattr("src.backends.opencode.client.OpenCodeClient", boom)

    with caplog.at_level("ERROR", logger="src.backends.opencode"):
        opencode_pkg.register(FakeRegistry)

    assert FakeRegistry.descriptors == [opencode_pkg.OPENCODE_DESCRIPTOR]
    assert any(
        "OpenCode backend client creation failed" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# auth.py — managed mode missing binary
# ---------------------------------------------------------------------------


def test_auth_managed_mode_reports_missing_binary(monkeypatch):
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setattr(opencode_auth.shutil, "which", lambda _name: None)

    result = opencode_auth.OpenCodeAuthProvider().validate()

    assert result == {
        "valid": False,
        "errors": ["opencode binary not found on PATH"],
        "config": {"mode": "managed"},
    }


# ---------------------------------------------------------------------------
# config.py — list-shaped command, unsupported type, malformed JSON
# ---------------------------------------------------------------------------


def test_command_list_handles_list_command_with_extra_args():
    server = {"command": ["uvx", "tool"], "args": ["--flag", 1]}
    assert opencode_config._command_list(server) == ["uvx", "tool", "--flag", "1"]


def test_convert_mcp_server_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unsupported MCP server type"):
        opencode_config._convert_mcp_server({"type": "websocket", "url": "ws://x"})


def test_parse_opencode_config_content_rejects_invalid_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        opencode_config.parse_opencode_config_content("{not json")


def test_parse_opencode_config_content_rejects_non_object_json():
    with pytest.raises(ValueError, match="must be a JSON object"):
        opencode_config.parse_opencode_config_content("[1, 2, 3]")


# ---------------------------------------------------------------------------
# constants.py — _parse_bool default branch
# ---------------------------------------------------------------------------


def test_parse_bool_returns_default_for_empty_input():
    assert opencode_constants._parse_bool("   ", default=True) is True
    assert opencode_constants._parse_bool("", default=False) is False
