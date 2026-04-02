"""Tests for named prompts management — service logic and API endpoints."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.system_prompt import (
    _validate_prompt_name,
    activate_named_prompt,
    delete_named_prompt,
    get_active_prompt_name,
    get_named_prompt,
    list_named_prompts,
    save_named_prompt,
)


# ---------------------------------------------------------------------------
# Unit tests — service logic
# ---------------------------------------------------------------------------


class TestValidatePromptName:
    def test_valid_names(self):
        assert _validate_prompt_name("my-prompt") == "my-prompt"
        assert _validate_prompt_name("test_v2") == "test_v2"
        assert _validate_prompt_name("Prompt 1") == "Prompt 1"
        assert _validate_prompt_name("a") == "a"

    def test_empty_name(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_prompt_name("")

    def test_invalid_characters(self):
        with pytest.raises(ValueError):
            _validate_prompt_name("../hack")

    def test_starts_with_special(self):
        with pytest.raises(ValueError):
            _validate_prompt_name("-start")


class TestNamedPromptsCRUD:
    @pytest.fixture(autouse=True)
    def setup_prompts_dir(self, tmp_path):
        """Patch _PROMPTS_DIR to use temp directory."""
        self.prompts_dir = tmp_path / "prompts"
        with patch("src.system_prompt._PROMPTS_DIR", self.prompts_dir):
            yield

    def test_list_empty(self):
        assert list_named_prompts() == []

    def test_save_and_get(self):
        data = save_named_prompt("test-prompt", "You are a helpful assistant")
        assert data["name"] == "test-prompt"
        assert data["content"] == "You are a helpful assistant"
        assert data["created_at"] is not None
        assert data["updated_at"] is not None

        loaded = get_named_prompt("test-prompt")
        assert loaded is not None
        assert loaded["content"] == "You are a helpful assistant"

    def test_list_after_save(self):
        save_named_prompt("alpha", "Content A")
        save_named_prompt("beta", "Content B")
        prompts = list_named_prompts()
        names = [p["name"] for p in prompts]
        assert "alpha" in names
        assert "beta" in names

    def test_update_existing(self):
        data1 = save_named_prompt("my-prompt", "Version 1")
        data2 = save_named_prompt("my-prompt", "Version 2")
        assert data2["content"] == "Version 2"
        assert data2["created_at"] == data1["created_at"]
        assert data2["updated_at"] >= data1["updated_at"]

    def test_delete(self):
        save_named_prompt("to-delete", "Some content")
        assert delete_named_prompt("to-delete") is True
        assert get_named_prompt("to-delete") is None

    def test_delete_nonexistent(self):
        assert delete_named_prompt("nope") is False

    def test_get_nonexistent(self):
        assert get_named_prompt("nope") is None

    def test_save_empty_content(self):
        with pytest.raises(ValueError, match="empty"):
            save_named_prompt("bad", "")

    def test_save_invalid_name(self):
        with pytest.raises(ValueError):
            save_named_prompt("../hack", "content")


class TestActivatePrompt:
    @pytest.fixture(autouse=True)
    def setup_dirs(self, tmp_path):
        self.prompts_dir = tmp_path / "prompts"
        self.data_dir = tmp_path / "data"
        persist_file = self.data_dir / "system_prompt.json"
        with (
            patch("src.system_prompt._PROMPTS_DIR", self.prompts_dir),
            patch("src.system_prompt._DATA_DIR", self.data_dir),
            patch("src.system_prompt._PERSIST_FILE", persist_file),
        ):
            yield

    def test_activate(self):
        save_named_prompt("active-test", "Custom prompt content")
        activate_named_prompt("active-test")
        assert get_active_prompt_name() == "active-test"

    def test_activate_nonexistent(self):
        with pytest.raises(ValueError, match="not found"):
            activate_named_prompt("nope")


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


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


class TestPromptEndpoints:
    @pytest.fixture(autouse=True)
    def setup_dirs(self, tmp_path):
        self.prompts_dir = tmp_path / "prompts"
        self.data_dir = tmp_path / "data"
        persist_file = self.data_dir / "system_prompt.json"
        with (
            patch("src.system_prompt._PROMPTS_DIR", self.prompts_dir),
            patch("src.system_prompt._DATA_DIR", self.data_dir),
            patch("src.system_prompt._PERSIST_FILE", persist_file),
        ):
            yield

    def test_list_empty(self, admin_client):
        r = admin_client.get("/admin/api/prompts")
        assert r.status_code == 200
        data = r.json()
        assert data["prompts"] == []

    def test_create_and_get(self, admin_client):
        r = admin_client.put(
            "/admin/api/prompts/my-test",
            json={"content": "Hello world prompt"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "my-test"

        r = admin_client.get("/admin/api/prompts/my-test")
        assert r.status_code == 200
        assert r.json()["content"] == "Hello world prompt"

    def test_list_after_create(self, admin_client):
        admin_client.put("/admin/api/prompts/p1", json={"content": "Prompt 1"})
        admin_client.put("/admin/api/prompts/p2", json={"content": "Prompt 2"})
        r = admin_client.get("/admin/api/prompts")
        assert r.status_code == 200
        names = [p["name"] for p in r.json()["prompts"]]
        assert "p1" in names
        assert "p2" in names

    def test_delete(self, admin_client):
        admin_client.put("/admin/api/prompts/deleteme", json={"content": "bye"})
        r = admin_client.delete("/admin/api/prompts/deleteme")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

        r = admin_client.get("/admin/api/prompts/deleteme")
        assert r.status_code == 404

    def test_delete_nonexistent(self, admin_client):
        r = admin_client.delete("/admin/api/prompts/nope")
        assert r.status_code == 404

    def test_get_nonexistent(self, admin_client):
        r = admin_client.get("/admin/api/prompts/nope")
        assert r.status_code == 404

    def test_activate(self, admin_client):
        admin_client.put("/admin/api/prompts/act", json={"content": "Activate me"})
        r = admin_client.post("/admin/api/prompts/act/activate")
        assert r.status_code == 200
        assert r.json()["status"] == "activated"
        assert r.json()["name"] == "act"

    def test_activate_nonexistent(self, admin_client):
        r = admin_client.post("/admin/api/prompts/nope/activate")
        assert r.status_code == 404

    def test_active_name_in_system_prompt(self, admin_client):
        admin_client.put("/admin/api/prompts/sp1", json={"content": "System 1"})
        admin_client.post("/admin/api/prompts/sp1/activate")
        r = admin_client.get("/admin/api/system-prompt")
        assert r.status_code == 200
        assert r.json()["active_name"] == "sp1"

    def test_active_name_in_prompts_list(self, admin_client):
        admin_client.put("/admin/api/prompts/sp2", json={"content": "System 2"})
        admin_client.post("/admin/api/prompts/sp2/activate")
        r = admin_client.get("/admin/api/prompts")
        assert r.status_code == 200
        assert r.json()["active_name"] == "sp2"

    def test_empty_content_rejected(self, admin_client):
        r = admin_client.put("/admin/api/prompts/bad", json={"content": ""})
        assert r.status_code == 422

    def test_get_invalid_name(self, admin_client):
        r = admin_client.get("/admin/api/prompts/-bad-start")
        assert r.status_code == 422
