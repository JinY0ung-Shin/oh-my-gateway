"""Unit tests for src/mcp_admin_service.py and src/mcp_connection_test.py.

Env servers (from MCP_CONFIG) are the immutable base; only manifest-layer
servers are mutable. Every successful mutation persists to the manifest and
hot-reloads. These tests isolate the manifest under ``tmp_path`` via
``GATEWAY_MCP_MANIFEST`` and drive the env base by patching
``src.mcp_config.MCP_CONFIG`` for the duration of each service call (the
reload triggered by a mutation re-reads that env source).
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from src import mcp_admin_service, mcp_connection_test
from src.mcp_admin_service import McpAdminError


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    """Point the MCP manifest at an isolated tmp file and reload after."""
    path = tmp_path / "m.json"
    monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(path))
    yield path
    # Rebind the effective config back to the (empty) env base so a leftover
    # tmp manifest can't leak into other tests via the module singleton.
    import src.mcp_config as mcp_config

    mcp_config.reload_mcp_config()


@contextmanager
def env_servers(servers):
    """Make ``servers`` the MCP_CONFIG env base for the wrapped call(s)."""
    with patch("src.mcp_config.MCP_CONFIG", json.dumps({"mcpServers": servers})):
        yield


# ---------------------------------------------------------------------------
# validate_config: pure preview, NEVER raises
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def test_valid_stdio_gives_pattern_with_dash_to_underscore(self):
        result = mcp_admin_service.validate_config(
            "my-server", {"type": "stdio", "command": "echo"}
        )
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["normalized_name"] == "my_server"
        assert result["tool_pattern"] == "mcp__my_server__*"
        assert result["server_type"] == "stdio"

    def test_valid_remote_gives_pattern(self):
        result = mcp_admin_service.validate_config(
            "docs", {"type": "streamable-http", "url": "http://localhost:3000/mcp"}
        )
        assert result["valid"] is True
        assert result["tool_pattern"] == "mcp__docs__*"
        assert result["server_type"] == "streamable-http"

    def test_default_type_is_stdio(self):
        result = mcp_admin_service.validate_config("plain", {"command": "ls"})
        assert result["valid"] is True
        assert result["server_type"] == "stdio"

    def test_invalid_config_not_a_dict(self):
        result = mcp_admin_service.validate_config("x", ["not", "a", "dict"])
        assert result["valid"] is False
        assert any("JSON object" in e for e in result["errors"])
        assert result["server_type"] is None

    def test_invalid_missing_required_field(self):
        result = mcp_admin_service.validate_config("bad", {"type": "stdio"})
        assert result["valid"] is False
        assert result["errors"]

    def test_invalid_unsupported_type(self):
        result = mcp_admin_service.validate_config(
            "weird", {"type": "grpc", "command": "foo"}
        )
        assert result["valid"] is False
        assert any("unsupported type" in e for e in result["errors"])

    def test_invalid_name_reported(self):
        result = mcp_admin_service.validate_config(
            "bad name!", {"type": "stdio", "command": "echo"}
        )
        assert result["valid"] is False
        assert any("invalid name" in e for e in result["errors"])

    def test_never_raises_on_none_inputs(self):
        # No name, no config -> reports invalid but must not raise.
        result = mcp_admin_service.validate_config(None, None)
        assert result["valid"] is False
        assert result["normalized_name"] == ""
        assert result["tool_pattern"] == ""


# ---------------------------------------------------------------------------
# create_server
# ---------------------------------------------------------------------------


class TestCreateServer:
    def test_happy_path_returns_created_and_patterns(self, manifest_file):
        with env_servers({}):
            result = mcp_admin_service.create_server(
                "my-router", {"type": "stdio", "command": "echo"}
            )
        assert result["status"] == "created"
        assert result["server"] == "my-router"
        assert result["patterns"] == ["mcp__my_router__*"]
        # Persisted to the manifest.
        from src import mcp_manifest

        assert "my-router" in mcp_manifest.list_servers()

    def test_duplicate_raises_already_exists(self, manifest_file):
        with env_servers({}):
            mcp_admin_service.create_server("dup", {"type": "stdio", "command": "echo"})
            with pytest.raises(McpAdminError, match="already exists"):
                mcp_admin_service.create_server(
                    "dup", {"type": "stdio", "command": "ls"}
                )

    def test_env_name_is_not_editable(self, manifest_file):
        with env_servers({"envsrv": {"type": "stdio", "command": "echo"}}):
            with pytest.raises(McpAdminError, match="not editable"):
                mcp_admin_service.create_server(
                    "envsrv", {"type": "stdio", "command": "ls"}
                )

    def test_collision_dash_vs_underscore(self, manifest_file):
        # Env base has "foo_bar"; a new "foo-bar" maps to the same
        # mcp__foo_bar__* namespace and must be rejected.
        with env_servers({"foo_bar": {"type": "stdio", "command": "echo"}}):
            with pytest.raises(McpAdminError, match="collides"):
                mcp_admin_service.create_server(
                    "foo-bar", {"type": "stdio", "command": "ls"}
                )

    def test_bad_name_raises(self, manifest_file):
        with env_servers({}):
            with pytest.raises(McpAdminError, match="invalid server name"):
                mcp_admin_service.create_server(
                    "has space", {"type": "stdio", "command": "echo"}
                )

    def test_bad_type_raises(self, manifest_file):
        with env_servers({}):
            with pytest.raises(McpAdminError):
                mcp_admin_service.create_server(
                    "weird", {"type": "carrier-pigeon", "command": "fly"}
                )

    def test_config_not_a_dict_raises(self, manifest_file):
        with env_servers({}):
            with pytest.raises(McpAdminError, match="JSON object"):
                mcp_admin_service.create_server("x", ["nope"])


# ---------------------------------------------------------------------------
# update_server
# ---------------------------------------------------------------------------


class TestUpdateServer:
    def test_unknown_raises_not_found(self, manifest_file):
        with env_servers({}):
            with pytest.raises(McpAdminError, match="not found"):
                mcp_admin_service.update_server(
                    "ghost", {"type": "stdio", "command": "echo"}
                )

    def test_env_name_not_editable(self, manifest_file):
        with env_servers({"envsrv": {"type": "stdio", "command": "echo"}}):
            with pytest.raises(McpAdminError, match="not editable"):
                mcp_admin_service.update_server(
                    "envsrv", {"type": "stdio", "command": "ls"}
                )

    def test_update_existing_manifest_server(self, manifest_file):
        with env_servers({}):
            mcp_admin_service.create_server("svc", {"type": "stdio", "command": "echo"})
            result = mcp_admin_service.update_server(
                "svc", {"type": "stdio", "command": "ls"}
            )
        assert result["status"] == "saved"
        from src import mcp_manifest

        assert mcp_manifest.get_server("svc")["command"] == "ls"


# ---------------------------------------------------------------------------
# delete_server
# ---------------------------------------------------------------------------


class TestDeleteServer:
    def test_unknown_raises_not_found(self, manifest_file):
        with env_servers({}):
            with pytest.raises(McpAdminError, match="not found"):
                mcp_admin_service.delete_server("ghost")

    def test_env_only_cannot_be_deleted(self, manifest_file):
        with env_servers({"envsrv": {"type": "stdio", "command": "echo"}}):
            with pytest.raises(McpAdminError, match="cannot be deleted"):
                mcp_admin_service.delete_server("envsrv")

    def test_manifest_server_is_deleted(self, manifest_file):
        with env_servers({}):
            mcp_admin_service.create_server("svc", {"type": "stdio", "command": "echo"})
            result = mcp_admin_service.delete_server("svc")
        assert result["status"] == "deleted"
        assert result["server"] == "svc"
        from src import mcp_manifest

        assert "svc" not in mcp_manifest.list_servers()


# ---------------------------------------------------------------------------
# test_connection (async): never raises; stdio non-spawn default; remote probe
# ---------------------------------------------------------------------------


class TestConnection:
    async def test_unknown_server_returns_not_found(self, manifest_file):
        with env_servers({}):
            import src.mcp_config as mcp_config

            mcp_config.reload_mcp_config()  # ensure singleton has no such server
            result = await mcp_admin_service.test_connection("nope")
        assert result["ok"] is False
        assert "not found" in result["detail"]

    async def test_stdio_resolvable_not_spawned(self, manifest_file, monkeypatch):
        # shutil.which resolves -> reachable; default MCP_TEST_ALLOW_SPAWN is
        # False so it must NOT spawn.
        monkeypatch.setattr(
            mcp_connection_test.shutil, "which", lambda exe: "/usr/bin/echo"
        )
        assert mcp_connection_test.MCP_TEST_ALLOW_SPAWN is False
        with env_servers({}):
            mcp_admin_service.create_server("cli", {"type": "stdio", "command": "echo"})
            result = await mcp_admin_service.test_connection("cli")
        assert result["ok"] is True
        assert "not spawned" in result["detail"]
        assert result["transport"] == "stdio"

    async def test_stdio_command_not_on_path(self, manifest_file, monkeypatch):
        monkeypatch.setattr(mcp_connection_test.shutil, "which", lambda exe: None)
        with env_servers({}):
            mcp_admin_service.create_server(
                "cli", {"type": "stdio", "command": "definitely-not-a-real-binary"}
            )
            result = await mcp_admin_service.test_connection("cli")
        assert result["ok"] is False
        assert "not found on PATH" in result["detail"]

    async def test_remote_any_status_is_reachable(self, manifest_file, monkeypatch):
        class FakeResponse:
            status_code = 405

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get(self, url, headers=None):
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        with env_servers({}):
            mcp_admin_service.create_server(
                "docs", {"type": "http", "url": "http://localhost:3000/mcp"}
            )
            result = await mcp_admin_service.test_connection("docs")
        assert result["ok"] is True
        assert "405" in result["detail"]
        assert result["transport"] == "http"

    async def test_remote_connect_error_is_unreachable(
        self, manifest_file, monkeypatch
    ):
        class BoomClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get(self, url, headers=None):
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "AsyncClient", BoomClient)
        with env_servers({}):
            mcp_admin_service.create_server(
                "docs", {"type": "http", "url": "http://localhost:3000/mcp"}
            )
            result = await mcp_admin_service.test_connection("docs")
        assert result["ok"] is False
        assert "connect failed" in result["detail"]

    async def test_remote_timeout_path(self, monkeypatch):
        # Drive the probe's timeout branch directly with a tiny timeout and a
        # slow fake client (test_connection hardcodes the 5s default).
        import asyncio

        class SlowClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get(self, url, headers=None):
                await asyncio.sleep(5)

        monkeypatch.setattr(httpx, "AsyncClient", SlowClient)
        result = await mcp_connection_test.test_mcp_server(
            "docs", {"type": "http", "url": "http://x"}, timeout=0.05
        )
        assert result["ok"] is False
        assert "timed out" in result["detail"]
