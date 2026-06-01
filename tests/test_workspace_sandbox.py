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
        good = await _call(hook, "NotebookEdit", {"notebook_path": str(workspace / "n.ipynb")})
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
        result = await _call(hook, "Glob", {"path": str(other_workspace), "pattern": "*"})
        assert _is_deny(result)

    async def test_grep_outside_denied(self, workspace, other_workspace):
        hook = make_workspace_sandbox_hook(workspace)
        result = await _call(hook, "Grep", {"path": str(other_workspace), "pattern": "x"})
        assert _is_deny(result)


class TestBash:
    async def test_inside_workspace_command_allowed(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Bash", {"command": "ls"}) == {}

    async def test_absolute_outside_path_in_command_denied(self, workspace, other_workspace):
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
        result = await _call(
            hook, "Bash", {"command": f"cat {workspace}/CLAUDE.md"}
        )
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
        result = await _call(
            hook, "Bash", {"command": "tar --file=../../escape.tar ."}
        )
        assert _is_deny(result)


class TestAllowOutside:
    async def test_read_category_releases_read_glob_grep(
        self, monkeypatch, workspace, other_workspace
    ):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "read")
        hook = make_workspace_sandbox_hook(workspace)
        for tool, key in (("Read", "file_path"), ("Glob", "path"), ("Grep", "path")):
            args = {key: str(other_workspace / "CLAUDE.md")} if tool == "Read" else {
                key: str(other_workspace), "pattern": "*"
            }
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
            assert await _call(
                hook, tool, {"file_path": str(other_workspace / "CLAUDE.md")}
            ) == {}
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

    async def test_multiple_categories(
        self, monkeypatch, workspace, other_workspace
    ):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "read, bash")
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(
            hook, "Read", {"file_path": str(other_workspace / "CLAUDE.md")}
        ) == {}
        assert await _call(
            hook, "Bash", {"command": f"cat {other_workspace}/CLAUDE.md"}
        ) == {}
        bad = await _call(
            hook, "Write", {"file_path": str(other_workspace / "CLAUDE.md")}
        )
        assert _is_deny(bad)

    async def test_unknown_category_ignored(self, monkeypatch, workspace, other_workspace):
        monkeypatch.setenv("WORKSPACE_SANDBOX_ALLOW_OUTSIDE", "nope,read")
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(
            hook, "Read", {"file_path": str(other_workspace / "CLAUDE.md")}
        ) == {}


class TestUnknownTool:
    async def test_unknown_tool_passes_through(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "Task", {"prompt": "do stuff"}) == {}

    async def test_mcp_tools_pass_through(self, workspace):
        hook = make_workspace_sandbox_hook(workspace)
        assert await _call(hook, "mcp__server__fetch", {"url": "..."}) == {}
