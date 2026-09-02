"""Canonical human-interaction translation for the Codex adapter (issue #173 §3-4).

A Codex human interaction is a server-originated JSON-RPC request whose request
id must stay pending while the user thinks. ``AppServerTransport`` already models
that as a generation-bound ``PendingInteraction`` with an explicit lifecycle
(``pending`` -> ``resolving`` -> ``resolved`` | ``invalidated`` | ...). This
module owns the *vendor translation* on top of that lifecycle:

* :func:`interaction_arguments` renders a native server request into the
  canonical AskUserQuestion card payload the existing ChatDRAGON UI understands
  (issue §4: generalize interactions; the AskUserQuestion card is the
  ``user_input`` renderer, ``kind`` lets richer renderers be added later).
* :func:`answer_result_from_output` turns the user's ``function_call_output``
  string back into the native JSON-RPC result for that exact request.

A vendor-specific request id stays private backend identity; the UI only ever
sees the canonical interaction/call id. The result schemas for the approval
family (command / file / permissions) are well known; other classes
(requestUserInput, MCP elicitation, dynamic tool call) let a structured
``output`` pass through verbatim rather than being faked into a fixed shape.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Dict, List, Optional

# Server-request methods whose answer is an approval decision.
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
MCP_TOOL_APPROVAL = "item/mcpToolCall/requestApproval"
DYNAMIC_TOOL_APPROVAL = "item/dynamicToolCall/requestApproval"
DECISION_METHODS = {
    COMMAND_APPROVAL,
    FILE_APPROVAL,
    MCP_TOOL_APPROVAL,
    DYNAMIC_TOOL_APPROVAL,
}

# Non-approval interaction classes (free-form / structured answers).
USER_INPUT = "item/tool/requestUserInput"
DYNAMIC_TOOL_CALL = "item/tool/call"
MCP_ELICITATION = "mcpServer/elicitation/request"

_DECISION_DESCRIPTIONS = {
    "accept": "Approve this request once.",
    "acceptForSession": "Approve matching requests for this session.",
    "acceptWithExecpolicyAmendment": "Approve and apply the proposed execpolicy amendment.",
    "applyNetworkPolicyAmendment": "Apply the proposed network policy rule.",
    "decline": "Deny and let Codex continue.",
    "cancel": "Deny and interrupt the turn.",
}

_DECISION_ALIASES = {
    "": "decline",
    "yes": "accept",
    "y": "accept",
    "allow": "accept",
    "approve": "accept",
    "approved": "accept",
    "once": "accept",
    "no": "decline",
    "n": "decline",
    "deny": "decline",
    "denied": "decline",
    "reject": "decline",
    "rejected": "decline",
    "always": "acceptForSession",
    "session": "acceptForSession",
    "stop": "cancel",
}

_KNOWN_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}


def interaction_kind(method: str) -> str:
    """Canonical interaction ``kind`` for a native server-request method."""
    if method == COMMAND_APPROVAL:
        return "command_approval"
    if method == FILE_APPROVAL:
        return "file_approval"
    if method == PERMISSIONS_APPROVAL:
        return "permission_approval"
    if method in {MCP_TOOL_APPROVAL, DYNAMIC_TOOL_APPROVAL}:
        return "command_approval"
    if method == USER_INPUT:
        return "user_input"
    if method == MCP_ELICITATION:
        return "mcp_elicitation"
    if method == DYNAMIC_TOOL_CALL:
        return "tool_call"
    return "approval"


def _question(kind: str, params: Dict[str, Any]) -> str:
    if kind == "command_approval":
        command = params.get("command")
        if isinstance(command, str) and command:
            return f"Codex requests approval to run command: {command}"
        return "Codex requests approval to run a command."
    if kind == "file_approval":
        return "Codex requests approval to apply file changes."
    if kind == "permission_approval":
        return "Codex requests additional permissions."
    if kind == "user_input":
        prompt = params.get("prompt") or params.get("question") or params.get("message")
        if isinstance(prompt, str) and prompt:
            return prompt
        return "Codex requests input to continue."
    if kind == "mcp_elicitation":
        message = params.get("message")
        if isinstance(message, str) and message:
            return message
        return "An MCP server requests information to continue."
    if kind == "tool_call":
        return "Codex requests a client-side tool call."
    return "Codex requests approval."


def _decision_label(decision: Any) -> str:
    if isinstance(decision, str):
        return decision
    if not isinstance(decision, dict) or not decision:
        return ""
    if "acceptWithExecpolicyAmendment" in decision:
        return "acceptWithExecpolicyAmendment"
    if "applyNetworkPolicyAmendment" in decision:
        amendment = decision.get("applyNetworkPolicyAmendment")
        if isinstance(amendment, dict):
            policy = amendment.get("network_policy_amendment")
            if isinstance(policy, dict):
                action = policy.get("action")
                host = policy.get("host")
                if action and host:
                    return f"applyNetworkPolicyAmendment:{action}:{host}"
        return "applyNetworkPolicyAmendment"
    return next(iter(decision.keys()), "")


def _options(kind: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if kind == "permission_approval":
        decisions: List[Any] = ["accept", "acceptForSession", "decline"]
    elif kind in {"command_approval", "file_approval", "approval"}:
        raw = params.get("availableDecisions")
        decisions = raw if isinstance(raw, list) else []
        if not decisions:
            decisions = ["accept", "acceptForSession", "decline", "cancel"]
    else:
        # Free-form interactions have no fixed option set.
        return []
    options: List[Dict[str, Any]] = []
    for decision in decisions:
        label = _decision_label(decision)
        if not label:
            continue
        option: Dict[str, Any] = {
            "label": label,
            "description": _DECISION_DESCRIPTIONS.get(label, f"Choose {label}."),
        }
        if isinstance(decision, dict):
            option["decision"] = decision
        options.append(option)
    return options


def interaction_arguments(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Render a native server request into the canonical AskUserQuestion payload."""
    kind = interaction_kind(method)
    arguments: Dict[str, Any] = {
        "kind": kind,
        "question": _question(kind, params),
    }
    if isinstance(params.get("command"), str):
        arguments["command"] = params["command"]
    if isinstance(params.get("cwd"), str):
        arguments["cwd"] = params["cwd"]
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        arguments["reason"] = reason
    if "permissions" in params:
        arguments["permissions"] = params.get("permissions") or {}
    if params.get("grantRoot"):
        arguments["grantRoot"] = params["grantRoot"]
    for key in (
        "itemId",
        "approvalId",
        "additionalPermissions",
        "commandActions",
        "networkApprovalContext",
        "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments",
    ):
        if params.get(key) is not None:
            arguments[key] = params[key]
    options = _options(kind, params)
    if options:
        arguments["options"] = options
    return arguments


def _normalize_decision(value: Any) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    raw = str(value or "").strip()
    if raw in _KNOWN_DECISIONS:
        return raw
    return _DECISION_ALIASES.get(raw, "decline")


def _decision_from_available_options(
    output: str, params: Dict[str, Any]
) -> Optional[Any]:
    raw = str(output or "").strip()
    decisions = params.get("availableDecisions")
    if not isinstance(decisions, list):
        return None
    for decision in decisions:
        if raw == _decision_label(decision):
            return decision
    return None


def answer_result_from_output(
    method: str, output: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """Translate the user's ``function_call_output`` into the native RPC result.

    Approval methods yield a ``{"decision": ...}`` (or ``{"permissions",
    "scope"}``) result; other interaction classes let a structured JSON object
    pass through verbatim, with a plain-text fallback.
    """
    parsed: Any = None
    if isinstance(output, str):
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(output)

    if method == PERMISSIONS_APPROVAL:
        if isinstance(parsed, dict) and "permissions" in parsed:
            return parsed
        decision = _normalize_decision(parsed if parsed is not None else output)
        if decision in {"accept", "acceptForSession"}:
            return {
                "permissions": params.get("permissions") or {},
                "scope": "session" if decision == "acceptForSession" else "turn",
            }
        return {"permissions": {}, "scope": "turn"}

    if method in DECISION_METHODS:
        if isinstance(parsed, dict):
            if "decision" in parsed:
                return {"decision": parsed["decision"]}
            if method in {COMMAND_APPROVAL, FILE_APPROVAL}:
                return {"decision": parsed}
        selected = _decision_from_available_options(output, params)
        if selected is not None:
            return {"decision": selected}
        return {
            "decision": _normalize_decision(parsed if parsed is not None else output)
        }

    # requestUserInput / elicitation / dynamic tool call: pass a structured
    # object through unchanged, else wrap free text.
    if isinstance(parsed, dict):
        return parsed
    return {"response": output}
