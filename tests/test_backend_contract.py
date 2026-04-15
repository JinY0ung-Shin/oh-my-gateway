"""Backend contract tests — parametrized tests that every backend must pass.

Ensures all backends conform to the BackendClient protocol and that
descriptors work correctly.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.backends.base import (
    BackendRegistry,
)
from src.backends.claude import CLAUDE_DESCRIPTOR, ClaudeCodeCLI


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


# ---------------------------------------------------------------------------
# Descriptor tests
# ---------------------------------------------------------------------------


class TestDescriptors:
    """Test that descriptors are correctly defined."""

    def test_claude_descriptor_fields(self):
        assert CLAUDE_DESCRIPTOR.name == "claude"
        assert CLAUDE_DESCRIPTOR.owned_by == "anthropic"
        assert len(CLAUDE_DESCRIPTOR.models) > 0
        assert CLAUDE_DESCRIPTOR.resolve_fn is not None

    def test_claude_descriptor_resolves_known_models(self):
        for model in CLAUDE_DESCRIPTOR.models:
            resolved = CLAUDE_DESCRIPTOR.resolve_fn(model)
            assert resolved is not None
            assert resolved.backend == "claude"

    def test_claude_descriptor_slash_syntax(self):
        resolved = CLAUDE_DESCRIPTOR.resolve_fn("claude/opus")
        assert resolved is not None
        assert resolved.backend == "claude"
        assert resolved.provider_model == "opus"

    def test_descriptor_returns_none_for_unknown(self):
        assert CLAUDE_DESCRIPTOR.resolve_fn("unknown-model") is None


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
        ids = BackendRegistry.all_model_ids()
        assert "opus" in ids
        assert "sonnet" in ids

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

    def test_claude_supported_models(self, mock_claude_cli):
        models = mock_claude_cli.supported_models()
        assert isinstance(models, list)
        assert "opus" in models
        assert "sonnet" in models


# ---------------------------------------------------------------------------
# get_auth_provider tests
# ---------------------------------------------------------------------------


class TestGetAuthProvider:
    """Test that backends return proper auth providers."""

    def test_claude_auth_provider(self, mock_claude_cli):
        provider = mock_claude_cli.get_auth_provider()
        assert provider.name == "claude"


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
        assert hasattr(mod, "ALL_MODELS")

    def test_import_claude_cli_shim(self):
        """src.claude_cli shim must provide ClaudeCodeCLI."""
        import importlib

        mod = importlib.import_module("src.claude_cli")
        assert hasattr(mod, "ClaudeCodeCLI")

    def test_import_backend_registry_shim(self):
        """src.backend_registry shim must provide BackendClient and BackendRegistry."""
        import importlib

        mod = importlib.import_module("src.backend_registry")
        assert hasattr(mod, "BackendClient")
        assert hasattr(mod, "BackendRegistry")
        assert hasattr(mod, "resolve_model")

    def test_import_auth_providers_from_auth(self):
        """src.auth must re-export ClaudeAuthProvider."""
        import importlib

        mod = importlib.import_module("src.auth")
        assert hasattr(mod, "ClaudeAuthProvider")

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
            "import src.backend_registry",
            "import src.auth",
            "from src.constants import CLAUDE_MODELS, ALL_MODELS",
            "from src.claude_cli import ClaudeCodeCLI",
            "from src.backend_registry import BackendClient, BackendRegistry, resolve_model",
            "from src.auth import ClaudeAuthProvider",
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
