"""Coverage tests for backend __init__.py modules.

Targets uncovered lines in:
- src/backends/__init__.py (discover_backends registration)
- src/backends/claude/__init__.py (lines 46-50: __getattr__ lazy imports;
  lines 59-79: register() exception handling)
"""

import pytest
from unittest.mock import patch

from src.backends.base import BackendRegistry


# ---------------------------------------------------------------------------
# Fixture: clean registry for every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def empty_registry():
    """Ensure an empty BackendRegistry (no descriptors) before and after each test."""
    BackendRegistry.clear()
    yield
    BackendRegistry.clear()


# ===========================================================================
# src/backends/__init__.py — discover_backends
# ===========================================================================


class TestDiscoverBackends:
    """Test discover_backends() in src/backends/__init__.py."""

    def test_discover_backends_registers_claude(self, tmp_path):
        """Happy path: Claude backend registers successfully."""
        with (
            patch(
                "src.auth.validate_claude_code_auth",
                return_value=(True, {"method": "claude_cli"}),
            ),
            patch("src.auth.auth_manager") as mock_auth,
            patch.dict("os.environ", {"CLAUDE_CWD": str(tmp_path)}),
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends import discover_backends

            discover_backends()

            assert BackendRegistry.is_registered("claude")

    def test_discover_backends_with_custom_registry_cls(self):
        """discover_backends accepts a custom registry_cls argument."""

        class FakeRegistry:
            descriptors = {}
            clients = {}

            @classmethod
            def register_descriptor(cls, desc):
                cls.descriptors[desc.name] = desc

            @classmethod
            def register(cls, name, client):
                cls.clients[name] = client

        with (
            patch(
                "src.auth.validate_claude_code_auth",
                return_value=(True, {"method": "claude_cli"}),
            ),
            patch("src.auth.auth_manager") as mock_auth,
            patch.dict("os.environ", {"CLAUDE_CWD": "/tmp"}),
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends import discover_backends

            discover_backends(registry_cls=FakeRegistry)

            assert "claude" in FakeRegistry.descriptors
            assert "claude" in FakeRegistry.clients


# ===========================================================================
# src/backends/claude/__init__.py — __getattr__ lazy imports
# ===========================================================================


class TestClaudeGetattr:
    """Test lazy attribute access on the claude subpackage."""

    def test_getattr_claude_code_cli(self):
        """Accessing ClaudeCodeCLI lazily imports from client module."""
        import src.backends.claude as claude_pkg

        cls = claude_pkg.ClaudeCodeCLI
        from src.backends.claude.client import ClaudeCodeCLI

        assert cls is ClaudeCodeCLI

    def test_getattr_claude_auth_provider(self):
        """Accessing ClaudeAuthProvider lazily imports from auth module."""
        import src.backends.claude as claude_pkg

        cls = claude_pkg.ClaudeAuthProvider
        from src.backends.claude.auth import ClaudeAuthProvider

        assert cls is ClaudeAuthProvider

    def test_getattr_unknown_raises_attribute_error(self):
        """Accessing an unknown attribute raises AttributeError."""
        import src.backends.claude as claude_pkg

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = claude_pkg.NonExistentAttribute


# ===========================================================================
# src/backends/claude/__init__.py — register() exception handling
# ===========================================================================


class TestClaudeRegister:
    """Test register() in src/backends/claude/__init__.py."""

    def test_register_happy_path(self, tmp_path):
        """Successful registration creates client and descriptor."""
        with (
            patch(
                "src.auth.validate_claude_code_auth",
                return_value=(True, {"method": "claude_cli"}),
            ),
            patch("src.auth.auth_manager") as mock_auth,
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends.claude import register

            register(cwd=str(tmp_path), timeout=5000)

            assert "claude" in BackendRegistry.all_descriptors()
            assert BackendRegistry.is_registered("claude")

    def test_register_client_creation_failure_propagates(self):
        """When ClaudeCodeCLI() raises, register() re-raises after logging."""
        from src.backends.claude import register

        with patch(
            "src.backends.claude.client.ClaudeCodeCLI",
            side_effect=RuntimeError("auth failure"),
        ):
            with pytest.raises(RuntimeError, match="auth failure"):
                register(cwd="/tmp", timeout=1000)

        # Descriptor should still be registered even though client creation failed
        assert "claude" in BackendRegistry.all_descriptors()
        assert not BackendRegistry.is_registered("claude")

    def test_register_client_failure_logs_error(self, caplog):
        """Client creation failure is logged as an error."""
        from src.backends.claude import register

        with (
            patch(
                "src.backends.claude.client.ClaudeCodeCLI",
                side_effect=RuntimeError("sdk init error"),
            ),
            caplog.at_level("ERROR", logger="src.backends.claude"),
        ):
            with pytest.raises(RuntimeError):
                register(cwd="/tmp", timeout=1000)

        assert any("Claude backend client creation failed" in r.message for r in caplog.records)

    def test_register_uses_default_registry_cls(self, tmp_path):
        """When registry_cls is None, defaults to BackendRegistry."""
        with (
            patch(
                "src.auth.validate_claude_code_auth",
                return_value=(True, {"method": "claude_cli"}),
            ),
            patch("src.auth.auth_manager") as mock_auth,
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends.claude import register

            register(registry_cls=None, cwd=str(tmp_path))

            assert BackendRegistry.is_registered("claude")

    def test_register_uses_env_cwd_when_none(self, tmp_path):
        """When cwd is None, register() falls back to CLAUDE_CWD env var."""
        with (
            patch(
                "src.auth.validate_claude_code_auth",
                return_value=(True, {"method": "claude_cli"}),
            ),
            patch("src.auth.auth_manager") as mock_auth,
            patch.dict("os.environ", {"CLAUDE_CWD": str(tmp_path)}),
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends.claude import register

            register(cwd=None)

            assert BackendRegistry.is_registered("claude")


