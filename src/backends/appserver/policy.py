"""Canonical capability + tool policy -> Codex runtime policy (issue #173 §6-7).

The enforcement model is **fail-closed** (#174 review §B1/§B2): the primary
boundary is a capability validator (:func:`resolve_runtime_policy`) that admits
a policy only when every allow/deny it expresses maps onto a proven executable
enforcement primitive, and raises :class:`CapabilityError` (refusing the
session) for anything unproven -- BEFORE the app-server process is spawned. It
runs on both fresh session creation and continuation (``update_request_policy``).

* **Proven-enforceable:** a ``filesystem.write`` / ``network`` deny -> the
  ``read-only`` sandbox (a real Codex setting).
* **Fail-closed (rejected):** any explicit ``allowed_tools`` allow-list (its
  denied complement cannot be enforced without a per-tool disable primitive,
  ``allowed_tools=[]`` being the obvious case), and any deny with no primitive
  (command/shell, Read/Glob/Grep, Skill, Task/Agent/subagents, ``mcp__*``, or an
  unrecognized name), from the request OR the global ``DISALLOWED_TOOLS`` env.
  ``approvalPolicy=on-request`` is NOT accepted as a stand-in barrier -- it is
  not "ask before every command".

A **secondary** approval-time layer (:func:`should_auto_deny_approval` /
:func:`should_auto_accept_approval`) mirrors the frozen backend's per-approval
handling for the surviving policies (forcing ``on-request`` when a tool policy
exists, ``acceptEdits`` auto-accepting file changes). It is defense-in-depth,
not the boundary: any policy that would depend on it for enforcement (e.g. an
MCP allow/deny) is already refused by the validator, because a matched approval
string is not proof a real tool invocation was blocked.
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


# The ONLY capability denies with a proven executable enforcement primitive on
# the (unpinned) app-server runtime: both map onto the ``read-only`` sandbox, a
# real Codex setting. Every other deny -- command/shell execution, read-only
# built-ins (Read/Glob/Grep), Skill, Task/Agent/subagents, MCP tool patterns,
# and any unrecognized tool name -- has NO proven runtime disable primitive, so
# a policy that asks for one must fail closed rather than run silently weakened.
_SANDBOX_ENFORCEABLE_DENIES = frozenset({FILESYSTEM_WRITE, NETWORK})


def _effective_disallowed(disallowed_tools: Optional[List[str]]) -> List[str]:
    """Request + global-env deny names, dropping blanks/non-strings."""
    merged = list(disallowed_tools or []) + _disallowed_from_env()
    return [name for name in merged if isinstance(name, str) and name.strip()]


def unenforceable_denies(disallowed_tools: Optional[List[str]]) -> List[str]:
    """Requested deny names with no proven Codex enforcement primitive (§B1).

    A deny is honorable only if it maps onto :data:`_SANDBOX_ENFORCEABLE_DENIES`
    (``filesystem.write`` / ``network`` -> read-only sandbox). Everything else --
    ``shell.execute`` / ``Bash``, ``Read`` / ``Glob`` / ``Grep``, ``Skill``,
    ``Task`` / ``Agent`` / subagents, ``mcp__server__tool`` patterns, and any
    unrecognized name -- is returned so the caller can fail the session closed.
    """
    unenforceable: List[str] = []
    for name in _effective_disallowed(disallowed_tools):
        cap = _TOOL_TO_CAPABILITY.get(name.strip().lower())
        if cap in _SANDBOX_ENFORCEABLE_DENIES:
            continue
        unenforceable.append(name)
    return unenforceable


def resolve_runtime_policy(
    *,
    default_sandbox: str,
    default_approval: str,
    permission_mode: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    disallowed_tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Return the Codex ``sandbox`` + ``approvalPolicy`` for the requested policy.

    This is the fail-closed capability validator (#174 review §B1/§B2): a policy
    is accepted only when every allow/deny it expresses maps onto a proven
    executable enforcement primitive; anything unproven raises
    :class:`CapabilityError` BEFORE the app-server process/thread is started, so
    an opt-in adapter session can never run with a requested constraint silently
    ignored.

    Proven-enforceable (accepted):
      * ``filesystem.write`` / ``network`` deny -> ``read-only`` sandbox.

    Fail-closed (rejected):
      * **Any explicit ``allowed_tools`` allow-list** (§B2). An allow-list means
        every omitted capability is denied; there is no per-tool disable
        primitive on this runtime to enforce that denied complement, so the
        allow-list cannot be honored (``allowed_tools=[]`` block-all is only the
        most obvious case). ``on-request`` is not "ask before every command", so
        it is not accepted as the missing barrier.
      * **Any deny without a proven primitive** (§B1): command/shell execution,
        Read/Glob/Grep, Skill, Task/Agent/subagents, ``mcp__*`` patterns, or an
        unrecognized name -- from the request OR the global ``DISALLOWED_TOOLS``.
      * a tool policy that, after forcing ``on-request``, still resolves to
        ``never`` (approvals could not be turned on).
    """
    denies = requested_denies(disallowed_tools)
    sandbox = default_sandbox
    if FILESYSTEM_WRITE in denies or NETWORK in denies:
        sandbox = "read-only"

    # §B2: an explicit allow-list is an allow-list contract -- omitted tools are
    # denied. Without an enforceable denied-complement primitive it is
    # unsupported and must fail closed (never merely auto-approve the named set).
    if allowed_tools is not None:
        raise CapabilityError(
            "an explicit allowed_tools allow-list is unsupported on this Codex "
            "runtime: it implies denying every omitted capability, and there is "
            "no proven per-tool disable primitive to enforce that complement "
            "(approvalPolicy=on-request is not 'ask before every command'). "
            "Refusing the session rather than running with the allow-list "
            "silently unenforced. Remove allowed_tools (rely on the sandbox / "
            "disallowed_tools capability denies) or use a runtime with an "
            "enforceable tool-disable primitive."
        )

    # §B1: every requested deny must map to a proven enforcement primitive.
    unenforceable = unenforceable_denies(disallowed_tools)
    if unenforceable:
        raise CapabilityError(
            "Codex cannot enforce these requested tool denies on this runtime "
            f"(no proven disable primitive): {sorted(set(unenforceable))}. Only "
            "filesystem.write / network denies (read-only sandbox) are "
            "enforceable; command/shell, Read/Glob/Grep, Skill, Task/Agent and "
            "mcp__* denies are refused rather than run silently weakened."
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
