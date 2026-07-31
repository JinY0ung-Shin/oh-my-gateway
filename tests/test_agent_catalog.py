"""Workspace-scope skills/subagents catalog + cached MCP health snapshot.

The catalog exists because ``/admin/api/plugins`` cannot see a user's own
``.claude/skills`` — a picker built from plugins alone hides skills the model can
still run. The health snapshot exists because per-server admin probes are not
poll-safe. Both are read-only and must never raise on malformed input.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src import agent_catalog, mcp_health


# ---------------------------------------------------------------- catalog


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_user_scope(monkeypatch):
    """Keep the developer's real ~/.claude out of these assertions."""
    monkeypatch.setattr(agent_catalog, "_user_scope_dir", lambda: None)


@pytest.fixture(autouse=True)
def _no_plugins(monkeypatch):
    monkeypatch.setattr(agent_catalog, "_plugin_entries", lambda kind: [])


def test_workspace_nested_skill_is_listed_with_frontmatter_description(tmp_path):
    _write(
        tmp_path / ".claude" / "skills" / "deep-research" / "SKILL.md",
        "---\nname: deep-research\ndescription: Multi-source research sweep\n---\n\nbody\n",
    )

    resources = agent_catalog.list_agent_resources(tmp_path)

    assert resources["skills"] == [
        {
            "name": "deep-research",
            "description": "Multi-source research sweep",
            "source": "project",
            "plugin": "",
        }
    ]


def test_workspace_flat_skill_and_agent_layouts(tmp_path):
    _write(tmp_path / ".claude" / "skills" / "triage.md", "---\ndescription: Sort issues\n---\n")
    _write(
        tmp_path / ".claude" / "agents" / "reviewer.md",
        "---\nname: reviewer\ndescription: Reviews diffs\n---\n",
    )

    resources = agent_catalog.list_agent_resources(tmp_path)

    assert [s["name"] for s in resources["skills"]] == ["triage"]
    # No frontmatter name -> filename is the key, matching how the CLI resolves it
    assert resources["skills"][0]["description"] == "Sort issues"
    assert resources["agents"][0] == {
        "name": "reviewer",
        "description": "Reviews diffs",
        "source": "project",
        "plugin": "",
    }


def test_multiline_description_is_collapsed_to_one_line(tmp_path):
    _write(
        tmp_path / ".claude" / "skills" / "wide" / "SKILL.md",
        "---\nname: wide\ndescription: >\n  first line\n  second line\n---\n",
    )

    (skill,) = agent_catalog.list_agent_resources(tmp_path)["skills"]

    assert skill["description"] == "first line second line"


def test_malformed_frontmatter_still_lists_the_skill(tmp_path):
    _write(tmp_path / ".claude" / "skills" / "broken" / "SKILL.md", "---\n: : :\nnope\n---\n")
    _write(tmp_path / ".claude" / "skills" / "plain.md", "no frontmatter at all\n")

    names = [s["name"] for s in agent_catalog.list_agent_resources(tmp_path)["skills"]]

    # A skill the model can run must appear even when we cannot describe it.
    assert names == ["broken", "plain"]


def test_symlinked_entries_are_skipped(tmp_path):
    real = tmp_path / "outside" / "SKILL.md"
    _write(real, "---\nname: outside\n---\n")
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "linked.md").symlink_to(real)

    assert agent_catalog.list_agent_resources(tmp_path)["skills"] == []


def test_missing_claude_dir_and_none_workspace_are_empty(tmp_path):
    assert agent_catalog.list_agent_resources(tmp_path) == {"skills": [], "agents": []}
    assert agent_catalog.list_agent_resources(None) == {"skills": [], "agents": []}


def test_project_scope_shadows_plugin_of_the_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(
        agent_catalog,
        "_plugin_entries",
        lambda kind: (
            [{"name": "triage", "description": "plugin one", "source": "plugin", "plugin": "octo"}]
            if kind == "skills"
            else []
        ),
    )
    _write(tmp_path / ".claude" / "skills" / "triage.md", "---\ndescription: mine\n---\n")

    (skill,) = agent_catalog.list_agent_resources(tmp_path)["skills"]

    # The definition that would actually run is the project one.
    assert (skill["source"], skill["description"]) == ("project", "mine")


def test_user_scope_only_when_setting_sources_include_user(tmp_path, monkeypatch):
    monkeypatch.undo()  # restore the real _user_scope_dir
    monkeypatch.setattr(agent_catalog, "_plugin_entries", lambda kind: [])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    _write(tmp_path / ".claude" / "skills" / "mine.md", "---\ndescription: user scope\n---\n")

    monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "project")
    assert agent_catalog.list_agent_resources(None)["skills"] == []

    monkeypatch.setenv("CLAUDE_SETTING_SOURCES", "user,project,local")
    (skill,) = agent_catalog.list_agent_resources(None)["skills"]
    assert (skill["name"], skill["source"]) == ("mine", "user")


# ---------------------------------------------------------------- MCP health


@pytest.fixture(autouse=True)
def _reset_health():
    mcp_health.reset_for_tests()
    yield
    mcp_health.reset_for_tests()


async def test_first_read_reports_unknown_and_refreshes_in_background():
    with (
        patch.object(mcp_health, "_server_names", return_value=["wiki"]),
        patch(
            "src.mcp_admin_service.test_connection",
            new=AsyncMock(return_value={"ok": True, "detail": "ok", "latency_ms": 12.0}),
        ),
    ):
        first = await mcp_health.get_health()
        # A poll must never wait on a probe.
        assert first["servers"][0]["status"] == "unknown"
        assert first["refreshing"] is True

        await mcp_health._refresh_task
        second = await mcp_health.get_health()

    assert second["servers"][0]["status"] == "up"
    assert second["servers"][0]["latency_ms"] == 12.0
    assert second["checked_at"] is not None


async def test_refresh_true_waits_and_reports_down_with_detail():
    with (
        patch.object(mcp_health, "_server_names", return_value=["jira"]),
        patch(
            "src.mcp_admin_service.test_connection",
            new=AsyncMock(return_value={"ok": False, "detail": "connection refused"}),
        ),
    ):
        result = await mcp_health.get_health(refresh=True)

    (server,) = result["servers"]
    assert (server["status"], server["detail"]) == ("down", "connection refused")
    assert result["refreshing"] is False


async def test_fresh_snapshot_is_not_reprobed():
    probe = AsyncMock(return_value={"ok": True, "detail": "ok"})
    with (
        patch.object(mcp_health, "_server_names", return_value=["wiki"]),
        patch("src.mcp_admin_service.test_connection", new=probe),
    ):
        await mcp_health.get_health(refresh=True)
        assert probe.await_count == 1
        await mcp_health.get_health()
        await mcp_health.get_health()

    assert probe.await_count == 1  # TTL still valid — polling is free


async def test_probe_exception_is_reported_as_down():
    with (
        patch.object(mcp_health, "_server_names", return_value=["boom"]),
        patch("src.mcp_admin_service.test_connection", new=AsyncMock(side_effect=RuntimeError("x"))),
    ):
        result = await mcp_health.get_health(refresh=True)

    assert result["servers"][0]["status"] == "down"
    assert "RuntimeError" in result["servers"][0]["detail"]


async def test_removed_server_drops_out_of_the_snapshot():
    probe = AsyncMock(return_value={"ok": True, "detail": "ok"})
    with patch("src.mcp_admin_service.test_connection", new=probe):
        with patch.object(mcp_health, "_server_names", return_value=["a", "b"]):
            first = await mcp_health.get_health(refresh=True)
            assert {s["name"] for s in first["servers"]} == {"a", "b"}
        with patch.object(mcp_health, "_server_names", return_value=["a"]):
            second = await mcp_health.get_health(refresh=True)

    assert [s["name"] for s in second["servers"]] == ["a"]


# ---------------------------------------------------------------- routes


def test_agent_resources_endpoint_scopes_to_the_caller_workspace(tmp_path, monkeypatch):
    from tests.test_main_api_unit import client_context

    monkeypatch.setattr("src.admin_auth.ADMIN_API_KEY", "test-key")
    monkeypatch.setenv("USER_WORKSPACES_DIR", str(tmp_path))
    _write(
        tmp_path / "kyu" / "claude" / ".claude" / "skills" / "deep-research" / "SKILL.md",
        "---\nname: deep-research\ndescription: sweep\n---\n",
    )
    from src import workspace_manager as ws

    monkeypatch.setattr(ws.workspace_manager, "base_path", tmp_path)

    with client_context() as (client, _cli):
        res = client.get("/v1/agent-resources", headers={"X-User-Email": "kyu@corp.example"})

    assert res.status_code == 200
    body = res.json()
    assert body["workspace_scoped"] is True
    assert [s["name"] for s in body["skills"] if s["source"] == "project"] == ["deep-research"]


def test_agent_resources_without_identity_header_reports_no_project_scope(monkeypatch):
    from tests.test_main_api_unit import client_context

    monkeypatch.setattr("src.admin_auth.ADMIN_API_KEY", "test-key")
    with client_context() as (client, _cli):
        res = client.get("/v1/agent-resources")

    assert res.status_code == 200
    assert res.json()["workspace_scoped"] is False


def test_mcp_health_endpoint_returns_the_snapshot(monkeypatch):
    from tests.test_main_api_unit import client_context

    monkeypatch.setattr("src.admin_auth.ADMIN_API_KEY", "test-key")
    with (
        client_context() as (client, _cli),
        patch.object(mcp_health, "_server_names", return_value=["wiki"]),
        patch(
            "src.mcp_admin_service.test_connection",
            new=AsyncMock(return_value={"ok": True, "detail": "ok", "latency_ms": 5.0}),
        ),
    ):
        res = client.get("/v1/mcp/health?refresh=true")

    assert res.status_code == 200
    body = res.json()
    assert body["servers"] == [
        {
            "name": "wiki",
            "status": "up",
            "detail": "ok",
            "latency_ms": 5.0,
            "transport": "",
            "checked_at": body["servers"][0]["checked_at"],
        }
    ]
