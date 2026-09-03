"""Tool-policy + capability parity tests for the Codex adapter (issue #173 §6-7).

Mirrors the frozen backend's approval-time enforcement so the opt-in adapter is
not a policy regression: allowed/disallowed tools (request + global env),
allowed_tools=[] block-all, MCP wildcards, forced on-request, and the
fail-closed capability sandbox mapping.
"""

from __future__ import annotations

import pytest

from src.backends.appserver.policy import (
    CapabilityError,
    has_tool_policy,
    resolve_approval_policy,
    resolve_runtime_policy,
    should_auto_accept_approval,
    should_auto_deny_approval,
)

COMMAND = "item/commandExecution/requestApproval"
FILE = "item/fileChange/requestApproval"


# -- approval policy resolution ---------------------------------------------


def test_default_permission_mode_uses_configured_default_approval():
    assert resolve_approval_policy(None, default_approval="never") == "never"


def test_unknown_permission_mode_falls_back_to_on_request():
    assert (
        resolve_approval_policy("weird-mode", default_approval="never") == "on-request"
    )


def test_tool_policy_forces_on_request_over_never():
    # A never policy is upgraded to on-request when a tool policy exists so the
    # per-tool auto-deny gate can run.
    assert (
        resolve_approval_policy(None, default_approval="never", has_tool_policy=True)
        == "on-request"
    )


def test_bypass_permissions_is_never_without_tool_policy():
    assert (
        resolve_approval_policy("bypassPermissions", default_approval="never")
        == "never"
    )


# -- has_tool_policy ---------------------------------------------------------


def test_empty_allow_list_is_a_policy():
    assert has_tool_policy([], None) is True


def test_no_policy_when_nothing_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    assert has_tool_policy(None, None) is False


def test_global_disallowed_env_is_a_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISALLOWED_TOOLS", "Bash")
    assert has_tool_policy(None, None) is True


# -- auto-deny (frozen parity) ----------------------------------------------


def test_disallowed_alias_denies_command(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    # "Bash" alias normalizes to the commandExecution bucket.
    assert should_auto_deny_approval(
        COMMAND, {}, allowed_tools=None, disallowed_tools=["Bash"]
    )


def test_block_all_allow_list_denies_everything(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    assert should_auto_deny_approval(
        COMMAND, {}, allowed_tools=[], disallowed_tools=None
    )
    assert should_auto_deny_approval(FILE, {}, allowed_tools=[], disallowed_tools=None)


def test_allow_list_permits_named_tool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    assert not should_auto_deny_approval(
        COMMAND, {}, allowed_tools=["Bash"], disallowed_tools=None
    )
    # A different tool is not on the allow-list -> denied.
    assert should_auto_deny_approval(
        FILE, {}, allowed_tools=["Bash"], disallowed_tools=None
    )


def test_mcp_wildcard_disallow(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    params = {"serverLabel": "github", "toolName": "create_issue"}
    assert should_auto_deny_approval(
        COMMAND, params, allowed_tools=None, disallowed_tools=["mcp__github__*"]
    )


def test_global_env_disallow_denies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISALLOWED_TOOLS", "Bash")
    assert should_auto_deny_approval(
        COMMAND, {}, allowed_tools=None, disallowed_tools=None
    )


def test_no_policy_does_not_auto_deny(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    assert not should_auto_deny_approval(
        COMMAND, {}, allowed_tools=None, disallowed_tools=None
    )


# -- auto-accept -------------------------------------------------------------


def test_accept_edits_auto_accepts_file_changes_only():
    assert should_auto_accept_approval(FILE, permission_mode="acceptEdits")
    assert not should_auto_accept_approval(COMMAND, permission_mode="acceptEdits")
    assert not should_auto_accept_approval(FILE, permission_mode="default")


# -- capability sandbox (fail closed) ---------------------------------------


def test_command_deny_is_fail_closed_session_refusal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    # on-request is not a proven "ask before every command" boundary, so a
    # command/shell deny with no enforceable primitive REFUSES the session
    # (#174 review §5) rather than running weakened.
    with pytest.raises(CapabilityError):
        resolve_runtime_policy(
            default_sandbox="workspace-write",
            default_approval="never",
            disallowed_tools=["shell.execute"],
        )
    with pytest.raises(CapabilityError):
        resolve_runtime_policy(
            default_sandbox="workspace-write",
            default_approval="never",
            disallowed_tools=["Bash"],
        )


def test_block_all_allow_list_is_fail_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    # allowed_tools=[] omits commandExecution -> command denied -> refuse.
    with pytest.raises(CapabilityError):
        resolve_runtime_policy(
            default_sandbox="workspace-write",
            default_approval="never",
            allowed_tools=[],
        )


def test_filesystem_write_deny_drops_to_read_only():
    policy = resolve_runtime_policy(
        default_sandbox="workspace-write",
        default_approval="never",
        disallowed_tools=["filesystem.write"],
    )
    assert policy["sandbox"] == "read-only"


def test_allow_list_including_command_is_accepted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DISALLOWED_TOOLS", raising=False)
    # An allow-list that DOES permit command execution is representable; it just
    # forces on-request so file/MCP approvals can still be gated.
    policy = resolve_runtime_policy(
        default_sandbox="workspace-write",
        default_approval="never",
        allowed_tools=["Bash", "Read"],
    )
    assert policy["approvalPolicy"] == "on-request"
