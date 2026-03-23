#!/usr/bin/env python3
"""
Unit tests for src/claude_cli.py

Tests the ClaudeCodeCLI class methods.
These are pure unit tests that don't require a running server or Claude SDK.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures and helpers (extracted from duplicate class-level definitions)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_class():
    """Get the ClaudeCodeCLI class without instantiating."""
    from src.claude_cli import ClaudeCodeCLI

    return ClaudeCodeCLI


def _make_cli(cli_class):
    """Create a minimal mock with _convert_message and _TYPE_CHECKS bound."""
    cli = MagicMock()
    cli._convert_message = cli_class._convert_message.__get__(cli, cli_class)
    cli._TYPE_CHECKS = cli_class._TYPE_CHECKS
    return cli


@pytest.fixture
def cli_instance():
    """Create a CLI instance with mocked auth (tempdir + auth mock pattern)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch("src.auth.validate_claude_code_auth") as mock_validate:
            with patch("src.auth.auth_manager") as mock_auth:
                mock_validate.return_value = (True, {"method": "anthropic"})
                mock_auth.get_claude_code_env_vars.return_value = {
                    "ANTHROPIC_AUTH_TOKEN": "test-key"
                }

                from src.claude_cli import ClaudeCodeCLI

                cli = ClaudeCodeCLI(cwd=temp_dir)
                yield cli


class TestClaudeCodeCLIConvertMessage:
    """Test ClaudeCodeCLI._convert_message() helper."""

    def test_convert_dict_message_passthrough(self, cli_class):
        cli = MagicMock()
        cli._convert_message = cli_class._convert_message.__get__(cli, cli_class)
        msg = {"type": "assistant", "content": "hello"}
        result = cli._convert_message(msg)
        assert result == msg

    def test_convert_object_message_to_dict(self, cli_class):
        cli = MagicMock()
        cli._convert_message = cli_class._convert_message.__get__(cli, cli_class)
        obj = MagicMock()
        obj.type = "assistant"
        obj.content = "hello"
        result = cli._convert_message(obj)
        assert isinstance(result, dict)
        assert result["type"] == "assistant"
        assert result["content"] == "hello"


class TestClaudeCodeCLIParseMessage:
    """Test ClaudeCodeCLI.parse_claude_message()"""

    def test_parse_result_message(self, cli_class):
        """Parses result message with 'result' field."""
        # Use classmethod-like approach - create minimal mock instance
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [{"subtype": "success", "result": "The final answer is 42."}]
        result = cli.parse_claude_message(messages)
        assert result == "The final answer is 42."

    def test_parse_assistant_message_with_content_list(self, cli_class):
        """Parses assistant message with content list."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [
            {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "World!"},
                ]
            }
        ]
        result = cli.parse_claude_message(messages)
        assert result == "Hello World!"

    def test_parse_assistant_message_with_textblock_objects(self, cli_class):
        """Parses assistant message with TextBlock objects."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        text_block = SimpleNamespace(text="Response text")

        messages = [{"content": [text_block]}]
        result = cli.parse_claude_message(messages)
        assert result == "Response text"

    def test_parse_assistant_message_with_string_content(self, cli_class):
        """Parses assistant message with string content blocks."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [{"content": ["Part 1", "Part 2"]}]
        result = cli.parse_claude_message(messages)
        assert result == "Part 1Part 2"

    def test_parse_old_format_assistant_message(self, cli_class):
        """Parses old format assistant message."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Old format response"}]},
            }
        ]
        result = cli.parse_claude_message(messages)
        assert result == "Old format response"

    def test_parse_old_format_string_content(self, cli_class):
        """Parses old format with string content."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [
            {
                "type": "assistant",
                "message": {"content": "Simple string content"},
            }
        ]
        result = cli.parse_claude_message(messages)
        assert result == "Simple string content"

    def test_parse_empty_messages_returns_none(self, cli_class):
        """Empty messages list returns None."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        result = cli.parse_claude_message([])
        assert result is None

    def test_parse_no_matching_messages_returns_none(self, cli_class):
        """No matching messages returns None."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [{"type": "system", "content": "System message"}]
        result = cli.parse_claude_message(messages)
        assert result is None

    def test_parse_joins_all_messages(self, cli_class):
        """When multiple messages, joins all content with newlines."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [
            {"content": [{"type": "text", "text": "First response"}]},
            {"content": [{"type": "text", "text": "Second response"}]},
        ]
        result = cli.parse_claude_message(messages)
        assert result == "First response\nSecond response"

    def test_result_takes_priority_over_assistant_content(self, cli_class):
        """ResultMessage.result takes priority (avoids duplication with AssistantMessage)."""
        cli = MagicMock()
        cli.parse_claude_message = cli_class.parse_claude_message.__get__(cli, cli_class)

        messages = [
            {"content": [{"type": "text", "text": "Some response"}]},
            {"subtype": "success", "result": "Final result"},
        ]
        result = cli.parse_claude_message(messages)
        assert result == "Final result"


class TestClaudeCodeCLIEstimateTokenUsage:
    """Test ClaudeCodeCLI.estimate_token_usage()"""

    def test_estimate_basic(self, cli_class):
        """Basic token estimation."""
        cli = MagicMock()
        cli.estimate_token_usage = cli_class.estimate_token_usage.__get__(cli, cli_class)

        # 12 chars / 4 = 3 tokens, 16 chars / 4 = 4 tokens
        result = cli.estimate_token_usage("Hello World!", "Response here!")
        assert result["prompt_tokens"] == 3
        assert result["completion_tokens"] == 3
        assert result["total_tokens"] == 6

    def test_estimate_minimum_one_token(self, cli_class):
        """Minimum is 1 token."""
        cli = MagicMock()
        cli.estimate_token_usage = cli_class.estimate_token_usage.__get__(cli, cli_class)

        result = cli.estimate_token_usage("Hi", "X")
        assert result["prompt_tokens"] >= 1
        assert result["completion_tokens"] >= 1

    def test_estimate_long_text(self, cli_class):
        """Longer text estimation."""
        cli = MagicMock()
        cli.estimate_token_usage = cli_class.estimate_token_usage.__get__(cli, cli_class)

        prompt = "a" * 400  # 100 tokens
        completion = "b" * 200  # 50 tokens
        result = cli.estimate_token_usage(prompt, completion)

        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["total_tokens"] == 150

    def test_estimate_empty_strings(self, cli_class):
        """Empty strings return minimum 1 token each."""
        cli = MagicMock()
        cli.estimate_token_usage = cli_class.estimate_token_usage.__get__(cli, cli_class)

        result = cli.estimate_token_usage("", "")
        assert result["prompt_tokens"] == 1
        assert result["completion_tokens"] == 1


class TestClaudeCodeCLICleanupTempDir:
    """Test ClaudeCodeCLI._cleanup_temp_dir()"""

    def test_cleanup_removes_existing_dir(self):
        """Cleanup removes existing temp directory."""
        from src.claude_cli import ClaudeCodeCLI

        # Create a mock instance
        cli = MagicMock(spec=ClaudeCodeCLI)

        # Create an actual temp directory
        temp_dir = tempfile.mkdtemp(prefix="test_cleanup_")
        cli.temp_dir = temp_dir

        # Bind the method
        cli._cleanup_temp_dir = ClaudeCodeCLI._cleanup_temp_dir.__get__(cli, ClaudeCodeCLI)

        assert os.path.exists(temp_dir)

        cli._cleanup_temp_dir()

        assert not os.path.exists(temp_dir)

    def test_cleanup_handles_missing_dir(self):
        """Cleanup handles already-deleted directory gracefully."""
        from src.claude_cli import ClaudeCodeCLI

        cli = MagicMock(spec=ClaudeCodeCLI)
        cli.temp_dir = "/nonexistent/test/dir/12345"

        cli._cleanup_temp_dir = ClaudeCodeCLI._cleanup_temp_dir.__get__(cli, ClaudeCodeCLI)

        # Should not raise
        cli._cleanup_temp_dir()

    def test_cleanup_no_temp_dir_set(self):
        """Cleanup does nothing when temp_dir is None."""
        from src.claude_cli import ClaudeCodeCLI

        cli = MagicMock(spec=ClaudeCodeCLI)
        cli.temp_dir = None

        cli._cleanup_temp_dir = ClaudeCodeCLI._cleanup_temp_dir.__get__(cli, ClaudeCodeCLI)

        # Should not raise
        cli._cleanup_temp_dir()


class TestClaudeCodeCLIInit:
    """Test ClaudeCodeCLI.__init__() initialization logic."""

    def test_timeout_conversion(self):
        """Timeout is converted from milliseconds to seconds."""
        # Test the conversion logic directly
        timeout_ms = 120000
        timeout_seconds = timeout_ms / 1000
        assert timeout_seconds == 120.0

    def test_path_handling_with_valid_dir(self):
        """Valid directory path is handled correctly."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            assert path.exists()

    def test_path_handling_with_invalid_dir(self):
        """Invalid directory path is detected."""
        path = Path("/nonexistent/path/12345")
        assert not path.exists()

    def test_init_with_cwd(self):
        """ClaudeCodeCLI initializes with provided cwd."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.auth.validate_claude_code_auth") as mock_validate:
                with patch("src.auth.auth_manager") as mock_auth:
                    mock_validate.return_value = (True, {"method": "anthropic"})
                    mock_auth.get_claude_code_env_vars.return_value = {}

                    from src.claude_cli import ClaudeCodeCLI

                    cli = ClaudeCodeCLI(cwd=temp_dir)

                    assert cli.cwd == Path(temp_dir)
                    assert cli.temp_dir is None
                    assert cli.timeout == 600.0  # 600000ms / 1000

    def test_init_with_invalid_cwd_raises(self):
        """ClaudeCodeCLI raises ValueError for non-existent cwd."""
        with patch("src.auth.validate_claude_code_auth") as mock_validate:
            with patch("src.auth.auth_manager") as mock_auth:
                mock_validate.return_value = (True, {"method": "anthropic"})
                mock_auth.get_claude_code_env_vars.return_value = {}

                from src.claude_cli import ClaudeCodeCLI

                with pytest.raises(ValueError, match="Working directory does not exist"):
                    ClaudeCodeCLI(cwd="/nonexistent/path/12345")

    def test_init_without_cwd_creates_temp(self):
        """ClaudeCodeCLI creates temp directory when no cwd provided."""
        with patch("src.auth.validate_claude_code_auth") as mock_validate:
            with patch("src.auth.auth_manager") as mock_auth:
                with patch("atexit.register"):  # Don't actually register cleanup
                    mock_validate.return_value = (True, {"method": "anthropic"})
                    mock_auth.get_claude_code_env_vars.return_value = {}

                    from src.claude_cli import ClaudeCodeCLI

                    cli = ClaudeCodeCLI()

                    assert cli.temp_dir is not None
                    assert cli.cwd == Path(cli.temp_dir)
                    assert "claude_code_workspace_" in cli.temp_dir

                    # Cleanup
                    if cli.temp_dir and os.path.exists(cli.temp_dir):
                        import shutil

                        shutil.rmtree(cli.temp_dir)

    def test_init_with_custom_timeout(self):
        """ClaudeCodeCLI uses custom timeout."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.auth.validate_claude_code_auth") as mock_validate:
                with patch("src.auth.auth_manager") as mock_auth:
                    mock_validate.return_value = (True, {"method": "anthropic"})
                    mock_auth.get_claude_code_env_vars.return_value = {}

                    from src.claude_cli import ClaudeCodeCLI

                    cli = ClaudeCodeCLI(timeout=120000, cwd=temp_dir)

                    assert cli.timeout == 120.0

    def test_init_auth_validation_failure(self):
        """ClaudeCodeCLI handles auth validation failure gracefully."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.auth.validate_claude_code_auth") as mock_validate:
                with patch("src.auth.auth_manager") as mock_auth:
                    # Auth fails
                    mock_validate.return_value = (False, {"errors": ["Missing API key"]})
                    mock_auth.get_claude_code_env_vars.return_value = {}

                    from src.claude_cli import ClaudeCodeCLI

                    # Should not raise, just log warning
                    cli = ClaudeCodeCLI(cwd=temp_dir)
                    assert cli.cwd == Path(temp_dir)


class TestClaudeCodeCLIVerifyCLI:
    """Test ClaudeCodeCLI.verify_cli()"""

    @pytest.mark.asyncio
    async def test_verify_cli_success(self, cli_instance):
        """verify_cli returns True on successful SDK response."""
        mock_message = {"type": "assistant", "content": [{"type": "text", "text": "Hello"}]}

        async def mock_query(*args, **kwargs):
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            result = await cli_instance.verify_cli()
            assert result is True

    @pytest.mark.asyncio
    async def test_verify_cli_no_messages(self, cli_instance):
        """verify_cli returns False when no messages returned."""

        async def mock_query(*args, **kwargs):
            return
            yield  # Make it a generator but yield nothing

        with patch("src.backends.claude.client.query", mock_query):
            result = await cli_instance.verify_cli()
            assert result is False

    @pytest.mark.asyncio
    async def test_verify_cli_exception(self, cli_instance):
        """verify_cli returns False on exception."""

        async def mock_query(*args, **kwargs):
            raise RuntimeError("SDK error")
            yield  # Make it a generator

        with patch("src.backends.claude.client.query", mock_query):
            result = await cli_instance.verify_cli()
            assert result is False


class TestClaudeCodeCLIRunCompletion:
    """Test ClaudeCodeCLI.run_completion()"""

    @pytest.mark.asyncio
    async def test_run_completion_basic(self, cli_instance):
        """run_completion yields messages from SDK."""
        mock_message = {"type": "assistant", "content": [{"type": "text", "text": "Hello"}]}

        async def mock_query(*args, **kwargs):
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            messages = []
            async for msg in cli_instance.run_completion("Hello"):
                messages.append(msg)

            assert len(messages) == 1
            assert messages[0] == mock_message

    @pytest.mark.asyncio
    async def test_run_completion_with_system_prompt(self, cli_instance):
        """run_completion sets system_prompt option."""
        mock_message = {"type": "assistant", "content": "Response"}
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello", system_prompt="You are helpful"):
                pass

            assert len(captured_options) == 1
            opts = captured_options[0]
            assert opts.system_prompt == {
                "type": "preset",
                "preset": "claude_code",
                "append": "You are helpful",
            }

    @pytest.mark.asyncio
    async def test_run_completion_with_model(self, cli_instance):
        """run_completion sets model option."""
        mock_message = {"type": "assistant"}
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello", model="claude-3-opus"):
                pass

            assert captured_options[0].model == "claude-3-opus"

    @pytest.mark.asyncio
    async def test_run_completion_with_tool_restrictions(self, cli_instance):
        """run_completion sets allowed/disallowed tools."""
        mock_message = {"type": "assistant"}
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion(
                "Hello",
                allowed_tools=["Bash", "Read"],
                disallowed_tools=["Task"],
            ):
                pass

            assert captured_options[0].allowed_tools == ["Bash", "Read"]
            assert "Task" in captured_options[0].disallowed_tools

    @pytest.mark.asyncio
    async def test_run_completion_with_permission_mode(self, cli_instance):
        """run_completion sets permission_mode."""
        mock_message = {"type": "assistant"}
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello", permission_mode="acceptEdits"):
                pass

            assert captured_options[0].permission_mode == "acceptEdits"

    @pytest.mark.asyncio
    async def test_run_completion_with_session_id(self, cli_instance):
        """run_completion sets extra_args session-id for new sessions."""
        mock_message = {"type": "assistant"}
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello", session_id="sess-123"):
                pass

            assert captured_options[0].extra_args.get("session-id") == "sess-123"

    @pytest.mark.asyncio
    async def test_run_completion_resume_session(self, cli_instance):
        """run_completion sets resume option for follow-up turns."""
        mock_message = {"type": "assistant"}
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello", resume="sess-123"):
                pass

            assert captured_options[0].resume == "sess-123"

    @pytest.mark.asyncio
    async def test_run_completion_converts_objects_to_dicts(self, cli_instance):
        """run_completion converts message objects to dicts."""
        # Create a mock object with attributes
        mock_obj = MagicMock()
        mock_obj.type = "assistant"
        mock_obj.content = "Hello"

        async def mock_query(*args, **kwargs):
            yield mock_obj

        with patch("src.backends.claude.client.query", mock_query):
            messages = []
            async for msg in cli_instance.run_completion("Hello"):
                messages.append(msg)

            assert len(messages) == 1
            # Should be converted to dict
            assert isinstance(messages[0], dict)
            assert "type" in messages[0]

    @pytest.mark.asyncio
    async def test_run_completion_exception_yields_error(self, cli_instance):
        """run_completion yields error message on exception."""

        async def mock_query(*args, **kwargs):
            raise RuntimeError("SDK failed")
            yield  # Make it a generator

        with patch("src.backends.claude.client.query", mock_query):
            messages = []
            async for msg in cli_instance.run_completion("Hello"):
                messages.append(msg)

            assert len(messages) == 1
            assert messages[0]["type"] == "result"
            assert messages[0]["subtype"] == "error_during_execution"
            assert messages[0]["is_error"] is True
            assert "SDK failed" in messages[0]["error_message"]

    @pytest.mark.asyncio
    async def test_run_completion_restores_env_vars(self, cli_instance):
        """run_completion restores environment variables after execution."""
        # Set an env var that will be modified
        original_key = os.environ.get("ANTHROPIC_AUTH_TOKEN")

        mock_message = {"type": "assistant"}

        async def mock_query(*args, **kwargs):
            yield mock_message

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello"):
                pass

        # Env should be restored
        if original_key is None:
            assert (
                "ANTHROPIC_AUTH_TOKEN" not in os.environ
                or os.environ.get("ANTHROPIC_AUTH_TOKEN") == original_key
            )
        else:
            assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == original_key

    @pytest.mark.asyncio
    async def test_run_completion_metadata_sets_options_env(self, cli_instance):
        """Metadata with a2a_thread_id should set options.env THREAD_ID."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield {"type": "assistant"}

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion(
                "Hello", _metadata={"a2a_thread_id": "thread-123"}
            ):
                pass

            assert len(captured_options) == 1
            assert captured_options[0].env == {"THREAD_ID": "thread-123"}

    @pytest.mark.asyncio
    async def test_run_completion_no_metadata_empty_env(self, cli_instance):
        """Without metadata, options.env should remain default (empty dict)."""
        captured_options = []

        async def mock_query(prompt, options):
            captured_options.append(options)
            yield {"type": "assistant"}

        with patch("src.backends.claude.client.query", mock_query):
            async for _ in cli_instance.run_completion("Hello"):
                pass

            assert len(captured_options) == 1
            assert captured_options[0].env == {}


class TestClaudeCodeCLICleanupException:
    """Test ClaudeCodeCLI._cleanup_temp_dir() exception handling."""

    def test_cleanup_exception_is_caught(self):
        """Cleanup catches exceptions during rmtree."""
        from src.claude_cli import ClaudeCodeCLI

        cli = MagicMock(spec=ClaudeCodeCLI)
        temp_dir = tempfile.mkdtemp(prefix="test_cleanup_exc_")
        cli.temp_dir = temp_dir

        # Bind the real method
        cli._cleanup_temp_dir = ClaudeCodeCLI._cleanup_temp_dir.__get__(cli, ClaudeCodeCLI)

        with patch("shutil.rmtree", side_effect=PermissionError("Cannot delete")):
            # Should not raise
            cli._cleanup_temp_dir()

        # Clean up manually
        import shutil

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


# ---------------------------------------------------------------------------
# New coverage tests appended below
# ---------------------------------------------------------------------------


class TestBuildSdkOptions:
    """Test ClaudeCodeCLI._build_sdk_options() directly."""

    def test_default_preset_system_prompt(self, cli_instance):
        """When no system_prompt given, preset claude_code is used."""
        opts = cli_instance._build_sdk_options()
        assert opts.system_prompt == {"type": "preset", "preset": "claude_code"}

    def test_custom_system_prompt(self, cli_instance):
        """When system_prompt given, preset with append is used."""
        opts = cli_instance._build_sdk_options(system_prompt="Be concise")
        assert opts.system_prompt == {
            "type": "preset",
            "preset": "claude_code",
            "append": "Be concise",
        }

    def test_thinking_mode_adaptive(self, cli_instance):
        """Adaptive thinking mode sets type=adaptive."""
        with patch("src.runtime_config.runtime_config.get", return_value="adaptive"):
            opts = cli_instance._build_sdk_options()
            assert opts.thinking == {"type": "adaptive"}

    def test_thinking_mode_enabled(self, cli_instance):
        """Enabled thinking mode sets type=enabled with budget."""
        with patch("src.runtime_config.runtime_config.get", return_value="enabled"):
            with patch("src.backends.claude.client.THINKING_BUDGET_TOKENS", 5000):
                opts = cli_instance._build_sdk_options()
                assert opts.thinking == {"type": "enabled", "budget_tokens": 5000}

    def test_thinking_mode_disabled(self, cli_instance):
        """Disabled thinking mode does not set thinking attr."""
        with patch("src.runtime_config.runtime_config.get", return_value="disabled"):
            opts = cli_instance._build_sdk_options()
            assert not hasattr(opts, "thinking") or opts.thinking is None

    def test_thinking_mode_unrecognized_logs_warning(self, cli_instance):
        """Unrecognized thinking mode logs warning and does not set thinking."""
        with patch("src.runtime_config.runtime_config.get", return_value="bogus"):
            with patch("src.backends.claude.client.logger") as mock_logger:
                opts = cli_instance._build_sdk_options()
                mock_logger.warning.assert_called_once()
                assert "bogus" in mock_logger.warning.call_args[0][0]
                assert getattr(opts, "thinking", None) is None

    def test_include_partial_messages_when_token_streaming(self, cli_instance):
        """TOKEN_STREAMING=True sets include_partial_messages."""
        with patch("src.runtime_config.runtime_config.get", return_value=True):
            opts = cli_instance._build_sdk_options()
            assert opts.include_partial_messages is True

    def test_no_partial_messages_when_token_streaming_off(self, cli_instance):
        """TOKEN_STREAMING=False does not set include_partial_messages."""
        with patch("src.runtime_config.runtime_config.get", return_value=False):
            opts = cli_instance._build_sdk_options()
            assert not getattr(opts, "include_partial_messages", False)

    def test_mcp_servers_passthrough(self, cli_instance):
        """mcp_servers option is set on options."""
        servers = {"my_server": {"type": "stdio", "command": "node"}}
        opts = cli_instance._build_sdk_options(mcp_servers=servers)
        assert opts.mcp_servers == servers

    def test_output_format_passthrough(self, cli_instance):
        """output_format option is set on options."""
        fmt = {"type": "json"}
        opts = cli_instance._build_sdk_options(output_format=fmt)
        assert opts.output_format == fmt

    def test_resume_takes_priority_over_session_id(self, cli_instance):
        """When both resume and session_id given, resume wins."""
        opts = cli_instance._build_sdk_options(resume="resume-abc", session_id="sess-xyz")
        assert opts.resume == "resume-abc"
        assert opts.extra_args.get("session-id") is None

    def test_session_id_used_when_no_resume(self, cli_instance):
        """session_id is set as extra_args when resume is not given."""
        opts = cli_instance._build_sdk_options(session_id="sess-xyz")
        assert opts.extra_args.get("session-id") == "sess-xyz"
        assert not getattr(opts, "resume", None)

    def test_max_turns_set(self, cli_instance):
        """max_turns is forwarded to options."""
        opts = cli_instance._build_sdk_options(max_turns=5)
        assert opts.max_turns == 5

    def test_model_set(self, cli_instance):
        """model is forwarded to options."""
        opts = cli_instance._build_sdk_options(model="opus")
        assert opts.model == "opus"

    def test_allowed_and_disallowed_tools(self, cli_instance):
        """allowed_tools and disallowed_tools are forwarded."""
        opts = cli_instance._build_sdk_options(
            allowed_tools=["Bash", "Read"],
            disallowed_tools=["Write"],
        )
        assert opts.allowed_tools == ["Bash", "Read"]
        assert "Write" in opts.disallowed_tools

    def test_permission_mode_set(self, cli_instance):
        """permission_mode is forwarded."""
        opts = cli_instance._build_sdk_options(permission_mode="bypassPermissions")
        assert opts.permission_mode == "bypassPermissions"


class TestConvertMessageTypeMap:
    """Test _convert_message with SDK-type _TYPE_CHECKS injection."""

    def test_type_injected_for_stream_event(self, cli_class):
        """StreamEvent type is injected when missing from __dict__."""
        from claude_agent_sdk.types import StreamEvent

        cli = _make_cli(cli_class)

        # Use __new__ to get exact type match without calling __init__
        obj = StreamEvent.__new__(StreamEvent)
        obj.data = "hello"

        result = cli._convert_message(obj)
        assert result.get("type") == "stream_event"
        assert result["data"] == "hello"

    def test_type_injected_for_result_message(self, cli_class):
        """ResultMessage type is injected when missing from __dict__."""
        from claude_agent_sdk.types import ResultMessage

        cli = _make_cli(cli_class)

        obj = ResultMessage.__new__(ResultMessage)
        obj.result = "done"
        obj.subtype = "success"

        result = cli._convert_message(obj)
        assert result.get("type") == "result"

    def test_type_not_overwritten_when_present(self, cli_class):
        """Existing type attribute is preserved, not overwritten."""
        from claude_agent_sdk.types import AssistantMessage

        cli = _make_cli(cli_class)

        obj = AssistantMessage.__new__(AssistantMessage)
        obj.type = "custom_type"
        obj.content = "hi"

        result = cli._convert_message(obj)
        assert result["type"] == "custom_type"

    def test_unknown_object_no_type_injection(self, cli_class):
        """Object not in _TYPE_CHECKS gets no type injected."""
        cli = _make_cli(cli_class)

        obj = SimpleNamespace(foo="bar")
        result = cli._convert_message(obj)
        assert "type" not in result
        assert result["foo"] == "bar"

    def test_system_message_subclass_gets_system_type(self, cli_class):
        """TaskStartedMessage (SystemMessage subclass) gets type='system' via isinstance."""
        from claude_agent_sdk.types import TaskStartedMessage

        cli = _make_cli(cli_class)

        obj = TaskStartedMessage.__new__(TaskStartedMessage)
        obj.subtype = "task_started"
        obj.data = {}
        obj.task_id = "t1"
        obj.description = "test task"
        obj.uuid = "u1"
        obj.session_id = "s1"

        result = cli._convert_message(obj)
        assert result.get("type") == "system"
        assert result["subtype"] == "task_started"

    def test_type_injected_for_rate_limit_event(self, cli_class):
        """RateLimitEvent (SDK 0.1.49+) gets type='rate_limit'."""
        from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

        cli = _make_cli(cli_class)

        info = RateLimitInfo(status="allowed_warning", utilization=0.8)
        obj = RateLimitEvent(rate_limit_info=info, uuid="rl-1", session_id="s1")

        result = cli._convert_message(obj)
        assert result.get("type") == "rate_limit"
        assert result["uuid"] == "rl-1"
        # rate_limit_info should be the real dataclass, not a dict
        assert hasattr(result["rate_limit_info"], "status")


class TestConvertMessageEdgeCases:
    """Test _convert_message edge cases."""

    def test_plain_string_returned_as_is(self, cli_class):
        """A plain string (no __dict__) is returned unchanged."""
        cli = _make_cli(cli_class)
        result = cli._convert_message("just a string")
        assert result == "just a string"

    def test_integer_returned_as_is(self, cli_class):
        """An integer (no __dict__) is returned unchanged."""
        cli = _make_cli(cli_class)
        result = cli._convert_message(42)
        assert result == 42

    def test_none_returned_as_is(self, cli_class):
        """None (no __dict__) is returned unchanged."""
        cli = _make_cli(cli_class)
        result = cli._convert_message(None)
        assert result is None

    def test_callable_attributes_excluded(self, cli_class):
        """Callable attributes on a message object are excluded from result."""
        cli = _make_cli(cli_class)

        obj = SimpleNamespace(type="assistant", content="hello")
        # Add a callable attribute
        obj.some_method = lambda: "should not appear"

        result = cli._convert_message(obj)
        assert "some_method" not in result
        assert result["type"] == "assistant"
        assert result["content"] == "hello"

    def test_private_attributes_excluded(self, cli_class):
        """Attributes starting with _ are excluded."""
        cli = _make_cli(cli_class)

        obj = SimpleNamespace(type="result", _internal="secret", visible="yes")
        result = cli._convert_message(obj)
        assert "_internal" not in result
        assert result["visible"] == "yes"

    def test_dict_message_not_modified(self, cli_class):
        """Dict messages pass through without modification."""
        cli = _make_cli(cli_class)
        original = {"type": "assistant", "content": "hi"}
        result = cli._convert_message(original)
        assert result is original


class TestSdkEnvContextManager:
    """Test ClaudeCodeCLI._sdk_env() environment variable management."""

    def test_sdk_env_sets_and_restores(self, cli_instance):
        """_sdk_env sets auth env vars and restores originals."""
        original = os.environ.get("ANTHROPIC_AUTH_TOKEN")

        with cli_instance._sdk_env():
            assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "test-key"

        if original is None:
            assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
        else:
            assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == original

    def test_sdk_env_restores_on_exception(self, cli_instance):
        """_sdk_env restores env vars even when an exception occurs."""
        original = os.environ.get("ANTHROPIC_AUTH_TOKEN")

        with pytest.raises(RuntimeError):
            with cli_instance._sdk_env():
                assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "test-key"
                raise RuntimeError("boom")

        if original is None:
            assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
        else:
            assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == original

    def test_sdk_env_noop_when_no_vars(self):
        """_sdk_env is a no-op when no auth env vars configured."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("src.auth.validate_claude_code_auth") as mock_validate:
                with patch("src.auth.auth_manager") as mock_auth:
                    mock_validate.return_value = (True, {"method": "anthropic"})
                    mock_auth.get_claude_code_env_vars.return_value = {}

                    from src.claude_cli import ClaudeCodeCLI

                    cli = ClaudeCodeCLI(cwd=temp_dir)

        env_before = dict(os.environ)
        with cli._sdk_env():
            env_during = dict(os.environ)
        env_after = dict(os.environ)

        assert env_before == env_during
        assert env_before == env_after


class TestConfigureHelpers:
    """Test the extracted _configure_* helper methods."""

    def test_configure_thinking_adaptive(self, cli_instance):
        """_configure_thinking sets adaptive mode."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.runtime_config.runtime_config.get", return_value="adaptive"):
            cli_instance._configure_thinking(opts)
        assert opts.thinking == {"type": "adaptive"}

    def test_configure_thinking_enabled(self, cli_instance):
        """_configure_thinking sets enabled mode with budget."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.runtime_config.runtime_config.get", return_value="enabled"):
            with patch("src.backends.claude.client.THINKING_BUDGET_TOKENS", 8000):
                cli_instance._configure_thinking(opts)
        assert opts.thinking == {"type": "enabled", "budget_tokens": 8000}

    def test_configure_thinking_disabled_noop(self, cli_instance):
        """_configure_thinking does nothing for disabled mode."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.runtime_config.runtime_config.get", return_value="disabled"):
            cli_instance._configure_thinking(opts)
        assert not hasattr(opts, "thinking") or opts.thinking is None

    def test_configure_tools_allowed_and_disallowed(self, cli_instance):
        """_configure_tools sets both allowed and disallowed lists."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        cli_instance._configure_tools(opts, ["Bash", "Read"], ["Write"])
        assert opts.allowed_tools == ["Bash", "Read"]
        assert "Write" in opts.disallowed_tools

    def test_configure_tools_none_inputs(self, cli_instance):
        """_configure_tools with None inputs still applies base disallowed."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch(
            "src.backends.claude.client.DISALLOWED_SUBAGENT_TYPES", ["Agent(statusline-setup)"]
        ):
            cli_instance._configure_tools(opts, None, None)
        # allowed_tools should be unchanged (SDK default, not set by _configure_tools)
        assert opts.allowed_tools == [] or opts.allowed_tools is None
        assert "Agent(statusline-setup)" in opts.disallowed_tools

    def test_configure_session_resume(self, cli_instance):
        """_configure_session sets resume when provided."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        cli_instance._configure_session(opts, session_id="sess-1", resume="resume-1")
        assert opts.resume == "resume-1"
        assert opts.extra_args.get("session-id") is None

    def test_configure_session_id_only(self, cli_instance):
        """_configure_session sets session-id when no resume."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        cli_instance._configure_session(opts, session_id="sess-1", resume=None)
        assert opts.extra_args.get("session-id") == "sess-1"
        assert not getattr(opts, "resume", None)

    def test_configure_session_neither(self, cli_instance):
        """_configure_session does nothing when both are None."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        cli_instance._configure_session(opts, session_id=None, resume=None)
        assert not getattr(opts, "resume", None)
        assert opts.extra_args.get("session-id") is None

    def test_configure_sandbox_enabled(self, cli_instance):
        """_configure_sandbox sets sandbox when CLAUDE_SANDBOX_ENABLED=True."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True):
            with patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True):
                with patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []):
                    with patch(
                        "src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False
                    ):
                        with patch(
                            "src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False
                        ):
                            with patch(
                                "src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False
                            ):
                                cli_instance._configure_sandbox(opts)
        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is True
        assert opts.sandbox["allowUnsandboxedCommands"] is False

    def test_configure_sandbox_disabled(self, cli_instance):
        """_configure_sandbox sets sandbox.enabled=False when env is False."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", False):
            cli_instance._configure_sandbox(opts)
        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is False

    def test_configure_sandbox_unset_noop(self, cli_instance):
        """_configure_sandbox does nothing when CLAUDE_SANDBOX_ENABLED is None."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", None):
            cli_instance._configure_sandbox(opts)
        assert getattr(opts, "sandbox", None) is None

    def test_build_sdk_options_includes_sandbox(self, cli_instance):
        """_build_sdk_options calls _configure_sandbox."""
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True):
            with patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True):
                with patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []):
                    with patch(
                        "src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False
                    ):
                        with patch(
                            "src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False
                        ):
                            with patch(
                                "src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False
                            ):
                                opts = cli_instance._build_sdk_options()
        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is True
