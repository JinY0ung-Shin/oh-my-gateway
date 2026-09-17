"""Integration seams for deferred Claude workspace resource materialization."""

from src.backends.claude import _install_workspace_resource_materializer
from src.backends.claude.workspace_resources import materialize_workspace_resources
from src.workspace_manager import WorkspaceManager


def test_workspace_resolve_prepares_roots_without_recursive_sync(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.resolve("alice", backend="claude")
    source = workspace / "skills" / "review" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: review\n---\n", encoding="utf-8")

    native = workspace / ".claude" / "skills" / "review" / "SKILL.md"
    assert not native.exists()  # FileNav-style resolve did not recursively rescan.

    materialize_workspace_resources(workspace)

    assert native.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


async def test_registered_client_wrapper_materializes_before_create_client(monkeypatch, tmp_path):
    calls = []

    class FakeCLI:
        async def create_client(self, *args, **kwargs):
            calls.append(("create", kwargs.get("cwd")))
            return "client"

    def fake_materialize(path):
        calls.append(("materialize", str(path)))

    monkeypatch.setattr(
        "src.backends.claude.workspace_resources.materialize_workspace_resources",
        fake_materialize,
    )
    cli = FakeCLI()
    _install_workspace_resource_materializer(cli)

    result = await cli.create_client(session=object(), cwd=str(tmp_path))

    assert result == "client"
    assert calls == [
        ("materialize", str(tmp_path)),
        ("create", str(tmp_path)),
    ]
