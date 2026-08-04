"""Exposing Claude models under their ANTHROPIC_DEFAULT_*_MODEL names.

When an ``ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`` override is set, the
gateway advertises that concrete name as a public model id (in addition to the
bare ``opus``/``sonnet``/``haiku`` aliases) and resolves it back to the bare
alias so the Claude CLI performs the real alias->model resolution.
"""

import pytest

from src.backends.claude import _claude_model_meta, _claude_resolve, CLAUDE_DESCRIPTOR
from src.backends.claude.constants import (
    CLAUDE_MODELS,
    configured_model_aliases,
    configured_public_models,
)

_ALIAS_ENV_VARS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


@pytest.fixture
def no_alias_env(monkeypatch):
    """Ensure none of the override env vars are set (isolates from the shell)."""
    for name in _ALIAS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestConfiguredModelAliases:
    def test_empty_when_no_overrides(self, no_alias_env):
        assert configured_model_aliases() == {}

    def test_single_override_maps_name_to_alias(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-5-20250929")
        assert configured_model_aliases() == {"claude-sonnet-4-5-20250929": "sonnet"}

    def test_all_three_overrides(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus-real")
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "sonnet-real")
        no_alias_env.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku-real")
        assert configured_model_aliases() == {
            "opus-real": "opus",
            "sonnet-real": "sonnet",
            "haiku-real": "haiku",
        }

    def test_value_equal_to_bare_alias_is_skipped(self, no_alias_env):
        # A no-op override (name == alias) must not create a self-referential entry.
        no_alias_env.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus")
        assert configured_model_aliases() == {}

    def test_blank_value_is_ignored(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "   ")
        assert configured_model_aliases() == {}

    def test_custom_upstream_alias_value(self, no_alias_env):
        # UniBridge/LiteLLM-style custom alias, not a concrete Anthropic id.
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "gpt-5.5")
        assert configured_model_aliases() == {"gpt-5.5": "sonnet"}


class TestConfiguredPublicModels:
    def test_default_surface_unchanged(self, no_alias_env):
        assert configured_public_models() == ["opus", "sonnet", "haiku"]

    def test_appends_configured_names_after_aliases(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-5-20250929")
        models = configured_public_models()
        # Bare aliases remain first and intact...
        assert models[:3] == ["opus", "sonnet", "haiku"]
        # ...with the configured name appended.
        assert "claude-sonnet-4-5-20250929" in models
        assert len(models) == 4

    def test_no_duplicate_when_value_equals_alias(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku")
        assert configured_public_models() == ["opus", "sonnet", "haiku"]

    def test_bare_aliases_always_present(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "opus-real")
        assert set(CLAUDE_MODELS) <= set(configured_public_models())


class TestClaudeResolve:
    def test_bare_alias_still_resolves(self, no_alias_env):
        resolved = _claude_resolve("sonnet")
        assert resolved is not None
        assert resolved.backend == "claude"
        assert resolved.public_model == "sonnet"
        assert resolved.provider_model == "sonnet"

    def test_claude_prefix_still_passes_through(self, no_alias_env):
        resolved = _claude_resolve("claude/some-model")
        assert resolved is not None
        assert resolved.provider_model == "some-model"

    def test_unknown_model_returns_none(self, no_alias_env):
        assert _claude_resolve("gpt-4o") is None

    def test_configured_name_resolves_back_to_alias(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-5-20250929")
        resolved = _claude_resolve("claude-sonnet-4-5-20250929")
        assert resolved is not None
        assert resolved.backend == "claude"
        # Public model echoes back the requested name...
        assert resolved.public_model == "claude-sonnet-4-5-20250929"
        # ...while the CLI receives the bare alias for real resolution.
        assert resolved.provider_model == "sonnet"

    def test_configured_custom_alias_resolves(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "gpt-5.5")
        resolved = _claude_resolve("gpt-5.5")
        assert resolved is not None
        assert resolved.provider_model == "sonnet"

    def test_configured_name_with_slash_not_swallowed(self, no_alias_env):
        # A slash-containing override (e.g. a provider-prefixed id) must resolve
        # via the alias path, not be intercepted by the claude/<sub-model> rule.
        no_alias_env.setenv(
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "bedrock/anthropic.claude-sonnet-4-5"
        )
        resolved = _claude_resolve("bedrock/anthropic.claude-sonnet-4-5")
        assert resolved is not None
        assert resolved.backend == "claude"
        assert resolved.provider_model == "sonnet"

    def test_non_claude_slash_still_returns_none(self, no_alias_env):
        # Unrelated provider-prefixed ids remain unclaimed by Claude.
        assert _claude_resolve("codex/gpt-5.5") is None

    def test_wired_descriptor_uses_live_env(self, no_alias_env):
        # The real registered descriptor's resolve_fn reads the env on each call.
        no_alias_env.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4-5-20250929")
        resolved = CLAUDE_DESCRIPTOR.resolve_fn("claude-opus-4-5-20250929")
        assert resolved is not None
        assert resolved.provider_model == "opus"


class TestModelMeta:
    """``/v1/models`` alias bookkeeping — clients offer the configured names."""

    def test_bare_alias_is_marked_even_without_overrides(self, no_alias_env):
        # Clients that only want real model names filter on ``alias``.
        assert _claude_model_meta("sonnet") == {"alias": True}
        assert _claude_model_meta("claude/some-model") == {}

    def test_configured_name_declares_its_alias(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-5-20250929")
        assert _claude_model_meta("claude-sonnet-4-5-20250929") == {"alias_of": "sonnet"}

    def test_superseded_bare_alias_points_at_configured_name(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-5-20250929")
        assert _claude_model_meta("sonnet") == {
            "alias": True,
            "configured_as": "claude-sonnet-4-5-20250929",
        }
        # Aliases without an override are still marked as aliases.
        assert _claude_model_meta("opus") == {"alias": True}

    def test_descriptor_is_wired(self, no_alias_env):
        no_alias_env.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "haiku-real")
        assert CLAUDE_DESCRIPTOR.model_meta_fn is not None
        assert CLAUDE_DESCRIPTOR.model_meta_fn("haiku") == {
            "alias": True,
            "configured_as": "haiku-real",
        }
