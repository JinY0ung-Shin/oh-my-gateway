"""Coverage tests for admin routes — fills gaps in endpoint testing."""

import os
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def admin_client():
    """FastAPI TestClient with admin auth bypassed."""
    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


class TestAdminPage:
    def test_get_admin_page(self, admin_client):
        r = admin_client.get("/admin")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "CLAUDE CODE GATEWAY" in r.text

    def test_get_admin_page_includes_integrity_and_crossorigin_for_cdn_assets(self, admin_client):
        r = admin_client.get("/admin")

        assert r.status_code == 200
        assert r.text.count('integrity="sha384-') >= 7
        assert r.text.count('crossorigin="anonymous"') >= 7
        assert "https://cdn.jsdelivr.net/npm/alpinejs@3.14.8/dist/cdn.min.js" in r.text
        assert "https://cdn.jsdelivr.net/npm/codemirror@5.65.18/lib/codemirror.min.js" in r.text

    def test_admin_page_script_has_no_empty_catch_blocks(self, admin_client):
        r = admin_client.get("/admin")

        assert r.status_code == 200
        assert re.search(r"catch\s*\(e\)\s*\{\s*\}", r.text) is None

    def test_admin_page_script_uses_visible_error_handling_for_async_loads(self, admin_client):
        r = admin_client.get("/admin")

        assert r.status_code == 200
        assert "Failed to load summary" in r.text
        assert "Failed to load metrics" in r.text
        assert "Failed to load full message" in r.text


class TestAdminChatPage:
    def test_get_admin_chat_page(self, admin_client):
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "GATEWAY CHAT" in r.text

    def test_admin_chat_page_supports_multi_select_ask_questions(self, admin_client):
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "data-multiple" in r.text
        assert "ask-option-marker" in r.text
        assert "aria-pressed" in r.text
        assert "questions = [argsObj]" in r.text
        assert "JSON.stringify(answersByQuestion)" in r.text


class TestAdminAuth:
    def test_logout(self, admin_client):
        r = admin_client.post("/admin/api/logout")
        assert r.status_code == 200

    def test_status(self, admin_client):
        r = admin_client.get("/admin/api/status")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data


class TestAdminSummary:
    def test_summary(self, admin_client):
        r = admin_client.get("/admin/api/summary")
        assert r.status_code == 200
        data = r.json()
        assert "health" in data
        assert "models" in data
        assert "sessions" in data
        assert "auth" in data


class TestAdminFiles:
    def test_list_files(self, admin_client):
        r = admin_client.get("/admin/api/files")
        assert r.status_code == 200
        data = r.json()
        assert "files" in data

    def test_read_file_not_found(self, admin_client):
        r = admin_client.get("/admin/api/files/.claude/agents/missing.md")
        assert r.status_code in (403, 404)

    def test_read_file_outside_allowlist(self, admin_client):
        r = admin_client.get("/admin/api/files/secret.env")
        assert r.status_code == 403


class TestAdminLogs:
    def test_get_logs(self, admin_client):
        r = admin_client.get("/admin/api/logs")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "stats" in data

    def test_get_logs_with_filters(self, admin_client):
        r = admin_client.get("/admin/api/logs?endpoint=/health&status=200&limit=10&offset=0")
        assert r.status_code == 200

    def test_get_logs_with_status_class(self, admin_client):
        r = admin_client.get("/admin/api/logs?status=4xx")
        assert r.status_code == 200


class TestAdminRateLimits:
    def test_get_rate_limits(self, admin_client):
        r = admin_client.get("/admin/api/rate-limits")
        assert r.status_code == 200
        data = r.json()
        assert "snapshot" in data


class TestAdminRuntimeConfig:
    def test_get_runtime_config(self, admin_client):
        r = admin_client.get("/admin/api/runtime-config")
        assert r.status_code == 200
        data = r.json()
        assert "settings" in data

    def test_update_runtime_config(self, admin_client):
        r = admin_client.patch(
            "/admin/api/runtime-config",
            json={"key": "default_max_turns", "value": 5},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "updated"

    def test_update_runtime_config_invalid_key(self, admin_client):
        r = admin_client.patch(
            "/admin/api/runtime-config",
            json={"key": "nonexistent_key", "value": 1},
        )
        assert r.status_code == 400

    def test_reset_runtime_config(self, admin_client):
        r = admin_client.post("/admin/api/runtime-config/reset?key=default_max_turns")
        assert r.status_code == 200

    def test_reset_all_runtime_config(self, admin_client):
        r = admin_client.post("/admin/api/runtime-config/reset")
        assert r.status_code == 200
        assert r.json()["status"] == "all_reset"

    def test_reset_invalid_key(self, admin_client):
        r = admin_client.post("/admin/api/runtime-config/reset?key=nonexistent")
        assert r.status_code == 400


class TestAdminSystemPrompt:
    def test_get_system_prompt(self, admin_client):
        r = admin_client.get("/admin/api/system-prompt")
        assert r.status_code == 200
        data = r.json()
        assert "mode" in data
        assert "prompt" in data

    def test_set_system_prompt(self, admin_client):
        r = admin_client.put(
            "/admin/api/system-prompt",
            json={"prompt": "You are a test assistant."},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "updated"

    def test_set_empty_system_prompt(self, admin_client):
        r = admin_client.put(
            "/admin/api/system-prompt",
            json={"prompt": "   "},
        )
        assert r.status_code == 422

    def test_reset_system_prompt(self, admin_client):
        r = admin_client.delete("/admin/api/system-prompt")
        assert r.status_code == 200
        assert r.json()["status"] == "reset"


class TestAdminSessionMessages:
    def test_session_messages_not_found(self, admin_client):
        r = admin_client.get("/admin/api/sessions/nonexistent/messages")
        assert r.status_code == 404

    def test_session_messages_existing(self, admin_client, isolated_session_manager):
        from src.models import Message
        from src.session_manager import session_manager

        session = session_manager.get_or_create_session("msg-test")
        session.add_messages([Message(role="user", content="hello")])

        r = admin_client.get("/admin/api/sessions/msg-test/messages")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["messages"][0]["role"] == "user"

    def test_delete_session(self, admin_client, isolated_session_manager):
        from src.session_manager import session_manager

        session_manager.get_or_create_session("del-test")

        r = admin_client.delete("/admin/api/sessions/del-test")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

    def test_delete_session_not_found(self, admin_client):
        r = admin_client.delete("/admin/api/sessions/nonexistent")
        assert r.status_code == 404
