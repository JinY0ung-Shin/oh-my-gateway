"""Tests for the per-workspace PreToolUse sandbox hook."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.backends.claude.workspace_sandbox import (
    make_workspace_sandbox_hook,
    sandbox_enabled,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "kyvhyvn.shim" / "claude"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("mine")
    other = tmp_path / "Perturabo" / "claude"
    other.mkdir(parents=True)
    (other / "CLAUDE.md").write_text("not yours")
    return ws


@pytest.fixture
def other_workspace(tmp_path: Path) -> Path:
    return tmp_path / "Perturabo" / "claude"


async def _call(hook, tool_name: str, tool_input: dict) -> dict:
    return await hook(
        {"tool_name": tool_name, "tool_input": tool_input},
        "tool-use-id",
        None,
    )


def _is_deny(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestEnabledFlag:
    def test_default_disabled(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_SANDBOX_ENABLED", raising=False)
        assert sandbox_enabled() is False

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
    def test_explicit_disable(self, monkeypatch, value):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ENABLED", value)
        assert sandbox_enabled() is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
    def test_explicit_enable(self, monkeypatch, value):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ENABLED", value)
        assert sandbox_enabled() is True


class TestFilePathTools:
    @pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "MultiEdit"])
    async def test_inside_workspace_allowed(self, workspace, tool):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, tool, {"file_path": str(workspace / "CLAUDE.md")})
        assert result == {}

    @pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "MultiEdit"])
    async def test_other_workspace_denied(self, workspace, other_workspace, tool):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, tool, {"file_path": str(other_workspace / "CLAUDE.md")}
        )
        assert _is_deny(result)

    async def test_notebook_path(self, workspace, other_workspace):
        hook = make_workspace_sandbox_hook(workspace)
        good = await _call(
            hook, "NotebookEdit", {"notebook_path": str(workspace / "n.ipynb")}
        )
        assert good == {}
        bad = await _call(
            hook, "NotebookEdit", {"notebook_path": str(other_workspace / "n.ipynb")}
        )
        assert _is_deny(bad)

    async def test_relative_path_resolved_against_workspace(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        # Plain filename → workspace-relative, allowed.
        ok = await _call(hook, "Read", {"file_path": "CLAUDE.md"})
        assert ok == {}
        # ../../etc — escapes via parent traversal.
        bad = await _call(hook, "Read", {"file_path": "../../etc/passwd"})
        assert _is_deny(bad)

    async def test_symlink_escape_blocked(self, workspace, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("nope")
        link = workspace / "shortcut"
        link.symlink_to(secret)
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Read", {"file_path": str(link)})
        assert _is_deny(result)


class TestOptionalPathTools:
    async def test_glob_no_path_allowed(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Glob", {"pattern": "**/*.py"}) == {}

    async def test_glob_outside_denied(self, workspace, other_workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Glob", {"path": str(other_workspace), "pattern": "*"}
        )
        assert _is_deny(result)

    async def test_grep_outside_denied(self, workspace, other_workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Grep", {"path": str(other_workspace), "pattern": "x"}
        )
        assert _is_deny(result)


class TestBash:
    async def test_inside_workspace_command_allowed(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Bash", {"command": "ls"}) == {}

    async def test_absolute_outside_path_in_command_denied(
        self, workspace, other_workspace
    ):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook,
            "Bash",
            {"command": f"cat {other_workspace}/CLAUDE.md"},
        )
        assert _is_deny(result)

    async def test_quoted_outside_path_denied(self, workspace, other_workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook,
            "Bash",
            {"command": f"cat '{other_workspace}/CLAUDE.md'"},
        )
        assert _is_deny(result)

    async def test_absolute_inside_workspace_allowed(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Bash", {"command": f"cat {workspace}/CLAUDE.md"})
        assert result == {}

    async def test_relative_traversal_denied(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Bash", {"command": "cat ../../etc/passwd"})
        assert _is_deny(result)

    async def test_relative_inside_subdir_allowed(self, workspace):
        (workspace / "logs").mkdir()
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Bash", {"command": "cat logs/app.log"})
        assert result == {}

    async def test_relative_symlink_target_denied(self, workspace):
        # ``ln -s`` with a relative target escapes via the parent traversal in
        # the link target argument, even though no token starts with ``/``.
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Bash", {"command": "ln -s ../../etc/passwd shortcut"}
        )
        assert _is_deny(result)

    async def test_home_reference_denied(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Bash", {"command": "cat ~/.ssh/id_rsa"})
        assert _is_deny(result)

    async def test_flag_value_traversal_denied(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Bash", {"command": "tar --file=../../escape.tar ."})
        assert _is_deny(result)


class TestAllowOutside:
    async def test_read_category_releases_read_glob_grep(
        self, monkeypatch, workspace, other_workspace
    ):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "read")
        hook = make_workspace_sandbox_hook(workspace)
        for tool, key in (("Read", "file_path"), ("Glob", "path"), ("Grep", "path")):
            args = (
                {key: str(other_workspace / "CLAUDE.md")}
                if tool == "Read"
                else {key: str(other_workspace), "pattern": "*"}
            )
            assert await _call(hook, tool, args) == {}
        bad = await _call(
            hook, "Write", {"file_path": str(other_workspace / "CLAUDE.md")}
        )
        assert _is_deny(bad)

    async def test_write_category_releases_writes_only(
        self, monkeypatch, workspace, other_workspace
    ):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "write")
        hook = make_workspace_sandbox_hook(workspace)
        for tool in ("Write", "Edit", "MultiEdit"):
            assert (
                await _call(
                    hook, tool, {"file_path": str(other_workspace / "CLAUDE.md")}
                )
                == {}
            )
        bad = await _call(
            hook, "Read", {"file_path": str(other_workspace / "CLAUDE.md")}
        )
        assert _is_deny(bad)

    async def test_bash_category_releases_bash(
        self, monkeypatch, workspace, other_workspace
    ):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "bash")
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Bash", {"command": f"cat {other_workspace}/CLAUDE.md"}
        )
        assert result == {}

    async def test_multiple_categories(self, monkeypatch, workspace, other_workspace):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "read, bash")
        hook = make_workspace_sandbox_hook(workspace)
        assert (
            await _call(hook, "Read", {"file_path": str(other_workspace / "CLAUDE.md")})
            == {}
        )
        assert (
            await _call(hook, "Bash", {"command": f"cat {other_workspace}/CLAUDE.md"})
            == {}
        )
        bad = await _call(
            hook, "Write", {"file_path": str(other_workspace / "CLAUDE.md")}
        )
        assert _is_deny(bad)

    async def test_unknown_category_ignored(
        self, monkeypatch, workspace, other_workspace
    ):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "nope,read")
        hook = make_workspace_sandbox_hook(workspace)
        assert (
            await _call(hook, "Read", {"file_path": str(other_workspace / "CLAUDE.md")})
            == {}
        )


class TestHomeClaudeRoot:
    """The Claude Code SDK's own ``$HOME/.claude`` state dir is always allowed
    (issue #115), while the rest of ``$HOME`` stays denied."""

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch) -> Path:
        home = tmp_path / "home" / "app"
        (home / ".claude" / "projects").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        return home

    async def test_bash_home_claude_tilde_allowed(self, workspace, home):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Bash", {"command": "cat ~/.claude/projects/x/tool-results/y.json"}
        )
        assert result == {}

    async def test_bash_home_claude_absolute_allowed(self, workspace, home):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Bash", {"command": f"cat {home}/.claude/projects/x/y.json"}
        )
        assert result == {}

    async def test_read_home_claude_allowed(self, workspace, home):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Read", {"file_path": str(home / ".claude" / "state.json")}
        )
        assert result == {}

    async def test_other_home_paths_still_denied(self, workspace, home):
        # Only $HOME/.claude is added, not all of $HOME — ~/.ssh stays denied.
        hook = make_workspace_sandbox_hook(workspace)
        assert _is_deny(await _call(hook, "Bash", {"command": "cat ~/.ssh/id_rsa"}))
        bad = await _call(hook, "Read", {"file_path": str(home / ".ssh" / "id_rsa")})
        assert _is_deny(bad)

    async def test_other_workspace_still_denied(self, workspace, other_workspace, home):
        hook = make_workspace_sandbox_hook(workspace)
        bad_read = await _call(
            hook, "Read", {"file_path": str(other_workspace / "CLAUDE.md")}
        )
        assert _is_deny(bad_read)
        bad_bash = await _call(
            hook, "Bash", {"command": f"cat {other_workspace}/CLAUDE.md"}
        )
        assert _is_deny(bad_bash)

    async def test_no_home_keeps_claude_denied(self, workspace, monkeypatch):
        # With $HOME unset there is no extra root, so ~/.claude is not special.
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_CLONE_ROOT", raising=False)
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(
            hook, "Bash", {"command": "cat ~/.claude/projects/x/y.json"}
        )
        assert _is_deny(result)


class TestPluginResourceRoots:
    """Plugin skills are shared assets: a session must be able to read a skill
    document and its bundled resources (and exec them via Bash) even when the
    plugin/marketplace lives outside ``$HOME/.claude`` (custom clone root,
    project/local-scope marketplace added from a local path), but it must NOT be
    able to overwrite that shared source via the write tools. Regression for the
    workspace-sandbox isolation blocking admin-hot-loaded plugin skills."""

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch) -> Path:
        home = tmp_path / "home" / "app"
        (home / ".claude" / "plugins").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAUDE_PLUGIN_CLONE_ROOT", raising=False)
        return home

    def _write_registry(self, home: Path, mkt_loc: Path, install_path: Path) -> None:
        import json

        plugins = home / ".claude" / "plugins"
        (plugins / "known_marketplaces.json").write_text(
            json.dumps({"scope-test": {"installLocation": str(mkt_loc), "source": {}}})
        )
        (plugins / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "demo@scope-test": [
                            {"installPath": str(install_path), "scope": "project"}
                        ]
                    }
                }
            )
        )

    async def test_marketplace_install_location_allowed(
        self, workspace, home, tmp_path
    ):
        mkt_loc = tmp_path / "outside" / "marketplaces" / "scope-test"
        install_path = home / ".claude" / "plugins" / "cache" / "scope-test" / "demo"
        skill = mkt_loc / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# skill")
        self._write_registry(home, mkt_loc, install_path)

        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Read", {"file_path": str(skill)}) == {}

    async def test_installed_plugin_path_allowed(self, workspace, home, tmp_path):
        mkt_loc = tmp_path / "mkt"
        install_path = tmp_path / "outside" / "cache" / "scope-test" / "demo" / "0.1.0"
        skill = install_path / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# skill")
        self._write_registry(home, mkt_loc, install_path)

        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Read", {"file_path": str(skill)}) == {}

    async def test_clone_root_env_allowed(self, workspace, home, tmp_path, monkeypatch):
        clone_root = tmp_path / "data" / "plugin-marketplaces"
        clone_root.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(clone_root))
        skill = clone_root / "mkt" / "skills" / "x" / "SKILL.md"

        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Read", {"file_path": str(skill)}) == {}

    async def test_unrelated_outside_path_still_denied(self, workspace, home, tmp_path):
        mkt_loc = tmp_path / "outside" / "marketplaces" / "scope-test"
        install_path = home / ".claude" / "plugins" / "cache" / "demo"
        self._write_registry(home, mkt_loc, install_path)

        hook = make_workspace_sandbox_hook(workspace)
        # A sibling of the marketplace root that is not itself a plugin root.
        bad = tmp_path / "outside" / "secrets.txt"
        assert _is_deny(await _call(hook, "Read", {"file_path": str(bad)}))

    async def test_corrupt_registry_tolerated(self, workspace, home):
        plugins = home / ".claude" / "plugins"
        (plugins / "known_marketplaces.json").write_text("{ not json")
        (plugins / "installed_plugins.json").write_text("[]")
        # Building the hook must not raise; ~/.claude itself stays allowed.
        hook = make_workspace_sandbox_hook(workspace)
        ok = await _call(
            hook, "Read", {"file_path": str(home / ".claude" / "state.json")}
        )
        assert ok == {}

    async def test_bash_may_exec_plugin_resource(self, workspace, home, tmp_path):
        # A skill that bundles a script must be runnable via Bash.
        mkt_loc = tmp_path / "outside" / "marketplaces" / "scope-test"
        install_path = home / ".claude" / "plugins" / "cache" / "demo"
        script = mkt_loc / "scripts" / "run.sh"
        script.parent.mkdir(parents=True)
        script.write_text("echo hi")
        self._write_registry(home, mkt_loc, install_path)

        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Bash", {"command": f"bash {script}"}) == {}

    @pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
    async def test_write_to_plugin_resource_denied(
        self, workspace, home, tmp_path, tool
    ):
        # Read is allowed, but the write tools must not reach shared plugin
        # source that lives outside $HOME/.claude.
        mkt_loc = tmp_path / "outside" / "marketplaces" / "scope-test"
        install_path = home / ".claude" / "plugins" / "cache" / "demo"
        skill = mkt_loc / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# skill")
        self._write_registry(home, mkt_loc, install_path)

        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Read", {"file_path": str(skill)}) == {}
        assert _is_deny(await _call(hook, tool, {"file_path": str(skill)}))

    async def test_relative_registry_path_ignored(self, workspace, home):
        # A relative installLocation must not be resolved against the gateway
        # CWD and granted as a root.
        import json
        import os

        plugins = home / ".claude" / "plugins"
        (plugins / "known_marketplaces.json").write_text(
            json.dumps({"x": {"installLocation": "relative/marketplace"}})
        )
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"d@x": [{"installPath": "../escape"}]}})
        )
        hook = make_workspace_sandbox_hook(workspace)
        # Had the relative value been resolved against CWD and granted, this
        # absolute path would be allowed; it must stay denied.
        cwd_relative = Path(os.getcwd()) / "relative" / "marketplace" / "SKILL.md"
        assert _is_deny(await _call(hook, "Read", {"file_path": str(cwd_relative)}))


class TestUnknownTool:
    async def test_unknown_tool_passes_through(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Task", {"prompt": "do stuff"}) == {}

    async def test_mcp_tools_pass_through(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "mcp__server__fetch", {"url": "..."}) == {}
