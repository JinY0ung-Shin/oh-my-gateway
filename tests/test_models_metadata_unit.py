"""Tests for /v1/models metadata expansion (backend + capabilities)."""

from unittest.mock import MagicMock

import pytest

from src.backend_registry import BackendDescriptor, BackendRegistry
from src.backends.claude import (
    CLAUDE_DESCRIPTOR,
    _claude_model_capabilities,
    _claude_model_entry_meta,
    claude_effort_levels,
)
from src.backends.claude.constants import (
    CLAUDE_MODELS,
    EFFORT_CAPABLE_MODELS,
    FIRST_PARTY_TIER_MODELS,
)
from src.backends.codex import CODEX_DESCRIPTOR
from src.backends.opencode import OPENCODE_DESCRIPTOR

from tests.test_main_api_unit import client_context


class TestDescriptorCapabilities:
    def test_all_backend_descriptors_declare_image_input(self):
        assert CLAUDE_DESCRIPTOR.capabilities == {
            "image_input": True,
            "reasoning_effort_accepted": True,
        }
        assert CODEX_DESCRIPTOR.capabilities == {"image_input": True}
        assert OPENCODE_DESCRIPTOR.capabilities == {"image_input": True}

    def test_only_claude_accepts_reasoning_effort(self):
        """``reasoning.effort`` is rejected for every non-claude backend by the
        responses route preflight, so only claude may advertise acceptance — a
        client that reads the flag must never be led into a 400."""
        assert CLAUDE_DESCRIPTOR.capabilities["reasoning_effort_accepted"] is True
        assert "reasoning_effort_accepted" not in CODEX_DESCRIPTOR.capabilities
        assert "reasoning_effort_accepted" not in OPENCODE_DESCRIPTOR.capabilities

    def test_no_descriptor_claims_reasoning_effort_is_applied(self):
        """The stronger claim is per model, never per backend.

        Whether the effort reaches the model depends on the upstream and on the
        id, so a descriptor-level ``True`` would advertise a guarantee for the
        arbitrary names configured via ``ANTHROPIC_DEFAULT_*_MODEL`` too.
        """
        for desc in (CLAUDE_DESCRIPTOR, CODEX_DESCRIPTOR, OPENCODE_DESCRIPTOR):
            assert "reasoning_effort" not in desc.capabilities

    def test_capabilities_default_to_empty_dict(self):
        desc = BackendDescriptor(
            name="bare",
            owned_by="test",
            models=["bare-model"],
            resolve_fn=lambda m: None,
        )
        assert desc.capabilities == {}


class TestAvailableModelsMetadata:
    def test_entries_include_backend_and_capabilities(self, clean_registry):
        BackendRegistry.register("claude", MagicMock())

        models = BackendRegistry.available_models()

        assert models
        for entry in models:
            # Existing fields stay untouched for compatibility
            assert entry["object"] == "model"
            assert entry["owned_by"] == "anthropic"
            assert isinstance(entry["id"], str)
            # New metadata fields
            assert entry["backend"] == "claude"
            assert entry["capabilities"]["image_input"] is True
            assert entry["capabilities"]["reasoning_effort_accepted"] is True
            # ``reasoning_effort`` is per model — pinned in
            # TestReasoningEffortIsGuaranteedPerModel below.
            assert isinstance(entry["capabilities"]["reasoning_effort"], bool)

    def test_image_input_defaults_false_for_capability_less_descriptor(
        self, clean_registry
    ):
        desc = BackendDescriptor(
            name="textonly",
            owned_by="test",
            models=["text-model"],
            resolve_fn=lambda m: None,
        )
        BackendRegistry.register_descriptor(desc)
        BackendRegistry.register("textonly", MagicMock())

        entries = [
            m for m in BackendRegistry.available_models() if m["id"] == "text-model"
        ]

        assert len(entries) == 1
        assert entries[0]["backend"] == "textonly"
        # Both always-present flags fail closed for a descriptor that declares
        # nothing: a client must be able to branch without a missing-key check.
        assert entries[0]["capabilities"] == {
            "image_input": False,
            "reasoning_effort": False,
            "reasoning_effort_accepted": False,
        }

    def test_unregistered_backend_models_stay_hidden(self, clean_registry):
        # Descriptors are registered by clean_registry, but no live clients —
        # the model list must remain empty.
        assert BackendRegistry.available_models() == []


def test_v1_models_endpoint_includes_new_fields():
    with client_context() as (client, _mock_cli):
        response = client.get("/v1/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"]
    for entry in payload["data"]:
        assert set(entry) >= {"id", "object", "owned_by", "backend", "capabilities"}
        assert isinstance(entry["capabilities"]["image_input"], bool)
        assert isinstance(entry["capabilities"]["reasoning_effort"], bool)
        assert isinstance(entry["capabilities"]["reasoning_effort_accepted"], bool)


_ALIAS_ENVS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


@pytest.fixture
def first_party_upstream(monkeypatch):
    """No custom upstream and no configured alias — the guaranteed baseline."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for name in _ALIAS_ENVS:
        monkeypatch.delenv(name, raising=False)


class TestReasoningEffortIsGuaranteedPerModel:
    """``reasoning_effort=true`` is a promise that the effort reaches the model.

    The CLI decides effort support from its own model registry *and* whether the
    base URL is first-party. With a custom ``ANTHROPIC_BASE_URL`` and an id it
    does not know, ``CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1`` makes it send effort
    anyway — and a 400 from the upstream makes it retry **without** effort. The
    turn then succeeds with the requested effort silently dropped, so a
    descriptor-level ``true`` would advertise a guarantee the gateway cannot
    keep for every id it lists (review blocker).
    """

    def test_bare_tier_aliases_whose_model_takes_an_effort_are_guaranteed(
        self, first_party_upstream
    ):
        for model in ("opus", "sonnet"):
            assert _claude_model_capabilities(model) == {"reasoning_effort": True}

    def test_a_tier_whose_model_has_no_effort_parameter_is_not_guaranteed(
        self, first_party_upstream
    ):
        """First-party, no override — and still false, because of the model.

        ``output_config.effort`` is per model, not per tier: bare ``haiku``
        resolves to Claude Haiku 4.5, which has no effort parameter and errors
        on one. A blanket true for every tier was a false positive on the most
        ordinary deployment there is (review blocker).
        """
        assert _claude_model_capabilities("haiku") == {"reasoning_effort": False}

    def test_every_tier_is_answered_by_its_resolved_concrete_model(self):
        """The guarantee is read off the model, so the tier table must cover it.

        A tier added to ``CLAUDE_MODELS`` without a row here silently fails
        closed; that is safe but invisible, and this keeps it visible.
        """
        for tier in CLAUDE_MODELS:
            resolved = FIRST_PARTY_TIER_MODELS.get(tier)
            assert resolved is not None, f"{tier} resolves to no concrete model"
            assert (
                resolved in EFFORT_CAPABLE_MODELS
            ), f"{resolved} has no effort-support row"

    def test_a_custom_upstream_withholds_the_guarantee_for_every_id(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.delenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", raising=False)
        for name in _ALIAS_ENVS:
            monkeypatch.delenv(name, raising=False)
        for model in ("opus", "sonnet", "haiku"):
            assert _claude_model_capabilities(model) == {
                "reasoning_effort": False
            }, "a custom upstream may answer 400 and make the CLI drop effort"

    def test_custom_upstream_can_certify_exact_effort_capable_models(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv(
            "CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS",
            "team-sonnet-v3, qwen-reasoner",
        )

        assert _claude_model_capabilities("team-sonnet-v3") == {
            "reasoning_effort": True
        }
        assert _claude_model_capabilities("qwen-reasoner") == {
            "reasoning_effort": True
        }
        assert _claude_model_capabilities("sonnet") == {"reasoning_effort": False}

    def test_custom_upstream_effort_wildcard_is_explicit_global_certification(
        self, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "*")

        for model in ("sonnet", "team-sonnet-v3", "vendor/reasoner"):
            assert _claude_model_capabilities(model) == {
                "reasoning_effort": True
            }

    def test_custom_upstream_certification_does_not_change_first_party_truth(
        self, first_party_upstream, monkeypatch
    ):
        monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "*")

        assert _claude_model_capabilities("haiku") == {"reasoning_effort": False}
        assert _claude_model_capabilities("sonnet") == {"reasoning_effort": True}

    def test_certification_can_declare_the_level_subset_the_upstream_accepts(
        self, monkeypatch
    ):
        """A served model's chat template decides the subset (litellm_serving#26:
        a Qwen3.x template takes low|medium|xhigh and 400s on high). The operator
        declares it per id and the entry advertises exactly that list."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv(
            "CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS",
            "qwen3.6-27b=xhigh|low|medium, glm-5-fp8",
        )
        assert _claude_model_capabilities("qwen3.6-27b") == {"reasoning_effort": True}
        # scale order, not declaration order — clients render it as a ladder
        assert claude_effort_levels("qwen3.6-27b") == ("low", "medium", "xhigh")
        assert _claude_model_entry_meta("qwen3.6-27b") == {
            "effort_levels": ["low", "medium", "xhigh"]
        }
        # certified without '=' → the whole ladder
        assert _claude_model_entry_meta("glm-5-fp8") == {
            "effort_levels": ["low", "medium", "high", "xhigh", "max"]
        }
        # not certified → no guarantee, no ladder
        assert claude_effort_levels("sonnet") is None
        assert "effort_levels" not in _claude_model_entry_meta("sonnet")

    def test_an_exact_id_wins_over_the_wildcard_levels(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv(
            "CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "*=low|medium|high,qwen3.6-27b=low|medium|xhigh"
        )
        assert claude_effort_levels("qwen3.6-27b") == ("low", "medium", "xhigh")
        assert claude_effort_levels("glm-5-fp8") == ("low", "medium", "high")

    @pytest.mark.parametrize(
        "raw",
        [
            "qwen3.6-27b=low|medium|xhi",  # typo
            "qwen3.6-27b=",  # declares nothing
            "=low|medium",  # no id
            "qwen3.6-27b=low,qwen3.6-27b=high",  # contradicts itself
        ],
    )
    def test_an_invalid_certification_certifies_nothing(self, monkeypatch, raw):
        """Invalid never narrows and never widens: a typo must not become either a
        stricter ladder or a blanket promise. The whole setting is ignored (the
        startup config check is what refuses a fresh deploy)."""
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", raw)
        assert _claude_model_capabilities("qwen3.6-27b") == {"reasoning_effort": False}
        assert claude_effort_levels("qwen3.6-27b") is None

    def test_first_party_guarantee_carries_the_whole_ladder(self, first_party_upstream):
        assert _claude_model_entry_meta("sonnet") == {
            "alias": True,
            "effort_levels": ["low", "medium", "high", "xhigh", "max"],
        }
        # haiku: no guarantee → no ladder, alias bookkeeping untouched
        assert _claude_model_entry_meta("haiku") == {"alias": True}

    def test_v1_models_entry_carries_effort_levels_iff_guaranteed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "qwen3.6-27b")
        monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "qwen3.6-27b=low|medium|xhigh")
        by_id = {
            e["id"]: e
            for e in (BackendRegistry._model_entry(CLAUDE_DESCRIPTOR, m) for m in ("sonnet", "qwen3.6-27b"))
        }
        assert by_id["qwen3.6-27b"]["capabilities"]["reasoning_effort"] is True
        assert by_id["qwen3.6-27b"]["effort_levels"] == ["low", "medium", "xhigh"]
        assert by_id["qwen3.6-27b"]["alias_of"] == "sonnet"
        assert by_id["sonnet"]["capabilities"]["reasoning_effort"] is False
        assert "effort_levels" not in by_id["sonnet"]

    def test_levels_the_upstream_states_on_v1_models_certify_without_any_env(self, monkeypatch):
        """The litellm_serving sanitizer learns each served model's levels from the
        model's own 400s (or a probe) and publishes ``effort_levels`` on the relayed
        ``/v1/models``. With discovery on, that statement IS the certification —
        nothing for an operator to copy into a second config."""
        from src.backends.claude import model_discovery as md

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        monkeypatch.setenv("MODEL_DISCOVERY_ENABLED", "true")
        monkeypatch.delenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", raising=False)
        md._reset_cache_for_tests()
        ids, levels = md._parse_models(
            {
                "data": [
                    {"id": "qwen3.6-27b", "object": "model", "effort_levels": ["xhigh", "low", "medium", "bogus"]},
                    {"id": "glm-5-fp8", "object": "model", "effort_levels": []},
                    {"id": "gemma-4-31b-it", "object": "model"},
                ]
            }
        )
        assert ids == ["qwen3.6-27b", "glm-5-fp8", "gemma-4-31b-it"]
        assert levels == {"qwen3.6-27b": ("low", "medium", "xhigh")}, "scale order; unknown names dropped; empty states nothing"
        md._cache = md._DiscoveryCache(source="http://litellm.internal:4000", model_ids=tuple(ids), expires_at=1e12, effort_levels=levels)
        try:
            assert _claude_model_capabilities("qwen3.6-27b") == {"reasoning_effort": True}
            assert _claude_model_entry_meta("qwen3.6-27b")["effort_levels"] == ["low", "medium", "xhigh"]
            assert _claude_model_capabilities("gemma-4-31b-it") == {"reasoning_effort": False}
            assert claude_effort_levels("glm-5-fp8") is None
            # the operator's word still wins where both exist
            monkeypatch.setenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", "qwen3.6-27b=low|medium")
            assert claude_effort_levels("qwen3.6-27b") == ("low", "medium")
            # discovery off → the statement is not read (opt-in stays opt-in)
            monkeypatch.delenv("CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS", raising=False)
            monkeypatch.setenv("MODEL_DISCOVERY_ENABLED", "false")
            assert _claude_model_capabilities("qwen3.6-27b") == {"reasoning_effort": False}
        finally:
            md._reset_cache_for_tests()

    def test_a_configured_alias_name_is_never_guaranteed(
        self, first_party_upstream, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "team-sonnet-v3")
        # the configured name itself: an arbitrary string, not in the CLI registry
        assert _claude_model_capabilities("team-sonnet-v3") == {
            "reasoning_effort": False
        }
        # and the tier it redirects: the CLI resolves `sonnet` to that id, so the
        # guarantee belongs to the id, not to the alias
        assert _claude_model_capabilities("sonnet") == {"reasoning_effort": False}
        # untouched tiers keep it
        assert _claude_model_capabilities("opus") == {"reasoning_effort": True}

    def test_an_id_discovered_from_the_upstream_is_never_guaranteed(
        self, first_party_upstream
    ):
        for model in ("gpt-4o", "claude/foo", "vendor/sonnet-ish"):
            assert _claude_model_capabilities(model) == {"reasoning_effort": False}

    def test_the_entry_reports_acceptance_while_withholding_the_guarantee(
        self, monkeypatch
    ):
        """The two flags must be separable: accepted, but not guaranteed.

        This is the state a LiteLLM deployment is in, and the one that made the
        single flag a false positive.
        """
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://litellm.internal:4000")
        for name in _ALIAS_ENVS:
            monkeypatch.delenv(name, raising=False)
        caps = BackendRegistry._model_entry(CLAUDE_DESCRIPTOR, "sonnet")["capabilities"]
        assert caps["reasoning_effort_accepted"] is True, "the field is still accepted"
        assert caps["reasoning_effort"] is False, "but not guaranteed to be applied"
        assert caps["image_input"] is True

    def test_the_guarantee_and_acceptance_agree_on_a_first_party_upstream(
        self, first_party_upstream
    ):
        caps = BackendRegistry._model_entry(CLAUDE_DESCRIPTOR, "sonnet")["capabilities"]
        assert caps["reasoning_effort"] is True
        assert caps["reasoning_effort_accepted"] is True

    def test_a_backend_without_the_hook_keeps_its_descriptor_flags(
        self, clean_registry
    ):
        """``model_capabilities_fn`` is optional — absent means no narrowing."""
        desc = BackendDescriptor(
            name="plain",
            owned_by="vendor",
            models=["plain-1"],
            resolve_fn=lambda m: None,
            capabilities={"image_input": True},
        )
        caps = BackendRegistry._model_entry(desc, "plain-1")["capabilities"]
        assert caps == {
            "image_input": True,
            "reasoning_effort": False,
            "reasoning_effort_accepted": False,
        }
