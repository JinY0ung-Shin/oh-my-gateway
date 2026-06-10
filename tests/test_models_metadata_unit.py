"""Tests for /v1/models metadata expansion (backend + capabilities)."""

from unittest.mock import MagicMock

from src.backend_registry import BackendDescriptor, BackendRegistry
from src.backends.claude import CLAUDE_DESCRIPTOR
from src.backends.codex import CODEX_DESCRIPTOR
from src.backends.opencode import OPENCODE_DESCRIPTOR

from tests.test_main_api_unit import client_context


class TestDescriptorCapabilities:
    def test_all_backend_descriptors_declare_image_input(self):
        assert CLAUDE_DESCRIPTOR.capabilities == {"image_input": True}
        assert CODEX_DESCRIPTOR.capabilities == {"image_input": True}
        assert OPENCODE_DESCRIPTOR.capabilities == {"image_input": True}

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
            assert entry["capabilities"] == {"image_input": True}

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
        assert entries[0]["capabilities"] == {"image_input": False}

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
