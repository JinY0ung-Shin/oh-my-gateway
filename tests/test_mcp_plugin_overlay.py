"""Tests for plugin MCP credential overlays."""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from src import mcp_plugin_overlay, mcp_plugin_overlay_service
from src.mcp_plugin_overlay_service import (
    McpPluginOverlayError,
    McpPluginOverlayNotFound,
)


@pytest.fixture
def overlay_file(tmp_path, monkeypatch):
    path = tmp_path / "plugin-overlay.json"
    monkeypatch.setenv("GATEWAY_MCP_PLUGIN_OVERLAY", str(path))
    mcp_plugin_overlay.reload_overlays()
    yield path
    # This teardown runs BEFORE monkeypatch undo, so re-point the store at a
    # path that never exists and reload — otherwise overlays this test saved
    # would stay in the module singleton and leak into later tests.
    monkeypatch.setenv("GATEWAY_MCP_PLUGIN_OVERLAY", str(tmp_path / "absent.json"))
    mcp_plugin_overlay.reload_overlays()


def _plugin_entry(name="ctx", plugin_id="p@mp", config=None, install_path=None):
    return {
        "plugin_id": plugin_id,
        "plugin_name": "p",
        "marketplace": "mp",
        "scope": "user",
        "origin": "managed",
        "server_name": name,
        "config": config
        or {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "svc"],
            "env": {"BASE": "1"},
        },
        "install_path": install_path,
    }


class TestOverlayStore:
    def test_upsert_and_get(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "secret"}, headers={})
        assert mcp_plugin_overlay.get_overlay("ctx")["env"]["TOKEN"] == "secret"
        assert "ctx" in mcp_plugin_overlay.list_overlay_names()

    def test_empty_overlay_deletes(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"A": "1"})
        mcp_plugin_overlay.upsert_overlay("ctx", env={}, headers={})
        assert mcp_plugin_overlay.get_overlay("ctx") == {}

    def test_merge_overlay_into_config(self):
        base = {
            "type": "stdio",
            "command": "echo",
            "env": {"BASE": "1", "SHARED": "old"},
        }
        overlay = {"env": {"SHARED": "new", "EXTRA": "x"}}
        merged = mcp_plugin_overlay.merge_overlay_into_config(base, overlay)
        assert merged["env"]["BASE"] == "1"
        assert merged["env"]["SHARED"] == "new"
        assert merged["env"]["EXTRA"] == "x"
        assert base["env"]["SHARED"] == "old"  # no mutate

    def test_materialize_skips_stale(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("gone", env={"T": "1"})
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            assert mcp_plugin_overlay.materialize_overlaid_plugin_servers() == {}

    def test_materialize_merges(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "{{env:TOK}}"})
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            out = mcp_plugin_overlay.materialize_overlaid_plugin_servers()
        assert out["ctx"]["command"] == "npx"
        assert out["ctx"]["env"]["BASE"] == "1"
        assert out["ctx"]["env"]["TOKEN"] == "{{env:TOK}}"

    def test_materialize_expands_plugin_root(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "t"})
        entry = _plugin_entry(
            config={
                "type": "stdio",
                "command": "${CLAUDE_PLUGIN_ROOT}/bin/serve",
                "args": ["--root", "${CLAUDE_PLUGIN_ROOT}/data"],
                "env": {"BASE": "${CLAUDE_PLUGIN_ROOT}/cfg"},
            },
            install_path="/opt/plugins/p",
        )
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[entry]):
            out = mcp_plugin_overlay.materialize_overlaid_plugin_servers()
        assert out["ctx"]["command"] == "/opt/plugins/p/bin/serve"
        assert out["ctx"]["args"] == ["--root", "/opt/plugins/p/data"]
        assert out["ctx"]["env"]["BASE"] == "/opt/plugins/p/cfg"
        assert out["ctx"]["env"]["TOKEN"] == "t"

    def test_materialize_plugin_server_single(self, overlay_file):
        """Single-server materialization works with and without an overlay."""
        entry = _plugin_entry(
            config={"type": "stdio", "command": "${CLAUDE_PLUGIN_ROOT}/run"},
            install_path="/opt/plugins/p",
        )
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[entry]):
            cfg = mcp_plugin_overlay.materialize_plugin_server("ctx")
            assert cfg == {"type": "stdio", "command": "/opt/plugins/p/run"}
            assert mcp_plugin_overlay.materialize_plugin_server("nope") is None

            mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "t"})
            cfg = mcp_plugin_overlay.materialize_plugin_server("ctx")
            assert cfg["env"]["TOKEN"] == "t"
            assert cfg["command"] == "/opt/plugins/p/run"


class TestOverlayService:
    def test_reject_non_plugin(self, overlay_file):
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            with pytest.raises(McpPluginOverlayError, match="not a plugin"):
                mcp_plugin_overlay_service.upsert_overlay("x", env={"A": "1"})

    def test_reject_non_string_env(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            with pytest.raises(McpPluginOverlayError, match="env"):
                mcp_plugin_overlay_service.upsert_overlay(
                    "ctx", env={"A": 1}  # type: ignore[dict-item]
                )

    def test_happy_path(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            result = mcp_plugin_overlay_service.upsert_overlay(
                "ctx", env={"TOKEN": "s3cret"}
            )
        assert result["status"] == "saved"
        assert result["server"] == "ctx"
        # Redacted in public response (TOKEN key is secret-looking).
        assert result["overlay"]["env"]["TOKEN"] == "***REDACTED***"
        # Stored raw.
        assert mcp_plugin_overlay.get_overlay("ctx")["env"]["TOKEN"] == "s3cret"

    def test_redacted_roundtrip_preserves_secret(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay(
                "ctx", env={"TOKEN": "real", "REGION": "us"}
            )
            mcp_plugin_overlay_service.upsert_overlay(
                "ctx",
                env={"TOKEN": "***REDACTED***", "REGION": "eu"},
            )
        stored = mcp_plugin_overlay.get_overlay("ctx")
        assert stored["env"]["TOKEN"] == "real"
        assert stored["env"]["REGION"] == "eu"

    def test_delete(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay("ctx", env={"A": "1"})
            mcp_plugin_overlay_service.delete_overlay("ctx")
        assert mcp_plugin_overlay.get_overlay("ctx") == {}
        with pytest.raises(McpPluginOverlayError, match="no overlay"):
            mcp_plugin_overlay_service.delete_overlay("ctx")

    def test_not_found_error_types(self, overlay_file):
        """Missing targets raise the NotFound subtype (routes map it to 404)."""
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            with pytest.raises(McpPluginOverlayNotFound):
                mcp_plugin_overlay_service.upsert_overlay("x", env={"A": "1"})
        with pytest.raises(McpPluginOverlayNotFound):
            mcp_plugin_overlay_service.delete_overlay("nope")

    def test_reject_placeholder_for_unknown_key(self, overlay_file):
        """A ***REDACTED*** value with no stored secret behind it is an error,
        not something to persist literally (e.g. a renamed key)."""
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay("ctx", env={"TOKEN": "real"})
            with pytest.raises(McpPluginOverlayError, match="placeholder"):
                mcp_plugin_overlay_service.upsert_overlay(
                    "ctx", env={"RENAMED": "***REDACTED***"}
                )
        # Stored overlay unchanged.
        assert mcp_plugin_overlay.get_overlay("ctx")["env"] == {"TOKEN": "real"}


class TestClaudeMerge:
    def test_merge_plugin_overlays_into_mcp_servers(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay(
            "ctx", env={"TOKEN": "{{env:TOK}}", "PLAIN": "x"}
        )
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from src.backends.claude.client import ClaudeCodeCLI

            # Minimal instance without full init
            cli = object.__new__(ClaudeCodeCLI)
            merged = cli._merge_plugin_mcp_overlays(
                {"gw": {"type": "stdio", "command": "gw"}}
            )
            assert "gw" in merged
            assert "ctx" in merged
            # Env refs stay templated here; _configure_mcp_servers resolves
            # the merged map at session create.
            assert merged["ctx"]["env"]["TOKEN"] == "{{env:TOK}}"
            assert merged["ctx"]["env"]["PLAIN"] == "x"

    def test_stale_overlay_materializes_nothing(self, overlay_file):
        """An overlay without an installed plugin does not materialize."""
        mcp_plugin_overlay.upsert_overlay("gone", env={"LEAK": "v"})
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            merged = cli._merge_plugin_mcp_overlays(
                {"gw": {"type": "stdio", "command": "gw"}}
            )
        assert merged == {"gw": {"type": "stdio", "command": "gw"}}

    def test_materialization_logs_replacement(self, overlay_file, caplog):
        """Materialization notes that the CLI runs the materialized copy in
        place of the plugin's own setting_sources registration (verified live
        on CLI 2.1.187, the pinned SDK bundle)."""
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "t"})
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            with caplog.at_level(logging.INFO):
                cli._merge_plugin_mcp_overlays(None)
        assert any(
            "replaces the plugin's own setting_sources registration"
            in r.getMessage()
            for r in caplog.records
        )

    def test_remote_header_overlay_lands_in_config(self, overlay_file):
        """Header overlays ride the materialized config — the copy the CLI
        actually registers and runs — so they apply to remote plugin servers."""
        mcp_plugin_overlay.upsert_overlay(
            "ctx", headers={"Authorization": "Bearer x"}
        )
        entry = _plugin_entry(config={"type": "http", "url": "https://mcp.example"})
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[entry]):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            merged = cli._merge_plugin_mcp_overlays(None)
        assert merged["ctx"]["headers"]["Authorization"] == "Bearer x"

    def test_overlay_env_never_reaches_session_process_env(self, overlay_file):
        """Regression for the scoped-env fix: overlay credentials must land in
        the materialized server config only. If session-process env injection
        is ever reintroduced, the agent's Bash would see the secret via `env`
        — this asserts options.env stays clean through the real MCP configure
        path."""
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "s3cret"})
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from claude_agent_sdk import ClaudeAgentOptions

            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            options = ClaudeAgentOptions()
            cli._configure_mcp_servers(options, None, ["mcp__ctx__*"])
        assert options.mcp_servers["ctx"]["env"]["TOKEN"] == "s3cret"
        assert "TOKEN" not in options.env


class TestOverlayRoutes:
    @pytest.fixture
    def admin_client(self):
        """FastAPI TestClient with admin auth bypassed (same shape as
        the fixture in test_admin_features)."""
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
            from src.admin_auth import require_admin
            from src.main import app

            app.dependency_overrides[require_admin] = lambda: True
            client = TestClient(app)
            yield client
            app.dependency_overrides.pop(require_admin, None)

    def test_requires_admin(self, overlay_file):
        from fastapi.testclient import TestClient

        from src.main import app

        client = TestClient(app)
        r = client.get("/admin/api/mcp-servers/ctx/plugin-overlay")
        assert r.status_code == 401

    def test_put_get_delete_roundtrip(self, overlay_file, admin_client):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            r = admin_client.put(
                "/admin/api/mcp-servers/ctx/plugin-overlay",
                json={"env": {"TOKEN": "s3cret"}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "saved"
            # Secrets never echo back.
            assert body["overlay"]["env"]["TOKEN"] == "***REDACTED***"

            r = admin_client.get("/admin/api/mcp-servers/ctx/plugin-overlay")
            assert r.status_code == 200
            detail = r.json()
            assert detail["exists"] is True
            assert detail["overlay"]["env"]["TOKEN"] == "***REDACTED***"

        r = admin_client.delete("/admin/api/mcp-servers/ctx/plugin-overlay")
        assert r.status_code == 200
        r = admin_client.delete("/admin/api/mcp-servers/ctx/plugin-overlay")
        assert r.status_code == 404

    def test_put_non_plugin_is_404(self, overlay_file, admin_client):
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            r = admin_client.put(
                "/admin/api/mcp-servers/ghost/plugin-overlay",
                json={"env": {"A": "1"}},
            )
        assert r.status_code == 404

    def test_put_invalid_name_is_400(self, overlay_file, admin_client):
        r = admin_client.put(
            "/admin/api/mcp-servers/bad%20name/plugin-overlay",
            json={"env": {"A": "1"}},
        )
        assert r.status_code == 400
