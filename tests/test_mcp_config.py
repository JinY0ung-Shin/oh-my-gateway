#!/usr/bin/env python3
"""
Unit tests for src/mcp_config.py
"""

import json
from unittest.mock import patch
from src.mcp_config import load_mcp_config, get_mcp_servers, get_mcp_tool_patterns


class TestLoadMcpConfig:
    """Test load_mcp_config() with various inputs."""

    def test_empty_config_env_returns_empty(self):
        with patch("src.mcp_config.MCP_CONFIG", ""):
            assert load_mcp_config() == {}

    def test_malformed_json_returns_empty(self):
        with patch("src.mcp_config.MCP_CONFIG", "{ malformed json }"):
            assert load_mcp_config() == {}

    def test_non_existent_file_as_json_fails_and_returns_empty(self):
        # When not a file, it's parsed as JSON string
        with patch("src.mcp_config.MCP_CONFIG", "/nonexistent/path/config.json"):
            assert load_mcp_config() == {}

    def test_valid_inline_json(self):
        config = {"mcpServers": {"test": {"type": "stdio", "command": "echo"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "test" in result
            assert result["test"]["command"] == "echo"

    def test_valid_json_file(self, tmp_path):
        config = {"mcpServers": {"file-server": {"type": "stdio", "command": "ls"}}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(config))

        with patch("src.mcp_config.MCP_CONFIG", str(config_file)):
            result = load_mcp_config()
            assert "file-server" in result
            assert result["file-server"]["command"] == "ls"

    def test_malformed_json_file_returns_empty(self, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("{ invalid file content }")

        with patch("src.mcp_config.MCP_CONFIG", str(config_file)):
            assert load_mcp_config() == {}

    def test_unsupported_server_type_is_skipped(self):
        config = {
            "mcpServers": {
                "valid": {"type": "stdio", "command": "ls"},
                "invalid": {"type": "grpc", "command": "foo"},
            }
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "valid" in result
            assert "invalid" not in result

    def test_not_a_dict_config_returns_empty(self):
        with patch("src.mcp_config.MCP_CONFIG", "[1, 2, 3]"):
            assert load_mcp_config() == {}

    def test_default_type_is_stdio(self):
        """Server without explicit type defaults to stdio and requires 'command'."""
        config = {"mcpServers": {"no-type": {"command": "echo"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "no-type" in result

    def test_stdio_missing_command_is_skipped(self):
        """stdio server without 'command' field is rejected."""
        config = {"mcpServers": {"bad-stdio": {"type": "stdio", "args": ["--foo"]}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "bad-stdio" not in result

    def test_sse_missing_url_is_skipped(self):
        """sse server without 'url' field is rejected."""
        config = {"mcpServers": {"bad-sse": {"type": "sse"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "bad-sse" not in result

    def test_http_missing_url_is_skipped(self):
        """http server without 'url' field is rejected."""
        config = {"mcpServers": {"bad-http": {"type": "http"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "bad-http" not in result

    def test_sse_with_url_is_accepted(self):
        """sse server with 'url' field is accepted."""
        config = {"mcpServers": {"good-sse": {"type": "sse", "url": "http://localhost:3000"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "good-sse" in result

    def test_http_with_url_is_accepted(self):
        """http server with 'url' field is accepted."""
        config = {"mcpServers": {"good-http": {"type": "http", "url": "http://localhost:3000"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "good-http" in result

    def test_flat_format_without_mcpServers_wrapper(self):
        """Config without mcpServers wrapper is accepted."""
        config = {"my-server": {"type": "stdio", "command": "echo"}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "my-server" in result

    def test_whitespace_only_config_returns_empty(self):
        """Whitespace-only MCP_CONFIG is treated as empty."""
        with patch("src.mcp_config.MCP_CONFIG", "   \n  "):
            assert load_mcp_config() == {}

    def test_empty_command_string_is_rejected(self):
        """stdio server with empty command string is rejected."""
        config = {"mcpServers": {"empty-cmd": {"type": "stdio", "command": ""}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "empty-cmd" not in result


class TestStreamableHttpSupport:
    """Test streamable-http transport type support."""

    def test_streamable_http_with_url_is_accepted(self):
        """streamable-http server with 'url' field is accepted."""
        config = {
            "mcpServers": {
                "sh-server": {"type": "streamable-http", "url": "http://localhost:3000/mcp"}
            }
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "sh-server" in result

    def test_streamable_http_missing_url_is_skipped(self):
        """streamable-http server without 'url' field is rejected."""
        config = {"mcpServers": {"bad-sh": {"type": "streamable-http"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "bad-sh" not in result


class TestGetMcpToolPatterns:
    """Test get_mcp_tool_patterns() symbolic tool name generation."""

    def test_empty_servers_returns_empty(self):
        assert get_mcp_tool_patterns({}) == []

    def test_single_server_pattern(self):
        servers = {"my-router": {"type": "stdio", "command": "echo"}}
        patterns = get_mcp_tool_patterns(servers)
        assert patterns == ["mcp__my_router__*"]

    def test_multiple_servers_patterns(self):
        servers = {
            "docs": {"type": "stdio", "command": "echo"},
            "mcp-router": {"type": "sse", "url": "http://localhost:3000"},
        }
        patterns = get_mcp_tool_patterns(servers)
        assert len(patterns) == 2
        assert "mcp__docs__*" in patterns
        assert "mcp__mcp_router__*" in patterns

    def test_hyphenated_names_converted_to_underscores(self):
        servers = {"my-cool-server": {"type": "stdio", "command": "echo"}}
        patterns = get_mcp_tool_patterns(servers)
        assert patterns == ["mcp__my_cool_server__*"]

    def test_underscore_names_preserved(self):
        servers = {"my_server": {"type": "stdio", "command": "echo"}}
        patterns = get_mcp_tool_patterns(servers)
        assert patterns == ["mcp__my_server__*"]


class TestGetMcpServers:
    """Test get_mcp_servers() returns the pre-loaded config."""

    def test_returns_dict(self):
        result = get_mcp_servers()
        assert isinstance(result, dict)
