"""Canonical capability + tool policy -> Codex runtime policy (issue #173 §6-7).

Two concerns live here:

* **Sandbox/capability** (§7): requested capability denies (from
  ``disallowed_tools``) map onto the Codex ``sandbox`` mode, and a deny that no
  Codex setting can enforce (``shell.execute``) rejects the session
  (``CapabilityError``) rather than silently weakening the harness.
* **Per-tool approval policy** (§6): parity with the frozen backend's
  approval-time enforcement — ``allowed_tools`` / ``disallowed_tools`` (request
  and global ``DISALLOWED_TOOLS`` env), including an explicit ``allowed_tools=[]``
  block-all and ``mcp__server__tool`` wildcards, are enforced by forcing
  ``approvalPolicy=on-request`` whenever a tool policy exists and auto-denying an
  approval for a tool the policy does not permit *before* it is bridged to the
  user. An ``acceptEdits`` mode auto-accepts file-change approvals.

The frozen client enforces the same rules (``src/backends/codex/client.py``);
this is a faithful port so the cutover is not a policy regression.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any, Dict, List, Optional

from src.backends.common import parse_csv

# Canonical capability tokens (issue §7).
FILESYSTEM_WRITE = "filesystem.write"
SHELL_EXECUTE = "shell.execute"
NETWORK = "network"

# Map common tool-name spellings a caller may put in disallowed_tools onto the
# canonical capability they gate (for the sandbox axis).
_TOOL_TO_CAPABILITY = {
    "filesystem.write": FILESYSTEM_WRITE,
    "write": FILESYSTEM_WRITE,
    "edit": FILESYSTEM_WRITE,
    "applypatch": FILESYSTEM_WRITE,
    "apply_patch": FILESYSTEM_WRITE,
    "shell.execute": SHELL_EXECUTE,
    "shell": SHELL_EXECUTE,
    "bash": SHELL_EXECUTE,
    "exec": SHELL_EXECUTE,
    "exec_command": SHELL_EXECUTE,
    "network": NETWORK,
    "web_search": NETWORK,
    "websearch": NETWORK,
}

# Claude tool aliases -> Codex-native enforcement bucket (mirrors the frozen
# backend's CODEX_TOOL_NAME_ALIASES so a policy written in either vocabulary
# enforces identically).
TOOL_NAME_ALIASES: Dict[str, str] = {
    # Claude tool names.
    "Bash": "commandExecution",
    "BashOutput": "commandExecution",
    "KillShell": "commandExecution",
    "Edit": "fileChange",
    "Write": "fileChange",
    "NotebookEdit": "fileChange",
    # Canonical capability tokens + lowercase spellings, so a policy written in
    # canonical vocabulary auto-denies the right Codex tool bucket.
    "shell.execute": "commandExecution",
    "shell": "commandExecution",
    "bash": "commandExecution",
    "exec": "commandExecution",
    "exec_command": "commandExecution",
    "filesystem.write": "fileChange",
    "write": "fileChange",
    "edit": "fileChange",
    "apply_patch": "fileChange",
    "applypatch": "fileChange",
}

# Approval server-request method -> the Codex tool it gates. v2 ServerRequest
# spellings only (the old item/mcpToolCall|dynamicToolCall/requestApproval names
# are not current v2 methods, per the #174 review §5).
APPROVAL_METHOD_TO_TOOL: Dict[str, str] = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
}

# Gateway permission_mode (Claude vocabulary) -> Codex approvalPolicy.
PERMISSION_MODE_TO_APPROVAL: Dict[str, str] = {
    "bypassPermissions": "never",
    "default": "on-request",
    "acceptEdits": "on-request",
    "plan": "on-request",
}
_UNKNOWN_PERMISSION_MODE_FALLBACK = "on-request"


class CapabilityError(ValueError):
    """A requested capability deny cannot be enforced on the Codex runtime."""


def _disallowed_from_env() -> List[str]:
    return parse_csv(os.getenv("DISALLOWED_TOOLS", ""))


def has_tool_policy(
    allowed_tools: Optional[List[str]],
    disallowed_tools: Optional[List[str]],
) -> bool:
    """Whether any tool allow/deny policy is in force.

    ``allowed_tools is not None`` matters because ``[]`` is a real block-all
    policy, distinct from "no allow-list set".
    """
    if allowed_tools is not None or disallowed_tools:
        return True
    return bool(_disallowed_from_env())


def resolve_approval_policy(
    permission_mode: Optional[str],
    *,
    has_tool_policy: bool = False,
    default_approval: Optional[str] = None,
) -> str:
    """Map permission_mode -> Codex approvalPolicy (frozen-parity).

    Unknown modes fall back to ``on-request`` (never silently bypass). When a
    tool policy exists, a resolved ``never`` is upgraded to ``on-request`` so
    Codex actually emits approval requests and the auto-deny gate can run.
    """
    if permission_mode is None:
        resolved = default_approval or "never"
    else:
        mapped = PERMISSION_MODE_TO_APPROVAL.get(permission_mode)
        resolved = mapped if mapped is not None else _UNKNOWN_PERMISSION_MODE_FALLBACK
    if has_tool_policy and resolved == "never":
        return "on-request"
    return resolved


def requested_denies(disallowed_tools: Optional[List[str]]) -> set:
    """Canonical capabilities the request asks to deny (from disallowed_tools)."""
    denies: set = set()
    for name in disallowed_tools or []:
        if not isinstance(name, str):
            continue
        cap = _TOOL_TO_CAPABILITY.get(name.strip().lower())
        if cap:
            denies.add(cap)
    return denies


def _command_execution_denied(
    allowed_tools: Optional[List[str]],
    disallowed_tools: Optional[List[str]],
) -> bool:
    """Whether the requested policy means to deny built-in command execution.

    True when ``commandExecution`` is disallowed (request or global env) or when
    an explicit allow-list (including ``allowed_tools=[]`` block-all) omits it.
    """
    disallowed = _normalize_tool_names(
        list(disallowed_tools or []) + _disallowed_from_env()
    )
    if "commandExecution" in disallowed:
        return True
    if allowed_tools is not None:
        return "commandExecution" not in _normalize_tool_names(allowed_tools)
    return False


def resolve_runtime_policy(
    *,
    default_sandbox: str,
    default_approval: str,
    permission_mode: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the Codex ``sandbox`` + ``approvalPolicy`` for the requested policy.

    Enforcement paths:
      * ``filesystem.write`` / ``network`` deny -> ``read-only`` sandbox (a real
        Codex primitive).

    Fail-closed (§5/§7): a requested deny with no proven enforcement primitive
    rejects the session (:class:`CapabilityError`) rather than running weakened:
      * denying built-in **command execution** (``shell.execute`` / ``Bash`` /
        an allow-list that omits it, incl. ``allowed_tools=[]``). ``on-request``
        is not a guarantee that every command surfaces an approval before it
        runs, so it is NOT accepted as a boundary; without an executable
        tool-disable primitive on the pinned runtime the session is refused.
      * a tool policy that, after forcing ``on-request``, still resolves to
        ``never`` (approvals could not be turned on).
    """
    denies = requested_denies(disallowed_tools)
    sandbox = default_sandbox
    if FILESYSTEM_WRITE in denies or NETWORK in denies:
        sandbox = "read-only"

    if _command_execution_denied(allowed_tools, disallowed_tools):
        raise CapabilityError(
            "Codex cannot guarantee blocking built-in command execution on this "
            "runtime (approvalPolicy=on-request is not 'ask before every "
            "command'); refusing the session rather than running weakened. "
            "Remove the command/shell deny or use a runtime with an enforceable "
            "tool-disable primitive."
        )

    tool_policy = has_tool_policy(allowed_tools, disallowed_tools)
    approval = resolve_approval_policy(
        permission_mode,
        has_tool_policy=tool_policy,
        default_approval=default_approval,
    )
    if tool_policy and approval == "never":
        raise CapabilityError(
            "a tool allow/deny policy requires approval interception but the "
            "resolved approvalPolicy is 'never'; refusing rather than bypassing it"
        )
    return {"sandbox": sandbox, "approvalPolicy": approval}


# -- per-tool approval enforcement (frozen-parity) --------------------------


def _normalize_tool_names(names: Optional[List[str]]) -> set:
    if not names:
        return set()
    return {TOOL_NAME_ALIASES.get(name, name) for name in names}


def _approval_tool_identities(method: str, params: Dict[str, Any]) -> set:
    """The tool identities an approval request touches (Codex tool + MCP ids)."""
    codex_tool = APPROVAL_METHOD_TO_TOOL.get(method)
    if codex_tool is None:
        return set()
    identities = {codex_tool}
    if not isinstance(params, dict):
        return identities
    # Best-effort MCP identity for a command/tool that names an MCP server+tool.
    server_label = params.get("serverLabel") or params.get("serverName")
    tool_name = params.get("toolName")
    if isinstance(server_label, str) and server_label:
        server_names = {server_label, "_".join(server_label.split("-"))}
        if isinstance(tool_name, str) and tool_name:
            for s in server_names:
                identities.add(f"mcp__{s}__{tool_name}")
        else:
            for s in server_names:
                identities.add(f"mcp__{s}__*")
    return identities


def _policy_matches(policy_names: set, tool_identities: set) -> bool:
    for policy_name in policy_names:
        for identity in tool_identities:
            if policy_name == identity:
                return True
            if policy_name.startswith("mcp__") and fnmatch.fnmatchcase(
                identity, policy_name
            ):
                return True
    return False


def should_auto_deny_approval(
    method: str,
    params: Dict[str, Any],
    *,
    allowed_tools: Optional[List[str]],
    disallowed_tools: Optional[List[str]],
) -> bool:
    """Whether an approval request must be auto-denied by the tool policy.

    Denied when the tool matches the (request + global env) disallow set, or
    when an explicit allow-list exists (including ``[]`` block-all) and the tool
    is not in it.
    """
    identities = _approval_tool_identities(method, params)
    if not identities:
        return False
    disallowed = _normalize_tool_names(
        list(disallowed_tools or []) + _disallowed_from_env()
    )
    if _policy_matches(disallowed, identities):
        return True
    if allowed_tools is not None:
        allowed = _normalize_tool_names(allowed_tools)
        if not _policy_matches(allowed, identities):
            return True
    return False


def should_auto_accept_approval(method: str, *, permission_mode: Optional[str]) -> bool:
    """acceptEdits auto-accepts file-change approvals; nothing else."""
    return (
        permission_mode == "acceptEdits" and method == "item/fileChange/requestApproval"
    )
