"""Backend contract tests — parametrized tests that every backend must pass.

Ensures all backends conform to the BackendClient protocol and that
descriptors, resolve, and build_options work correctly.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.backends.base import (
    BackendConfigError,
    BackendRegistry,
    ResolvedModel,
)
from src.backends.claude import CLAUDE_DESCRIPTOR, ClaudeCodeCLI
from src.backends.codex import CODEX_DESCRIPTOR


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure a clean BackendRegistry for each test."""
    BackendRegistry.clear()
    yield
    BackendRegistry.clear()


@pytest.fixture
def mock_claude_cli():
    """Create a ClaudeCodeCLI with mocked SDK dependencies."""
    with patch(
        "src.auth.validate_claude_code_auth",
        return_value=(True, {"method": "claude_cli"}),
    ):
        with patch("src.auth.auth_manager") as mock_auth:
            mock_auth.get_claude_code_env_vars.return_value = {}
            import tempfile

            with tempfile.TemporaryDirectory() as tmpdir:
                cli = ClaudeCodeCLI(timeout=1000, cwd=tmpdir)
                yield cli


@pytest.fixture
def mock_codex_cli(tmp_path):
    """Create a CodexCLI with a mocked binary."""
    # Create a fake binary
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\necho test")
    fake_bin.chmod(0o755)

    with patch.dict("os.environ", {"CODEX_CLI_PATH": str(fake_bin)}):
        # Reimport to pick up patched env
        from src.backends.codex.client import CodexCLI as _CodexCLI

        cli = _CodexCLI(timeout=1000, cwd=str(tmp_path))
        yield cli


# ---------------------------------------------------------------------------
# Descriptor tests
# ---------------------------------------------------------------------------


class TestDescriptors:
    """Test that descriptors are correctly defined."""

    @pytest.mark.parametrize(
        "descriptor,expected_name,expected_owned_by",
        [
            (CLAUDE_DESCRIPTOR, "claude", "anthropic"),
            (CODEX_DESCRIPTOR, "codex", "openai"),
        ],
    )
    def test_descriptor_fields(self, descriptor, expected_name, expected_owned_by):
        assert descriptor.name == expected_name
        assert descriptor.owned_by == expected_owned_by
        assert len(descriptor.models) > 0
        assert descriptor.resolve_fn is not None

    def test_claude_descriptor_resolves_known_models(self):
        for model in CLAUDE_DESCRIPTOR.models:
            resolved = CLAUDE_DESCRIPTOR.resolve_fn(model)
            assert resolved is not None
            assert resolved.backend == "claude"

    def test_codex_descriptor_resolves_known_models(self):
        for model in CODEX_DESCRIPTOR.models:
            resolved = CODEX_DESCRIPTOR.resolve_fn(model)
            assert resolved is not None
            assert resolved.backend == "codex"

    def test_claude_descriptor_slash_syntax(self):
        resolved = CLAUDE_DESCRIPTOR.resolve_fn("claude/opus")
        assert resolved is not None
        assert resolved.backend == "claude"
        assert resolved.provider_model == "opus"

    def test_codex_descriptor_slash_syntax(self):
        resolved = CODEX_DESCRIPTOR.resolve_fn("codex/o3")
        assert resolved is not None
        assert resolved.backend == "codex"
        assert resolved.provider_model == "o3"

    def test_descriptor_returns_none_for_unknown(self):
        assert CLAUDE_DESCRIPTOR.resolve_fn("codex") is None
        assert CODEX_DESCRIPTOR.resolve_fn("opus") is None


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistryDescriptors:
    """Test registry descriptor management."""

    def test_register_descriptor(self):
        BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)
        assert "claude" in BackendRegistry.all_descriptors()

    def test_all_model_ids(self):
        BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)
        BackendRegistry.register_descriptor(CODEX_DESCRIPTOR)
        ids = BackendRegistry.all_model_ids()
        assert "opus" in ids
        assert "sonnet" in ids
        assert "codex" in ids

    def test_get_known_but_not_available(self):
        """A descriptor is registered but no live client — get() should raise with helpful message."""
        BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)
        with pytest.raises(ValueError, match="known but not available"):
            BackendRegistry.get("claude")

    def test_available_models_uses_descriptors(self):
        """available_models should use descriptor metadata."""
        BackendRegistry.register_descriptor(CLAUDE_DESCRIPTOR)
        # No client registered → no models in available list
        assert BackendRegistry.available_models() == []

        # Register a mock client
        mock_client = MagicMock()
        BackendRegistry.register("claude", mock_client)
        models = BackendRegistry.available_models()
        assert len(models) == len(CLAUDE_DESCRIPTOR.models)
        assert all(m["owned_by"] == "anthropic" for m in models)


# ---------------------------------------------------------------------------
# Protocol conformance tests
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Test that concrete backends satisfy the protocol."""

    def test_claude_has_name_property(self, mock_claude_cli):
        assert mock_claude_cli.name == "claude"

    def test_claude_has_owned_by_property(self, mock_claude_cli):
        assert mock_claude_cli.owned_by == "anthropic"

    def test_claude_supported_models(self, mock_claude_cli):
        models = mock_claude_cli.supported_models()
        assert isinstance(models, list)
        assert "opus" in models
        assert "sonnet" in models

    def test_claude_resolve_known_model(self, mock_claude_cli):
        resolved = mock_claude_cli.resolve("opus")
        assert resolved is not None
        assert resolved.backend == "claude"
        assert resolved.provider_model == "opus"

    def test_claude_resolve_unknown_returns_none(self, mock_claude_cli):
        assert mock_claude_cli.resolve("codex") is None

    def test_codex_has_name_property(self, mock_codex_cli):
        assert mock_codex_cli.name == "codex"

    def test_codex_has_owned_by_property(self, mock_codex_cli):
        assert mock_codex_cli.owned_by == "openai"

    def test_codex_supported_models(self, mock_codex_cli):
        models = mock_codex_cli.supported_models()
        assert isinstance(models, list)
        assert "codex" in models

    def test_codex_resolve_known_model(self, mock_codex_cli):
        resolved = mock_codex_cli.resolve("codex")
        assert resolved is not None
        assert resolved.backend == "codex"

    def test_codex_resolve_slash_model(self, mock_codex_cli):
        resolved = mock_codex_cli.resolve("codex/o3")
        assert resolved is not None
        assert resolved.backend == "codex"
        assert resolved.provider_model == "o3"

    def test_codex_resolve_unknown_returns_none(self, mock_codex_cli):
        assert mock_codex_cli.resolve("opus") is None


# ---------------------------------------------------------------------------
# build_options tests
# ---------------------------------------------------------------------------


class TestBuildOptions:
    """Test build_options on concrete backends."""

    def _make_request(self, enable_tools=True, model="opus"):
        """Create a minimal mock request."""
        req = MagicMock()
        req.enable_tools = enable_tools
        req.to_claude_options.return_value = {"max_turns": 10}
        req.model = model
        return req

    def test_claude_build_options_tools_enabled(self, mock_claude_cli):
        req = self._make_request(enable_tools=True)
        resolved = ResolvedModel(public_model="opus", backend="claude", provider_model="opus")
        options = mock_claude_cli.build_options(req, resolved)
        assert "allowed_tools" in options
        assert options["permission_mode"] == "bypassPermissions"

    def test_claude_build_options_tools_disabled(self, mock_claude_cli):
        req = self._make_request(enable_tools=False)
        resolved = ResolvedModel(public_model="opus", backend="claude", provider_model="opus")
        options = mock_claude_cli.build_options(req, resolved)
        assert "disallowed_tools" in options
        assert options["max_turns"] == 1

    def test_claude_build_options_with_overrides(self, mock_claude_cli):
        req = self._make_request(enable_tools=True)
        resolved = ResolvedModel(public_model="opus", backend="claude", provider_model="opus")
        options = mock_claude_cli.build_options(req, resolved, overrides={"max_turns": 5})
        assert options["max_turns"] == 5

    def test_claude_build_options_mcp_tools_added_when_enabled(self, mock_claude_cli):
        """MCP tool patterns are added to allowed_tools when tools are enabled."""
        servers = {"my-router": {"type": "stdio", "command": "echo"}}
        with patch("src.backends.claude.client.get_mcp_servers", return_value=servers):
            req = self._make_request(enable_tools=True)
            resolved = ResolvedModel(public_model="opus", backend="claude", provider_model="opus")
            options = mock_claude_cli.build_options(req, resolved)
            assert "mcp__my_router__*" in options["allowed_tools"]
            assert "mcp_servers" in options

    def test_claude_build_options_mcp_tools_not_added_when_disabled(self, mock_claude_cli):
        """MCP tool patterns are not added when tools are disabled."""
        servers = {"my-router": {"type": "stdio", "command": "echo"}}
        with patch("src.backends.claude.client.get_mcp_servers", return_value=servers):
            req = self._make_request(enable_tools=False)
            resolved = ResolvedModel(public_model="opus", backend="claude", provider_model="opus")
            options = mock_claude_cli.build_options(req, resolved)
            assert "allowed_tools" not in options
            assert "mcp_servers" in options

    def test_codex_build_options_tools_enabled(self, mock_codex_cli):
        req = self._make_request(enable_tools=True, model="codex")
        resolved = ResolvedModel(public_model="codex", backend="codex", provider_model="gpt-5.4")
        options = mock_codex_cli.build_options(req, resolved)
        assert options["permission_mode"] == "bypassPermissions"

    def test_codex_build_options_tools_disabled_raises(self, mock_codex_cli):
        req = self._make_request(enable_tools=False, model="codex")
        resolved = ResolvedModel(public_model="codex", backend="codex", provider_model="gpt-5.4")
        with pytest.raises(BackendConfigError, match="does not support disabling tools"):
            mock_codex_cli.build_options(req, resolved)


# ---------------------------------------------------------------------------
# get_auth_provider tests
# ---------------------------------------------------------------------------


class TestGetAuthProvider:
    """Test that backends return proper auth providers."""

    def test_claude_auth_provider(self, mock_claude_cli):
        provider = mock_claude_cli.get_auth_provider()
        assert provider.name == "claude"

    def test_codex_auth_provider(self, mock_codex_cli):
        provider = mock_codex_cli.get_auth_provider()
        assert provider.name == "codex"


# ---------------------------------------------------------------------------
# Clean-process import smoke tests
# ---------------------------------------------------------------------------


class TestCleanImports:
    """Verify that importing legacy shim modules does not trigger circular imports.

    These tests run in-process but import the modules in a controlled order
    to catch circular dependency regressions.  The real guarantee comes from
    ``uv run python -c "import src.constants"`` etc. in CI, but these
    provide fast feedback during normal test runs.
    """

    def test_import_constants(self):
        """src.constants must import cleanly (re-exports backend constants)."""
        import importlib

        mod = importlib.import_module("src.constants")
        assert hasattr(mod, "CLAUDE_MODELS")
        assert hasattr(mod, "CODEX_MODELS")
        assert hasattr(mod, "ALL_MODELS")

    def test_import_claude_cli_shim(self):
        """src.claude_cli shim must provide ClaudeCodeCLI."""
        import importlib

        mod = importlib.import_module("src.claude_cli")
        assert hasattr(mod, "ClaudeCodeCLI")

    def test_import_codex_cli_shim(self):
        """src.codex_cli shim must provide CodexCLI and helper functions."""
        import importlib

        mod = importlib.import_module("src.codex_cli")
        assert hasattr(mod, "CodexCLI")
        assert hasattr(mod, "normalize_codex_event")

    def test_import_backend_registry_shim(self):
        """src.backend_registry shim must provide BackendClient and BackendRegistry."""
        import importlib

        mod = importlib.import_module("src.backend_registry")
        assert hasattr(mod, "BackendClient")
        assert hasattr(mod, "BackendRegistry")
        assert hasattr(mod, "resolve_model")

    def test_import_auth_providers_from_auth(self):
        """src.auth must re-export ClaudeAuthProvider and CodexAuthProvider."""
        import importlib

        mod = importlib.import_module("src.auth")
        assert hasattr(mod, "ClaudeAuthProvider")
        assert hasattr(mod, "CodexAuthProvider")

    def test_claude_cli_shim_constants(self):
        """src.claude_cli shim must re-export Claude-specific constants."""
        import importlib

        mod = importlib.import_module("src.claude_cli")
        assert hasattr(mod, "THINKING_MODE")
        assert hasattr(mod, "TOKEN_STREAMING")

    def test_codex_cli_shim_constants(self):
        """src.codex_cli shim must re-export Codex-specific constants."""
        import importlib

        mod = importlib.import_module("src.codex_cli")
        assert hasattr(mod, "CODEX_CLI_PATH")
        assert hasattr(mod, "CODEX_TIMEOUT_MS")


# ---------------------------------------------------------------------------
# Subprocess-based clean-process import tests
# These spawn a fresh Python interpreter to catch circular imports that
# in-process tests miss (due to pytest's module pre-loading).
# ---------------------------------------------------------------------------


class TestCleanProcessImports:
    """Spawn a fresh interpreter for each import to guarantee no hidden state."""

    @pytest.mark.parametrize(
        "import_stmt",
        [
            "import src.constants",
            "import src.claude_cli",
            "import src.codex_cli",
            "import src.backend_registry",
            "import src.auth",
            "from src.constants import CLAUDE_MODELS, CODEX_MODELS, ALL_MODELS",
            "from src.claude_cli import ClaudeCodeCLI",
            "from src.claude_cli import THINKING_MODE",
            "from src.codex_cli import CodexCLI",
            "from src.codex_cli import CODEX_CLI_PATH",
            "from src.codex_cli import normalize_codex_event",
            "from src.backend_registry import BackendClient, BackendRegistry, resolve_model",
            "from src.auth import ClaudeAuthProvider, CodexAuthProvider",
            "from src.auth import BackendAuthProvider",
        ],
    )
    def test_clean_import(self, import_stmt):
        """Each import must succeed in a fresh interpreter (no circular imports)."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", import_stmt],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Import failed: {import_stmt}\nstderr: {result.stderr}"
