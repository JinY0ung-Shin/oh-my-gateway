"""Create-only named prompt contract used by external admin UIs."""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.named_prompt_create import create_named_prompt
from src.system_prompt import get_named_prompt, save_named_prompt


@pytest.fixture
def prompt_dir(tmp_path):
    prompts = tmp_path / "prompts"
    with patch("src.system_prompt._PROMPTS_DIR", prompts):
        yield prompts


@pytest.fixture
def admin_client(prompt_dir):
    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        client = TestClient(app)
        yield client
        app.dependency_overrides.pop(require_admin, None)


def test_service_create_only_preserves_existing_content(prompt_dir):
    first = create_named_prompt("safe-name", "first version")
    assert first["content"] == "first version"

    with pytest.raises(FileExistsError):
        create_named_prompt("safe-name", "must not overwrite")

    saved = get_named_prompt("safe-name")
    assert saved is not None
    assert saved["content"] == "first version"


def test_post_creates_but_duplicate_returns_409(admin_client):
    first = admin_client.post(
        "/admin/api/prompts/atomic",
        json={"content": "original"},
    )
    assert first.status_code == 200
    assert first.json()["content"] == "original"

    duplicate = admin_client.post(
        "/admin/api/prompts/atomic",
        json={"content": "replacement"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "Prompt already exists: atomic"

    loaded = admin_client.get("/admin/api/prompts/atomic")
    assert loaded.status_code == 200
    assert loaded.json()["content"] == "original"


def test_legacy_put_remains_explicit_upsert(admin_client):
    create = admin_client.post(
        "/admin/api/prompts/editable",
        json={"content": "v1"},
    )
    assert create.status_code == 200

    update = admin_client.put(
        "/admin/api/prompts/editable",
        json={"content": "v2"},
    )
    assert update.status_code == 200
    assert update.json()["content"] == "v2"
    assert get_named_prompt("editable")["content"] == "v2"


def test_post_reuses_gateway_validation(admin_client):
    invalid = admin_client.post(
        "/admin/api/prompts/-bad-start",
        json={"content": "content"},
    )
    assert invalid.status_code == 422

    blank = admin_client.post(
        "/admin/api/prompts/new-name",
        json={"content": "   "},
    )
    assert blank.status_code == 422
