"""Tests for the gateway-managed ``env`` block of the Claude settings file."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import pytest

from src import claude_settings_env, claude_settings_env_service
from src.claude_settings_env_service import ClaudeSettingsEnvError


@pytest.fixture
def env_files(tmp_path, monkeypatch):
    """Per-test settings file + admin store, with both layers reloaded.

    Teardown reloads against absent paths BEFORE monkeypatch undo so nothing a
    test wrote stays in the module singletons.
    """
    settings = tmp_path / "home" / ".claude" / "settings.json"
    store = tmp_path / "store.json"
    monkeypatch.setenv("GATEWAY_CLAUDE_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("GATEWAY_CLAUDE_SETTINGS_ENV_STORE", str(store))
    monkeypatch.delenv("GATEWAY_CLAUDE_SETTINGS_ENV", raising=False)
    monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")
    claude_settings_env.reload()
    yield settings
    monkeypatch.setenv("GATEWAY_CLAUDE_SETTINGS_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("GATEWAY_CLAUDE_SETTINGS_ENV_STORE", str(tmp_path / "gone.json"))
    monkeypatch.delenv("GATEWAY_CLAUDE_SETTINGS_ENV", raising=False)
    claude_settings_env.reload()


@pytest.fixture
def env_layer(monkeypatch):
    """Setter for the deploy-time ``GATEWAY_CLAUDE_SETTINGS_ENV`` layer."""

    def _set(value: str) -> None:
        monkeypatch.setenv(claude_settings_env.ENV_LAYER_VAR, value)
        claude_settings_env.reload()

    yield _set
    monkeypatch.delenv(claude_settings_env.ENV_LAYER_VAR, raising=False)
    claude_settings_env.reload()


def _read(settings_file):
    return json.loads(settings_file.read_text(encoding="utf-8"))


class TestDeployLayer:
    def test_inline_json(self, env_files, env_layer):
        env_layer('{"TEAM_ID": "platform"}')
        assert claude_settings_env.get_env_layer() == {"TEAM_ID": "platform"}

    def test_file_path(self, env_files, env_layer, tmp_path):
        path = tmp_path / "session-env.json"
        path.write_text('{"TEAM_ID": "from-file"}', encoding="utf-8")
        env_layer(str(path))
        assert claude_settings_env.get_env_layer() == {"TEAM_ID": "from-file"}

    def test_store_wrapper_shape(self, env_files, env_layer):
        env_layer('{"version": 1, "env": {"TEAM_ID": "wrapped"}}')
        assert claude_settings_env.get_env_layer() == {"TEAM_ID": "wrapped"}

    def test_invalid_json_ignored(self, env_files, env_layer, caplog):
        with caplog.at_level(logging.ERROR):
            env_layer("{nope")
        assert claude_settings_env.get_env_layer() == {}
        assert any(
            claude_settings_env.ENV_LAYER_VAR in r.getMessage() for r in caplog.records
        )

    def test_non_object_ignored(self, env_files, env_layer):
        env_layer('["TEAM_ID"]')
        assert claude_settings_env.get_env_layer() == {}

    def test_reserved_key_refused(self, env_files, env_layer, caplog):
        """settings.json env overrides the process env, so accepting an auth key
        here would silently break the gateway's own Claude authentication."""
        with caplog.at_level(logging.ERROR):
            env_layer('{"ANTHROPIC_AUTH_TOKEN": "hijack", "TEAM_ID": "ok"}')
        assert claude_settings_env.get_env_layer() == {"TEAM_ID": "ok"}
        assert any("reserved key" in r.getMessage() for r in caplog.records)

    def test_invalid_names_and_values_dropped(self, env_files, env_layer):
        env_layer('{"OK": "1", "bad-name": "x", "9LEAD": "y", "NUM": 5}')
        assert claude_settings_env.get_env_layer() == {"OK": "1"}

    def test_admin_wins_per_key(self, env_files, env_layer):
        env_layer('{"TEAM_ID": "from-env", "KEEP": "env"}')
        claude_settings_env.replace_admin_env({"TEAM_ID": "from-admin"})
        assert claude_settings_env.get_effective_env() == {
            "TEAM_ID": "from-admin",
            "KEEP": "env",
        }
        assert claude_settings_env.key_sources() == {
            "TEAM_ID": "env+admin",
            "KEEP": "env",
        }
        # The store keeps only admin keys — an env-declared value is never frozen.
        assert claude_settings_env.get_admin_env() == {"TEAM_ID": "from-admin"}


class TestProjection:
    def test_creates_file_and_parent_dir(self, env_files):
        report = claude_settings_env.replace_admin_env({"TEAM_ID": "platform"})
        assert report["ok"] is True
        assert report["written"] == ["TEAM_ID"]
        assert _read(env_files) == {"env": {"TEAM_ID": "platform"}}

    def test_preserves_other_settings_keys(self, env_files):
        env_files.parent.mkdir(parents=True, exist_ok=True)
        env_files.write_text(
            json.dumps({"permissions": {"allow": ["Bash"]}, "model": "opus"}),
            encoding="utf-8",
        )
        claude_settings_env.replace_admin_env({"TEAM_ID": "platform"})
        data = _read(env_files)
        assert data["permissions"] == {"allow": ["Bash"]}
        assert data["model"] == "opus"
        assert data["env"] == {"TEAM_ID": "platform"}

    def test_preserves_unmanaged_env_keys(self, env_files):
        env_files.parent.mkdir(parents=True, exist_ok=True)
        env_files.write_text(
            json.dumps({"env": {"HAND_ADDED": "keep-me"}}), encoding="utf-8"
        )
        report = claude_settings_env.replace_admin_env({"TEAM_ID": "platform"})
        assert report["kept_foreign"] == ["HAND_ADDED"]
        assert _read(env_files)["env"] == {
            "HAND_ADDED": "keep-me",
            "TEAM_ID": "platform",
        }

    def test_prunes_only_previously_projected_keys(self, env_files):
        env_files.parent.mkdir(parents=True, exist_ok=True)
        env_files.write_text(
            json.dumps({"env": {"HAND_ADDED": "keep-me"}}), encoding="utf-8"
        )
        claude_settings_env.replace_admin_env({"A": "1", "B": "2"})
        report = claude_settings_env.replace_admin_env({"A": "1"})
        assert report["pruned"] == ["B"]
        assert _read(env_files)["env"] == {"HAND_ADDED": "keep-me", "A": "1"}

    def test_clear_removes_env_block_but_keeps_file(self, env_files):
        env_files.parent.mkdir(parents=True, exist_ok=True)
        env_files.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        claude_settings_env.replace_admin_env({"A": "1"})
        claude_settings_env.clear_admin_env()
        data = _read(env_files)
        assert "env" not in data
        assert data["model"] == "opus"

    def test_resolves_env_templates_at_projection(self, env_files, monkeypatch):
        monkeypatch.setenv("CORP_PROXY_URL", "http://proxy.internal:3128")
        claude_settings_env.replace_admin_env({"HTTPS_PROXY": "{{env:CORP_PROXY_URL}}"})
        assert _read(env_files)["env"]["HTTPS_PROXY"] == "http://proxy.internal:3128"
        # The template, not the resolved value, is what is stored.
        assert claude_settings_env.get_admin_env() == {
            "HTTPS_PROXY": "{{env:CORP_PROXY_URL}}"
        }

    def test_reproject_picks_up_rotated_value(self, env_files, monkeypatch):
        monkeypatch.setenv("CORP_TOKEN", "v1")
        claude_settings_env.replace_admin_env({"CORP": "{{env:CORP_TOKEN}}"})
        monkeypatch.setenv("CORP_TOKEN", "v2")
        claude_settings_env_service.reproject()
        assert _read(env_files)["env"]["CORP"] == "v2"

    def test_corrupt_settings_file_is_not_clobbered(self, env_files, caplog):
        env_files.parent.mkdir(parents=True, exist_ok=True)
        env_files.write_text("{ this is not json", encoding="utf-8")
        with caplog.at_level(logging.ERROR):
            report = claude_settings_env.project()
        assert report["ok"] is False
        assert "not valid JSON" in (report["error"] or "")
        assert env_files.read_text(encoding="utf-8") == "{ this is not json"

    def test_startup_projection_skips_when_nothing_declared(self, env_files):
        report = claude_settings_env.project_at_startup()
        assert report.get("skipped") is True
        assert not env_files.exists()

    def test_startup_projection_writes_declared_layer(self, env_files, env_layer):
        env_layer('{"TEAM_ID": "platform"}')
        report = claude_settings_env.project_at_startup()
        assert report["ok"] is True
        assert _read(env_files)["env"] == {"TEAM_ID": "platform"}

    def test_missing_home_and_path_reports_error(self, env_files, monkeypatch):
        monkeypatch.delenv("GATEWAY_CLAUDE_SETTINGS_PATH", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        report = claude_settings_env.project()
        assert report["ok"] is False
        assert "HOME is unset" in (report["error"] or "")


class TestSettingSources:
    def test_applies_to_sessions_tracks_the_var(self, env_files, monkeypatch):
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "project,local")
        assert claude_settings_env.applies_to_sessions() is False
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project")
        assert claude_settings_env.applies_to_sessions() is True

    def test_default_matches_the_claude_client(self, monkeypatch):
        """The duplicated default must not drift from the SDK option builder."""
        from src.backends.claude.client import _DEFAULT_SETTING_SOURCES

        assert tuple(_DEFAULT_SETTING_SOURCES) == tuple(
            claude_settings_env._DEFAULT_SETTING_SOURCES
        )
        monkeypatch.delenv("CLAUDE_SETTING_SOURCES", raising=False)
        assert claude_settings_env.applies_to_sessions() is (
            "user" in _DEFAULT_SETTING_SOURCES
        )


class TestService:
    def test_rejects_reserved_key(self, env_files):
        with pytest.raises(ClaudeSettingsEnvError, match="reserved"):
            claude_settings_env_service.replace_env({"ANTHROPIC_BASE_URL": "http://x"})

    def test_rejects_invalid_name(self, env_files):
        with pytest.raises(ClaudeSettingsEnvError, match="invalid env var name"):
            claude_settings_env_service.replace_env({"bad-name": "x"})

    def test_rejects_non_string_value(self, env_files):
        with pytest.raises(ClaudeSettingsEnvError, match="string value"):
            claude_settings_env_service.replace_env({"OK": 5})

    def test_redacted_roundtrip_preserves_secret(self, env_files):
        claude_settings_env_service.replace_env({"CORP_TOKEN": "s3cret"})
        detail = claude_settings_env_service.get_detail()
        assert detail["admin"]["CORP_TOKEN"] == "***REDACTED***"
        # Saving the redacted form back must keep the stored secret.
        claude_settings_env_service.replace_env({"CORP_TOKEN": "***REDACTED***"})
        assert _read(env_files)["env"]["CORP_TOKEN"] == "s3cret"

    def test_placeholder_for_unknown_key_is_rejected(self, env_files):
        with pytest.raises(ClaudeSettingsEnvError, match="redaction placeholder"):
            claude_settings_env_service.replace_env({"NEW_KEY": "***REDACTED***"})

    def test_env_ref_stays_visible(self, env_files):
        claude_settings_env_service.replace_env({"CORP_TOKEN": "{{env:SOME_VAR}}"})
        detail = claude_settings_env_service.get_detail()
        assert detail["admin"]["CORP_TOKEN"] == "{{env:SOME_VAR}}"

    def test_detail_reports_sources_and_unmanaged(self, env_files, env_layer):
        env_files.parent.mkdir(parents=True, exist_ok=True)
        env_files.write_text(json.dumps({"env": {"HAND_ADDED": "x"}}), encoding="utf-8")
        env_layer('{"FROM_ENV": "1"}')
        claude_settings_env_service.replace_env({"FROM_ADMIN": "2"})
        detail = claude_settings_env_service.get_detail()
        assert detail["sources"] == {"FROM_ENV": "env", "FROM_ADMIN": "admin"}
        assert detail["unmanaged_keys"] == ["HAND_ADDED"]
        assert detail["env_layer_declared"] is True
        assert detail["applies_to_sessions"] is True
        assert detail["warnings"] == []

    def test_detail_warns_without_user_setting_source(self, env_files, monkeypatch):
        claude_settings_env_service.replace_env({"TEAM_ID": "x"})
        monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "project,local")
        detail = claude_settings_env_service.get_detail()
        assert detail["applies_to_sessions"] is False
        assert any("does not include 'user'" in w for w in detail["warnings"])

    def test_clear_keeps_the_env_layer(self, env_files, env_layer):
        env_layer('{"FROM_ENV": "1"}')
        claude_settings_env_service.replace_env({"FROM_ADMIN": "2"})
        claude_settings_env_service.clear_env()
        assert claude_settings_env.get_admin_env() == {}
        assert _read(env_files)["env"] == {"FROM_ENV": "1"}


class TestStartup:
    async def test_lifespan_projects_the_declared_layer(self, env_files, env_layer):
        """A GATEWAY_CLAUDE_SETTINGS_ENV declaration must land without any admin
        action — that is the whole point of the deploy-time layer."""
        from unittest.mock import AsyncMock

        import src.main as main

        env_layer('{"TEAM_ID": "platform"}')
        with (
            patch("src.admin_auth.validate_admin_config"),
            patch.object(
                main,
                "validate_claude_code_auth",
                return_value=(True, {"method": "claude_cli"}),
            ),
            patch.object(main, "get_mcp_servers", return_value={}),
            patch.object(main, "discover_backends"),
            patch.object(main, "_verify_backends", AsyncMock()),
            patch.object(main.session_manager, "start_cleanup_task"),
            patch.object(main.session_manager, "async_shutdown", AsyncMock()),
        ):
            async with main.lifespan(main.app):
                pass
        assert _read(env_files)["env"] == {"TEAM_ID": "platform"}


class TestRoutes:
    @pytest.fixture
    def admin_client(self):
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
            from src.admin_auth import require_admin
            from src.main import app

            app.dependency_overrides[require_admin] = lambda: True
            client = TestClient(app)
            yield client
            app.dependency_overrides.pop(require_admin, None)

    def test_requires_admin(self, env_files):
        from fastapi.testclient import TestClient

        from src.main import app

        client = TestClient(app)
        assert client.get("/admin/api/claude-settings-env").status_code == 401
        assert client.put("/admin/api/claude-settings-env", json={}).status_code == 401

    def test_put_get_delete_roundtrip(self, env_files, admin_client):
        r = admin_client.put(
            "/admin/api/claude-settings-env", json={"env": {"CORP_TOKEN": "s3cret"}}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "saved"
        assert body["report"]["written"] == ["CORP_TOKEN"]
        assert body["detail"]["admin"]["CORP_TOKEN"] == "***REDACTED***"
        assert _read(env_files)["env"]["CORP_TOKEN"] == "s3cret"

        r = admin_client.get("/admin/api/claude-settings-env")
        assert r.status_code == 200
        assert r.json()["sources"] == {"CORP_TOKEN": "admin"}

        r = admin_client.post("/admin/api/claude-settings-env/reproject")
        assert r.status_code == 200

        r = admin_client.delete("/admin/api/claude-settings-env")
        assert r.status_code == 200
        assert "env" not in _read(env_files)

    def test_reserved_key_is_400(self, env_files, admin_client):
        r = admin_client.put(
            "/admin/api/claude-settings-env",
            json={"env": {"ANTHROPIC_AUTH_TOKEN": "hijack"}},
        )
        assert r.status_code == 400
        assert "reserved" in r.json()["error"]
