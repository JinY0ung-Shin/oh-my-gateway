"""Tests for plugin MCP credential overlays."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src import mcp_plugin_overlay, mcp_plugin_overlay_service
from src.mcp_plugin_overlay_service import McpPluginOverlayError


@pytest.fixture
def overlay_file(tmp_path, monkeypatch):
    path = tmp_path / "plugin-overlay.json"
    monkeypatch.setenv("GATEWAY_MCP_PLUGIN_OVERLAY", str(path))
    mcp_plugin_overlay.reload_overlays()
    yield path
    mcp_plugin_overlay.reload_overlays()


def _plugin_entry(name="ctx", plugin_id="p@mp", config=None):
    return {
        "plugin_id": plugin_id,
        "plugin_name": "p",
        "marketplace": "mp",
        "scope": "user",
        "origin": "managed",
        "server_name": name,
        "config": config
        or {"type": "stdio", "command": "npx", "args": ["-y", "svc"], "env": {"BASE": "1"}},
    }


class TestOverlayStore:
    def test_upsert_and_get(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay(
            "ctx", env={"TOKEN": "secret"}, headers={}
        )
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
        with patch(
            "src.plugin_service.list_plugin_mcp_servers", return_value=[]
        ):
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


class TestOverlayService:
    def test_reject_non_plugin(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers", return_value=[]
        ):
            with pytest.raises(McpPluginOverlayError, match="not a plugin"):
                mcp_plugin_overlay_service.upsert_overlay(
                    "x", env={"A": "1"}
                )

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


class TestClaudeMerge:
    def test_merge_plugin_overlays_into_mcp_servers(self, overlay_file, monkeypatch):
        monkeypatch.setenv("TOK", "from-env")
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
            class _Opts:
                def __init__(self):
                    self.env = {}
                    self.mcp_servers = None
                    self.allowed_tools = None

            options = _Opts()
            merged = cli._merge_plugin_mcp_overlays(
                {"gw": {"type": "stdio", "command": "gw"}}, options
            )
            assert "gw" in merged
            assert "ctx" in merged
            assert merged["ctx"]["env"]["TOKEN"] == "{{env:TOK}}"
            # Process env injection resolves templates.
            assert options.env["TOKEN"] == "from-env"
            assert options.env["PLAIN"] == "x"
