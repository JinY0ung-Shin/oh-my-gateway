"""Catalog behavior for backend-neutral workspace resources."""

from pathlib import Path

from src import agent_catalog
from src.backends.claude.workspace_resources import ensure_workspace_resources


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _isolate_external_scopes(monkeypatch) -> None:
    monkeypatch.setattr(agent_catalog, "_user_scope_dir", lambda: None)
    monkeypatch.setattr(agent_catalog, "_plugin_entries", lambda kind: [])


def test_catalog_reads_canonical_visible_resource_when_mirror_is_managed(tmp_path, monkeypatch):
    _isolate_external_scopes(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ensure_workspace_resources(workspace)
    _write(
        workspace / "skills" / "review" / "SKILL.md",
        "---\nname: review\ndescription: canonical\n---\n",
    )
    ensure_workspace_resources(workspace)

    resources = agent_catalog.list_agent_resources(workspace)

    assert resources["skills"] == [
        {"name": "review", "description": "canonical", "source": "project", "plugin": ""}
    ]


def test_unmanaged_native_conflict_wins_same_name_because_claude_executes_it(
    tmp_path, monkeypatch
):
    _isolate_external_scopes(monkeypatch)
    workspace = tmp_path / "workspace"
    _write(
        workspace / "skills" / "review" / "SKILL.md",
        "---\nname: review\ndescription: visible\n---\n",
    )
    _write(
        workspace / ".claude" / "skills" / "review" / "SKILL.md",
        "---\nname: review\ndescription: native\n---\n",
    )

    resources = agent_catalog.list_agent_resources(workspace)

    assert resources["skills"] == [
        {"name": "review", "description": "native", "source": "project", "plugin": ""}
    ]
