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
        assert "OH MY GATEWAY" in r.text

    def test_get_admin_page_includes_integrity_and_crossorigin_for_cdn_assets(self, admin_client):
        r = admin_client.get("/admin")

        assert r.status_code == 200
        # SRI-pinned CDN assets carry both attributes. CodeMirror was dropped
        # when the admin file/skill editors were removed; Alpine.js remains.
        assert r.text.count('integrity="sha384-') >= 1
        assert r.text.count('crossorigin="anonymous"') >= 1
        # The Alpine.js CDN <script> is loaded with SRI + crossorigin pinning.
        alpine_tag = re.search(r"<script[^>]*alpinejs@3\.14\.8[^>]*>", r.text)
        assert alpine_tag is not None
        assert 'integrity="sha384-' in alpine_tag.group(0)
        assert 'crossorigin="anonymous"' in alpine_tag.group(0)

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
    def test_build_chat_page_returns_html(self):
        """Direct smoke test of the static builder, independent of routing."""
        from src.chat_page import build_chat_page

        html = build_chat_page()
        assert isinstance(html, str)
        assert "GATEWAY CHAT" in html
        assert html.lstrip().startswith("<!DOCTYPE html>")

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

    def test_admin_chat_page_renders_reasoning_stream_events(self, admin_client):
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "thinking-panel" in r.text
        assert "response.reasoning_text.delta" in r.text
        assert "response.reasoning_summary_text.delta" in r.text
        assert "extractReasoningTexts" in r.text

    def test_admin_chat_page_splits_text_bubbles_around_tool_events(self, admin_client):
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "pendingTextSeparator" not in r.text
        assert "let activeBubble = null" in r.text
        assert "activeBubbleText += evt.delta" in r.text

        # A tool_use still finalizes the current text bubble before its card is
        # rendered, so text segments split by a tool call stay visually separate.
        tool_use_idx = r.text.index("if (type === 'response.tool_use')")
        tool_use_finalize_idx = r.text.index("finalizeActiveBubble();", tool_use_idx)
        tool_use_render_idx = r.text.index("renderToolUse(evt)", tool_use_idx)
        assert tool_use_finalize_idx < tool_use_render_idx

    def test_admin_chat_page_pairs_tool_result_with_its_tool_use(self, admin_client):
        """A tool_result merges back into the card of the call it answers
        (looked up by tool_use_id) instead of rendering a disconnected card."""
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "toolEventsById[evt.tool_use_id]" in r.text
        assert "function attachToolResult(" in r.text
        # tool_result no longer unconditionally finalizes + appends a sibling card;
        # the merge path runs before any standalone fallback.
        tr_idx = r.text.index("if (type === 'response.tool_result')")
        assert r.text.index("attachToolResult(card", tr_idx) < r.text.index(
            "createToolCard({", tr_idx
        )

    def test_admin_chat_page_renders_per_tool_badges(self, admin_client):
        """Each tool type — and the Agent/Task tool in particular — gets a
        distinct category badge rather than a generic 'TOOL' label."""
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "function toolMeta(" in r.text
        assert "cat-agent" in r.text
        assert "tool-agent" in r.text
        assert "cat-bash" in r.text

    def test_admin_chat_page_nests_task_lifecycle_under_agent(self, admin_client):
        """Subagent lifecycle events collapse into one in-place status line that
        nests under the spawning agent card."""
        r = admin_client.get("/admin/chat")

        assert r.status_code == 200
        assert "function upsertTaskStatus(" in r.text
        assert "task-status-line" in r.text
        assert "parent_tool_use_id || evt.tool_use_id" in r.text

    def test_admin_chat_page_serves_login_gate_to_unauthenticated_clients(self):
        """Anonymous GET /admin/chat should return 200 with the inline auth gate."""
        with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
            from src.main import app

            client = TestClient(app)
            r = client.get("/admin/chat")

        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert 'id="auth-overlay"' in r.text
        assert "/admin/api/login" in r.text
        assert "ACCESS TERMINAL" in r.text


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
