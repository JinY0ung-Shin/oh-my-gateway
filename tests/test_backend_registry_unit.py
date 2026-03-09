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

    def test_codex_bare(self):
        r = resolve_model("codex")
        assert r.backend == "codex"
        assert r.provider_model is None
        assert r.public_model == "codex"

    def test_codex_slash_submodel(self):
        r = resolve_model("codex/o3")
        assert r.backend == "codex"
        assert r.provider_model == "o3"
        assert r.public_model == "codex/o3"

    def test_codex_slash_complex_submodel(self):
        r = resolve_model("codex/o4-mini")
        assert r.backend == "codex"
        assert r.provider_model == "o4-mini"

    def test_codex_slash_gpt5(self):
        r = resolve_model("codex/gpt-5")
        assert r.backend == "codex"
        assert r.provider_model == "gpt-5"

    def test_unknown_model_defaults_to_claude(self):
        r = resolve_model("some-unknown-model")
        assert r.backend == "claude"
        assert r.provider_model == "some-unknown-model"

    def test_unknown_slash_model_defaults_to_claude(self):
        """Unknown prefix/submodel should fall through to Claude with full string."""
        r = resolve_model("future-backend/model-x")
        assert r.backend == "claude"
        assert r.provider_model == "future-backend/model-x"

    def test_resolved_model_is_frozen(self):
        r = resolve_model("sonnet")
        with pytest.raises(AttributeError):
            r.backend = "codex"  # type: ignore[misc]

    def test_empty_submodel_after_slash(self):
        """'codex/' should resolve to codex with provider_model=None."""
        r = resolve_model("codex/")
        assert r.backend == "codex"
        assert r.provider_model is None


# ---------------------------------------------------------------------------
# BackendRegistry
# ---------------------------------------------------------------------------


class FakeBackend:
    """Minimal stub for registry tests (not a real BackendClient)."""

    def __init__(self, name: str = "fake"):
        self.name = name


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure registry is clean before and after each test."""
    BackendRegistry.clear()
    yield
    BackendRegistry.clear()


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

    def test_available_models_claude_only(self):
        BackendRegistry.register("claude", FakeBackend())
        models = BackendRegistry.available_models()
        ids = [m["id"] for m in models]
        assert "opus" in ids
        assert "sonnet" in ids
        assert "haiku" in ids
        assert "codex" not in ids
        for m in models:
            assert m["owned_by"] == "anthropic"

    def test_available_models_both_backends(self):
        BackendRegistry.register("claude", FakeBackend())
        BackendRegistry.register("codex", FakeBackend())
        models = BackendRegistry.available_models()
        ids = [m["id"] for m in models]
        assert "sonnet" in ids
        assert "codex" in ids
        codex_entry = [m for m in models if m["id"] == "codex"][0]
        assert codex_entry["owned_by"] == "openai"

    def test_available_models_codex_only(self):
        BackendRegistry.register("codex", FakeBackend())
        models = BackendRegistry.available_models()
        ids = [m["id"] for m in models]
        assert "codex" in ids
        assert "sonnet" not in ids

    def test_get_error_message_lists_available(self):
        BackendRegistry.register("claude", FakeBackend())
        try:
            BackendRegistry.get("codex")
        except ValueError as e:
            assert "claude" in str(e)
