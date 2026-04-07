"""
Coverage gap tests for small modules.

Targets uncovered lines in:
- src/auth.py (lines 114-115, 139-140, 242, 279-280)
- src/parameter_validator.py (lines 24-25, 31-32, 136-137)
- src/session_manager.py (lines 154-156, 165-166)
- src/models.py (lines 36-37, 90)
- src/mcp_config.py (lines 64-65)
- src/message_adapter.py (line 92)
"""

import asyncio
import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.models import ChatCompletionRequest, ContentPart, Message


# ============================================================================
# src/auth.py coverage gaps
# ============================================================================


class TestAuthGetProviderRegistryException:
    """Cover lines 114-115: Exception in get_provider() when BackendRegistry fails."""

    def test_get_provider_falls_back_when_registry_raises(self):
        """When BackendRegistry.is_registered raises, fall back to direct instantiation."""
        import src.auth

        importlib.reload(src.auth)

        # Patch the BackendRegistry class at its source so the local import picks it up
        mock_registry = MagicMock()
        mock_registry.is_registered.side_effect = Exception("registry broken")

        with patch("src.backends.base.BackendRegistry", mock_registry):
            provider = src.auth.auth_manager.get_provider("claude")
            assert provider.name == "claude"


class TestAuthGetApiKeyImportException:
    """Cover lines 139-140: Exception in get_api_key() when importing main fails."""

    def test_get_api_key_falls_back_to_env_when_import_fails(self):
        """When 'from src import main' raises ImportError, fall back to env_api_key."""
        with patch.dict(os.environ, {"API_KEY": "env-fallback-key"}):
            import src.auth

            importlib.reload(src.auth)

            # Force ImportError when trying to import src.main inside get_api_key
            original_import = (
                __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__
            )

            def failing_import(name, *args, **kwargs):
                if name == "src" or name == "src.main":
                    raise ImportError("forced import failure")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=failing_import):
                result = src.auth.auth_manager.get_api_key()
                assert result == "env-fallback-key"


class TestAuthValidateBackendAuthFailure:
    """Cover line 242: Error logging in validate_backend_auth() when validation fails."""

    def test_validate_backend_auth_logs_error_on_invalid(self):
        """When provider.validate() returns valid=False, the error is logged."""
        env_copy = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_AUTH_TOKEN"}
        env_copy["CLAUDE_AUTH_METHOD"] = "anthropic"
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)

            with patch("src.auth.logger") as mock_logger:
                is_valid, status = src.auth.validate_backend_auth("claude")
                assert is_valid is False
                mock_logger.error.assert_called_once()
                assert "claude" in str(mock_logger.error.call_args)


class TestAuthGetAllBackendsAuthInfoException:
    """Cover lines 279-280: Exception in get_all_backends_auth_info() per backend."""

    def test_get_all_backends_catches_per_backend_exception(self):
        """When get_provider raises for one backend, result still includes error info."""
        import src.auth

        importlib.reload(src.auth)

        original_get_provider = src.auth.auth_manager.get_provider

        def flaky_get_provider(backend):
            if backend == "claude":
                raise RuntimeError("claude provider exploded")
            return original_get_provider(backend)

        with patch.object(src.auth.auth_manager, "get_provider", side_effect=flaky_get_provider):
            result = src.auth.get_all_backends_auth_info()

        assert "claude" in result
        assert result["claude"]["status"]["valid"] is False
        assert "claude provider exploded" in result["claude"]["status"]["errors"][0]


# ============================================================================
# src/parameter_validator.py coverage gaps
# ============================================================================


class TestParameterValidatorRegistryException:
    """Cover lines 24-25: Exception when BackendRegistry.all_model_ids() fails."""

    def test_get_supported_models_falls_back_when_registry_raises(self):
        """When BackendRegistry.all_model_ids() raises, fall back to constants."""
        from src.parameter_validator import ParameterValidator

        # Patch at the source module so the local `from src.backends.base import ...` picks it up
        mock_registry = MagicMock()
        mock_registry.all_model_ids.side_effect = Exception("registry broken")

        with patch("src.backends.base.BackendRegistry", mock_registry):
            models = ParameterValidator._get_supported_models()
            # Should fall back to ALL_MODELS from constants
            assert isinstance(models, set)
            assert len(models) > 0


class TestParameterValidatorDoubleException:
    """Cover lines 31-32: Fallback exception when importing ALL_MODELS fails."""

    def test_get_supported_models_returns_empty_when_all_fail(self):
        """When both BackendRegistry and ALL_MODELS import fail, return empty set."""
        from src.parameter_validator import ParameterValidator

        mock_registry = MagicMock()
        mock_registry.all_model_ids.side_effect = Exception("registry broken")

        # Make the constants module not have ALL_MODELS
        mock_constants = MagicMock(spec=[])  # spec=[] means no attributes

        with patch("src.backends.base.BackendRegistry", mock_registry):
            with patch.dict("sys.modules", {"src.constants": mock_constants}):
                models = ParameterValidator._get_supported_models()
                assert models == set()


class TestCompatibilityReporterNGreaterThan1:
    """Cover lines 136-137: Suggestion for unsupported n parameter (n > 1)."""

    def test_n_greater_than_1_suggestion(self):
        """CompatibilityReporter generates suggestion when n > 1."""
        from src.parameter_validator import CompatibilityReporter

        # Create request with n=1 (the only valid value) but manually override for report
        request = ChatCompletionRequest(
            model="claude-sonnet-4-20250514",
            messages=[Message(role="user", content="Hello")],
            n=1,
        )
        # Manually set n > 1 to bypass validation for testing the reporter
        object.__setattr__(request, "n", 2)

        report = CompatibilityReporter.generate_compatibility_report(request)
        assert "n" in report["unsupported_parameters"]
        assert any("single responses" in s for s in report["suggestions"])


# ============================================================================
# src/session_manager.py coverage gaps
# ============================================================================


class TestSessionManagerCleanupCancelled:
    """Cover lines 151-157: CancelledError in cleanup_loop()."""

    async def test_cleanup_loop_handles_cancellation(self):
        """When the cleanup task is cancelled, it logs and re-raises CancelledError."""
        from src.session_manager import SessionManager

        manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=1)

        # Patch asyncio.sleep so the cleanup_loop completes one iteration quickly
        # then raises CancelledError on the second, triggering lines 155-157.
        call_count = 0
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First sleep returns instantly so lines 153-154 execute
                await original_sleep(0)
            else:
                # Second sleep raises CancelledError to trigger lines 155-157
                raise asyncio.CancelledError()

        with patch("src.session_manager.asyncio.sleep", side_effect=fast_sleep):
            with patch("src.session_manager.logger") as mock_logger:
                manager.start_cleanup_task()
                assert manager._cleanup_task is not None

                # Let the task run and handle the CancelledError
                with pytest.raises(asyncio.CancelledError):
                    await manager._cleanup_task

                mock_logger.info.assert_any_call("Session cleanup task cancelled")


class TestSessionManagerNoEventLoop:
    """Cover lines 165-166: No running event loop for automatic cleanup."""

    def test_start_cleanup_task_warns_without_event_loop(self):
        """When no event loop is running, start_cleanup_task logs warning."""
        from src.session_manager import SessionManager

        manager = SessionManager(default_ttl_minutes=60, cleanup_interval_minutes=5)

        # Patch get_running_loop to raise RuntimeError (no loop)
        with patch("src.session_manager.asyncio.get_running_loop", side_effect=RuntimeError):
            with patch("src.session_manager.logger") as mock_logger:
                manager.start_cleanup_task()

                assert manager._cleanup_task is None
                mock_logger.warning.assert_any_call(
                    "No running event loop, automatic session cleanup disabled"
                )


# ============================================================================
# src/models.py coverage gaps
# ============================================================================


class TestMessageTextParsingErrorRecovery:
    """Cover lines 36-37: Text parsing error recovery (empty content from dicts).

    Lines 36-37 handle dicts in the content list. Since Pydantic coerces valid
    text-dicts to ContentPart, we construct a Message with model_construct()
    to bypass validation and put raw dicts in the content list, then invoke
    the normalize_content validator manually.
    """

    def test_message_normalizes_dict_content_with_missing_text(self):
        """Dict content parts with missing 'text' key produce empty string via .get()."""
        # Construct without validation to put raw dicts in content
        msg = Message.model_construct(
            role="user",
            content=[
                {"type": "text"},  # Missing 'text' key -> .get("text", "")
                {"type": "text", "text": "Valid"},
            ],
        )
        # Run the model_validator manually
        msg = msg.normalize_content()
        assert msg.content == "\nValid"

    def test_message_normalizes_non_text_content_parts_to_empty(self):
        """Content parts that are neither ContentPart nor text-dicts are skipped."""
        msg = Message.model_construct(
            role="user",
            content=[
                {"type": "image", "url": "http://example.com/img.png"},
            ],
        )
        msg = msg.normalize_content()
        assert msg.content == ""

    def test_message_normalizes_mixed_content_parts(self):
        """Mix of ContentPart objects and raw dicts processes correctly."""
        msg = Message.model_construct(
            role="user",
            content=[
                ContentPart(type="text", text="From object"),
                {"type": "text", "text": "From dict"},
                {"type": "image"},  # Skipped — not text type
            ],
        )
        msg = msg.normalize_content()
        assert msg.content == "From object\nFrom dict"


class TestModelValidateNGreaterThan1:
    """Cover line 90: Validator for n > 1 (raises ValueError)."""

    def test_n_greater_than_1_raises_value_error(self):
        """ChatCompletionRequest with n > 1 raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ChatCompletionRequest(
                messages=[Message(role="user", content="Hi")],
                n=5,
            )
        assert "multiple choices" in str(exc_info.value).lower()

    def test_n_equal_to_2_raises_value_error(self):
        """ChatCompletionRequest with n=2 raises ValidationError."""
        with pytest.raises(ValidationError):
            ChatCompletionRequest(
                messages=[Message(role="user", content="Hi")],
                n=2,
            )


# ============================================================================
# src/mcp_config.py coverage gaps
# ============================================================================


class TestMcpConfigNonDictServer:
    """Cover lines 64-65: MCP server config that is not a dict."""

    def test_non_dict_server_config_is_skipped(self):
        """Server config values that are not dicts are skipped with warning."""
        config = {
            "mcpServers": {
                "valid": {"type": "stdio", "command": "echo"},
                "bad-string": "not a dict",
                "bad-list": [1, 2, 3],
                "bad-number": 42,
            }
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            from src.mcp_config import load_mcp_config

            with patch("src.mcp_config.logger") as mock_logger:
                result = load_mcp_config()

            assert "valid" in result
            assert "bad-string" not in result
            assert "bad-list" not in result
            assert "bad-number" not in result
            warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
            assert any("bad-string" in w for w in warning_calls)

    def test_non_dict_server_config_without_mcpservers_wrapper(self):
        """Flat format with non-dict server values are also skipped."""
        config = {
            "good-server": {"type": "stdio", "command": "ls"},
            "bad-server": "just a string",
        }
        with patch("src.mcp_config.MCP_CONFIG", json.dumps(config)):
            from src.mcp_config import load_mcp_config

            result = load_mcp_config()

        assert "good-server" in result
        assert "bad-server" not in result


# ============================================================================
# src/message_adapter.py coverage gaps
# ============================================================================


class TestMessageAdapterThinkingBlockObject:
    """Cover line 92: ThinkingBlock formatting with <think> tags."""

    def test_format_block_thinking_object(self):
        """ThinkingBlock object (with .thinking attr) is formatted with <think> tags."""
        from src.message_adapter import MessageAdapter

        thinking_block = SimpleNamespace(thinking="deep thought about the problem")
        result = MessageAdapter.format_block(thinking_block)
        assert result == "<think>deep thought about the problem</think>"

    def test_format_block_thinking_object_empty(self):
        """ThinkingBlock object with empty thinking is formatted as empty <think> tags."""
        from src.message_adapter import MessageAdapter

        thinking_block = SimpleNamespace(thinking="")
        result = MessageAdapter.format_block(thinking_block)
        assert result == "<think></think>"

    def test_format_block_thinking_object_none(self):
        """ThinkingBlock object with None thinking is formatted with empty content."""
        from src.message_adapter import MessageAdapter

        thinking_block = SimpleNamespace(thinking=None)
        result = MessageAdapter.format_block(thinking_block)
        assert result == "<think></think>"


# Reset auth module state after auth tests
@pytest.fixture(autouse=True)
def _reset_auth_module_after_test():
    """Reset auth module after each test to avoid cross-test pollution."""
    yield
    try:
        import src.auth

        importlib.reload(src.auth)
    except Exception:
        pass
