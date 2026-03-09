#!/usr/bin/env python3
"""
Unit tests for src/auth.py

Tests the ClaudeCodeAuthManager and authentication functions.
These are pure unit tests that don't require a running server.
"""

import pytest
import os
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException

# We need to patch environment before importing auth module
import importlib


class TestClaudeCodeAuthManagerDetectMethod:
    """Test _detect_auth_method()"""

    def test_explicit_cli_method(self):
        """CLAUDE_AUTH_METHOD=cli uses claude_cli."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "cli"}, clear=False):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.auth_method == "claude_cli"

    def test_explicit_claude_cli_method(self):
        """CLAUDE_AUTH_METHOD=claude_cli uses claude_cli."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "claude_cli"}, clear=False):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.auth_method == "claude_cli"

    def test_explicit_api_key_method(self):
        """CLAUDE_AUTH_METHOD=api_key uses anthropic."""
        with patch.dict(
            os.environ,
            {"CLAUDE_AUTH_METHOD": "api_key", "ANTHROPIC_AUTH_TOKEN": "test-key-12345"},
            clear=False,
        ):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.auth_method == "anthropic"

    def test_explicit_anthropic_method(self):
        """CLAUDE_AUTH_METHOD=anthropic uses anthropic."""
        with patch.dict(
            os.environ,
            {"CLAUDE_AUTH_METHOD": "anthropic", "ANTHROPIC_AUTH_TOKEN": "test-key-12345"},
            clear=False,
        ):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.auth_method == "anthropic"

    def test_unknown_method_raises_error(self):
        """Unknown CLAUDE_AUTH_METHOD raises ValueError."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "unknown_method"}, clear=False):
            import src.auth

            with pytest.raises(ValueError, match="Unsupported CLAUDE_AUTH_METHOD"):
                importlib.reload(src.auth)

    def test_bedrock_method_raises_error(self):
        """CLAUDE_AUTH_METHOD=bedrock raises ValueError (no longer supported)."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "bedrock"}, clear=False):
            import src.auth

            with pytest.raises(ValueError, match="Unsupported CLAUDE_AUTH_METHOD"):
                importlib.reload(src.auth)

    def test_vertex_method_raises_error(self):
        """CLAUDE_AUTH_METHOD=vertex raises ValueError (no longer supported)."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "vertex"}, clear=False):
            import src.auth

            with pytest.raises(ValueError, match="Unsupported CLAUDE_AUTH_METHOD"):
                importlib.reload(src.auth)

    def test_auto_detect_anthropic_key(self):
        """ANTHROPIC_AUTH_TOKEN auto-detects to anthropic."""
        env = {"ANTHROPIC_AUTH_TOKEN": "test-key-12345678901234567890"}
        env_copy = {k: v for k, v in os.environ.items() if k not in ["CLAUDE_AUTH_METHOD"]}
        env_copy.update(env)
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.auth_method == "anthropic"

    def test_default_to_claude_cli(self):
        """No env vars defaults to claude_cli."""
        env_copy = {
            k: v
            for k, v in os.environ.items()
            if k
            not in [
                "CLAUDE_AUTH_METHOD",
                "ANTHROPIC_AUTH_TOKEN",
            ]
        }
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.auth_method == "claude_cli"


class TestClaudeCodeAuthManagerValidation:
    """Test authentication validation methods."""

    def test_validate_anthropic_valid(self):
        """Valid ANTHROPIC_AUTH_TOKEN passes validation."""
        with patch.dict(
            os.environ,
            {
                "CLAUDE_AUTH_METHOD": "anthropic",
                "ANTHROPIC_AUTH_TOKEN": "sk-ant-api03-validkey1234567890",
            },
        ):
            import src.auth

            importlib.reload(src.auth)
            status = src.auth.auth_manager.auth_status
            assert status["valid"] is True
            assert status["method"] == "anthropic"

    def test_validate_anthropic_missing_key(self):
        """Missing ANTHROPIC_AUTH_TOKEN fails validation."""
        env_copy = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_AUTH_TOKEN"}
        env_copy["CLAUDE_AUTH_METHOD"] = "anthropic"
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            status = src.auth.auth_manager.auth_status
            assert status["valid"] is False
            assert any("ANTHROPIC_AUTH_TOKEN" in err for err in status["errors"])

    def test_validate_anthropic_short_key(self):
        """Short ANTHROPIC_AUTH_TOKEN still passes validation with a warning."""
        with patch.dict(
            os.environ,
            {"CLAUDE_AUTH_METHOD": "anthropic", "ANTHROPIC_AUTH_TOKEN": "short"},
        ):
            import src.auth

            importlib.reload(src.auth)
            status = src.auth.auth_manager.auth_status
            assert status["valid"] is True

    def test_validate_claude_cli_always_valid(self):
        """Claude CLI auth is always considered valid initially."""
        env_copy = {k: v for k, v in os.environ.items() if k != "CLAUDE_AUTH_METHOD"}
        env_copy["CLAUDE_AUTH_METHOD"] = "cli"
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            status = src.auth.auth_manager.auth_status
            assert status["valid"] is True
            assert status["method"] == "claude_cli"


class TestClaudeCodeAuthManagerEnvVars:
    """Test get_claude_code_env_vars()"""

    def test_anthropic_env_vars(self):
        """Anthropic method returns ANTHROPIC_AUTH_TOKEN."""
        with patch.dict(
            os.environ,
            {
                "CLAUDE_AUTH_METHOD": "anthropic",
                "ANTHROPIC_AUTH_TOKEN": "test-key-12345",
            },
        ):
            import src.auth

            importlib.reload(src.auth)
            env_vars = src.auth.auth_manager.get_claude_code_env_vars()
            assert "ANTHROPIC_AUTH_TOKEN" in env_vars
            assert env_vars["ANTHROPIC_AUTH_TOKEN"] == "test-key-12345"

    def test_cli_env_vars_empty(self):
        """CLI method returns no environment variables."""
        env_copy = {k: v for k, v in os.environ.items() if k != "CLAUDE_AUTH_METHOD"}
        env_copy["CLAUDE_AUTH_METHOD"] = "cli"
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            env_vars = src.auth.auth_manager.get_claude_code_env_vars()
            assert env_vars == {}


class TestVerifyApiKey:
    """Test verify_api_key() function."""

    @pytest.mark.asyncio
    async def test_no_api_key_configured_allows_all(self):
        """When no API_KEY is set, all requests are allowed."""
        env_copy = {k: v for k, v in os.environ.items() if k != "API_KEY"}
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)

            # Mock auth_manager to have no API key
            with patch.object(src.auth.auth_manager, "get_api_key", return_value=None):
                mock_request = MagicMock()
                result = await src.auth.verify_api_key(mock_request)
                assert result is True

    @pytest.mark.asyncio
    async def test_valid_api_key_passes(self):
        """Valid API key in Authorization header passes."""
        with patch.dict(os.environ, {"API_KEY": "test-secret-key"}):
            import src.auth

            importlib.reload(src.auth)

            from fastapi.security import HTTPAuthorizationCredentials

            mock_request = MagicMock()
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer", credentials="test-secret-key"
            )

            with patch.object(src.auth.auth_manager, "get_api_key", return_value="test-secret-key"):
                result = await src.auth.verify_api_key(mock_request, credentials)
                assert result is True

    @pytest.mark.asyncio
    async def test_invalid_api_key_raises_401(self):
        """Invalid API key raises 401 HTTPException."""
        with patch.dict(os.environ, {"API_KEY": "correct-key"}):
            import src.auth

            importlib.reload(src.auth)

            from fastapi.security import HTTPAuthorizationCredentials

            mock_request = MagicMock()
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-key")

            with patch.object(src.auth.auth_manager, "get_api_key", return_value="correct-key"):
                with pytest.raises(HTTPException) as exc_info:
                    await src.auth.verify_api_key(mock_request, credentials)
                assert exc_info.value.status_code == 401
                assert "Invalid API key" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_credentials_raises_401(self):
        """Missing credentials raise 401 HTTPException."""
        with patch.dict(os.environ, {"API_KEY": "test-key"}):
            import src.auth

            importlib.reload(src.auth)

            mock_request = MagicMock()
            # Mock security to return None (no credentials)
            with patch.object(src.auth, "security", AsyncMock(return_value=None)):
                with patch.object(src.auth.auth_manager, "get_api_key", return_value="test-key"):
                    with pytest.raises(HTTPException) as exc_info:
                        await src.auth.verify_api_key(mock_request, None)
                    assert exc_info.value.status_code == 401
                    assert "Missing API key" in exc_info.value.detail


class TestValidateClaudeCodeAuth:
    """Test validate_claude_code_auth() function."""

    def test_valid_auth_returns_true(self):
        """Valid auth returns (True, status)."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "cli"}):
            import src.auth

            importlib.reload(src.auth)

            is_valid, status = src.auth.validate_claude_code_auth()
            assert is_valid is True
            assert status["valid"] is True

    def test_invalid_auth_returns_false(self):
        """Invalid auth returns (False, status)."""
        env_copy = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_AUTH_TOKEN"}
        env_copy["CLAUDE_AUTH_METHOD"] = "anthropic"
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)

            is_valid, status = src.auth.validate_claude_code_auth()
            assert is_valid is False
            assert status["valid"] is False


class TestGetClaudeCodeAuthInfo:
    """Test get_claude_code_auth_info() function."""

    def test_returns_auth_info(self):
        """Returns comprehensive auth information."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "cli"}):
            import src.auth

            importlib.reload(src.auth)

            info = src.auth.get_claude_code_auth_info()
            assert "method" in info
            assert "status" in info
            assert "environment_variables" in info


class TestGetApiKey:
    """Test ClaudeCodeAuthManager.get_api_key()"""

    def test_returns_env_api_key(self):
        """Returns API_KEY from environment."""
        with patch.dict(os.environ, {"API_KEY": "env-api-key"}):
            import src.auth

            importlib.reload(src.auth)
            assert src.auth.auth_manager.get_api_key() == "env-api-key"

    def test_runtime_api_key_takes_precedence(self):
        """runtime_api_key in src.main should take precedence over env API_KEY."""
        with patch.dict(os.environ, {"API_KEY": "env-key"}):
            import src.auth
            import src.main as main

            # Manually set runtime_api_key
            main.runtime_api_key = "runtime-secret"

            try:
                # Reload to ensure it tries to import from main
                importlib.reload(src.auth)
                assert src.auth.auth_manager.get_api_key() == "runtime-secret"
            finally:
                main.runtime_api_key = None

    def test_returns_runtime_key_when_available(self):
        """Returns runtime key when set in main module."""
        with patch.dict(os.environ, {"API_KEY": "env-key"}):
            import src.auth

            importlib.reload(src.auth)

            # Mock the runtime API key
            mock_main = MagicMock()
            mock_main.runtime_api_key = "runtime-key"

            with patch.dict("sys.modules", {"src.main": mock_main}):
                # Need to reload to pick up the mock
                result = src.auth.auth_manager.get_api_key()
                # May return env key if import fails, but shouldn't error
                assert result in ["env-key", "runtime-key"]


class TestCleanStaleEnvVars:
    """Test clean_stale_env_vars()"""

    def test_removes_stale_bedrock_vars(self):
        """Removes stale Bedrock/Vertex env vars from os.environ."""
        stale_vars = {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "test-project",
            "CLOUD_ML_REGION": "us-central1",
        }
        with patch.dict(os.environ, stale_vars, clear=False):
            import src.auth

            importlib.reload(src.auth)
            # Verify vars exist before cleanup
            for var in stale_vars:
                assert var in os.environ
            src.auth.auth_manager.clean_stale_env_vars()
            # Verify all removed
            for var in stale_vars:
                assert var not in os.environ

    def test_no_error_when_stale_vars_absent(self):
        """Does not error when stale vars are not present."""
        env_copy = {
            k: v
            for k, v in os.environ.items()
            if k
            not in [
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "ANTHROPIC_VERTEX_PROJECT_ID",
                "CLOUD_ML_REGION",
            ]
        }
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            # Should not raise
            src.auth.auth_manager.clean_stale_env_vars()


class TestTimingSafeComparison:
    """Verify API key comparison uses timing-safe method."""

    @pytest.mark.asyncio
    async def test_uses_hmac_compare_digest(self):
        """verify_api_key uses hmac.compare_digest for comparison."""
        with patch.dict(os.environ, {"API_KEY": "test-key"}):
            import src.auth

            importlib.reload(src.auth)

            from fastapi.security import HTTPAuthorizationCredentials

            mock_request = MagicMock()
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-key")

            with patch.object(src.auth.auth_manager, "get_api_key", return_value="test-key"):
                with patch("src.auth.hmac") as mock_hmac:
                    mock_hmac.compare_digest.return_value = False
                    with pytest.raises(HTTPException) as exc_info:
                        await src.auth.verify_api_key(mock_request, credentials)
                    assert exc_info.value.status_code == 401
                    mock_hmac.compare_digest.assert_called_once_with("wrong-key", "test-key")


class TestBackendAuthProviders:
    """Test BackendAuthProvider ABC and concrete implementations."""

    def test_claude_provider_name(self):
        """ClaudeAuthProvider reports name 'claude'."""
        from src.auth import ClaudeAuthProvider

        provider = ClaudeAuthProvider()
        assert provider.name == "claude"

    def test_codex_provider_name(self):
        """CodexAuthProvider reports name 'codex'."""
        from src.auth import CodexAuthProvider

        provider = CodexAuthProvider()
        assert provider.name == "codex"

    def test_codex_validate_missing_key(self):
        """CodexAuthProvider fails when OPENAI_API_KEY is not set."""
        env_copy = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env_copy, clear=True):
            from src.auth import CodexAuthProvider

            provider = CodexAuthProvider()
            status = provider.validate()
            assert status["valid"] is False
            assert any("OPENAI_API_KEY" in e for e in status["errors"])

    def test_codex_validate_valid_key(self):
        """CodexAuthProvider passes with a valid sk-... key."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-12345"}):
            from src.auth import CodexAuthProvider

            provider = CodexAuthProvider()
            status = provider.validate()
            assert status["valid"] is True
            assert status["config"]["api_key_present"] is True

    def test_codex_validate_non_sk_key_warns(self):
        """CodexAuthProvider warns when key doesn't start with sk-."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-sk-key"}):
            from src.auth import CodexAuthProvider

            provider = CodexAuthProvider()
            status = provider.validate()
            # Still valid, just warns
            assert status["valid"] is True

    def test_codex_build_env(self):
        """CodexAuthProvider.build_env returns OPENAI_API_KEY."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-12345"}):
            from src.auth import CodexAuthProvider

            provider = CodexAuthProvider()
            env = provider.build_env()
            assert env == {"OPENAI_API_KEY": "sk-test-key-12345"}

    def test_codex_build_env_empty_when_no_key(self):
        """CodexAuthProvider.build_env returns empty dict without key."""
        env_copy = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env_copy, clear=True):
            from src.auth import CodexAuthProvider

            provider = CodexAuthProvider()
            assert provider.build_env() == {}

    def test_claude_isolation_vars(self):
        """ClaudeAuthProvider isolates OPENAI_API_KEY."""
        from src.auth import ClaudeAuthProvider

        provider = ClaudeAuthProvider()
        assert "OPENAI_API_KEY" in provider.get_isolation_vars()

    def test_codex_isolation_vars(self):
        """CodexAuthProvider isolates ANTHROPIC_AUTH_TOKEN."""
        from src.auth import CodexAuthProvider

        provider = CodexAuthProvider()
        assert "ANTHROPIC_AUTH_TOKEN" in provider.get_isolation_vars()


class TestCrossIsolation:
    """Verify that backend env isolation works bidirectionally."""

    def test_claude_env_excludes_openai_key(self):
        """Claude build_env never includes OPENAI_API_KEY."""
        with patch.dict(
            os.environ,
            {
                "CLAUDE_AUTH_METHOD": "anthropic",
                "ANTHROPIC_AUTH_TOKEN": "ant-key-12345",
                "OPENAI_API_KEY": "sk-openai-key-12345",
            },
        ):
            from src.auth import ClaudeAuthProvider

            provider = ClaudeAuthProvider()
            env = provider.build_env()
            assert "OPENAI_API_KEY" not in env
            assert "ANTHROPIC_AUTH_TOKEN" in env

    def test_codex_env_excludes_anthropic_token(self):
        """Codex build_env never includes ANTHROPIC_AUTH_TOKEN."""
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "ant-key-12345",
                "OPENAI_API_KEY": "sk-openai-key-12345",
            },
        ):
            from src.auth import CodexAuthProvider

            provider = CodexAuthProvider()
            env = provider.build_env()
            assert "ANTHROPIC_AUTH_TOKEN" not in env
            assert "OPENAI_API_KEY" in env

    def test_isolation_vars_are_symmetric(self):
        """Each provider's isolation list contains the other's key."""
        from src.auth import ClaudeAuthProvider, CodexAuthProvider

        claude_iso = ClaudeAuthProvider().get_isolation_vars()
        codex_iso = CodexAuthProvider().get_isolation_vars()
        # Claude isolates OPENAI_API_KEY (Codex's key)
        assert "OPENAI_API_KEY" in claude_iso
        # Codex isolates ANTHROPIC_AUTH_TOKEN (Claude's key)
        assert "ANTHROPIC_AUTH_TOKEN" in codex_iso


class TestAuthManagerGetProvider:
    """Test ClaudeCodeAuthManager.get_provider()."""

    def test_get_claude_provider(self):
        """get_provider('claude') returns ClaudeAuthProvider."""
        import src.auth

        importlib.reload(src.auth)
        provider = src.auth.auth_manager.get_provider("claude")
        assert provider.name == "claude"

    def test_get_codex_provider(self):
        """get_provider('codex') returns CodexAuthProvider."""
        import src.auth

        importlib.reload(src.auth)
        provider = src.auth.auth_manager.get_provider("codex")
        assert provider.name == "codex"

    def test_get_unknown_provider_raises(self):
        """get_provider with unknown name raises ValueError."""
        import src.auth

        importlib.reload(src.auth)
        with pytest.raises(ValueError, match="Unknown backend"):
            src.auth.auth_manager.get_provider("unknown")

    def test_codex_provider_is_lazy(self):
        """Codex provider is only created on first access."""
        import src.auth

        importlib.reload(src.auth)
        assert src.auth.auth_manager._codex_provider is None
        src.auth.auth_manager.get_provider("codex")
        assert src.auth.auth_manager._codex_provider is not None


class TestValidateBackendAuth:
    """Test validate_backend_auth() function."""

    def test_validate_claude_backend(self):
        """validate_backend_auth('claude') works."""
        with patch.dict(os.environ, {"CLAUDE_AUTH_METHOD": "cli"}):
            import src.auth

            importlib.reload(src.auth)
            is_valid, status = src.auth.validate_backend_auth("claude")
            assert is_valid is True
            assert status["method"] == "claude"

    def test_validate_codex_backend_missing_key(self):
        """validate_backend_auth('codex') fails without OPENAI_API_KEY."""
        env_copy = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            is_valid, status = src.auth.validate_backend_auth("codex")
            assert is_valid is False

    def test_validate_codex_backend_with_key(self):
        """validate_backend_auth('codex') passes with OPENAI_API_KEY."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-12345"}):
            import src.auth

            importlib.reload(src.auth)
            is_valid, status = src.auth.validate_backend_auth("codex")
            assert is_valid is True


class TestGetAllBackendsAuthInfo:
    """Test get_all_backends_auth_info() function."""

    def test_returns_both_backends(self):
        """Returns info for both claude and codex."""
        with patch.dict(
            os.environ,
            {"CLAUDE_AUTH_METHOD": "cli", "OPENAI_API_KEY": "sk-test-12345"},
        ):
            import src.auth

            importlib.reload(src.auth)
            info = src.auth.get_all_backends_auth_info()
            assert "claude" in info
            assert "codex" in info
            assert info["claude"]["status"]["valid"] is True
            assert info["codex"]["status"]["valid"] is True

    def test_codex_invalid_when_no_key(self):
        """Codex shows invalid when OPENAI_API_KEY is missing."""
        env_copy = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env_copy["CLAUDE_AUTH_METHOD"] = "cli"
        with patch.dict(os.environ, env_copy, clear=True):
            import src.auth

            importlib.reload(src.auth)
            info = src.auth.get_all_backends_auth_info()
            assert info["claude"]["status"]["valid"] is True
            assert info["codex"]["status"]["valid"] is False


# Reset module state after tests
@pytest.fixture(autouse=True)
def reset_auth_module():
    """Reset auth module after each test."""
    yield
    # Restore default state
    import src.auth

    importlib.reload(src.auth)
