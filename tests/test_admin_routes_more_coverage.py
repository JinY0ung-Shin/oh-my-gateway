"""Additional coverage tests for admin routes — attacks the remaining uncovered lines."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client():
    """FastAPI TestClient with admin auth bypassed."""
    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-admin-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Login endpoint (line 109)
# ---------------------------------------------------------------------------


class TestAdminLogin:
    def test_login_success(self):
        """POST /admin/api/login with correct key sets cookie and returns ok."""
        with patch.dict(os.environ, {"ADMIN_API_KEY": "test-admin-key"}):
            # Re-import admin_auth so it picks up the patched env key
            import importlib

            import src.admin_auth as admin_auth_mod

            admin_auth_mod.ADMIN_API_KEY = "test-admin-key"

            from src.main import app

            client = TestClient(app)
            r = client.post("/admin/api/login", json={"api_key": "test-admin-key"})

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "ttl" in data

    def test_login_wrong_key_returns_401(self):
        """POST /admin/api/login with wrong key returns 401."""
        import src.admin_auth as admin_auth_mod

        original_key = admin_auth_mod.ADMIN_API_KEY
        admin_auth_mod.ADMIN_API_KEY = "correct-key"
        try:
            from src.main import app

            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/admin/api/login", json={"api_key": "wrong-key"})
        finally:
            admin_auth_mod.ADMIN_API_KEY = original_key

        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Summary endpoint with registered backends (lines 143-144, 155)
# ---------------------------------------------------------------------------


class TestAdminSummaryWithBackends:
    def test_summary_lists_models_from_registered_backend(self, admin_client):
        """Summary endpoint iterates registered backends to populate models list."""
        from src.backends.base import BackendRegistry

        mock_backend = MagicMock()
        mock_backend.supported_models.return_value = ["model-a", "model-b"]

        BackendRegistry.register("mock-be", mock_backend)
        try:
            r = admin_client.get("/admin/api/summary")
        finally:
            BackendRegistry.unregister("mock-be")

        assert r.status_code == 200
        data = r.json()
        model_ids = [m["id"] for m in data["models"]]
        assert "model-a" in model_ids
        assert "model-b" in model_ids
        # Backend health check populates the backends dict too (line 155)
        assert "mock-be" in data["health"]["backends"]
        assert data["health"]["backends"]["mock-be"] == "registered"


# ---------------------------------------------------------------------------
# Server-info endpoint (lines 172-180)
# ---------------------------------------------------------------------------


class TestServerInfo:
    def test_server_info_without_started_at(self, admin_client):
        """GET /admin/api/server-info returns version/stats even without started_at."""
        from src.main import app

        # Ensure started_at is absent so the None branch is exercised
        if hasattr(app.state, "started_at"):
            del app.state.started_at

        r = admin_client.get("/admin/api/server-info")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert "session_stats" in data
        assert data["uptime_seconds"] is None  # no started_at set
        assert data["started_at"] is None

    def test_server_info_with_started_at(self, admin_client):
        """GET /admin/api/server-info calculates uptime when started_at is set."""
        import time

        from src.main import app

        # Set a realistic started_at on app.state
        app.state.started_at = time.time() - 42.0
        try:
            r = admin_client.get("/admin/api/server-info")
        finally:
            del app.state.started_at

        assert r.status_code == 200
        data = r.json()
        assert data["uptime_seconds"] is not None
        assert data["uptime_seconds"] >= 0


# ---------------------------------------------------------------------------
# Session stats & cleanup (lines 242-244, 250-254)
# ---------------------------------------------------------------------------


class TestSessionStats:
    def test_get_session_stats(self, admin_client):
        """GET /admin/api/sessions/stats returns session statistics dict."""
        r = admin_client.get("/admin/api/sessions/stats")
        assert r.status_code == 200
        data = r.json()
        # session_manager.get_stats() returns keys like active, total_messages, etc.
        assert isinstance(data, dict)

    def test_trigger_session_cleanup(self, admin_client):
        """POST /admin/api/sessions/cleanup returns removed count and stats."""
        r = admin_client.post("/admin/api/sessions/cleanup")
        assert r.status_code == 200
        data = r.json()
        assert "removed" in data


# ---------------------------------------------------------------------------
# Bulk delete (lines 265-283)
# ---------------------------------------------------------------------------


class TestBulkDeleteSessions:
    def test_bulk_delete_expired_only(self, admin_client):
        """expired_only=true triggers cleanup and returns mode=expired_only."""
        r = admin_client.post(
            "/admin/api/sessions/bulk-delete",
            json={"expired_only": True},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "expired_only"
        assert "deleted_count" in data

    def test_bulk_delete_by_session_ids(self, admin_client, isolated_session_manager):
        """Providing session_ids deletes matched sessions and reports not_found."""
        from src.session_manager import session_manager

        session_manager.get_or_create_session("bulk-s1")
        session_manager.get_or_create_session("bulk-s2")

        r = admin_client.post(
            "/admin/api/sessions/bulk-delete",
            json={"session_ids": ["bulk-s1", "bulk-s2", "does-not-exist"]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["deleted_count"] == 2
        assert set(data["deleted_ids"]) == {"bulk-s1", "bulk-s2"}
        assert data["not_found"] == ["does-not-exist"]

    def test_bulk_delete_no_params_returns_400(self, admin_client):
        """Empty body (no session_ids and expired_only=false) returns 400."""
        r = admin_client.post(
            "/admin/api/sessions/bulk-delete",
            json={},
        )
        assert r.status_code == 400
        data = r.json()
        assert "error" in data


# ---------------------------------------------------------------------------
# Config endpoint (line 336)
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_get_config_returns_redacted_config(self, admin_client):
        """GET /admin/api/config delegates to get_redacted_config()."""
        with patch("src.routes.admin.get_redacted_config", return_value={"key": "val"}):
            r = admin_client.get("/admin/api/config")
        assert r.status_code == 200
        assert r.json() == {"key": "val"}


# ---------------------------------------------------------------------------
# Runtime config — ValueError / TypeError path (lines 461-462)
# ---------------------------------------------------------------------------


class TestRuntimeConfigUpdateErrors:
    def test_update_runtime_config_invalid_value_type_returns_422(self, admin_client):
        """PATCH /admin/api/runtime-config with a value that fails type coercion returns 422."""
        with patch("src.runtime_config.RuntimeConfig.set", side_effect=ValueError("bad type")):
            r = admin_client.patch(
                "/admin/api/runtime-config",
                json={"key": "default_max_turns", "value": "not-an-int"},
            )
        assert r.status_code == 422
        assert "Invalid value" in r.json()["error"]

    def test_update_runtime_config_type_error_returns_422(self, admin_client):
        """PATCH /admin/api/runtime-config with TypeError also returns 422."""
        with patch("src.runtime_config.RuntimeConfig.set", side_effect=TypeError("type mismatch")):
            r = admin_client.patch(
                "/admin/api/runtime-config",
                json={"key": "default_max_turns", "value": []},
            )
        assert r.status_code == 422
        assert "Invalid value" in r.json()["error"]


# ---------------------------------------------------------------------------
# System prompt templates (lines 491-500)
# ---------------------------------------------------------------------------


class TestPromptTemplates:
    def test_list_prompt_templates_returns_list(self, admin_client):
        """GET /admin/api/system-prompt/templates returns a templates list."""
        r = admin_client.get("/admin/api/system-prompt/templates")
        assert r.status_code == 200
        data = r.json()
        assert "templates" in data
        assert isinstance(data["templates"], list)

    def test_list_prompt_templates_with_mocked_files(self, admin_client, tmp_path):
        """Templates endpoint reads and strips markdown headers from .md files."""
        # Create a fake system-prompt markdown file
        md_file = tmp_path / "system-prompt-test.md"
        md_file.write_text(
            "# Title\n> blockquote\n---\nThis is the body content.",
            encoding="utf-8",
        )

        with patch("pathlib.Path.glob", return_value=[md_file]):
            r = admin_client.get("/admin/api/system-prompt/templates")

        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["templates"], list)


# ---------------------------------------------------------------------------
# System prompt set — OSError path (lines 538-539)
# ---------------------------------------------------------------------------


class TestSetSystemPromptOSError:
    def test_set_system_prompt_oserror_returns_500(self, admin_client):
        """PUT /admin/api/system-prompt returns 500 if persist fails with OSError."""
        with patch(
            "src.system_prompt.set_system_prompt",
            side_effect=OSError("disk full"),
        ):
            r = admin_client.put(
                "/admin/api/system-prompt",
                json={"prompt": "Some valid prompt text"},
            )
        assert r.status_code == 500
        assert "Failed to persist" in r.json()["error"]


# ---------------------------------------------------------------------------
# System prompt reset — OSError path (lines 554-555)
# ---------------------------------------------------------------------------


class TestResetSystemPromptOSError:
    def test_reset_system_prompt_oserror_returns_500(self, admin_client):
        """DELETE /admin/api/system-prompt returns 500 if persist removal fails."""
        with patch(
            "src.system_prompt.reset_system_prompt",
            side_effect=OSError("permission denied"),
        ):
            r = admin_client.delete("/admin/api/system-prompt")
        assert r.status_code == 500
        assert "Failed to persist" in r.json()["error"]


# ---------------------------------------------------------------------------
# Named prompts — save OSError path (lines 598-599)
# ---------------------------------------------------------------------------


class TestSaveNamedPromptOSError:
    def test_save_prompt_oserror_returns_500(self, admin_client):
        """PUT /admin/api/prompts/{name} returns 500 if file write fails."""
        with patch(
            "src.system_prompt.save_named_prompt",
            side_effect=OSError("read-only filesystem"),
        ):
            r = admin_client.put(
                "/admin/api/prompts/my-prompt",
                json={"content": "Some prompt content"},
            )
        assert r.status_code == 500
        assert "Failed to save" in r.json()["error"]


# ---------------------------------------------------------------------------
# Named prompts — delete ValueError and OSError paths (lines 610-613)
# ---------------------------------------------------------------------------


class TestDeleteNamedPromptErrors:
    def test_delete_prompt_value_error_returns_422(self, admin_client):
        """DELETE /admin/api/prompts/{name} with invalid name returns 422."""
        with patch(
            "src.system_prompt.delete_named_prompt",
            side_effect=ValueError("invalid name"),
        ):
            r = admin_client.delete("/admin/api/prompts/bad--name")
        assert r.status_code == 422
        assert "error" in r.json()

    def test_delete_prompt_oserror_returns_500(self, admin_client):
        """DELETE /admin/api/prompts/{name} returns 500 if file removal fails."""
        with patch(
            "src.system_prompt.delete_named_prompt",
            side_effect=OSError("file locked"),
        ):
            r = admin_client.delete("/admin/api/prompts/my-prompt")
        assert r.status_code == 500
        assert "Failed to delete" in r.json()["error"]


# ---------------------------------------------------------------------------
# Named prompts — activate OSError path (lines 628-629)
# ---------------------------------------------------------------------------


class TestActivateNamedPromptOSError:
    def test_activate_prompt_oserror_returns_500(self, admin_client):
        """POST /admin/api/prompts/{name}/activate returns 500 if persist fails."""
        with patch(
            "src.system_prompt.activate_named_prompt",
            side_effect=OSError("disk error"),
        ):
            r = admin_client.post("/admin/api/prompts/my-prompt/activate")
        assert r.status_code == 500
        assert "Failed to activate" in r.json()["error"]
