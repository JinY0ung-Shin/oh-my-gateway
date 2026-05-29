"""Tests to fill coverage gaps across multiple modules.

Only contains tests for branches NOT already covered by existing test files.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# admin_service — get_redacted_config edge cases
# ---------------------------------------------------------------------------


class TestRedactedConfigEdgeCases:
    def test_whitespace_secret_shows_not_set(self):
        """Whitespace-only secret values show '(not set)' (covers _redact else branch)."""
        from src.admin_service import get_redacted_config

        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "   "}):
            config = get_redacted_config()
            env = config["environment"]
            assert env["ANTHROPIC_AUTH_TOKEN"] == "(not set)"

    def test_mcp_servers_in_config(self):
        """MCP servers are listed by name in config."""
        from src.admin_service import get_redacted_config

        mock_servers = {"server1": {"type": "stdio"}, "server2": {"type": "sse"}}
        with patch("src.mcp_config.get_mcp_servers", return_value=mock_servers):
            config = get_redacted_config()
            assert config["mcp_servers"] == ["server1", "server2"]

    def test_mcp_servers_exception(self):
        """MCP import failure should not crash config."""
        from src.admin_service import get_redacted_config

        with patch("src.mcp_config.get_mcp_servers", side_effect=RuntimeError("no mcp")):
            config = get_redacted_config()
            assert "runtime" in config


# ---------------------------------------------------------------------------
# admin_service — get_tools_registry MCP edge cases
# ---------------------------------------------------------------------------


class TestGetToolsRegistryMcp:
    def test_mcp_tools_exception(self):
        from src.admin_service import get_tools_registry

        with patch("src.mcp_config.get_mcp_servers", side_effect=RuntimeError("boom")):
            result = get_tools_registry()
            assert result["mcp_tools"] == []

    def test_mcp_tools_empty(self):
        from src.admin_service import get_tools_registry

        with patch("src.mcp_config.get_mcp_servers", return_value=None):
            result = get_tools_registry()
            assert result["mcp_tools"] == []


# ---------------------------------------------------------------------------
# plugin_service — _read_text edge cases
# ---------------------------------------------------------------------------


class TestPluginReadText:
    def test_read_text_symlink_rejected(self, tmp_path):
        from src.plugin_service import _read_text

        real = tmp_path / "real.txt"
        real.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        assert _read_text(link) is None

    def test_read_text_oversized(self, tmp_path):
        from src.plugin_service import _read_text

        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * (256 * 1024 + 1))
        assert _read_text(big) is None

    def test_read_text_missing(self, tmp_path):
        from src.plugin_service import _read_text

        assert _read_text(tmp_path / "nope.txt") is None

    def test_read_text_valid(self, tmp_path):
        from src.plugin_service import _read_text

        f = tmp_path / "ok.txt"
        f.write_text("hello")
        assert _read_text(f) == "hello"


# ---------------------------------------------------------------------------
# plugin_service — marketplace / blocklist edge cases
# ---------------------------------------------------------------------------


class TestPluginMarketplaceEdgeCases:
    def test_marketplaces_non_dict_entry(self, tmp_path):
        """Non-dict marketplace entries are skipped."""
        from src.plugin_service import list_marketplaces

        root = tmp_path / "plugins"
        root.mkdir()
        (root / "known_marketplaces.json").write_text(
            json.dumps({"good-mkt": {"source": {"source": "github"}}, "bad": "not-a-dict"})
        )
        with patch("src.plugin_service._plugins_root", return_value=root):
            result = list_marketplaces()
            assert len(result) == 1
            assert result[0]["name"] == "good-mkt"

    def test_blocklist_non_dict_entry(self, tmp_path):
        """Non-dict blocklist entries are skipped."""
        from src.plugin_service import get_plugin_blocklist

        root = tmp_path / "plugins"
        root.mkdir()
        (root / "blocklist.json").write_text(
            json.dumps({"plugins": [{"plugin": "bad-one", "reason": "vuln"}, "not-a-dict", 42]})
        )
        with patch("src.plugin_service._plugins_root", return_value=root):
            result = get_plugin_blocklist()
            assert len(result) == 1

    def test_blocklist_plugins_not_list(self, tmp_path):
        from src.plugin_service import get_plugin_blocklist

        root = tmp_path / "plugins"
        root.mkdir()
        (root / "blocklist.json").write_text(json.dumps({"plugins": "not-a-list"}))
        with patch("src.plugin_service._plugins_root", return_value=root):
            assert get_plugin_blocklist() == []

    def test_marketplaces_non_dict_data(self, tmp_path):
        from src.plugin_service import list_marketplaces

        root = tmp_path / "plugins"
        root.mkdir()
        (root / "known_marketplaces.json").write_text(json.dumps(["not", "a", "dict"]))
        with patch("src.plugin_service._plugins_root", return_value=root):
            assert list_marketplaces() == []

    def test_blocklist_non_dict_data(self, tmp_path):
        from src.plugin_service import get_plugin_blocklist

        root = tmp_path / "plugins"
        root.mkdir()
        (root / "blocklist.json").write_text(json.dumps(["not", "a", "dict"]))
        with patch("src.plugin_service._plugins_root", return_value=root):
            assert get_plugin_blocklist() == []


# ---------------------------------------------------------------------------
# plugin_service — get_plugin_skill_content no plugins dir
# ---------------------------------------------------------------------------


class TestPluginSkillContentEdgeCases:
    def test_no_plugins_dir(self):
        from src.plugin_service import get_plugin_skill_content

        with patch("src.plugin_service._plugins_root", return_value=None):
            assert get_plugin_skill_content("any@mkt", "skill") is None


# ---------------------------------------------------------------------------
# streaming_utils — _extract_rate_limit_status
# ---------------------------------------------------------------------------


class TestExtractRateLimitStatus:
    def test_no_rate_limit_info(self):
        from src.streaming_utils import _extract_rate_limit_status

        assert _extract_rate_limit_status({}) == "unknown"

    def test_dict_rate_limit_info(self):
        from src.streaming_utils import _extract_rate_limit_status

        chunk = {"rate_limit_info": {"status": "ok"}}
        assert _extract_rate_limit_status(chunk) == "ok"

    def test_object_rate_limit_info(self):
        from src.streaming_utils import _extract_rate_limit_status

        chunk = {"rate_limit_info": SimpleNamespace(status="rejected")}
        assert _extract_rate_limit_status(chunk) == "rejected"

    def test_dict_missing_status(self):
        from src.streaming_utils import _extract_rate_limit_status

        chunk = {"rate_limit_info": {}}
        assert _extract_rate_limit_status(chunk) == "unknown"


# ---------------------------------------------------------------------------
# streaming_utils — extract_embedded_tool_blocks fallback
# ---------------------------------------------------------------------------


class TestExtractEmbeddedToolBlocks:
    def test_generic_sdk_object_fallback(self):
        """SDK objects with type attr use fallback normalization."""
        from src.streaming_utils import extract_embedded_tool_blocks

        obj = SimpleNamespace(type="tool_use", id="t1", name="my_tool", input={})
        chunk = {"type": "assistant", "content": [obj]}
        result = extract_embedded_tool_blocks(chunk)
        assert len(result) == 1
        assert result[0]["type"] == "tool_use"
        assert result[0]["id"] == "t1"

    def test_not_assistant_chunk(self):
        from src.streaming_utils import extract_embedded_tool_blocks

        assert extract_embedded_tool_blocks({"type": "user", "content": []}) == []


# ---------------------------------------------------------------------------
# streaming_utils — _keepalive_wrapper
# ---------------------------------------------------------------------------


class TestKeepaliveWrapper:
    async def test_disabled_when_interval_zero(self):
        from src.streaming_utils import _keepalive_wrapper

        async def gen():
            yield "a"
            yield "b"

        items = []
        async for item in _keepalive_wrapper(gen(), interval=0):
            items.append(item)
        assert items == ["a", "b"]

    async def test_exception_propagated(self):
        from src.streaming_utils import _keepalive_wrapper

        async def failing_gen():
            yield "ok"
            raise ValueError("test error")

        items = []
        with pytest.raises(ValueError, match="test error"):
            async for item in _keepalive_wrapper(failing_gen(), interval=10):
                items.append(item)
        assert items == ["ok"]


