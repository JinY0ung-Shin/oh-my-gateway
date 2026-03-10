"""Coverage tests for backend __init__.py modules.

Targets uncovered lines in:
- src/backends/__init__.py (lines 28-45: Codex registration failure handling)
- src/backends/claude/__init__.py (lines 46-50: __getattr__ lazy imports;
  lines 59-79: register() exception handling)
- src/backends/codex/__init__.py (lines 48-70: __getattr__ lazy imports;
  lines 79-98: register() exception handling)
"""

import pytest
from unittest.mock import patch

from src.backends.base import BackendRegistry


# ---------------------------------------------------------------------------
# Fixture: clean registry for every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure a clean BackendRegistry before and after each test."""
    BackendRegistry.clear()
    yield
    BackendRegistry.clear()


# ===========================================================================
# src/backends/__init__.py — discover_backends
# ===========================================================================


class TestDiscoverBackends:
    """Test discover_backends() in src/backends/__init__.py."""

    def test_discover_backends_registers_claude_and_codex(self, tmp_path):
        """Happy path: both backends register successfully."""
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth, patch.dict(
            "os.environ", {"CODEX_CLI_PATH": str(fake_bin), "CLAUDE_CWD": str(tmp_path)}
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends import discover_backends

            discover_backends()

            assert BackendRegistry.is_registered("claude")
            assert BackendRegistry.is_registered("codex")

    def test_discover_backends_codex_import_failure(self):
        """When Codex register() raises, Claude still registers and no crash."""
        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth, patch.dict(
            "os.environ", {"CLAUDE_CWD": "/tmp"}
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends import discover_backends

            # Patch Codex register to raise
            with patch(
                "src.backends.codex.register",
                side_effect=RuntimeError("codex binary not found"),
            ):
                discover_backends()

            assert BackendRegistry.is_registered("claude")
            assert not BackendRegistry.is_registered("codex")

    def test_discover_backends_codex_import_failure_logs_warning(self, caplog):
        """Codex failure is logged as a warning."""
        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth, patch.dict(
            "os.environ", {"CLAUDE_CWD": "/tmp"}
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends import discover_backends

            with patch(
                "src.backends.codex.register",
                side_effect=RuntimeError("no codex binary"),
            ), caplog.at_level("WARNING", logger="src.backends"):
                discover_backends()

            assert any("Codex backend not available" in r.message for r in caplog.records)

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

        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth, patch.dict(
            "os.environ", {"CLAUDE_CWD": "/tmp"}
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends import discover_backends

            # Codex will likely fail but that's fine — we're testing registry_cls plumbing
            with patch(
                "src.backends.codex.register",
                side_effect=RuntimeError("skip"),
            ):
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
        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth:
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

        with patch(
            "src.backends.claude.client.ClaudeCodeCLI",
            side_effect=RuntimeError("sdk init error"),
        ), caplog.at_level("ERROR", logger="src.backends.claude"):
            with pytest.raises(RuntimeError):
                register(cwd="/tmp", timeout=1000)

        assert any(
            "Claude backend client creation failed" in r.message for r in caplog.records
        )

    def test_register_uses_default_registry_cls(self, tmp_path):
        """When registry_cls is None, defaults to BackendRegistry."""
        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth:
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends.claude import register

            register(registry_cls=None, cwd=str(tmp_path))

            assert BackendRegistry.is_registered("claude")

    def test_register_uses_env_cwd_when_none(self, tmp_path):
        """When cwd is None, register() falls back to CLAUDE_CWD env var."""
        with patch(
            "src.auth.validate_claude_code_auth",
            return_value=(True, {"method": "claude_cli"}),
        ), patch("src.auth.auth_manager") as mock_auth, patch.dict(
            "os.environ", {"CLAUDE_CWD": str(tmp_path)}
        ):
            mock_auth.get_claude_code_env_vars.return_value = {}

            from src.backends.claude import register

            register(cwd=None)

            assert BackendRegistry.is_registered("claude")


# ===========================================================================
# src/backends/codex/__init__.py — __getattr__ lazy imports
# ===========================================================================


class TestCodexGetattr:
    """Test lazy attribute access on the codex subpackage."""

    def test_getattr_codex_cli(self):
        """Accessing CodexCLI lazily imports from client module."""
        import src.backends.codex as codex_pkg

        cls = codex_pkg.CodexCLI
        from src.backends.codex.client import CodexCLI

        assert cls is CodexCLI

    def test_getattr_codex_auth_provider(self):
        """Accessing CodexAuthProvider lazily imports from auth module."""
        import src.backends.codex as codex_pkg

        cls = codex_pkg.CodexAuthProvider
        from src.backends.codex.auth import CodexAuthProvider

        assert cls is CodexAuthProvider

    def test_getattr_normalize_codex_event(self):
        """Accessing normalize_codex_event lazily imports from client module."""
        import src.backends.codex as codex_pkg

        fn = codex_pkg.normalize_codex_event
        from src.backends.codex.client import normalize_codex_event

        assert fn is normalize_codex_event

    def test_getattr_private_helpers(self):
        """Accessing private helper names lazily imports from client module."""
        import src.backends.codex as codex_pkg
        import src.backends.codex.client as codex_client

        helpers = [
            "_extract_text_and_collab",
            "_strip_collab_json",
            "_collab_to_tool_blocks",
            "_build_content_blocks",
            "_normalize_usage",
        ]
        for name in helpers:
            result = getattr(codex_pkg, name)
            expected = getattr(codex_client, name)
            assert result is expected, f"Lazy import mismatch for {name}"

    def test_getattr_unknown_raises_attribute_error(self):
        """Accessing an unknown attribute raises AttributeError."""
        import src.backends.codex as codex_pkg

        with pytest.raises(AttributeError, match="has no attribute"):
            _ = codex_pkg.TotallyFakeAttribute


# ===========================================================================
# src/backends/codex/__init__.py — register() exception handling
# ===========================================================================


class TestCodexRegister:
    """Test register() in src/backends/codex/__init__.py."""

    def test_register_happy_path(self, tmp_path):
        """Successful registration when codex binary exists."""
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        with patch.dict("os.environ", {"CODEX_CLI_PATH": str(fake_bin)}):
            from src.backends.codex import register

            register(cwd=str(tmp_path))

            assert "codex" in BackendRegistry.all_descriptors()
            assert BackendRegistry.is_registered("codex")

    def test_register_binary_missing_graceful(self):
        """When CodexCLI() raises (binary missing), register() logs warning and continues."""
        from src.backends.codex import register

        with patch(
            "src.backends.codex.client.CodexCLI",
            side_effect=FileNotFoundError("codex binary not found"),
        ):
            # Should NOT raise
            register(cwd="/tmp", timeout=1000)

        # Descriptor should still be registered
        assert "codex" in BackendRegistry.all_descriptors()
        # But no client
        assert not BackendRegistry.is_registered("codex")

    def test_register_binary_missing_logs_warnings(self, caplog):
        """Binary-missing failure is logged with both warning messages."""
        from src.backends.codex import register

        with patch(
            "src.backends.codex.client.CodexCLI",
            side_effect=FileNotFoundError("binary not found"),
        ), caplog.at_level("WARNING", logger="src.backends.codex"):
            register(cwd="/tmp", timeout=1000)

        messages = [r.message for r in caplog.records]
        assert any("Codex backend registration failed" in m for m in messages)
        assert any("Codex models will not be available" in m for m in messages)

    def test_register_generic_exception_graceful(self):
        """Any exception from CodexCLI() is handled gracefully."""
        from src.backends.codex import register

        with patch(
            "src.backends.codex.client.CodexCLI",
            side_effect=RuntimeError("unexpected error"),
        ):
            # Should NOT raise
            register(cwd="/tmp", timeout=5000)

        assert "codex" in BackendRegistry.all_descriptors()
        assert not BackendRegistry.is_registered("codex")

    def test_register_uses_default_registry_cls(self, tmp_path):
        """When registry_cls is None, defaults to BackendRegistry."""
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        with patch.dict("os.environ", {"CODEX_CLI_PATH": str(fake_bin)}):
            from src.backends.codex import register

            register(registry_cls=None, cwd=str(tmp_path))

            assert BackendRegistry.is_registered("codex")

    def test_register_uses_env_cwd_when_none(self, tmp_path):
        """When cwd is None, register() falls back to CLAUDE_CWD env var."""
        fake_bin = tmp_path / "codex"
        fake_bin.write_text("#!/bin/sh\necho ok")
        fake_bin.chmod(0o755)

        with patch.dict(
            "os.environ", {"CODEX_CLI_PATH": str(fake_bin), "CLAUDE_CWD": str(tmp_path)}
        ):
            from src.backends.codex import register

            register(cwd=None)

            assert BackendRegistry.is_registered("codex")
