"""Tests for Claude backend bash sandbox configuration.

Covers tri-state env parsing, strict boolean validation, default security values,
and SDK SandboxSettings object assembly.
"""

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# constants.py — env variable parsing
# ---------------------------------------------------------------------------


class TestSandboxEnvParsing:
    """Test CLAUDE_SANDBOX_ENABLED tri-state parsing in constants.py."""

    def _reload_constants(self):
        """Reload constants module to pick up patched env vars."""
        import importlib
        import src.backends.claude.constants as mod

        importlib.reload(mod)
        return mod

    def test_unset_returns_none(self):
        """Unset CLAUDE_SANDBOX_ENABLED → None (respect project settings)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SANDBOX_ENABLED", None)
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is None

    def test_true_returns_true(self):
        """CLAUDE_SANDBOX_ENABLED=true → True."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "true"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is True

    def test_yes_returns_true(self):
        """CLAUDE_SANDBOX_ENABLED=yes → True."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "yes"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is True

    def test_one_returns_true(self):
        """CLAUDE_SANDBOX_ENABLED=1 → True."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "1"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is True

    def test_false_returns_false(self):
        """CLAUDE_SANDBOX_ENABLED=false → False."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "false"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is False

    def test_no_returns_false(self):
        """CLAUDE_SANDBOX_ENABLED=no → False."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "no"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is False

    def test_zero_returns_false(self):
        """CLAUDE_SANDBOX_ENABLED=0 → False."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "0"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is False

    def test_invalid_value_warns_and_returns_none(self):
        """CLAUDE_SANDBOX_ENABLED=foo → logs warning, returns None."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "foo"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is None

    def test_case_insensitive(self):
        """CLAUDE_SANDBOX_ENABLED=TRUE → True (case-insensitive)."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ENABLED": "TRUE"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ENABLED is True


class TestSandboxSecurityDefaults:
    """Verify strict security defaults for sandbox settings."""

    def _reload_constants(self):
        import importlib
        import src.backends.claude.constants as mod

        importlib.reload(mod)
        return mod

    def test_allow_unsandboxed_defaults_false(self):
        """allowUnsandboxedCommands defaults to false (strict)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", None)
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED is False

    def test_excluded_commands_defaults_empty(self):
        """excludedCommands defaults to empty list (strict)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SANDBOX_EXCLUDED_COMMANDS", None)
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_EXCLUDED_COMMANDS == []

    def test_network_allow_local_defaults_false(self):
        """networkAllowLocalBinding defaults to false."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", None)
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL is False

    def test_weaker_nested_defaults_false(self):
        """enableWeakerNestedSandbox defaults to false (explicit opt-in only)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SANDBOX_WEAKER_NESTED", None)
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_WEAKER_NESTED is False

    def test_auto_allow_bash_defaults_true(self):
        """autoAllowBashIfSandboxed defaults to true."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_SANDBOX_AUTO_ALLOW_BASH", None)
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_AUTO_ALLOW_BASH is True


class TestExcludedCommandsParsing:
    """Test CLAUDE_SANDBOX_EXCLUDED_COMMANDS list parsing."""

    def _reload_constants(self):
        import importlib
        import src.backends.claude.constants as mod

        importlib.reload(mod)
        return mod

    def test_empty_string(self):
        """Empty string → empty list."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_EXCLUDED_COMMANDS": ""}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_EXCLUDED_COMMANDS == []

    def test_single_command(self):
        """Single command → single-element list."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_EXCLUDED_COMMANDS": "git"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_EXCLUDED_COMMANDS == ["git"]

    def test_multiple_commands(self):
        """Comma-separated commands → list."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_EXCLUDED_COMMANDS": "git,docker,npm"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_EXCLUDED_COMMANDS == ["git", "docker", "npm"]

    def test_whitespace_trimmed(self):
        """Whitespace around commands is trimmed."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_EXCLUDED_COMMANDS": " git , docker "}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_EXCLUDED_COMMANDS == ["git", "docker"]

    def test_trailing_comma_ignored(self):
        """Trailing comma does not produce empty element."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_EXCLUDED_COMMANDS": "git,"}):
            mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_EXCLUDED_COMMANDS == ["git"]


# ---------------------------------------------------------------------------
# client.py — _configure_sandbox() and SDK object assembly
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_instance():
    """Create a CLI instance with mocked auth."""
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


class TestConfigureSandboxTriState:
    """Test _configure_sandbox() tri-state behavior."""

    def test_none_does_not_set_sandbox(self, cli_instance):
        """CLAUDE_SANDBOX_ENABLED=None → options.sandbox untouched."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", None):
            cli_instance._configure_sandbox(opts)
        assert getattr(opts, "sandbox", None) is None

    def test_true_sets_sandbox_enabled(self, cli_instance):
        """CLAUDE_SANDBOX_ENABLED=True → options.sandbox.enabled=True."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is True

    def test_false_sets_sandbox_disabled(self, cli_instance):
        """CLAUDE_SANDBOX_ENABLED=False → options.sandbox={enabled: False}."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", False):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is False


class TestConfigureSandboxParams:
    """Test _configure_sandbox() parameter forwarding."""

    def _configure_with(self, cli_instance, **overrides):
        """Helper to call _configure_sandbox with patched constants."""
        from claude_agent_sdk import ClaudeAgentOptions

        defaults = {
            "CLAUDE_SANDBOX_ENABLED": True,
            "CLAUDE_SANDBOX_AUTO_ALLOW_BASH": True,
            "CLAUDE_SANDBOX_EXCLUDED_COMMANDS": [],
            "CLAUDE_SANDBOX_ALLOW_UNSANDBOXED": False,
            "CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL": False,
            "CLAUDE_SANDBOX_WEAKER_NESTED": False,
        }
        defaults.update(overrides)

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        patches = {f"src.backends.claude.client.{k}": v for k, v in defaults.items()}
        with patch.multiple("", **{}) if not patches else _multi_patch(patches):
            cli_instance._configure_sandbox(opts)
        return opts

    def test_auto_allow_bash_forwarded(self, cli_instance):
        """autoAllowBashIfSandboxed is forwarded."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox["autoAllowBashIfSandboxed"] is False

    def test_excluded_commands_forwarded(self, cli_instance):
        """excludedCommands list is forwarded."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", ["git", "docker"]), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox["excludedCommands"] == ["git", "docker"]

    def test_allow_unsandboxed_forwarded(self, cli_instance):
        """allowUnsandboxedCommands is forwarded."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox["allowUnsandboxedCommands"] is True

    def test_network_allow_local_forwarded(self, cli_instance):
        """network.allowLocalBinding is forwarded."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox["network"]["allowLocalBinding"] is True

    def test_weaker_nested_forwarded(self, cli_instance):
        """enableWeakerNestedSandbox is forwarded."""
        from claude_agent_sdk import ClaudeAgentOptions

        opts = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", True):
            cli_instance._configure_sandbox(opts)

        assert opts.sandbox["enableWeakerNestedSandbox"] is True


class TestBuildSdkOptionsSandboxIntegration:
    """Test that _build_sdk_options() integrates sandbox correctly."""

    def test_sandbox_included_when_enabled(self, cli_instance):
        """_build_sdk_options includes sandbox when CLAUDE_SANDBOX_ENABLED=True."""
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", []), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            opts = cli_instance._build_sdk_options()

        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is True

    def test_sandbox_not_set_when_unset(self, cli_instance):
        """_build_sdk_options does not set sandbox when env is unset."""
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", None):
            opts = cli_instance._build_sdk_options()

        assert getattr(opts, "sandbox", None) is None

    def test_sandbox_disabled_when_false(self, cli_instance):
        """_build_sdk_options sets sandbox.enabled=False when env is False."""
        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", False):
            opts = cli_instance._build_sdk_options()

        assert opts.sandbox is not None
        assert opts.sandbox["enabled"] is False


class TestExcludedCommandsCopyPerRequest:
    """Verify excludedCommands is copied per request to prevent mutation leaks."""

    def test_excluded_commands_is_independent_copy(self, cli_instance):
        """Each request gets its own copy of excludedCommands, not a shared reference."""
        from claude_agent_sdk import ClaudeAgentOptions

        source_list = ["git", "docker"]
        opts1 = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)
        opts2 = ClaudeAgentOptions(max_turns=1, cwd=cli_instance.cwd)

        with patch("src.backends.claude.client.CLAUDE_SANDBOX_ENABLED", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_AUTO_ALLOW_BASH", True), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_EXCLUDED_COMMANDS", source_list), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL", False), \
             patch("src.backends.claude.client.CLAUDE_SANDBOX_WEAKER_NESTED", False):
            cli_instance._configure_sandbox(opts1)
            cli_instance._configure_sandbox(opts2)

        # Mutate one request's list
        opts1.sandbox["excludedCommands"].append("npm")

        # Other request and source should be unaffected
        assert "npm" not in opts2.sandbox["excludedCommands"]
        assert "npm" not in source_list


class TestStrictBooleanParsingForAllEnvVars:
    """Verify strict boolean parsing warns on invalid values for all sandbox booleans."""

    def _reload_constants(self):
        import importlib
        import src.backends.claude.constants as mod

        importlib.reload(mod)
        return mod

    def test_auto_allow_bash_invalid_warns(self, caplog):
        """Invalid CLAUDE_SANDBOX_AUTO_ALLOW_BASH warns and uses default (true)."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_AUTO_ALLOW_BASH": "ture"}):
            with caplog.at_level("WARNING", logger="src.backends.claude.constants"):
                mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_AUTO_ALLOW_BASH is True
            assert "CLAUDE_SANDBOX_AUTO_ALLOW_BASH" in caplog.text

    def test_allow_unsandboxed_invalid_warns(self, caplog):
        """Invalid CLAUDE_SANDBOX_ALLOW_UNSANDBOXED warns and uses default (false)."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_ALLOW_UNSANDBOXED": "yse"}):
            with caplog.at_level("WARNING", logger="src.backends.claude.constants"):
                mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_ALLOW_UNSANDBOXED is False
            assert "CLAUDE_SANDBOX_ALLOW_UNSANDBOXED" in caplog.text

    def test_network_allow_local_invalid_warns(self, caplog):
        """Invalid CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL warns and uses default (false)."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL": "xyz"}):
            with caplog.at_level("WARNING", logger="src.backends.claude.constants"):
                mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL is False
            assert "CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL" in caplog.text

    def test_weaker_nested_invalid_warns(self, caplog):
        """Invalid CLAUDE_SANDBOX_WEAKER_NESTED warns and uses default (false)."""
        with patch.dict(os.environ, {"CLAUDE_SANDBOX_WEAKER_NESTED": "maybe"}):
            with caplog.at_level("WARNING", logger="src.backends.claude.constants"):
                mod = self._reload_constants()
            assert mod.CLAUDE_SANDBOX_WEAKER_NESTED is False
            assert "CLAUDE_SANDBOX_WEAKER_NESTED" in caplog.text


# ---------------------------------------------------------------------------
# Helper to avoid deeply nested patch context managers
# ---------------------------------------------------------------------------


@contextmanager
def _multi_patch(patches: dict):
    """Apply multiple patches from a {target: value} dict."""
    import unittest.mock

    stack = []
    try:
        for target, value in patches.items():
            p = unittest.mock.patch(target, value)
            p.start()
            stack.append(p)
        yield
    finally:
        for p in reversed(stack):
            p.stop()
