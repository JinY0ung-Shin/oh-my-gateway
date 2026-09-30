"""Versioned system-prompt library: versions, deploy/rollback, deploy log, admin API."""

import json
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src import prompt_library, system_prompt


@pytest.fixture
def lib(tmp_path):
    data = tmp_path / "data"
    with (
        patch.object(system_prompt, "_DATA_DIR", data),
        patch.object(system_prompt, "_PERSIST_FILE", data / "system_prompt.json"),
        patch.object(system_prompt, "_PROMPTS_DIR", data / "prompts"),
        patch.object(system_prompt, "_runtime_prompt", None),
        patch.object(system_prompt, "_runtime_prompt_raw", None),
        patch.object(system_prompt, "_default_prompt", None),
        patch.object(system_prompt, "_default_prompt_raw", None),
        patch.object(system_prompt, "_active_prompt_name", None),
        patch.object(system_prompt, "_active_meta", {}),
    ):
        yield data


def test_legacy_prompt_file_reads_as_version_one(lib):
    (lib / "prompts").mkdir(parents=True)
    (lib / "prompts" / "old.json").write_text(
        json.dumps({"name": "old", "content": "legacy text", "created_at": "t0", "updated_at": "t1"})
    )
    data = prompt_library.get_prompt("old")
    assert [v["version"] for v in data["versions"]] == [1]
    assert data["versions"][0]["content"] == "legacy text"
    assert data["content"] == "legacy text"


def test_versions_are_append_only_with_author_and_note(lib):
    prompt_library.create_prompt("ops", "v1 text", message="first", author="kim")
    prompt_library.commit_version("ops", "v2 text", message="tone", author="lee", base_version=1)
    data = prompt_library.get_prompt("ops")
    assert [(v["version"], v["content"], v["author"], v["message"]) for v in data["versions"]] == [
        (1, "v1 text", "kim", "first"),
        (2, "v2 text", "lee", "tone"),
    ]
    # legacy readers still see the latest text in ``content``
    assert system_prompt.get_named_prompt("ops")["content"] == "v2 text"


def test_versions_carry_stable_git_style_commit_ids(lib):
    (lib / "prompts").mkdir(parents=True)
    (lib / "prompts" / "old.json").write_text(json.dumps({"name": "old", "content": "legacy", "updated_at": "t1"}))
    legacy = prompt_library.get_prompt("old")["versions"][0]["sha"]
    assert len(legacy) == 40 and prompt_library.get_prompt("old")["versions"][0]["sha"] == legacy

    prompt_library.create_prompt("ops", "a", author="kim")
    prompt_library.commit_version("ops", "b", author="lee")
    v1, v2 = prompt_library.get_prompt("ops")["versions"]
    assert v1["sha"] != v2["sha"] and v1["parent"] is None and v2["parent"] == v1["sha"]
    stored = json.loads((lib / "prompts" / "ops.json").read_text())["versions"]
    assert [v["sha"] for v in stored] == [v1["sha"], v2["sha"]]
    assert all("parent" not in v for v in stored), "parent is derived on read, never stored"
    summary = next(p for p in prompt_library.list_prompts() if p["name"] == "ops")
    assert summary["latest_sha"] == v2["sha"]
    assert prompt_library.deploy("ops", 1)["sha"] == v1["sha"]


def test_identical_content_is_no_change(lib):
    prompt_library.create_prompt("ops", "same")
    with pytest.raises(prompt_library.NoChange):
        prompt_library.commit_version("ops", "  same \n")


def test_stale_base_version_conflicts(lib):
    prompt_library.create_prompt("ops", "a")
    prompt_library.commit_version("ops", "b", base_version=1)
    with pytest.raises(prompt_library.VersionConflict) as exc:
        prompt_library.commit_version("ops", "c", base_version=1)
    assert exc.value.latest == 2


def test_create_is_create_only(lib):
    prompt_library.create_prompt("ops", "a")
    with pytest.raises(prompt_library.PromptExists):
        prompt_library.create_prompt("ops", "b")
    assert prompt_library.get_prompt("ops")["content"] == "a"


def test_deploy_points_live_at_version_and_logs_rollback(lib):
    prompt_library.create_prompt("ops", "one {{LANGUAGE}}")
    prompt_library.commit_version("ops", "two")
    first = prompt_library.deploy("ops", 2, author="kim", note="ship")
    assert first["action"] == "deploy" and first["from"] == "preset"
    ref = system_prompt.get_live_ref()
    assert (ref["mode"], ref["name"], ref["version"], ref["deployed_by"]) == ("custom", "ops", 2, "kim")
    assert system_prompt.get_system_prompt() == "two"

    back = prompt_library.deploy("ops", 1, author="lee")
    assert back["action"] == "rollback" and back["from"] == "ops@v2"
    assert "{{LANGUAGE}}" not in system_prompt.get_system_prompt()  # resolved like before

    log = prompt_library.list_deployments()
    assert [(e["action"], e["version"], e["by"]) for e in log] == [("rollback", 1, "lee"), ("deploy", 2, "kim")]
    # the live pointer survives a restart
    persisted = json.loads((lib / "system_prompt.json").read_text())
    assert (persisted["active_name"], persisted["active_version"]) == ("ops", 1)


def test_live_pointer_restored_on_startup(lib):
    prompt_library.create_prompt("ops", "text")
    prompt_library.deploy("ops", 1, author="kim")
    with patch.object(system_prompt, "_runtime_prompt", None), patch.object(
        system_prompt, "_active_meta", {}
    ):
        system_prompt.load_default_prompt("")
        assert system_prompt.get_live_ref()["version"] == 1
        assert system_prompt.get_live_ref()["deployed_by"] == "kim"


def test_reset_logs_and_live_prompt_cannot_be_deleted(lib):
    prompt_library.create_prompt("ops", "text")
    prompt_library.deploy("ops", 1)
    with pytest.raises(prompt_library.PromptIsLive):
        prompt_library.delete_prompt("ops")
    entry = prompt_library.reset_to_default(author="kim")
    assert entry["action"] == "reset" and entry["from"] == "ops@v1"
    assert system_prompt.get_live_ref()["mode"] == "preset"
    prompt_library.delete_prompt("ops")
    with pytest.raises(prompt_library.PromptNotFound):
        prompt_library.get_prompt("ops")


def test_import_live_adopts_untracked_override_without_changing_it(lib):
    system_prompt.set_system_prompt("hand edited")
    assert system_prompt.get_live_ref()["name"] is None
    prompt_library.import_live("current", author="kim")
    ref = system_prompt.get_live_ref()
    assert (ref["name"], ref["version"]) == ("current", 1)
    assert system_prompt.get_system_prompt() == "hand edited"
    assert prompt_library.list_deployments() == []  # not a deploy: nothing new sessions see changed


def test_legacy_save_appends_a_version_instead_of_overwriting(lib):
    system_prompt.save_named_prompt("ops", "a")
    system_prompt.save_named_prompt("ops", "b")
    system_prompt.save_named_prompt("ops", "b")  # identical → no new version
    assert [v["content"] for v in prompt_library.get_prompt("ops")["versions"]] == ["a", "b"]
    system_prompt.activate_named_prompt("ops")
    assert system_prompt.get_live_ref()["version"] == 2


def test_analyze_flags_unknown_and_spaced_placeholders(lib):
    report = prompt_library.analyze("Reply in {{LANGUAGE}} from {{WORKING_DIRECTORY}}. {{TEAM}} {{ SHELL }}")
    by_name = {p["name"]: p for p in report["placeholders"]}
    assert by_name["LANGUAGE"]["known"] and by_name["LANGUAGE"]["exact"]
    assert not by_name["TEAM"]["known"]
    assert by_name["SHELL"]["known"] and not by_name["SHELL"]["exact"]  # resolver needs {{SHELL}}
    assert "/workspace/<session>" in report["rendered"] and "{{LANGUAGE}}" not in report["rendered"]


async def test_new_session_records_which_version_it_snapshotted(lib, tmp_path):
    from src.backends.base import ResolvedModel
    from src.session_guard import acquire_session_preflight
    from src.session_manager import Session

    prompt_library.create_prompt("ops", "from {{WORKING_DIRECTORY}}")
    prompt_library.deploy("ops", 1)
    session = Session(session_id="s1")
    resolved = ResolvedModel(public_model="opus", backend="claude", provider_model="opus")
    pf = await acquire_session_preflight(session, resolved, "s1", workspace=str(tmp_path))
    session.lock.release()
    assert pf.is_new
    assert session.base_system_prompt == f"from {tmp_path}"
    assert (session.base_prompt_ref["name"], session.base_prompt_ref["version"]) == ("ops", 1)

    # a later deploy does not touch the snapshot the session already took
    prompt_library.commit_version("ops", "newer")
    prompt_library.deploy("ops", 2)
    assert session.base_prompt_ref["version"] == 1


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(lib):
    with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
        from src.admin_auth import require_admin
        from src.main import app

        app.dependency_overrides[require_admin] = lambda: True
        yield TestClient(app)
        app.dependency_overrides.pop(require_admin, None)


BASE = "/admin/api/prompt-library"


def test_api_create_commit_deploy_flow_is_attributed(client):
    actor = {"X-Admin-Actor": "Kim Admin"}
    r = client.post(f"{BASE}/prompts", json={"name": "ops", "content": "v1", "message": "init"}, headers=actor)
    assert r.status_code == 200 and r.json()["versions"][0]["author"] == "Kim Admin"
    r = client.post(f"{BASE}/prompts/ops/versions", json={"content": "v2", "message": "tone", "base_version": 1}, headers=actor)
    assert r.status_code == 200 and r.json()["versions"][-1]["version"] == 2

    r = client.post(f"{BASE}/prompts/ops/deploy", json={"version": 2, "expected_live": "preset"}, headers=actor)
    assert r.status_code == 200, r.text
    assert r.json()["live"]["label"] == "ops@v2"
    assert r.json()["live"]["sha"] == client.get(f"{BASE}/prompts/ops").json()["versions"][1]["sha"]

    overview = client.get(BASE).json()
    assert overview["live"]["deployed_by"] == "Kim Admin"
    assert overview["prompts"][0]["live_version"] == 2
    log = client.get(f"{BASE}/deployments").json()["deployments"]
    assert log[0]["by"] == "Kim Admin" and log[0]["version"] == 2


def test_api_deploy_refuses_when_live_changed_meanwhile(client):
    client.post(f"{BASE}/prompts", json={"name": "ops", "content": "v1"})
    client.post(f"{BASE}/prompts/ops/deploy", json={"version": 1})
    r = client.post(f"{BASE}/prompts/ops/deploy", json={"version": 1, "expected_live": "preset"})
    assert r.status_code == 409 and r.json()["code"] == "live_changed" and r.json()["live"] == "ops@v1"


def test_api_errors_are_specific(client):
    client.post(f"{BASE}/prompts", json={"name": "ops", "content": "v1"})
    assert client.post(f"{BASE}/prompts", json={"name": "ops", "content": "x"}).status_code == 409
    r = client.post(f"{BASE}/prompts/ops/versions", json={"content": "v1"})
    assert r.status_code == 422 and r.json()["code"] == "no_change"
    client.post(f"{BASE}/prompts/ops/versions", json={"content": "v2"})
    r = client.post(f"{BASE}/prompts/ops/versions", json={"content": "v3", "base_version": 1})
    assert r.status_code == 409 and r.json()["latest_version"] == 2
    client.post(f"{BASE}/prompts/ops/deploy", json={"version": 2})
    assert client.delete(f"{BASE}/prompts/ops").json()["code"] == "live"
    assert client.post(f"{BASE}/prompts/ops/deploy", json={"version": 9}).status_code == 404
    assert client.get(f"{BASE}/prompts/nope").status_code == 404


def test_api_legacy_direct_edit_shows_as_untracked_and_can_be_imported(client):
    client.put("/admin/api/system-prompt", json={"prompt": "raw edit"})
    live = client.get(BASE).json()["live"]
    assert live["label"] == "untracked" and live["content"] == "raw edit"
    r = client.post(f"{BASE}/import-live", json={"name": "current"})
    assert r.status_code == 200 and r.json()["live"]["label"] == "current@v1"
    actions = [e["action"] for e in client.get(f"{BASE}/deployments").json()["deployments"]]
    assert actions == ["direct"]


class _FakeBackend:
    def __init__(self):
        self.kwargs = None

    async def create_client(self, **kwargs):
        self.kwargs = kwargs
        return object()

    async def run_completion_with_client(self, client, prompt, session):
        # The real backend yields ``_convert_message`` dicts whose content blocks
        # are still SDK objects — reproduce exactly that, not raw SDK messages.
        from claude_agent_sdk import TextBlock, ToolUseBlock

        yield {"type": "system", "subtype": "init", "data": {}}
        yield {"type": "assistant", "content": [ToolUseBlock(id="t1", name="Read", input={})]}
        yield {"type": "assistant", "content": [TextBlock(text="안녕하세요")]}
        yield {
            "type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }


def test_api_try_runs_the_draft_as_the_base_prompt(client, tmp_path):
    from src.backends.base import ResolvedModel

    backend = _FakeBackend()
    resolved = ResolvedModel(public_model="sonnet", backend="claude", provider_model="sonnet")
    with (
        patch("src.routes.deps.resolve_and_get_backend", return_value=(resolved, backend)),
        patch("src.routes.deps.validate_backend_auth_or_raise"),
        patch("src.workspace_manager.workspace_manager.resolve", return_value=tmp_path / "ws"),
        patch("src.workspace_manager.workspace_manager.cleanup_temp_workspace"),
    ):
        (tmp_path / "ws").mkdir()
        r = client.post(
            f"{BASE}/try",
            json={"content": "Draft in {{LANGUAGE}} at {{WORKING_DIRECTORY}}", "message": "hi"},
        )
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["start", "tool", "text", "result", "done"]
    assert events[0]["prompt"] == "draft" and events[2]["text"] == "안녕하세요"
    base = backend.kwargs["_custom_base"]
    assert base.startswith("Draft in ") and str(tmp_path / "ws") in base and "{{" not in base
    assert backend.kwargs["disallowed_tools"] == ["AskUserQuestion"]
