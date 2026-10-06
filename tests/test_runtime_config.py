"""Tests for runtime_config module."""

import os

import pytest

from src.runtime_config import (
    EDITABLE_KEYS,
    get_default_max_turns,
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


class TestAgentTeamsOriginal:
    """agent_teams_enabled derives its original from the CLI gate env var."""

    def test_original_false_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", raising=False)
        assert runtime_config.get("agent_teams_enabled") is False

    def test_original_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")
        assert runtime_config.get("agent_teams_enabled") is True

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("1", True),
            ("true", True),
            (" TRUE ", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("off", False),
            ("", False),
            (" ", False),
        ],
    )
    def test_original_mirrors_cli_gate_parse(self, monkeypatch, value, expected):
        """CLI 2.1.283 reads the gate as 1/true/yes/on only ("0" is off).

        tests/test_cli_task_identity.py pins that parse against the real CLI.
        """
        monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", value)
        assert runtime_config.get("agent_teams_enabled") is expected

    def test_override_beats_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1")
        runtime_config.set("agent_teams_enabled", False)
        assert runtime_config.get("agent_teams_enabled") is False


class TestAgentTeamsDefault:
    """src/constants.py installs the gate ON unless the operator set a value."""

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_unset_or_blank_defaults_on(self, monkeypatch, value):
        from src.constants import AGENT_TEAMS_ENV, _ensure_agent_teams_default_on

        if value is None:
            monkeypatch.delenv(AGENT_TEAMS_ENV, raising=False)
        else:
            monkeypatch.setenv(AGENT_TEAMS_ENV, value)
        _ensure_agent_teams_default_on()
        assert os.environ[AGENT_TEAMS_ENV] == "1"
        assert runtime_config.get("agent_teams_enabled") is True

    @pytest.mark.parametrize("value", ["0", "false", "true"])
    def test_explicit_operator_value_wins(self, monkeypatch, value):
        from src.constants import AGENT_TEAMS_ENV, _ensure_agent_teams_default_on

        monkeypatch.setenv(AGENT_TEAMS_ENV, value)
        _ensure_agent_teams_default_on()
        assert os.environ[AGENT_TEAMS_ENV] == value


class TestConvenienceGetters:
    def test_get_default_model(self):
        from src.constants import DEFAULT_MODEL

        assert get_default_model() == DEFAULT_MODEL
        runtime_config.set("default_model", "custom")
        assert get_default_model() == "custom"

    def test_get_default_max_turns(self):
        runtime_config.set("default_max_turns", 7)
        assert get_default_max_turns() == 7

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


class TestRuntimeConfigPersistence:
    """Admin overrides survive a restart / image rebuild via data/runtime_config.json."""

    @pytest.fixture
    def store(self, tmp_path):
        from src.runtime_config import RuntimeConfig

        return RuntimeConfig(persist_path=tmp_path / "data" / "runtime_config.json")

    def _restart(self, store):
        from src.runtime_config import RuntimeConfig

        fresh = RuntimeConfig(persist_path=store.persist_path)
        fresh.load_persisted()
        return fresh

    def test_overrides_survive_restart(self, store):
        store.set("workspace_upload_max_bytes", 50 * 1024 * 1024)
        store.set("thinking_mode", "adaptive")
        store.set("token_streaming", "false")

        fresh = self._restart(store)
        assert fresh.get("workspace_upload_max_bytes") == 50 * 1024 * 1024
        assert fresh.get("thinking_mode") == "adaptive"
        assert fresh.get("token_streaming") is False
        assert fresh.is_overridden("workspace_upload_max_bytes")

    def test_zero_upload_limit_survives_restart(self, store):
        # 0 = uploads disabled; must not be mistaken for "unset" on reload
        store.set("workspace_upload_max_bytes", 0)
        fresh = self._restart(store)
        assert fresh.is_overridden("workspace_upload_max_bytes")
        assert fresh.get("workspace_upload_max_bytes") == 0

    def test_reset_is_persisted(self, store):
        store.set("default_max_turns", 77)
        store.set("default_model", "m")
        store.reset("default_max_turns")
        fresh = self._restart(store)
        assert not fresh.is_overridden("default_max_turns")
        assert fresh.get("default_model") == "m"

        store.reset_all()
        assert not store.persist_path.exists()
        assert not self._restart(store).is_overridden("default_model")

    def test_no_file_means_no_overrides(self, store):
        store.load_persisted()
        assert not any(store.is_overridden(k) for k in EDITABLE_KEYS)

    @pytest.mark.parametrize(
        "content",
        ["{not json", "[]", '{"overrides": 3}', '"x"'],
    )
    def test_corrupt_file_is_ignored(self, store, content):
        store.persist_path.parent.mkdir(parents=True)
        store.persist_path.write_text(content, encoding="utf-8")
        store.load_persisted()
        assert not any(store.is_overridden(k) for k in EDITABLE_KEYS)

    def test_unknown_and_invalid_entries_are_dropped(self, store):
        import json

        store.persist_path.parent.mkdir(parents=True)
        store.persist_path.write_text(
            json.dumps(
                {
                    "overrides": {
                        "removed_key": 1,
                        "default_max_turns": 0,
                        "thinking_mode": "bogus",
                        "default_model": "kept",
                    }
                }
            ),
            encoding="utf-8",
        )
        store.load_persisted()
        assert store.get("default_model") == "kept"
        assert not store.is_overridden("default_max_turns")
        assert not store.is_overridden("thinking_mode")

    def test_failed_write_leaves_memory_unchanged(self, store, monkeypatch):
        store.set("default_model", "before")

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("src.runtime_config.os.replace", boom)
        with pytest.raises(OSError):
            store.set("default_model", "after")
        with pytest.raises(OSError):
            store.reset("default_max_turns")
        assert store.get("default_model") == "before"
        assert self._restart(store).get("default_model") == "before"

    def test_invalid_value_does_not_touch_file(self, store):
        store.set("default_max_turns", 5)
        before = store.persist_path.read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            store.set("default_max_turns", 0)
        assert store.persist_path.read_text(encoding="utf-8") == before

    def test_singleton_persists_under_data_dir(self):
        from src import runtime_config as module

        assert module._PERSIST_FILE.name == "runtime_config.json"
        assert module._PERSIST_FILE.parent.name == "data"


def test_admin_patch_persists_and_startup_restores(tmp_path, monkeypatch):
    """PATCH /admin/api/runtime-config writes the file the next process restores."""
    from fastapi.testclient import TestClient

    from src.admin_auth import require_admin
    from src.main import app
    from src.runtime_config import RuntimeConfig

    path = tmp_path / "runtime_config.json"
    monkeypatch.setattr(runtime_config, "persist_path", path)
    app.dependency_overrides[require_admin] = lambda: True
    try:
        client = TestClient(app)
        r = client.patch(
            "/admin/api/runtime-config",
            json={"key": "workspace_upload_max_bytes", "value": 123456},
        )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.pop(require_admin, None)

    fresh = RuntimeConfig(persist_path=path)
    fresh.load_persisted()
    assert fresh.get("workspace_upload_max_bytes") == 123456


def test_admin_patch_reports_persist_failure(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.admin_auth import require_admin
    from src.main import app

    monkeypatch.setattr(runtime_config, "persist_path", tmp_path / "rc.json")

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr("src.runtime_config.os.replace", boom)
    app.dependency_overrides[require_admin] = lambda: True
    try:
        client = TestClient(app)
        r = client.patch(
            "/admin/api/runtime-config",
            json={"key": "default_max_turns", "value": 9},
        )
    finally:
        app.dependency_overrides.pop(require_admin, None)
    assert r.status_code == 500
    assert not runtime_config.is_overridden("default_max_turns")
