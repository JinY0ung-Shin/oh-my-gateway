"""Tests to fill coverage gaps across multiple modules.

Only contains tests for branches NOT already covered by existing test files.
"""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# admin_service — _resolve_workspace_root fallback branches
# ---------------------------------------------------------------------------


class TestResolveWorkspaceRoot:
    def test_fallback_to_claude_backend_cwd(self, tmp_path):
        """When CLAUDE_CWD is not set, _resolve_workspace_root falls back to Claude backend."""
        from src.admin_service import _resolve_workspace_root
        from src.backends.base import BackendRegistry

        fake_backend = SimpleNamespace(cwd=str(tmp_path))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CWD", None)
            with patch.object(BackendRegistry, "is_registered", return_value=True):
                with patch.object(BackendRegistry, "get", return_value=fake_backend):
                    result = _resolve_workspace_root()
                    assert result == tmp_path.resolve()

    def test_fallback_exception_returns_none(self):
        """When backend lookup raises, returns None."""
        from src.admin_service import _resolve_workspace_root
        from src.backends.base import BackendRegistry

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CWD", None)
            with patch.object(BackendRegistry, "is_registered", side_effect=RuntimeError("boom")):
                result = _resolve_workspace_root()
                assert result is None

    def test_get_workspace_root_raises_when_none(self):
        """get_workspace_root raises RuntimeError when no root found."""
        from src.admin_service import get_workspace_root

        with patch("src.admin_service._resolve_workspace_root", return_value=None):
            with pytest.raises(RuntimeError, match="Workspace root"):
                get_workspace_root()

    def test_env_cwd_not_a_dir(self, tmp_path):
        """CLAUDE_CWD pointing to a non-directory falls through to backend."""
        from src.admin_service import _resolve_workspace_root
        from src.backends.base import BackendRegistry

        fake_file = tmp_path / "not-a-dir.txt"
        fake_file.write_text("file")
        with patch.dict(os.environ, {"CLAUDE_CWD": str(fake_file)}):
            with patch.object(BackendRegistry, "is_registered", return_value=False):
                result = _resolve_workspace_root()
                assert result is None


# ---------------------------------------------------------------------------
# admin_service — _has_symlink_ancestor
# ---------------------------------------------------------------------------


class TestHasSymlinkAncestor:
    def test_outside_root_returns_true(self, tmp_path):
        from src.admin_service import _has_symlink_ancestor

        outside = Path("/completely/outside")
        assert _has_symlink_ancestor(outside, tmp_path) is True

    def test_no_symlinks_returns_false(self, tmp_path):
        from src.admin_service import _has_symlink_ancestor

        child = tmp_path / "a" / "b"
        child.mkdir(parents=True)
        assert _has_symlink_ancestor(child, tmp_path) is False


# ---------------------------------------------------------------------------
# admin_service — list_workspace_files: symlink parent in rglob
# ---------------------------------------------------------------------------


class TestListWorkspaceFilesEdgeCases:
    def test_child_with_symlink_parent_excluded(self, tmp_path):
        """Files under a symlink parent component in rglob are excluded."""
        from src.admin_service import list_workspace_files

        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "normal.md").write_text("ok")

        external = tmp_path / "_ext"
        external.mkdir()
        (external / "evil.md").write_text("leak")
        (agents_dir / "subdir").symlink_to(external)

        with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
            files = list_workspace_files()
            paths = [f["path"] for f in files]
            assert ".claude/agents/normal.md" in paths
            assert not any("evil" in p for p in paths)

        # cleanup symlink target
        shutil.rmtree(external, ignore_errors=True)


# ---------------------------------------------------------------------------
# admin_service — get_session_messages: dict content parts
# ---------------------------------------------------------------------------


class TestGetSessionMessagesDictParts:
    def test_dict_image_url_part(self, isolated_session_manager):
        """Message content with dict image_url parts renders as [Image]."""
        from src.admin_service import get_session_messages

        session = isolated_session_manager.get_or_create_session("test-dict-parts")
        session.messages.append(
            SimpleNamespace(
                role="user",
                content=[
                    {"type": "text", "text": "Check this image"},
                    {"type": "image_url", "url": "data:image/png;base64,abc"},
                ],
                name=None,
            )
        )
        result = get_session_messages("test-dict-parts")
        assert result is not None
        assert "[Image]" in result[0]["content"]
        assert "Check this image" in result[0]["content"]


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
# admin_service — export_session_json multimodal content
# ---------------------------------------------------------------------------


class TestExportSessionJsonMultimodal:
    def test_export_multimodal_message(self, isolated_session_manager):
        from src.admin_service import export_session_json

        session = isolated_session_manager.get_or_create_session("test-export-mm")
        session.messages.append(
            SimpleNamespace(
                role="user",
                content=[
                    SimpleNamespace(type="text", text="Look at this"),
                    SimpleNamespace(type="image_url", text=None),
                ],
                name=None,
            )
        )
        result = export_session_json("test-export-mm")
        assert result is not None
        assert len(result["messages"]) == 1
        # Verify image_url part is handled (redacted in export)
        content_parts = result["messages"][0]["content"]
        assert isinstance(content_parts, list)


# ---------------------------------------------------------------------------
# admin_service — list_skills: oversized file excluded
# ---------------------------------------------------------------------------


class TestListSkillsOversized:
    def test_oversized_skill_file_excluded(self, tmp_path):
        from src.admin_service import MAX_FILE_SIZE, list_skills

        skills_dir = tmp_path / ".claude" / "skills" / "big-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_bytes(b"x" * (MAX_FILE_SIZE + 1))

        with patch("src.admin_service.get_workspace_root", return_value=tmp_path):
            result = list_skills()
            assert len(result) == 0


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


