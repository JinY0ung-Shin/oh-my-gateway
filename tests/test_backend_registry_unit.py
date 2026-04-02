"""Unit tests for src/backend_registry.py."""

import pytest

from src.backend_registry import BackendRegistry, resolve_model


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    """Test model string resolution to backend + provider model."""

    def test_claude_models(self):
        for model in ("opus", "sonnet", "haiku"):
            r = resolve_model(model)
            assert r.backend == "claude"
            assert r.provider_model == model
            assert r.public_model == model

    def test_unknown_model_returns_none(self):
        r = resolve_model("some-unknown-model")
        assert r is None

    def test_unknown_slash_model_returns_none(self):
        """Unknown prefix/submodel should return None."""
        r = resolve_model("future-backend/model-x")
        assert r is None

    def test_resolved_model_is_frozen(self):
        r = resolve_model("sonnet")
        with pytest.raises(AttributeError):
            r.backend = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BackendRegistry
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal stub for registry tests (not a real BackendClient)."""

    def __init__(self, name: str = "fake"):
        self.name = name


@pytest.fixture(autouse=True)
def _auto_clean_registry(clean_registry):
    """Use the shared clean_registry fixture from conftest.py."""
    pass


class TestBackendRegistry:
    def test_register_and_get(self):
        fb = FakeBackend("a")
        BackendRegistry.register("test", fb)
        assert BackendRegistry.get("test") is fb

    def test_get_missing_raises(self):
        with pytest.raises(ValueError, match="not registered"):
            BackendRegistry.get("nonexistent")

    def test_is_registered(self):
        assert not BackendRegistry.is_registered("x")
        BackendRegistry.register("x", FakeBackend())
        assert BackendRegistry.is_registered("x")

    def test_unregister(self):
        BackendRegistry.register("x", FakeBackend())
        BackendRegistry.unregister("x")
        assert not BackendRegistry.is_registered("x")

    def test_unregister_missing_is_noop(self):
        BackendRegistry.unregister("missing")  # should not raise

    def test_clear(self):
        BackendRegistry.register("a", FakeBackend())
        BackendRegistry.register("b", FakeBackend())
        BackendRegistry.clear()
        assert BackendRegistry.all_backends() == {}

    def test_all_backends_returns_snapshot(self):
        fb = FakeBackend()
        BackendRegistry.register("a", fb)
        snap = BackendRegistry.all_backends()
        assert snap == {"a": fb}
        # Mutating snapshot should not affect registry
        snap["b"] = FakeBackend()
        assert not BackendRegistry.is_registered("b")

    def test_available_models_empty_when_no_backends(self):
        assert BackendRegistry.available_models() == []

    def test_available_models(self):
        BackendRegistry.register("claude", FakeBackend())
        models = BackendRegistry.available_models()
        ids = [m["id"] for m in models]
        assert "opus" in ids
        assert "sonnet" in ids
        assert "haiku" in ids
        for m in models:
            assert m["owned_by"] == "anthropic"

    def test_get_error_message_known_but_not_available(self):
        """When a descriptor is registered but no client, error says 'known but not available'."""
        from src.backends.base import BackendDescriptor

        fake_desc = BackendDescriptor(
            name="fake_unavailable",
            models=["fake-model"],
            owned_by="test",
            resolve_fn=lambda m: None,
        )
        BackendRegistry.register_descriptor(fake_desc)
        BackendRegistry.register("claude", FakeBackend())
        with pytest.raises(ValueError, match="known but not available"):
            BackendRegistry.get("fake_unavailable")

    def test_get_error_message_lists_available_for_unknown(self):
        """When a backend is completely unknown, error lists available backends."""
        BackendRegistry.register("claude", FakeBackend())
        try:
            BackendRegistry.get("nonexistent")
        except ValueError as e:
            assert "claude" in str(e)
