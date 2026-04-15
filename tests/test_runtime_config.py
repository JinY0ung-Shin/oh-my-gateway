"""Tests for runtime_config module."""

import pytest

from src.runtime_config import (
    EDITABLE_KEYS,
    get_default_model,
    get_thinking_mode,
    get_token_streaming,
    runtime_config,
)


@pytest.fixture(autouse=True)
def clean_overrides():
    """Reset runtime overrides before each test."""
    runtime_config.reset_all()
    yield
    runtime_config.reset_all()


class TestRuntimeConfig:
    def test_get_returns_original_when_no_override(self):
        from src.constants import DEFAULT_MODEL

        assert runtime_config.get("default_model") == DEFAULT_MODEL

    def test_set_and_get(self):
        runtime_config.set("default_model", "test-model")
        assert runtime_config.get("default_model") == "test-model"

    def test_set_unknown_key_raises(self):
        with pytest.raises(KeyError, match="not editable"):
            runtime_config.set("unknown_key", "value")

    def test_reset_single_key(self):
        from src.constants import DEFAULT_MODEL

        runtime_config.set("default_model", "changed")
        runtime_config.reset("default_model")
        assert runtime_config.get("default_model") == DEFAULT_MODEL

    def test_reset_all(self):
        runtime_config.set("default_model", "changed")
        runtime_config.set("default_max_turns", 99)
        runtime_config.reset_all()
        all_settings = runtime_config.get_all()
        assert not any(v["overridden"] for v in all_settings.values())

    def test_get_all_structure(self):
        result = runtime_config.get_all()
        assert set(result.keys()) == set(EDITABLE_KEYS.keys())
        for key, meta in result.items():
            assert "value" in meta
            assert "original" in meta
            assert "overridden" in meta
            assert "label" in meta
            assert "type" in meta

    def test_get_all_shows_override(self):
        runtime_config.set("default_max_turns", 42)
        result = runtime_config.get_all()
        assert result["default_max_turns"]["value"] == 42
        assert result["default_max_turns"]["overridden"] is True

    def test_reset_unknown_key_raises(self):
        with pytest.raises(KeyError, match="not editable"):
            runtime_config.reset("unknown_key")

    def test_is_overridden(self):
        assert runtime_config.is_overridden("default_model") is False
        runtime_config.set("default_model", "test")
        assert runtime_config.is_overridden("default_model") is True
        runtime_config.reset("default_model")
        assert runtime_config.is_overridden("default_model") is False


class TestTypeCoercion:
    def test_int_coercion(self):
        runtime_config.set("default_max_turns", "20")
        assert runtime_config.get("default_max_turns") == 20

    def test_int_rejects_zero(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            runtime_config.set("default_max_turns", 0)

    def test_int_rejects_negative(self):
        with pytest.raises(ValueError, match="must be >= 1"):
            runtime_config.set("default_max_turns", -5)

    def test_bool_from_string(self):
        runtime_config.set("token_streaming", "false")
        assert runtime_config.get("token_streaming") is False

        runtime_config.set("token_streaming", "true")
        assert runtime_config.get("token_streaming") is True

    def test_bool_from_bool(self):
        runtime_config.set("token_streaming", False)
        assert runtime_config.get("token_streaming") is False

    def test_bool_rejects_garbage(self):
        """Invalid bool strings must raise ValueError, not silently become False."""
        with pytest.raises(ValueError, match="must be a boolean"):
            runtime_config.set("token_streaming", "banana")

    def test_bool_rejects_disabled(self):
        """'disabled' is not a valid boolean — must use true/false/yes/no."""
        with pytest.raises(ValueError, match="must be a boolean"):
            runtime_config.set("token_streaming", "disabled")

    def test_string_coercion(self):
        runtime_config.set("default_model", 123)
        assert runtime_config.get("default_model") == "123"


class TestConvenienceGetters:
    def test_get_default_model(self):
        from src.constants import DEFAULT_MODEL

        assert get_default_model() == DEFAULT_MODEL
        runtime_config.set("default_model", "custom")
        assert get_default_model() == "custom"

    def test_get_thinking_mode(self):
        runtime_config.set("thinking_mode", "disabled")
        assert get_thinking_mode() == "disabled"

    def test_get_token_streaming(self):
        runtime_config.set("token_streaming", False)
        assert get_token_streaming() is False


class TestSessionManagerTTLIntegration:
    """Verify SessionManager still honors constructor TTL."""

    def test_constructor_ttl_honored_without_override(self):
        """Non-global SessionManager instances must use their own TTL."""
        from src.session_manager import SessionManager

        mgr = SessionManager(default_ttl_minutes=7)
        session = mgr.get_or_create_session("test-ttl-001")
        try:
            assert session.ttl_minutes == 7
        finally:
            mgr.delete_session("test-ttl-001")

    def test_runtime_override_takes_precedence(self):
        """When admin sets a TTL override, new sessions use that."""
        from src.session_manager import SessionManager

        runtime_config.set("session_max_age_minutes", 120)
        try:
            mgr = SessionManager(default_ttl_minutes=7)
            session = mgr.get_or_create_session("test-ttl-002")
            try:
                assert session.ttl_minutes == 120
            finally:
                mgr.delete_session("test-ttl-002")
        finally:
            runtime_config.reset("session_max_age_minutes")


class TestThreadSafety:
    def test_concurrent_set_get(self):
        """Basic thread-safety smoke test."""
        import threading

        errors = []

        def writer():
            try:
                for i in range(100):
                    runtime_config.set("default_max_turns", i + 1)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    v = runtime_config.get("default_max_turns")
                    assert isinstance(v, int)
                    assert v >= 1
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []
