"""Additional coverage tests for src/backends/claude/client.py.

Targets uncovered lines found in the 88%-coverage run:
  75-80, 151, 155, 264, 293-310, 346-350, 414-415, 515, 524, 528,
  605, 796-801, 828-834.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cli(cwd: Optional[str] = None):
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd=cwd or "/tmp")


# ---------------------------------------------------------------------------
# _get_setting_sources() — lines 75-80
# ---------------------------------------------------------------------------


class TestGetSettingSources:
    """Tests for _get_setting_sources() validation paths."""

    def test_invalid_source_logs_warning_and_returns_default(self, monkeypatch, caplog):
        """Invalid sources in CLAUDE_SETTING_SOURCES warn and return the default (lines 75-80)."""
        import logging
        from src.backends.claude.client import _get_setting_sources

        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "bad_source,also_bad")
        with caplog.at_level(logging.WARNING, logger="src.backends.claude.client"):
            result = _get_setting_sources()

        assert result == ["project", "local"]
        assert "Invalid CLAUDE_SETTING_SOURCES" in caplog.text

    def test_empty_string_after_strip_returns_default(self, monkeypatch):
        """Whitespace-only value returns default (lines 75-80)."""
        from src.backends.claude.client import _get_setting_sources

        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "  ,  ")
        result = _get_setting_sources()
        assert result == ["project", "local"]

    def test_mixed_valid_and_invalid_returns_default(self, monkeypatch, caplog):
        """A mix of valid and invalid sources falls back to default (lines 75-80)."""
        import logging
        from src.backends.claude.client import _get_setting_sources

        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,BAD_VALUE")
        with caplog.at_level(logging.WARNING):
            result = _get_setting_sources()

        assert result == ["project", "local"]


# ---------------------------------------------------------------------------
# ClaudeCodeCLI.image_handler property — line 151
# ClaudeCodeCLI.cleanup_images() — line 155
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIImageHandler:
    """Tests for image_handler property and cleanup_images()."""

    def test_image_handler_property_returns_handler(self):
        """image_handler property returns the ImageHandler instance (line 151)."""
        cli = _make_cli()
        handler = cli.image_handler
        assert handler is not None
        # It should be the same object stored as _image_handler
        assert handler is cli._image_handler

    def test_cleanup_images_delegates_to_handler(self):
        """cleanup_images() delegates to _image_handler.cleanup() (line 155)."""
        cli = _make_cli()
        with patch.object(cli._image_handler, "cleanup", return_value=3) as mock_cleanup:
            result = cli.cleanup_images(max_age_seconds=1800)
        mock_cleanup.assert_called_once_with(1800)
        assert result == 3


# ---------------------------------------------------------------------------
# ClaudeCodeCLI._resolve_custom_base_prompt() — line 264
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIResolveCustomBasePrompt:
    """Tests for _resolve_custom_base_prompt() error path."""

    def test_invalid_type_raises_type_error(self):
        """Passing something other than str/None/UNSET raises TypeError (line 264)."""
        cli = _make_cli()
        with pytest.raises(TypeError, match="_custom_base must be"):
            cli._resolve_custom_base_prompt(42, Path("/tmp"))  # 42 is not str/None/UNSET


# ---------------------------------------------------------------------------
# ClaudeCodeCLI._configure_mcp_servers() — lines 293-310
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIConfigureMcpServers:
    """Tests for _configure_mcp_servers() various paths."""

    def _fresh_options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(max_turns=5, cwd=Path("/tmp"))

    def test_mcp_servers_none_is_noop(self):
        """No mcp_servers means options.mcp_servers is not populated (early return)."""
        cli = _make_cli()
        options = self._fresh_options()
        before = dict(getattr(options, "mcp_servers", {}) or {})
        cli._configure_mcp_servers(options, None, None)
        # After a no-op call, mcp_servers should remain empty / unchanged
        after = dict(getattr(options, "mcp_servers", {}) or {})
        assert after == before

    def test_allowed_tools_filters_mcp_servers(self):
        """When allowed_tools is set, only matching MCP servers are kept (lines 293-310)."""
        cli = _make_cli()
        options = self._fresh_options()
        options.allowed_tools = ["mcp__my_server__read"]

        mcp_servers = {
            "my-server": {"type": "stdio", "command": "myserver"},
            "other-server": {"type": "stdio", "command": "other"},
        }

        cli._configure_mcp_servers(options, mcp_servers, ["mcp__my_server__*"])

        # Only my-server should be in filtered result
        assert options.mcp_servers is not None
        assert "my-server" in options.mcp_servers
        assert "other-server" not in options.mcp_servers

    def test_no_matching_servers_skips_mcp(self, caplog):
        """When no MCP server matches allowed_tools, mcp_servers is not set (lines 300-302)."""
        import logging

        cli = _make_cli()
        options = self._fresh_options()
        mcp_servers = {
            "some-server": {"type": "stdio", "command": "some"},
        }

        with caplog.at_level(logging.DEBUG, logger="src.backends.claude.client"):
            cli._configure_mcp_servers(options, mcp_servers, ["mcp__other__*"])

        assert "No MCP servers match" in caplog.text
        # options.mcp_servers should not have been set
        assert not (hasattr(options, "mcp_servers") and options.mcp_servers)

    def test_no_allowed_tools_sets_all_mcp_servers(self):
        """Without allowed_tools, all MCP servers are forwarded (lines 312-317)."""
        cli = _make_cli()
        options = self._fresh_options()
        mcp_servers = {
            "srv1": {"type": "stdio", "command": "s1"},
            "srv2": {"type": "stdio", "command": "s2"},
        }

        cli._configure_mcp_servers(options, mcp_servers, None)

        assert options.mcp_servers == mcp_servers

    def test_allowed_tools_patterns_appended_to_existing_list(self):
        """With allowed_tools and existing mcp patterns, allowed_tools gets extended (line 305-308)."""
        cli = _make_cli()
        options = self._fresh_options()
        options.allowed_tools = ["mcp__my_server__*"]

        mcp_servers = {
            "my-server": {"type": "stdio", "command": "myserver"},
        }

        cli._configure_mcp_servers(options, mcp_servers, ["mcp__my_server__*"])

        assert options.mcp_servers is not None
        assert "my-server" in options.mcp_servers

    def test_no_allowed_tools_allows_all_mcp_tools_for_plugins(self):
        """Without caller allowed_tools, MCP_CONFIG pins a default allowlist that
        must not lock out plugin-bundled MCP servers the SDK loads via
        setting_sources. The CLI treats ``mcp__*`` as "every MCP tool", so the
        gateway adds it rather than only the MCP_CONFIG servers' narrow patterns.
        """
        cli = _make_cli()
        options = self._fresh_options()
        mcp_servers = {
            "srv1": {"type": "stdio", "command": "s1"},
        }

        cli._configure_mcp_servers(options, mcp_servers, None)

        # MCP_CONFIG servers are forwarded, and the allowlist is broadened to
        # all MCP tools so a plugin server like ``mcp__some_plugin__*`` is not
        # silently blocked just because MCP_CONFIG was set.
        assert options.mcp_servers == mcp_servers
        assert "mcp__*" in options.allowed_tools

    def test_explicit_allowed_tools_not_broadened_to_all_mcp(self):
        """A caller that passes explicit allowed_tools keeps full control: the
        gateway must NOT inject ``mcp__*`` and re-enable every MCP tool behind
        their back."""
        cli = _make_cli()
        options = self._fresh_options()
        options.allowed_tools = ["mcp__my_server__*"]

        mcp_servers = {"my-server": {"type": "stdio", "command": "myserver"}}

        cli._configure_mcp_servers(options, mcp_servers, ["mcp__my_server__*"])

        assert "mcp__*" not in options.allowed_tools


# ---------------------------------------------------------------------------
# ClaudeCodeCLI._configure_metadata_env() — lines 346-350
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIConfigureMetadataEnv:
    """Tests for _configure_metadata_env()."""

    def _fresh_options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(max_turns=5, cwd=Path("/tmp"))

    def test_configure_metadata_env_filters_by_allowlist(self, monkeypatch):
        """Only env vars in METADATA_ENV_ALLOWLIST are injected (lines 346-350)."""
        from src.constants import METADATA_ENV_ALLOWLIST

        # Pick a key that IS in the allowlist
        allowed_key = next(iter(METADATA_ENV_ALLOWLIST)) if METADATA_ENV_ALLOWLIST else None
        if allowed_key is None:
            pytest.skip("METADATA_ENV_ALLOWLIST is empty")

        cli = _make_cli()
        options = self._fresh_options()
        extra_env = {allowed_key: "injected_value", "BLOCKED_KEY": "never"}
        cli._configure_metadata_env(options, extra_env)

        assert options.env.get(allowed_key) == "injected_value"
        assert "BLOCKED_KEY" not in options.env

    def test_configure_metadata_env_with_empty_env_is_noop(self):
        """Empty extra_env is a no-op (early return before line 346)."""
        cli = _make_cli()
        options = self._fresh_options()
        before = dict(options.env)
        cli._configure_metadata_env(options, {})
        assert dict(options.env) == before

    def test_configure_metadata_env_with_none_is_noop(self):
        """None extra_env is a no-op."""
        cli = _make_cli()
        options = self._fresh_options()
        before = dict(options.env)
        cli._configure_metadata_env(options, None)
        assert dict(options.env) == before


# ---------------------------------------------------------------------------
# ClaudeCodeCLI._configure_system_prompt() — lines 346-350
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIConfigureSystemPrompt:
    """Tests for _configure_system_prompt() paths."""

    def _fresh_options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(max_turns=5, cwd=Path("/tmp"))

    def test_custom_base_with_system_prompt(self):
        """custom_base + system_prompt are concatenated (line 346-347)."""
        cli = _make_cli()
        options = self._fresh_options()
        cli._configure_system_prompt(options, "base instructions", "extra context")
        assert options.system_prompt == "base instructions\n\nextra context"

    def test_custom_base_without_system_prompt(self):
        """custom_base alone is used directly (line 346-347)."""
        cli = _make_cli()
        options = self._fresh_options()
        cli._configure_system_prompt(options, "base only", None)
        assert options.system_prompt == "base only"

    def test_system_prompt_without_custom_base(self):
        """system_prompt alone → preset + append (lines 348-350)."""
        cli = _make_cli()
        options = self._fresh_options()
        cli._configure_system_prompt(options, None, "extra")
        assert isinstance(options.system_prompt, dict)
        assert options.system_prompt["type"] == "preset"
        assert options.system_prompt.get("append") == "extra"

    def test_neither_prompt_uses_preset_only(self):
        """Neither prompt → plain preset (line 350 else branch)."""
        cli = _make_cli()
        options = self._fresh_options()
        cli._configure_system_prompt(options, None, None)
        assert isinstance(options.system_prompt, dict)
        assert options.system_prompt == {"type": "preset", "preset": "claude_code"}


# ---------------------------------------------------------------------------
# ClaudeCodeCLI._build_sdk_options() user injection — lines 414-415
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIBuildSdkOptionsUser:
    """Tests for user context injection in _build_sdk_options()."""

    def test_user_context_prepended_to_system_prompt(self):
        """When user is provided, it's appended to the system prompt (lines 414-415)."""
        cli = _make_cli()
        options = cli._build_sdk_options(
            system_prompt="Do something",
            user="alice",
            _custom_base="Base context",
        )
        assert "Current user: alice" in options.system_prompt
        assert "Do something" in options.system_prompt

    def test_user_context_without_existing_system_prompt(self):
        """user context alone is appended to the preset system_prompt."""
        cli = _make_cli()
        options = cli._build_sdk_options(user="bob", _custom_base=None)
        # When _custom_base=None, no system_prompt text, and user="bob":
        # system_prompt becomes the preset dict with "Current user: bob" in append
        sp = options.system_prompt
        if isinstance(sp, dict):
            assert "Current user: bob" in (sp.get("append") or "")
        else:
            assert "Current user: bob" in sp


# ---------------------------------------------------------------------------
# ClaudeCodeCLI.verify() — line 515, 524 (no-messages path)
# ---------------------------------------------------------------------------


class TestClaudeCodeCLIVerify:
    """Tests for verify() branching."""

    async def test_verify_returns_false_when_no_messages(self):
        """verify() returns False when the SDK returns no messages (line 515)."""
        cli = _make_cli()

        async def empty_query(*args, **kwargs):
            return
            yield  # make it an async generator

        with patch("src.backends.claude.client.query", side_effect=empty_query):
            result = await cli.verify()

        # Empty message list → returns False
        assert result is False

    async def test_verify_returns_false_on_exception(self):
        """verify() returns False on SDK exception (line 524+)."""
        cli = _make_cli()

        async def failing_query(*args, **kwargs):
            raise RuntimeError("SDK unavailable")
            yield

        with patch("src.backends.claude.client.query", side_effect=failing_query):
            result = await cli.verify()

        assert result is False

    async def test_verify_returns_true_when_assistant_message_arrives(self):
        """verify() returns True when an assistant message is received (line 528)."""
        cli = _make_cli()

        async def mock_query(*args, **kwargs):
            msg = SimpleNamespace(type="assistant", content="Hello")
            yield msg

        with patch("src.backends.claude.client.query", side_effect=mock_query):
            result = await cli.verify()

        assert result is True


# ---------------------------------------------------------------------------
# ClaudeCodeCLI._sdk_env() — line 605 (cross-isolation restore)
# ---------------------------------------------------------------------------


class TestClaudeCodeCLISdkEnv:
    """Tests for _sdk_env() environment isolation."""

    def test_sdk_env_removes_and_restores_isolation_vars(self, monkeypatch):
        """_sdk_env() removes OPENAI_API_KEY during the call and restores it after (line 605)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")

        cli = _make_cli()
        cli.claude_env_vars = {}

        captured_inside = {}

        with cli._sdk_env():
            captured_inside["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY")

        assert captured_inside["OPENAI_API_KEY"] is None  # removed during call
        assert os.environ.get("OPENAI_API_KEY") == "sk-secret"  # restored after

    def test_sdk_env_injects_claude_vars(self, monkeypatch):
        """_sdk_env() injects claude_env_vars into os.environ during the call."""
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        cli = _make_cli()
        cli.claude_env_vars = {"ANTHROPIC_AUTH_TOKEN": "injected-key"}

        captured = {}
        with cli._sdk_env():
            captured["tok"] = os.environ.get("ANTHROPIC_AUTH_TOKEN")

        assert captured["tok"] == "injected-key"
        # Restored after: the env var should be absent (it wasn't set before)
        assert os.environ.get("ANTHROPIC_AUTH_TOKEN") is None

    def test_sdk_env_restores_existing_claude_var(self, monkeypatch):
        """_sdk_env() restores pre-existing values when exiting (line 524)."""
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "original-value")

        cli = _make_cli()
        # Inject a different value, overriding the existing one
        cli.claude_env_vars = {"ANTHROPIC_AUTH_TOKEN": "new-value"}

        captured = {}
        with cli._sdk_env():
            captured["inside"] = os.environ.get("ANTHROPIC_AUTH_TOKEN")

        assert captured["inside"] == "new-value"
        # After the context, the original value must be restored (line 524)
        assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "original-value"


# ---------------------------------------------------------------------------
# run_completion_with_client() hook-fired break path — lines 796-801
# ---------------------------------------------------------------------------


class TestRunCompletionWithClientHookBreak:
    """Tests for the hook-fired break path in run_completion_with_client()."""

    async def test_run_stops_when_break_event_fires_without_message(self):
        """When break_event fires before any message, the loop stops cleanly (line 801)."""
        from src.session_manager import Session

        cli = _make_cli()
        session = Session(session_id="sess-break")

        mock_client = AsyncMock()

        # Provide a receive_response that never yields
        async def slow_receive():
            await asyncio.sleep(10)  # simulate waiting forever
            return
            yield

        mock_client.receive_response = slow_receive

        # Fire the break event immediately via query side effect (receives prompt arg)
        async def fire_break_and_return(prompt):
            if session.stream_break_event is not None:
                session.stream_break_event.set()

        mock_client.query.side_effect = fire_break_and_return

        messages = []
        async for msg in cli.run_completion_with_client(mock_client, "test", session):
            messages.append(msg)

        # No messages yielded — just a clean break
        assert messages == []
        mock_client.query.assert_awaited_once_with("test")

    async def test_run_yields_concurrent_message_on_break(self):
        """When break fires with a concurrent message available, that message is yielded (lines 796-800)."""
        from src.session_manager import Session

        cli = _make_cli()
        session = Session(session_id="sess-concurrent")

        msg_obj = SimpleNamespace(type="assistant", content="concurrent message")

        # We'll use a queue to coordinate the concurrent message + break
        msg_ready = asyncio.Event()
        msg_queue = asyncio.Queue()

        async def fire_break():
            # Simulate: break fires with a message already in the done set
            await asyncio.sleep(0)
            if session.stream_break_event is not None:
                session.stream_break_event.set()

        mock_client = AsyncMock()
        mock_client.query.side_effect = fire_break

        async def mock_receive_response():
            # First yield a message synchronously
            yield msg_obj

        mock_client.receive_response = mock_receive_response

        messages = []
        async for msg in cli.run_completion_with_client(mock_client, "hello", session):
            messages.append(msg)

        # Either 0 or 1 messages (race) — no errors, no hangs
        for m in messages:
            assert isinstance(m, dict)


# ---------------------------------------------------------------------------
# receive_response_from_client() — lines 828-834
# ---------------------------------------------------------------------------


class TestReceiveResponseFromClient:
    """Tests for receive_response_from_client() error path."""

    async def test_receive_response_error_clears_client_and_yields_error(self):
        """receive_response_from_client() clears session.client on error (lines 832-834)."""
        from src.session_manager import Session

        cli = _make_cli()
        session = Session(session_id="sess-recv")
        session.client = MagicMock()

        mock_client = AsyncMock()

        async def failing_receive():
            raise RuntimeError("receive broken")
            yield

        mock_client.receive_response = failing_receive

        messages = []
        async for msg in cli.receive_response_from_client(mock_client, session):
            messages.append(msg)

        assert len(messages) == 1
        assert messages[0]["type"] == "error"
        assert "receive broken" in messages[0]["error_message"]
        assert session.client is None

    async def test_receive_response_yields_messages_normally(self):
        """receive_response_from_client() yields all messages on happy path (line 829-830)."""
        from src.session_manager import Session

        cli = _make_cli()
        session = Session(session_id="sess-recv-ok")

        mock_client = AsyncMock()
        msgs = [
            SimpleNamespace(type="assistant", content="hello"),
            SimpleNamespace(type="result", subtype="success", result="done"),
        ]

        async def mock_receive():
            for m in msgs:
                yield m

        mock_client.receive_response = mock_receive

        result = []
        async for msg in cli.receive_response_from_client(mock_client, session):
            result.append(msg)

        assert len(result) == 2
        assert result[0]["type"] == "assistant"


# ---------------------------------------------------------------------------
# _inject_mcp_user_header() — per-request user header into MCP configs
# ---------------------------------------------------------------------------


class TestInjectMcpUserHeader:
    """Gateway injects the authenticated user as an MCP header (issue #124)."""

    def _mcp(self):
        return {
            "ragaas": {"type": "http", "url": "http://127.0.0.1:10074/mcp"},
            "local": {"type": "stdio", "command": "x"},
            "sse1": {"type": "sse", "url": "y", "headers": {"A": "1"}},
        }

    def test_disabled_when_header_unset(self, monkeypatch):
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "")
        cli = _make_cli()
        mcp = self._mcp()
        # No-op returns the same object untouched.
        assert cli._inject_mcp_user_header(mcp, "alice") is mcp

    def test_disabled_when_no_user(self, monkeypatch):
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "X-OpenWebUI-User-Name")
        cli = _make_cli()
        mcp = self._mcp()
        assert cli._inject_mcp_user_header(mcp, None) is mcp

    def test_injects_into_http_and_sse_only(self, monkeypatch):
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "X-OpenWebUI-User-Name")
        cli = _make_cli()
        mcp = self._mcp()
        out = cli._inject_mcp_user_header(mcp, "alice")

        assert out["ragaas"]["headers"] == {"X-OpenWebUI-User-Name": "alice"}
        # Existing headers preserved (merged, not clobbered).
        assert out["sse1"]["headers"] == {"A": "1", "X-OpenWebUI-User-Name": "alice"}
        # stdio servers have no headers key added.
        assert "headers" not in out["local"]
        # The shared config passed in is never mutated (deep-copied).
        assert "headers" not in mcp["ragaas"]

    def test_non_ascii_user_is_quoted(self, monkeypatch):
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "X-User")
        cli = _make_cli()
        out = cli._inject_mcp_user_header(self._mcp(), "김철수")
        # Value is percent-encoded so it survives as an HTTP header.
        assert out["ragaas"]["headers"]["X-User"].startswith("%")
        out["ragaas"]["headers"]["X-User"].encode("ascii")  # must not raise

    def test_crlf_in_user_is_encoded_not_injected(self, monkeypatch):
        # An ASCII value with CR/LF must be percent-encoded so it cannot smuggle
        # an extra header into the outbound MCP request (header injection).
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "X-User")
        cli = _make_cli()
        out = cli._inject_mcp_user_header(self._mcp(), "alice\r\nX-Injected: evil")
        value = out["ragaas"]["headers"]["X-User"]
        assert "\r" not in value and "\n" not in value
        assert "%0" in value.upper()  # CR/LF percent-encoded

    def test_forward_headers_injected_alongside_user(self, monkeypatch):
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "X-OpenWebUI-User-Name")
        cli = _make_cli()
        out = cli._inject_mcp_user_header(
            self._mcp(), "alice", {"X-Cookie-dscrowd.token_key": "tok123"}
        )
        assert out["ragaas"]["headers"] == {
            "X-OpenWebUI-User-Name": "alice",
            "X-Cookie-dscrowd.token_key": "tok123",
        }
        # Forwarded header rides http/SSE only; stdio untouched.
        assert "headers" not in out["local"]
        # Shared config never mutated.
        assert "headers" not in self._mcp()["ragaas"]

    def test_forward_headers_independent_of_user_header(self, monkeypatch):
        # Even with no user identity header configured, a forwarded credential
        # header still reaches the MCP server.
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "")
        cli = _make_cli()
        out = cli._inject_mcp_user_header(
            self._mcp(), None, {"X-Cookie-dscrowd.token_key": "tok123"}
        )
        assert out["ragaas"]["headers"] == {"X-Cookie-dscrowd.token_key": "tok123"}

    def test_no_op_when_nothing_to_inject(self, monkeypatch):
        monkeypatch.setattr("src.constants.MCP_FORWARD_USER_HEADER", "")
        cli = _make_cli()
        mcp = self._mcp()
        assert cli._inject_mcp_user_header(mcp, None, {}) is mcp


class TestBuildMcpContextHeaders:
    """MCP_FORWARD_CONTEXT resolution into a single JSON header (issue #124)."""

    def _headers(self, **kw):
        # Case-insensitive getter mirroring Starlette request.headers.get.
        lowered = {k.lower(): v for k, v in kw.items()}
        return lambda name: lowered.get(name.lower())

    def test_disabled_when_unset(self, monkeypatch):
        monkeypatch.delenv("MCP_FORWARD_CONTEXT", raising=False)
        from src.constants import build_mcp_context_headers

        assert build_mcp_context_headers("alice", self._headers()) == {}

    def test_resolves_user_and_header_tokens(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_FORWARD_CONTEXT",
            '{"user_id":"{{user}}","dscrowd_token":"{{header:X-Cookie-dscrowd.token_key}}"}',
        )
        monkeypatch.delenv("MCP_FORWARD_CONTEXT_HEADER", raising=False)
        from src.constants import build_mcp_context_headers

        out = build_mcp_context_headers(
            "alice", self._headers(**{"X-Cookie-dscrowd.token_key": "tok123"})
        )
        assert set(out) == {"X-MCP-Context"}
        payload = json.loads(out["X-MCP-Context"])
        assert payload == {"user_id": "alice", "dscrowd_token": "tok123"}

    def test_custom_header_name(self, monkeypatch):
        monkeypatch.setenv("MCP_FORWARD_CONTEXT", '{"user_id":"{{user}}"}')
        monkeypatch.setenv("MCP_FORWARD_CONTEXT_HEADER", "X-Ctx")
        from src.constants import build_mcp_context_headers

        out = build_mcp_context_headers("bob", self._headers())
        assert list(out) == ["X-Ctx"]
        assert json.loads(out["X-Ctx"]) == {"user_id": "bob"}

    def test_empty_resolved_keys_dropped(self, monkeypatch):
        # user is None and the header is absent -> both keys resolve empty -> {}.
        monkeypatch.setenv(
            "MCP_FORWARD_CONTEXT",
            '{"user_id":"{{user}}","tok":"{{header:X-Missing}}"}',
        )
        from src.constants import build_mcp_context_headers

        assert build_mcp_context_headers(None, self._headers()) == {}

    def test_partial_resolution_keeps_present_keys(self, monkeypatch):
        monkeypatch.setenv(
            "MCP_FORWARD_CONTEXT",
            '{"user_id":"{{user}}","tok":"{{header:X-Missing}}"}',
        )
        from src.constants import build_mcp_context_headers

        out = build_mcp_context_headers("carol", self._headers())
        assert json.loads(out["X-MCP-Context"]) == {"user_id": "carol"}

    def test_empty_header_name_token_does_not_leak(self, monkeypatch):
        # A misconfigured {{header:}} must resolve to "" (key dropped), never
        # leak the literal token into the downstream payload.
        monkeypatch.setenv("MCP_FORWARD_CONTEXT", '{"tok":"{{header:}}"}')
        from src.constants import build_mcp_context_headers

        assert build_mcp_context_headers("alice", self._headers()) == {}

    def test_invalid_json_is_ignored(self, monkeypatch):
        monkeypatch.setenv("MCP_FORWARD_CONTEXT", "{not json")
        from src.constants import build_mcp_context_headers

        assert build_mcp_context_headers("alice", self._headers()) == {}

    def test_non_ascii_value_is_json_escaped(self, monkeypatch):
        monkeypatch.setenv("MCP_FORWARD_CONTEXT", '{"user_id":"{{user}}"}')
        from src.constants import build_mcp_context_headers

        out = build_mcp_context_headers("김철수", self._headers())
        # ensure_ascii keeps the header value transmittable; value round-trips.
        out["X-MCP-Context"].encode("ascii")  # must not raise
        assert json.loads(out["X-MCP-Context"]) == {"user_id": "김철수"}
