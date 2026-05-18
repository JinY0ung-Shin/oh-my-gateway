"""Tests for admin panel features: backends, MCP, metrics, tools, sandbox, sessions."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.admin_service import (
    export_session_json,
    get_mcp_servers_detail,
    get_sandbox_config,
    get_session_detail,
    get_tools_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_backend():
    """Create a mock backend client."""
    backend = MagicMock()
    backend.name = "claude"
    backend.supported_models.return_value = ["opus", "sonnet"]
    backend.verify = AsyncMock(return_value=True)
    return backend


@pytest.fixture
def mock_auth_provider():
    """Create a mock auth provider."""
    provider = MagicMock()
    provider.validate.return_value = {
        "valid": True,
        "errors": [],
        "config": {"auth_method": "cli"},
    }
    provider.build_env.return_value = {"ANTHROPIC_AUTH_TOKEN": "***"}
    provider.get_isolation_vars.return_value = ["OPENAI_API_KEY"]
    return provider


@pytest.fixture
def admin_client():
    """FastAPI TestClient with admin auth bypassed."""
    from fastapi.testclient import TestClient

    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# get_backends_health (async)
# ---------------------------------------------------------------------------


class TestGetBackendsHealth:
    async def test_returns_backend_info(self, mock_backend, mock_auth_provider):
        from src.admin_service import get_backends_health
        from src.backends.base import BackendRegistry

        BackendRegistry.register("claude", mock_backend)
        try:
            with patch("src.auth.auth_manager") as mock_mgr:
                mock_mgr.get_provider.return_value = mock_auth_provider
                results = await get_backends_health()

            claude = next((b for b in results if b["name"] == "claude"), None)
            assert claude is not None
            assert claude["registered"] is True
            assert claude["healthy"] is True
            assert "opus" in claude["models"]
            assert claude["auth"]["valid"] is True
            assert claude["auth"]["method"] == "cli"
        finally:
            BackendRegistry.unregister("claude")

    async def test_unregistered_backend(self):
        from src.admin_service import get_backends_health

        with patch("src.auth.auth_manager") as mock_mgr:
            mock_mgr.get_provider.side_effect = Exception("Not available")
            results = await get_backends_health()

        # At least one backend should appear with not-registered status
        assert isinstance(results, list)
        assert len(results) > 0

    async def test_verify_failure(self, mock_backend, mock_auth_provider):
        from src.admin_service import get_backends_health
        from src.backends.base import BackendRegistry

        mock_backend.verify = AsyncMock(side_effect=RuntimeError("connection refused"))
        BackendRegistry.register("claude", mock_backend)
        try:
            with patch("src.auth.auth_manager") as mock_mgr:
                mock_mgr.get_provider.return_value = mock_auth_provider
                results = await get_backends_health()

            claude = next(b for b in results if b["name"] == "claude")
            assert claude["healthy"] is False
            assert "connection refused" in claude["health_error"]
        finally:
            BackendRegistry.unregister("claude")


# ---------------------------------------------------------------------------
# get_sandbox_config
# ---------------------------------------------------------------------------


class TestGetSandboxConfig:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = get_sandbox_config()
        assert config["permission_mode"] == "default"
        assert config["sandbox_enabled"] == "true"
        assert config["metadata_env_allowlist"] == []

    def test_custom_values(self):
        env = {
            "PERMISSION_MODE": "bypassPermissions",
            "CLAUDE_SANDBOX_ENABLED": "false",
            "METADATA_ENV_ALLOWLIST": "THREAD_ID,A2A_BASE_URL",
        }
        with patch.dict(os.environ, env, clear=True):
            config = get_sandbox_config()
        assert config["permission_mode"] == "bypassPermissions"
        assert config["sandbox_enabled"] == "false"
        assert config["metadata_env_allowlist"] == ["A2A_BASE_URL", "THREAD_ID"]


# ---------------------------------------------------------------------------
# get_tools_registry
# ---------------------------------------------------------------------------


class TestGetToolsRegistry:
    def test_includes_claude_tools(self):
        result = get_tools_registry()
        assert "claude" in result["backends"]
        claude = result["backends"]["claude"]
        assert "Bash" in claude["all_tools"]
        assert "Read" in claude["default_allowed"]
        assert len(claude["default_allowed"]) <= len(claude["all_tools"])

    def test_mcp_tools_key_present(self):
        result = get_tools_registry()
        assert "mcp_tools" in result


# ---------------------------------------------------------------------------
# get_mcp_servers_detail
# ---------------------------------------------------------------------------


class TestGetMcpServersDetail:
    def test_no_servers(self):
        with patch("src.mcp_config.get_mcp_servers", return_value={}):
            result = get_mcp_servers_detail()
        assert result == []

    def test_with_servers(self):
        servers = {"test-server": {"type": "stdio", "command": "node", "args": ["server.js"]}}
        patterns = ["mcp__test-server__tool1"]
        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_config.get_mcp_tool_patterns", return_value=patterns),
        ):
            result = get_mcp_servers_detail()
        assert len(result) == 1
        assert result[0]["name"] == "test-server"
        assert result[0]["type"] == "stdio"


# ---------------------------------------------------------------------------
# get_session_detail / export_session_json
# ---------------------------------------------------------------------------


class TestSessionDetail:
    def test_nonexistent_session(self):
        assert get_session_detail("nonexistent") is None

    def test_existing_session(self, isolated_session_manager):
        from src.session_manager import session_manager

        session = session_manager.get_or_create_session("test-session")
        session.backend = "claude"
        session.turn_counter = 3

        detail = get_session_detail("test-session")
        assert detail is not None
        assert detail["session_id"] == "test-session"
        assert detail["backend"] == "claude"
        assert detail["turn_counter"] == 3
        assert detail["created_at"] is not None

    def test_export_nonexistent(self):
        assert export_session_json("nonexistent") is None

    def test_export_existing(self, isolated_session_manager):
        from src.models import Message
        from src.session_manager import session_manager

        session = session_manager.get_or_create_session("export-test")
        session.backend = "claude"
        session.add_messages([Message(role="user", content="hello")])
        session.add_messages(
            [Message(role="assistant", content="hi there", thinking=["plan"])]
        )

        data = export_session_json("export-test")
        assert data is not None
        assert data["session_id"] == "export-test"
        assert data["backend"] == "claude"
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "hello"
        assert data["messages"][1]["thinking"] == ["plan"]


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestAdminEndpoints:
    def test_backends_endpoint(self, admin_client):
        r = admin_client.get("/admin/api/backends")
        assert r.status_code == 200
        data = r.json()
        assert "backends" in data
        assert isinstance(data["backends"], list)

    def test_mcp_servers_endpoint(self, admin_client):
        r = admin_client.get("/admin/api/mcp-servers")
        assert r.status_code == 200
        data = r.json()
        assert "servers" in data

    def test_metrics_endpoint(self, admin_client):
        r = admin_client.get("/admin/api/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "stats" in data
        assert "total_logged" in data

    def test_tools_endpoint(self, admin_client):
        r = admin_client.get("/admin/api/tools")
        assert r.status_code == 200
        data = r.json()
        assert "backends" in data
        assert "claude" in data["backends"]
        assert "mcp_tools" in data

    def test_sandbox_endpoint(self, admin_client):
        r = admin_client.get("/admin/api/sandbox")
        assert r.status_code == 200
        data = r.json()
        assert "permission_mode" in data
        assert "sandbox_enabled" in data
        assert "metadata_env_allowlist" in data

    def test_session_detail_not_found(self, admin_client):
        r = admin_client.get("/admin/api/sessions/nonexistent/detail")
        assert r.status_code == 404

    def test_session_export_not_found(self, admin_client):
        r = admin_client.get("/admin/api/sessions/nonexistent/export")
        assert r.status_code == 404

    def test_session_detail_existing(self, admin_client, isolated_session_manager):
        from src.session_manager import session_manager

        session = session_manager.get_or_create_session("detail-test")
        session.backend = "claude"

        r = admin_client.get("/admin/api/sessions/detail-test/detail")
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == "detail-test"
        assert data["backend"] == "claude"

    def test_session_export_existing(self, admin_client, isolated_session_manager):
        from src.models import Message
        from src.session_manager import session_manager

        session = session_manager.get_or_create_session("export-api-test")
        session.add_messages([Message(role="user", content="test")])

        r = admin_client.get("/admin/api/sessions/export-api-test/export")
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == "export-api-test"
        assert len(data["messages"]) == 1


# ---------------------------------------------------------------------------
# request_logger stats (p50/p99 additions)
# ---------------------------------------------------------------------------


class TestRequestLoggerStats:
    def test_percentile_stats(self):
        from src.request_logger import RequestLogEntry, RequestLogger

        logger = RequestLogger(maxlen=100)
        # Add entries with known latencies
        for i in range(100):
            logger.log(
                RequestLogEntry(
                    timestamp=1000000 + i,
                    method="GET",
                    path="/health",
                    status_code=200,
                    response_time_ms=float(i + 1),  # 1..100
                    client_ip="127.0.0.1",
                )
            )

        data = logger.query(limit=0)
        stats = data["stats"]
        assert stats["total_requests"] == 100
        assert stats["p50_latency_ms"] > 0
        assert stats["p95_latency_ms"] > 0
        assert stats["p99_latency_ms"] > 0
        assert stats["p50_latency_ms"] <= stats["p95_latency_ms"]
        assert stats["p95_latency_ms"] <= stats["p99_latency_ms"]
        assert stats["error_rate"] == 0.0

    def test_empty_stats(self):
        from src.request_logger import RequestLogger

        logger = RequestLogger(maxlen=100)
        data = logger.query(limit=0)
        stats = data["stats"]
        assert stats["total_requests"] == 0
        assert stats["p50_latency_ms"] == 0.0
        assert stats["p99_latency_ms"] == 0.0
