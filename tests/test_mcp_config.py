#!/usr/bin/env python3
"""
Unit tests for src/mcp_config.py
"""

import json
from unittest.mock import patch

import pytest

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
        config = {
            "mcpServers": {"good-sse": {"type": "sse", "url": "http://localhost:3000"}}
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
            assert "good-sse" in result

    def test_http_with_url_is_accepted(self):
        """http server with 'url' field is accepted."""
        config = {
            "mcpServers": {
                "good-http": {"type": "http", "url": "http://localhost:3000"}
            }
        }
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
                "sh-server": {
                    "type": "streamable-http",
                    "url": "http://localhost:3000/mcp",
                }
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


class TestValidatedMcpConfig:
    """Test reusable validated MCP config access."""

    def test_returns_copy_of_loaded_servers(self, monkeypatch):
        import src.mcp_config as mcp_config

        monkeypatch.setattr(
            mcp_config,
            "_server_mcp_config",
            {"demo": {"type": "stdio", "command": "uvx"}},
        )

        result = mcp_config.get_validated_mcp_config()
        result["demo"]["command"] = "changed"

        assert mcp_config.get_validated_mcp_config()["demo"]["command"] == "uvx"


class TestValidateServer:
    """Exact accept/drop reason strings for the shared validator."""

    def test_non_dict_config_reason(self):
        import src.mcp_config as mcp_config

        ok, reason = mcp_config.validate_server("x", "not a dict")
        assert ok is False
        assert reason == "not a dict"

    def test_unsupported_type_reason(self):
        import src.mcp_config as mcp_config

        ok, reason = mcp_config.validate_server("x", {"type": "grpc", "command": "y"})
        assert ok is False
        assert reason == "unsupported type 'grpc'"

    def test_missing_field_reason_names_field_and_type(self):
        import src.mcp_config as mcp_config

        ok, reason = mcp_config.validate_server("x", {"type": "stdio"})
        assert ok is False
        assert "command" in reason
        assert "stdio" in reason

    def test_type_defaults_to_stdio(self):
        """No explicit type is validated as stdio (needs command)."""
        import src.mcp_config as mcp_config

        ok, _ = mcp_config.validate_server("x", {"command": "echo"})
        assert ok is True
        ok, reason = mcp_config.validate_server("x", {})
        assert ok is False
        assert "stdio" in reason

    def test_empty_command_rejected(self):
        import src.mcp_config as mcp_config

        ok, reason = mcp_config.validate_server("x", {"type": "stdio", "command": ""})
        assert ok is False
        assert "command" in reason

    def test_valid_server_returns_none_reason(self):
        import src.mcp_config as mcp_config

        ok, reason = mcp_config.validate_server("x", {"type": "stdio", "command": "ls"})
        assert ok is True
        assert reason is None


class TestValidateMcpServers:
    """(validated, dropped) split; dropped carry name/type/reason; warns on drop."""

    def test_split_and_dropped_shape(self):
        import src.mcp_config as mcp_config

        validated, dropped = mcp_config.validate_mcp_servers(
            {
                "good": {"type": "stdio", "command": "ls"},
                "bad": {"type": "grpc", "command": "x"},
            }
        )
        assert list(validated) == ["good"]
        assert len(dropped) == 1
        assert dropped[0]["name"] == "bad"
        assert dropped[0]["type"] == "grpc"
        assert "grpc" in dropped[0]["reason"]

    def test_dropped_type_is_none_for_non_dict(self):
        import src.mcp_config as mcp_config

        _, dropped = mcp_config.validate_mcp_servers({"bad": "a string"})
        assert dropped[0]["name"] == "bad"
        assert dropped[0]["type"] is None

    def test_warns_on_each_drop(self):
        import src.mcp_config as mcp_config

        with patch("src.mcp_config.logger") as mock_logger:
            mcp_config.validate_mcp_servers({"bad": {"type": "grpc"}})
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("bad" in w for w in warning_calls)

    def test_load_mcp_config_warns_on_drop(self):
        """Guards the test_edge_cases_unit.py:256-257 warning-scrape contract:
        load_mcp_config must still route drops through logger.warning."""
        import src.mcp_config as mcp_config

        config = {"mcpServers": {"bad": {"type": "grpc", "command": "x"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            with patch("src.mcp_config.logger") as mock_logger:
                result = mcp_config.load_mcp_config()
        assert "bad" not in result
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("bad" in w for w in warning_calls)


class TestReloadMcpConfig:
    """Overlay-merge (env base + manifest overlay) + rebind-not-mutate hot reload."""

    def test_reload_overlays_env_base_with_manifest(self, tmp_path, monkeypatch):
        import src.mcp_config as mcp_config

        # pin the singleton for restoration (reload rebinds the module global)
        monkeypatch.setattr(mcp_config, "_server_mcp_config", {})

        env_config = {"mcpServers": {"env-srv": {"type": "stdio", "command": "envcmd"}}}
        manifest = {
            "version": 1,
            "servers": {"man-srv": {"type": "stdio", "command": "mancmd"}},
        }
        manifest_file = tmp_path / "m.json"
        manifest_file.write_text(json.dumps(manifest))
        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(manifest_file))

        with patch("src.mcp_config.MCP_CONFIG", json.dumps(env_config)):
            mcp_config.reload_mcp_config()
            servers = mcp_config.get_mcp_servers()

        # core Proposal-A guarantee: env base still present after overlay
        assert "env-srv" in servers
        assert servers["env-srv"]["command"] == "envcmd"
        # manifest server layered on top
        assert "man-srv" in servers
        assert servers["man-srv"]["command"] == "mancmd"

    def test_manifest_wins_on_name_collision(self, tmp_path, monkeypatch):
        import src.mcp_config as mcp_config

        monkeypatch.setattr(mcp_config, "_server_mcp_config", {})

        env_config = {"mcpServers": {"dup": {"type": "stdio", "command": "from-env"}}}
        manifest = {
            "version": 1,
            "servers": {"dup": {"type": "stdio", "command": "from-manifest"}},
        }
        manifest_file = tmp_path / "m.json"
        manifest_file.write_text(json.dumps(manifest))
        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(manifest_file))

        with patch("src.mcp_config.MCP_CONFIG", json.dumps(env_config)):
            mcp_config.reload_mcp_config()
            servers = mcp_config.get_mcp_servers()

        assert servers["dup"]["command"] == "from-manifest"

    def test_reload_rebinds_not_mutates(self, tmp_path, monkeypatch):
        """An upsert must REBIND the singleton, leaving the dict a prior session
        pinned unchanged (proves existing-session MCP-set pinning)."""
        import src.mcp_config as mcp_config

        monkeypatch.setattr(mcp_config, "_server_mcp_config", {})

        manifest_file = tmp_path / "m.json"
        manifest_file.write_text(json.dumps({"version": 1, "servers": {}}))
        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(manifest_file))

        with patch("src.mcp_config.MCP_CONFIG", ""):
            mcp_config.reload_mcp_config()
            d1 = mcp_config.get_mcp_servers()
            assert d1 == {}

            # upsert a server via the manifest, then reload
            manifest_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "servers": {"new": {"type": "stdio", "command": "x"}},
                    }
                )
            )
            mcp_config.reload_mcp_config()
            d2 = mcp_config.get_mcp_servers()

        assert d2 is not d1  # rebound to a fresh dict
        assert d1 == {}  # the old dict a pinned session holds is untouched
        assert "new" in d2

    def test_compute_effective_config_returns_fresh_dict(self, monkeypatch):
        import src.mcp_config as mcp_config

        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", "/nonexistent/manifest.json")
        with patch("src.mcp_config.MCP_CONFIG", ""):
            a = mcp_config._compute_effective_config()
            b = mcp_config._compute_effective_config()
        assert a is not b


class TestListDroppedServers:
    """Diagnostics: env-dropped vs manifest-dropped, with shadowing."""

    def test_env_dropped_surfaces_with_source_env(self, monkeypatch):
        import src.mcp_config as mcp_config

        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", "/nonexistent/manifest.json")
        env_config = {"mcpServers": {"bad-env": {"type": "grpc", "command": "x"}}}
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(env_config)):
            dropped = mcp_config.list_dropped_servers()

        by_name = {d["name"]: d for d in dropped}
        assert by_name["bad-env"]["source"] == "env"
        assert "grpc" in by_name["bad-env"]["reason"]

    def test_manifest_dropped_surfaces_with_source_manifest(
        self, tmp_path, monkeypatch
    ):
        import src.mcp_config as mcp_config

        manifest = {
            "version": 1,
            "servers": {"bad-man": {"type": "carrier-pigeon", "command": "fly"}},
        }
        manifest_file = tmp_path / "m.json"
        manifest_file.write_text(json.dumps(manifest))
        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(manifest_file))

        with patch("src.mcp_config.MCP_CONFIG", ""):
            dropped = mcp_config.list_dropped_servers()

        by_name = {d["name"]: d for d in dropped}
        assert by_name["bad-man"]["source"] == "manifest"

    def test_env_drop_shadowed_by_valid_manifest_not_reported(
        self, tmp_path, monkeypatch
    ):
        """An env server that is invalid but supplied validly by the manifest
        (overlay wins) must NOT be reported as dropped."""
        import src.mcp_config as mcp_config

        env_config = {"mcpServers": {"shadowed": {"type": "grpc", "command": "x"}}}
        # manifest supplies the SAME name (even if itself invalid here it is a
        # manifest entry, so the env drop must be suppressed)
        manifest = {
            "version": 1,
            "servers": {"shadowed": {"type": "stdio", "command": "ok"}},
        }
        manifest_file = tmp_path / "m.json"
        manifest_file.write_text(json.dumps(manifest))
        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(manifest_file))

        with patch("src.mcp_config.MCP_CONFIG", json.dumps(env_config)):
            dropped = mcp_config.list_dropped_servers()

        # valid manifest entry -> not dropped at all, and the env drop is shadowed
        assert all(d["name"] != "shadowed" for d in dropped)


class TestOpenCodeConfigGeneration:
    """Test conversion from wrapper MCP config into OpenCode config."""

    def test_build_opencode_config_includes_safe_defaults_and_mcp_servers(self):
        from src.backends.opencode.config import build_opencode_config

        config = build_opencode_config(
            base_config={},
            mcp_servers={
                "filesystem": {
                    "type": "stdio",
                    "command": "npx",
                    "args": [
                        "-y",
                        "@modelcontextprotocol/server-filesystem",
                        "/workspace",
                    ],
                    "env": {"ROOT": "/workspace"},
                },
                "docs": {
                    "type": "streamable-http",
                    "url": "http://localhost:3000/mcp",
                    "headers": {"Authorization": "Bearer token"},
                },
            },
            default_model="openai/gpt-5.5",
            question_permission="ask",
        )

        assert config["permission"]["question"] == "ask"
        assert config["share"] == "disabled"
        assert config["model"] == "openai/gpt-5.5"
        assert config["mcp"]["filesystem"] == {
            "type": "local",
            "command": [
                "npx",
                "-y",
                "@modelcontextprotocol/server-filesystem",
                "/workspace",
            ],
            "environment": {"ROOT": "/workspace"},
        }
        assert config["mcp"]["docs"] == {
            "type": "remote",
            "url": "http://localhost:3000/mcp",
            "headers": {"Authorization": "Bearer token"},
        }

    def test_build_opencode_config_preserves_explicit_base_config_over_defaults(self):
        from src.backends.opencode.config import build_opencode_config

        config = build_opencode_config(
            base_config={
                "share": "enabled",
                "permission": {"question": "deny"},
                "mcp": {"filesystem": {"enabled": False}},
            },
            mcp_servers={
                "filesystem": {"type": "stdio", "command": "npx", "args": ["demo"]}
            },
            default_model="openai/gpt-5.5",
            question_permission="ask",
        )

        assert config["share"] == "enabled"
        assert config["permission"]["question"] == "deny"
        assert config["model"] == "openai/gpt-5.5"
        assert config["mcp"]["filesystem"] == {"enabled": False}

    def test_parse_opencode_config_content_adds_env_context_to_json_errors(self):
        from src.backends.opencode.config import parse_opencode_config_content

        try:
            parse_opencode_config_content("{ invalid json }")
        except ValueError as exc:
            assert "OPENCODE_CONFIG_CONTENT is not valid JSON" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_parse_opencode_config_content_rejects_non_object_json(self):
        """Valid JSON that is not an object (e.g. a list) is rejected."""
        from src.backends.opencode.config import parse_opencode_config_content

        with pytest.raises(ValueError, match="must be a JSON object"):
            parse_opencode_config_content("[1, 2, 3]")

    def test_build_opencode_config_rejects_stdio_server_missing_command(self):
        """A local (stdio) MCP server without a 'command' is a config error."""
        from src.backends.opencode.config import build_opencode_config

        with pytest.raises(ValueError, match="missing required 'command'"):
            build_opencode_config(
                base_config={},
                mcp_servers={"broken": {"type": "stdio", "args": ["x"]}},
                default_model="openai/gpt-5.5",
                question_permission="ask",
            )

    def test_build_opencode_config_rejects_remote_server_missing_url(self):
        """A remote (streamable-http) MCP server without a 'url' is a config error."""
        from src.backends.opencode.config import build_opencode_config

        with pytest.raises(ValueError, match="missing required 'url'"):
            build_opencode_config(
                base_config={},
                mcp_servers={"broken": {"type": "streamable-http"}},
                default_model="openai/gpt-5.5",
                question_permission="ask",
            )

    def test_build_opencode_config_rejects_unsupported_server_type(self):
        """An MCP server type that is neither stdio nor a known remote type errors."""
        from src.backends.opencode.config import build_opencode_config

        with pytest.raises(ValueError, match="Unsupported MCP server type"):
            build_opencode_config(
                base_config={},
                mcp_servers={"weird": {"type": "carrier-pigeon", "command": "fly"}},
                default_model="openai/gpt-5.5",
                question_permission="ask",
            )

    def test_build_opencode_config_accepts_list_command(self):
        """A stdio server may specify 'command' as a list; it is flattened with args."""
        from src.backends.opencode.config import build_opencode_config

        config = build_opencode_config(
            base_config={},
            mcp_servers={
                "fs": {"type": "stdio", "command": ["npx", "server"], "args": ["/ws"]}
            },
            default_model="openai/gpt-5.5",
            question_permission="ask",
        )

        assert config["mcp"]["fs"]["command"] == ["npx", "server", "/ws"]

    def test_parse_opencode_config_content_empty_returns_empty_dict(self):
        from src.backends.opencode.config import parse_opencode_config_content

        assert parse_opencode_config_content(None) == {}
        assert parse_opencode_config_content("") == {}

    def test_parse_opencode_config_content_returns_parsed_object(self):
        from src.backends.opencode.config import parse_opencode_config_content

        assert parse_opencode_config_content('{"share": "disabled"}') == {
            "share": "disabled"
        }


# ---------------------------------------------------------------------------
# env/headers validation + {{env:VAR}} resolve
# ---------------------------------------------------------------------------


class TestValidateStringMapAndEnv:
    def test_env_must_be_string_values(self):
        from src.mcp_config import validate_server

        ok, reason = validate_server(
            "x", {"type": "stdio", "command": "echo", "env": {"N": 1}}
        )
        assert ok is False
        assert "env" in (reason or "")

    def test_headers_must_be_object(self):
        from src.mcp_config import validate_server

        ok, reason = validate_server(
            "x",
            {
                "type": "http",
                "url": "https://example.test",
                "headers": ["not", "a", "map"],
            },
        )
        assert ok is False
        assert "headers" in (reason or "")

    def test_valid_env_and_headers_accepted(self):
        from src.mcp_config import validate_server

        ok, reason = validate_server(
            "x",
            {
                "type": "stdio",
                "command": "echo",
                "env": {"A": "1", "B": "{{env:TOKEN}}"},
            },
        )
        assert ok is True
        assert reason is None

    def test_invalid_env_drops_server_from_load(self):
        config = {
            "mcpServers": {
                "bad": {"type": "stdio", "command": "echo", "env": {"X": 99}},
                "good": {"type": "stdio", "command": "echo", "env": {"X": "ok"}},
            }
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            result = load_mcp_config()
        assert "bad" not in result
        assert "good" in result


class TestResolveEnvRefs:
    def test_resolve_string_and_maps(self):
        from src.mcp_config import (
            list_env_refs,
            resolve_env_refs_in_string,
            resolve_mcp_server_config,
            resolve_mcp_servers,
        )

        env = {"TOKEN": "secret", "REGION": "ap"}
        assert resolve_env_refs_in_string("Bearer {{env:TOKEN}}", env) == "Bearer secret"
        assert resolve_env_refs_in_string("{{ env:REGION }}", env) == "ap"
        assert resolve_env_refs_in_string("{{env:MISSING}}", env) == ""

        cfg = {
            "type": "stdio",
            "command": "echo",
            "env": {"API_KEY": "{{env:TOKEN}}", "REGION": "{{env:REGION}}"},
            "headers": {"Authorization": "Bearer {{env:TOKEN}}"},
        }
        assert list_env_refs(cfg) == ["REGION", "TOKEN"]
        resolved = resolve_mcp_server_config(cfg, environ=env)
        assert resolved["env"]["API_KEY"] == "secret"
        assert resolved["env"]["REGION"] == "ap"
        assert resolved["headers"]["Authorization"] == "Bearer secret"
        # Input not mutated
        assert cfg["env"]["API_KEY"] == "{{env:TOKEN}}"

        multi = resolve_mcp_servers({"a": cfg}, environ=env)
        assert multi["a"]["env"]["API_KEY"] == "secret"

    def test_resolve_none_passthrough(self):
        from src.mcp_config import resolve_mcp_servers

        assert resolve_mcp_servers(None) is None
        assert resolve_mcp_servers({}) == {}

    def test_mcp_secret_maps_meta(self):
        from src.mcp_config import mcp_secret_maps_meta

        meta = mcp_secret_maps_meta(
            {
                "env": {"A": "1", "B": "{{env:X}}"},
                "headers": {"Authorization": "{{env:Y}}"},
            }
        )
        assert meta["env_key_count"] == 2
        assert meta["header_key_count"] == 1
        assert meta["env_refs"] == ["X", "Y"]
