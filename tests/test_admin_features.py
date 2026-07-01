"""Tests for admin panel features: backends, MCP, metrics, tools, sandbox, sessions."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.admin_service import (
    compute_mcp_server_reach,
    compute_plugin_mcp_reach,
    export_session_json,
    get_dropped_mcp_servers,
    get_mcp_servers_detail,
    get_plugin_mcp_servers_detail,
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
        servers = {
            "test-server": {"type": "stdio", "command": "node", "args": ["server.js"]}
        }
        patterns = ["mcp__test-server__tool1"]
        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_config.get_mcp_tool_patterns", return_value=patterns),
        ):
            result = get_mcp_servers_detail()
        assert len(result) == 1
        assert result[0]["name"] == "test-server"
        assert result[0]["type"] == "stdio"

    def test_dash_name_tool_pattern_regression(self, clean_registry):
        """Dashed server name must normalise dash->underscore in the tool pattern.

        The tool namespace convention is ``mcp__<safe_name>__*`` with dashes
        turned into underscores. ``get_mcp_tool_patterns`` already emits the
        normalised form, so ``tools`` (which filters patterns by the normalised
        prefix) and ``pattern`` must both use ``my_server`` — never ``my-server``.
        """
        servers = {"my-server": {"type": "stdio", "command": "node"}}
        # get_mcp_tool_patterns is the real normaliser; call it for fidelity.
        from src.mcp_config import get_mcp_tool_patterns

        patterns = get_mcp_tool_patterns(servers)
        assert patterns == ["mcp__my_server__*"]  # sanity: no dash leaks through

        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_config.get_mcp_tool_patterns", return_value=patterns),
        ):
            result = get_mcp_servers_detail()

        assert len(result) == 1
        entry = result[0]
        assert entry["name"] == "my-server"  # original name is preserved
        assert entry["tools"] == ["mcp__my_server__*"]  # the regression assertion
        assert "my-server" not in entry["tools"][0]
        assert entry["pattern"] == "mcp__my_server__*"

    def test_source_env_and_editable_false(self, clean_registry):
        """A server absent from the manifest is env-sourced and not editable."""
        servers = {"envonly": {"type": "stdio", "command": "node"}}
        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_manifest.list_servers", return_value={}),
        ):
            result = get_mcp_servers_detail()
        assert result[0]["source"] == "env"
        assert result[0]["editable"] is False

    def test_source_manifest_and_editable_true(self, clean_registry):
        """A server present in the manifest is manifest-sourced and editable."""
        cfg = {"type": "stdio", "command": "node"}
        servers = {"managed": cfg}
        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_manifest.list_servers", return_value={"managed": cfg}),
        ):
            result = get_mcp_servers_detail()
        assert result[0]["source"] == "manifest"
        assert result[0]["editable"] is True

    def test_config_redacts_headers_and_env_tokens(self, clean_registry):
        """Redacted config: every header value + secret-looking env values are masked."""
        servers = {
            "remote": {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer sk-super-secret"},
                "env": {"MY_TOKEN": "abc123", "REGION": "us-east"},
            }
        }
        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_manifest.list_servers", return_value={}),
        ):
            result = get_mcp_servers_detail()

        cfg = result[0]["config"]
        # Every headers value is redacted regardless of key name.
        assert cfg["headers"]["Authorization"] == "***REDACTED***"
        # Secret-looking env keys are redacted; benign env values pass through.
        assert cfg["env"]["MY_TOKEN"] == "***REDACTED***"
        assert cfg["env"]["REGION"] == "us-east"
        # Non-secret fields survive unchanged.
        assert cfg["url"] == "https://example.test/mcp"

    def test_reach_is_list_per_backend(self, clean_registry):
        """``reach`` is a list carrying one entry per backend with a ``reaches`` flag."""
        servers = {"srv": {"type": "stdio", "command": "node"}}
        with (
            patch("src.mcp_config.get_mcp_servers", return_value=servers),
            patch("src.mcp_manifest.list_servers", return_value={}),
        ):
            result = get_mcp_servers_detail()
        reach = result[0]["reach"]
        assert isinstance(reach, list)
        backends = {r["backend"] for r in reach}
        assert backends == {"claude", "codex", "opencode"}
        assert all("reaches" in r for r in reach)


# ---------------------------------------------------------------------------
# get_plugin_mcp_servers_detail (plugin-contributed servers, read-only)
# ---------------------------------------------------------------------------


class TestGetPluginMcpServersDetail:
    def _entries(self):
        return [
            {
                "plugin_id": "context7@official",
                "plugin_name": "context7",
                "marketplace": "official",
                "scope": "user",
                "origin": "managed",
                "server_name": "context7",
                "config": {
                    "command": "npx",
                    "args": ["-y", "@upstash/context7-mcp"],
                    "env": {"API_KEY": "sekret", "REGION": "us"},
                },
            }
        ]

    def test_empty_when_no_plugin_servers(self):
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            assert get_plugin_mcp_servers_detail() == []

    def test_source_plugin_read_only(self, clean_registry):
        with (
            patch(
                "src.plugin_service.list_plugin_mcp_servers",
                return_value=self._entries(),
            ),
            patch("src.mcp_config.get_mcp_servers", return_value={}),
        ):
            result = get_plugin_mcp_servers_detail()
        assert len(result) == 1
        row = result[0]
        assert row["source"] == "plugin"
        assert row["editable"] is False
        assert row["plugin"] == "context7@official"
        assert row["tools"] == ["mcp__context7__*"]
        assert row["pattern"] == "mcp__context7__*"
        assert row["shadowed"] is False
        assert row["valid"] is True

    def test_secret_env_redacted(self, clean_registry):
        with (
            patch(
                "src.plugin_service.list_plugin_mcp_servers",
                return_value=self._entries(),
            ),
            patch("src.mcp_config.get_mcp_servers", return_value={}),
        ):
            row = get_plugin_mcp_servers_detail()[0]
        assert row["config"]["env"]["API_KEY"] == "***REDACTED***"
        assert row["config"]["env"]["REGION"] == "us"

    def test_shadowed_when_name_in_effective_config(self, clean_registry):
        with (
            patch(
                "src.plugin_service.list_plugin_mcp_servers",
                return_value=self._entries(),
            ),
            patch(
                "src.mcp_config.get_mcp_servers",
                return_value={"context7": {"command": "x"}},
            ),
        ):
            row = get_plugin_mcp_servers_detail()[0]
        assert row["shadowed"] is True

    def test_invalid_config_flagged(self, clean_registry):
        bad = [
            {**self._entries()[0], "server_name": "bad", "config": {"type": "stdio"}}
        ]
        with (
            patch("src.plugin_service.list_plugin_mcp_servers", return_value=bad),
            patch("src.mcp_config.get_mcp_servers", return_value={}),
        ):
            row = get_plugin_mcp_servers_detail()[0]
        assert row["valid"] is False
        assert "command" in (row["invalid_reason"] or "")

    def test_dash_name_normalised_in_pattern(self, clean_registry):
        entries = [{**self._entries()[0], "server_name": "my-plugin-server"}]
        with (
            patch("src.plugin_service.list_plugin_mcp_servers", return_value=entries),
            patch("src.mcp_config.get_mcp_servers", return_value={}),
        ):
            row = get_plugin_mcp_servers_detail()[0]
        assert row["pattern"] == "mcp__my_plugin_server__*"
        assert row["name"] == "my-plugin-server"  # original preserved

    def test_reach_is_claude_only(self, clean_registry):
        reach = compute_plugin_mcp_reach("context7")
        by = {r["backend"]: r for r in reach}
        assert set(by) == {"claude", "codex", "opencode"}
        assert by["codex"]["reaches"] is False
        assert by["opencode"]["reaches"] is False
        assert by["claude"]["mode"] == "setting_sources"

    def test_endpoint_appends_plugin_rows(self, admin_client, clean_registry):
        with (
            patch(
                "src.plugin_service.list_plugin_mcp_servers",
                return_value=self._entries(),
            ),
            patch(
                "src.mcp_config.get_mcp_servers",
                return_value={"envsrv": {"type": "stdio", "command": "node"}},
            ),
            patch("src.mcp_manifest.list_servers", return_value={}),
        ):
            r = admin_client.get("/admin/api/mcp-servers")
        assert r.status_code == 200
        sources = {s["name"]: s["source"] for s in r.json()["servers"]}
        assert sources.get("envsrv") == "env"
        assert sources.get("context7") == "plugin"


# ---------------------------------------------------------------------------
# get_dropped_mcp_servers
# ---------------------------------------------------------------------------


class TestGetDroppedMcpServers:
    def test_delegates_to_list_dropped_servers(self):
        dropped = [{"name": "bad", "type": "stdio", "reason": "boom", "source": "env"}]
        with patch("src.mcp_config.list_dropped_servers", return_value=dropped):
            result = get_dropped_mcp_servers()
        assert result == dropped

    def test_empty_when_none_dropped(self):
        with patch("src.mcp_config.list_dropped_servers", return_value=[]):
            assert get_dropped_mcp_servers() == []

    def test_swallows_errors(self):
        with patch(
            "src.mcp_config.list_dropped_servers", side_effect=RuntimeError("boom")
        ):
            assert get_dropped_mcp_servers() == []


# ---------------------------------------------------------------------------
# compute_mcp_server_reach (display-only, gated by BACKENDS + registration)
# ---------------------------------------------------------------------------


def _fake_backend(metadata=None):
    """A registrable backend stub whose runtime_metadata returns *metadata*."""
    backend = MagicMock()
    backend.runtime_metadata.return_value = metadata or {}
    return backend


def _reach_for(reach, backend):
    return next(r for r in reach if r["backend"] == backend)


class TestComputeMcpServerReach:
    def test_returns_entry_per_backend(self, clean_registry, monkeypatch):
        monkeypatch.setenv("BACKENDS", "claude")
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        assert {r["backend"] for r in reach} == {"claude", "codex", "opencode"}

    def test_claude_follows_backends_and_registration(
        self, clean_registry, monkeypatch
    ):
        from src.backends.base import BackendRegistry

        # Enabled AND registered -> reaches. clean_registry registers only
        # descriptors, so a client must be registered too.
        monkeypatch.setenv("BACKENDS", "claude")
        BackendRegistry.register("claude", _fake_backend())
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        assert _reach_for(reach, "claude")["reaches"] is True

    def test_claude_not_reached_when_not_in_backends(self, clean_registry, monkeypatch):
        from src.backends.base import BackendRegistry

        # Registered but disabled via BACKENDS -> does not reach.
        monkeypatch.setenv("BACKENDS", "codex")
        BackendRegistry.register("claude", _fake_backend())
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        assert _reach_for(reach, "claude")["reaches"] is False

    def test_claude_not_reached_when_unregistered(self, clean_registry, monkeypatch):
        # Enabled in BACKENDS but no client registered -> does not reach.
        monkeypatch.setenv("BACKENDS", "claude")
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        assert _reach_for(reach, "claude")["reaches"] is False

    def test_codex_follows_backends(self, clean_registry, monkeypatch):
        from src.backends.base import BackendRegistry

        monkeypatch.setenv("BACKENDS", "codex")
        BackendRegistry.register(
            "codex", _fake_backend({"approval_policy": "on-request"})
        )
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        codex = _reach_for(reach, "codex")
        assert codex["reaches"] is True
        assert codex["approval_policy"] == "on-request"

    def test_opencode_false_without_wrapper(self, clean_registry, monkeypatch):
        from src.backends.base import BackendRegistry

        # Managed mode but wrapper disabled -> does not reach.
        monkeypatch.setenv("BACKENDS", "opencode")
        monkeypatch.setattr(
            "src.backends.opencode.constants.use_wrapper_mcp_config",
            lambda: False,
        )
        BackendRegistry.register("opencode", _fake_backend({"mode": "managed"}))
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        assert _reach_for(reach, "opencode")["reaches"] is False

    def test_opencode_false_when_external(self, clean_registry, monkeypatch):
        from src.backends.base import BackendRegistry

        # Wrapper on but external mode -> does not reach (managed-only).
        monkeypatch.setenv("BACKENDS", "opencode")
        monkeypatch.setattr(
            "src.backends.opencode.constants.use_wrapper_mcp_config",
            lambda: True,
        )
        BackendRegistry.register("opencode", _fake_backend({"mode": "external"}))
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        assert _reach_for(reach, "opencode")["reaches"] is False

    def test_opencode_true_when_managed_and_wrapper(self, clean_registry, monkeypatch):
        from src.backends.base import BackendRegistry

        # Managed + wrapper + allowed type -> reaches.
        monkeypatch.setenv("BACKENDS", "opencode")
        monkeypatch.setattr(
            "src.backends.opencode.constants.use_wrapper_mcp_config",
            lambda: True,
        )
        BackendRegistry.register("opencode", _fake_backend({"mode": "managed"}))
        reach = compute_mcp_server_reach("srv", {"type": "stdio", "command": "node"})
        oc = _reach_for(reach, "opencode")
        assert oc["reaches"] is True
        assert oc["opencode_mode"] == "managed"

    def test_opencode_false_for_unsupported_type(self, clean_registry, monkeypatch):
        from src.backends.base import BackendRegistry

        # Managed + wrapper but a type outside ALLOWED_TYPES -> does not reach.
        monkeypatch.setenv("BACKENDS", "opencode")
        monkeypatch.setattr(
            "src.backends.opencode.constants.use_wrapper_mcp_config",
            lambda: True,
        )
        BackendRegistry.register("opencode", _fake_backend({"mode": "managed"}))
        reach = compute_mcp_server_reach("srv", {"type": "bogus"})
        assert _reach_for(reach, "opencode")["reaches"] is False


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
