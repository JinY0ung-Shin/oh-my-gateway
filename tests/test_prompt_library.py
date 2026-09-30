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
    # Not a deploy (new sessions get the same text), but the live ref moved: logged.
    assert [e["action"] for e in prompt_library.list_deployments()] == ["import"]


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
    assert actions == ["import", "direct"]


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


# ---------------------------------------------------------------------------
# Concurrency: one publish unit, one mutation boundary
# ---------------------------------------------------------------------------

import threading  # noqa: E402


class _PausingPath:
    """Stand-in for ``_PERSIST_FILE`` whose write blocks until released."""

    def __init__(self, real, entered, release):
        self._real, self._entered, self._release = real, entered, release

    def __getattr__(self, name):
        return getattr(self._real, name)

    def write_text(self, *args, **kwargs):
        self._entered.set()
        assert self._release.wait(5)
        return self._real.write_text(*args, **kwargs)


def test_snapshot_never_pairs_one_deploys_text_with_anothers_ref(lib):
    prompt_library.create_prompt("ops", "one")
    prompt_library.commit_version("ops", "two")
    prompt_library.deploy("ops", 1)
    entered, release = threading.Event(), threading.Event()
    real = system_prompt._PERSIST_FILE
    with patch.object(system_prompt, "_PERSIST_FILE", _PausingPath(real, entered, release)):
        writer = threading.Thread(target=prompt_library.deploy, args=("ops", 2))
        writer.start()
        assert entered.wait(5)  # the writer is mid-publish, inside the lock
        seen = []
        reader = threading.Thread(target=lambda: seen.append(system_prompt.get_live_snapshot()))
        reader.start()
        reader.join(0.2)
        assert reader.is_alive(), "a reader must wait for the publish, not see half of it"
        release.set()
        writer.join(5)
        reader.join(5)
    text, ref = seen[0]
    assert (text, ref["version"]) == ("two", 2)


def test_snapshot_pairs_stay_consistent_under_deploy_and_reset_churn(lib):
    prompt_library.create_prompt("ops", "one")
    prompt_library.commit_version("ops", "two")
    expected = {1: "one", 2: "two"}
    stop = threading.Event()
    bad = []

    def read():
        while not stop.is_set():
            text, ref = system_prompt.get_live_snapshot()
            if ref["mode"] == "custom":
                if expected.get(ref["version"]) != text:
                    bad.append((text, ref))
            elif text is not None:
                bad.append((text, ref))

    readers = [threading.Thread(target=read) for _ in range(3)]
    for t in readers:
        t.start()
    for i in range(150):
        if i % 5 == 4:
            prompt_library.reset_to_default()
        else:
            prompt_library.deploy("ops", 1 + i % 2)
    stop.set()
    for t in readers:
        t.join(5)
    assert bad == []


def test_direct_edit_waits_on_the_mutation_boundary(lib):
    done = threading.Event()
    with system_prompt.mutation_lock:  # e.g. a deploy mid expected_live check
        t = threading.Thread(target=lambda: (system_prompt.set_system_prompt("direct"), done.set()))
        t.start()
        assert not done.wait(0.2), "a direct edit must not slip between check and deploy"
    t.join(5)
    assert done.is_set() and system_prompt.get_system_prompt() == "direct"


def test_import_cannot_overwrite_a_deploy_that_lands_meanwhile(lib):
    system_prompt.set_system_prompt("hand edited")
    prompt_library.create_prompt("other", "newer prompt")
    entered, release = threading.Event(), threading.Event()
    real_create = prompt_library.create_prompt

    def slow_create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_create(*args, **kwargs)

    with patch.object(prompt_library, "create_prompt", slow_create):
        importer = threading.Thread(target=prompt_library.import_live, args=("current",))
        importer.start()
        assert entered.wait(5)
        deployer = threading.Thread(target=prompt_library.deploy, args=("other", 1))
        deployer.start()
        deployer.join(0.2)
        assert deployer.is_alive(), "the deploy must wait for the import transaction"
        release.set()
        importer.join(5)
        deployer.join(5)
    ref = system_prompt.get_live_ref()
    assert (ref["name"], ref["version"]) == ("other", 1), "the later deploy wins, never the import"
    assert system_prompt.get_system_prompt() == "newer prompt"
    assert [e["action"] for e in prompt_library.list_deployments()] == ["deploy", "import"]


def test_session_usage_copies_sessions_under_the_manager_lock(lib):
    from src.routes.admin_prompt_library import _session_usage
    from src.session_manager import session_manager

    out = []
    with session_manager.lock:
        t = threading.Thread(target=lambda: out.append(_session_usage()))
        t.start()
        t.join(0.2)
        assert t.is_alive(), "session usage must read the dict under SessionManager.lock"
    t.join(5)
    assert out and "total" in out[0]


def test_legacy_delete_of_the_live_prompt_resets_under_the_boundary_and_logs(lib):
    prompt_library.create_prompt("ops", "text")
    prompt_library.deploy("ops", 1)
    assert system_prompt.delete_named_prompt("ops") is True
    assert system_prompt.get_live_ref()["mode"] == "preset"
    assert prompt_library.list_deployments()[0]["action"] == "reset"


# ---------------------------------------------------------------------------
# CAS identity: the revision, not the display label
# ---------------------------------------------------------------------------


def test_two_direct_edits_share_a_label_but_never_a_revision(lib):
    system_prompt.set_system_prompt("direct A")
    a = system_prompt.get_live_ref()
    system_prompt.set_system_prompt("direct B")
    b = system_prompt.get_live_ref()
    assert prompt_library._ref_label(a) == prompt_library._ref_label(b) == "untracked"
    assert a["revision"] != b["revision"]
    assert system_prompt.get_live_ref()["revision"] == b["revision"], "stable while nothing changes"


def test_api_deploy_and_reset_refuse_a_stale_revision_across_untracked_edits(client):
    client.post(f"{BASE}/prompts", json={"name": "ops", "content": "v1"})
    client.put("/admin/api/system-prompt", json={"prompt": "direct A"})
    seen = client.get(BASE).json()["live"]  # admin A opens the confirm dialog here
    client.put("/admin/api/system-prompt", json={"prompt": "direct B"})  # admin B

    r = client.post(
        f"{BASE}/prompts/ops/deploy",
        json={"version": 1, "expected_live": seen["label"], "expected_revision": seen["revision"]},
    )
    assert r.status_code == 409 and r.json()["code"] == "live_changed"
    assert r.json()["revision"] != seen["revision"]
    r = client.post(f"{BASE}/reset", json={"expected_revision": seen["revision"]})
    assert r.status_code == 409
    assert system_prompt.get_system_prompt() == "direct B", "B's edit must survive"

    fresh = client.get(BASE).json()["live"]
    r = client.post(f"{BASE}/prompts/ops/deploy", json={"version": 1, "expected_revision": fresh["revision"]})
    assert r.status_code == 200


def test_revision_survives_restart(lib):
    prompt_library.create_prompt("ops", "text")
    prompt_library.deploy("ops", 1)
    before = system_prompt.get_live_ref()["revision"]
    with patch.object(system_prompt, "_runtime_prompt", None), patch.object(system_prompt, "_active_meta", {}):
        system_prompt.load_default_prompt("")
        assert system_prompt.get_live_ref()["revision"] == before


def test_legacy_writes_and_deletes_are_attributed(client):
    actor = {"X-Admin-Actor": "legacy-admin"}
    assert client.post("/admin/api/prompts/ops", json={"content": "v1"}, headers=actor).status_code == 200
    assert client.put("/admin/api/prompts/ops", json={"content": "v2"}, headers=actor).status_code == 200
    versions = client.get(f"{BASE}/prompts/ops").json()["versions"]
    assert [v["author"] for v in versions] == ["legacy-admin", "legacy-admin"]

    client.put("/admin/api/system-prompt", json={"prompt": "direct"}, headers=actor)
    client.post("/admin/api/prompts/ops/activate", headers=actor)
    assert client.delete("/admin/api/prompts/ops", headers=actor).status_code == 200
    log = client.get(f"{BASE}/deployments").json()["deployments"]
    reset, deploy, direct = log[0], log[1], log[2]
    assert reset["action"] == "reset" and reset["by"] == "legacy-admin" and reset["from"] == "ops@v2"
    assert deploy["action"] == "deploy" and deploy["from"] == "untracked"
    assert direct["action"] == "direct" and direct["from"] == "preset", "a direct edit records what it replaced"
