"""Canonical human-interaction translation for the Codex adapter (issue #173 §3-4).

A Codex human interaction is a server-originated JSON-RPC request whose request
id must stay pending while the user thinks. ``AppServerTransport`` models that as
a generation-bound ``PendingInteraction``. This module owns the *vendor
translation* between the native request and the existing ChatDRAGON
AskUserQuestion contract.

The canonical AskUserQuestion ``arguments`` the UI parser
(``ChatDRAGON_UI/.../protocol/events.ts::parseAskUserQuestion``) requires is:

    {"questions": [{"question": str, "header"?: str, "multiSelect"?: bool,
                    "options": [{"label": str, "description"?: str}]}]}

and the UI submits answers back through the BFF as a positional
``function_call_output.output = {"answers": string[][]}`` (outer index = question
index, inner = the selected option strings). Every bridged interaction is
therefore rendered as one or more canonical questions, and the answer is
translated back to the exact native result schema for that request:

* command / file approval  -> ``{"decision": ...}``
* permission approval      -> ``{"permissions": {...}, "scope": ...}``
* requestUserInput         -> ``{"answers": {[questionId]: {"answers": [...]}}}``

Native request-id and question-id correlation stays private backend identity;
the UI only ever sees the canonical call id and positional question order.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Dict, List, Optional

# v2 ServerRequest methods (the old item/mcpToolCall|dynamicToolCall/requestApproval
# names are NOT current v2 methods — #174 review §5).
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
DECISION_METHODS = {COMMAND_APPROVAL, FILE_APPROVAL}

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
    if method == USER_INPUT:
        return "user_input"
    if method == MCP_ELICITATION:
        return "mcp_elicitation"
    if method == DYNAMIC_TOOL_CALL:
        return "tool_call"
    return "approval"


# -- approval decision options ----------------------------------------------


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


def _decision_options(kind: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    if kind == "permission_approval":
        decisions: List[Any] = ["accept", "acceptForSession", "decline"]
    else:
        raw = params.get("availableDecisions")
        decisions = raw if isinstance(raw, list) else []
        if not decisions:
            decisions = ["accept", "acceptForSession", "decline", "cancel"]
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


def _approval_question_text(kind: str, params: Dict[str, Any]) -> str:
    if kind == "command_approval":
        command = params.get("command")
        if isinstance(command, str) and command:
            return f"Codex requests approval to run command: {command}"
        return "Codex requests approval to run a command."
    if kind == "file_approval":
        return "Codex requests approval to apply file changes."
    if kind == "permission_approval":
        return "Codex requests additional permissions."
    return "Codex requests approval."


# -- requestUserInput questions ---------------------------------------------


def _option_label(option: Any) -> Optional[Dict[str, Any]]:
    """Map one native question option into a canonical ``{label, description?}``."""
    if isinstance(option, str):
        return {"label": option}
    if isinstance(option, dict):
        label = option.get("label") or option.get("value") or option.get("text")
        if isinstance(label, str) and label:
            entry: Dict[str, Any] = {"label": label}
            desc = option.get("description")
            if isinstance(desc, str) and desc:
                entry["description"] = desc
            return entry
    return None


def _user_input_questions(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map native ``questions[]`` into canonical AskUserQuestion ``questions[]``.

    Native question: ``{id, header, question, isOther, isSecret, options}``.
    ``isSecret``/``isOther`` are carried through as extra keys (harmless to the
    current card, available to a richer renderer); the core mapping is
    ``question`` + ``header`` + ``options``.
    """
    native = params.get("questions")
    if not isinstance(native, list):
        return []
    out: List[Dict[str, Any]] = []
    for q in native:
        if not isinstance(q, dict):
            continue
        text = q.get("question")
        if not isinstance(text, str) or not text:
            continue
        canonical: Dict[str, Any] = {"question": text}
        header = q.get("header")
        if isinstance(header, str) and header:
            canonical["header"] = header
        options = []
        raw_options = q.get("options")
        if isinstance(raw_options, list):
            for opt in raw_options:
                mapped = _option_label(opt)
                if mapped is not None:
                    options.append(mapped)
        canonical["options"] = options
        # Best-effort passthrough of richer semantics.
        if q.get("isSecret"):
            canonical["isSecret"] = True
        if q.get("isOther"):
            canonical["isOther"] = True
        out.append(canonical)
    return out


def interaction_arguments(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Render a native server request into canonical AskUserQuestion arguments.

    Always returns a top-level ``questions`` array so the existing card parser
    (`parseAskUserQuestion`) can consume it. Approvals become a single question
    whose options are the decisions; ``requestUserInput`` maps its ``questions[]``.
    """
    kind = interaction_kind(method)
    arguments: Dict[str, Any] = {"kind": kind}

    if method == USER_INPUT:
        arguments["questions"] = _user_input_questions(params)
    else:
        question: Dict[str, Any] = {
            "question": _approval_question_text(kind, params),
            "header": "Approval",
            "options": _decision_options(kind, params),
        }
        arguments["questions"] = [question]
        # Carry approval context for a richer renderer (harmless extras).
        for key in ("command", "cwd", "reason"):
            if isinstance(params.get(key), str) and params[key]:
                arguments[key] = params[key]
        if "permissions" in params:
            arguments["permissions"] = params.get("permissions") or {}
    return arguments


# -- answer translation ------------------------------------------------------


def _canonical_answers(output: str) -> Optional[List[List[str]]]:
    """Parse the UI's ``{"answers": string[][]}`` positional answer payload."""
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(output) if isinstance(output, str) else output
        if isinstance(parsed, dict):
            answers = parsed.get("answers")
            if isinstance(answers, list):
                norm: List[List[str]] = []
                for a in answers:
                    if isinstance(a, list):
                        norm.append([str(x) for x in a])
                    else:
                        norm.append([str(a)])
                return norm
    return None


def _normalize_decision(value: Any) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    raw = str(value or "").strip()
    if raw in _KNOWN_DECISIONS:
        return raw
    return _DECISION_ALIASES.get(raw, "decline")


def _decision_from_available_options(raw: str, params: Dict[str, Any]) -> Optional[Any]:
    decisions = params.get("availableDecisions")
    if not isinstance(decisions, list):
        return None
    for decision in decisions:
        if raw == _decision_label(decision):
            return decision
    return None


def _first_answer(output: str) -> str:
    """Extract the single decision string from an approval answer."""
    canonical = _canonical_answers(output)
    if canonical and canonical[0]:
        return canonical[0][0]
    # Fallbacks: a bare JSON string/object, or plain text.
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(output) if isinstance(output, str) else output
        if isinstance(parsed, dict) and "decision" in parsed:
            return _decision_label(parsed["decision"]) or str(parsed["decision"])
        if isinstance(parsed, str):
            return parsed
    return output if isinstance(output, str) else ""


def answer_result_from_output(
    method: str, output: str, params: Dict[str, Any]
) -> Dict[str, Any]:
    """Translate the user's ``function_call_output`` into the native RPC result."""
    if method == USER_INPUT:
        canonical = _canonical_answers(output) or []
        native_questions = params.get("questions")
        answers: Dict[str, Any] = {}
        if isinstance(native_questions, list):
            for i, q in enumerate(native_questions):
                if not isinstance(q, dict):
                    continue
                qid = q.get("id")
                if qid is None:
                    continue
                selected = canonical[i] if i < len(canonical) else []
                answers[str(qid)] = {"answers": list(selected)}
        return {"answers": answers}

    if method == PERMISSIONS_APPROVAL:
        raw = _first_answer(output)
        decision = _normalize_decision(raw)
        if decision in {"accept", "acceptForSession"}:
            return {
                "permissions": params.get("permissions") or {},
                "scope": "session" if decision == "acceptForSession" else "turn",
            }
        return {"permissions": {}, "scope": "turn"}

    if method in DECISION_METHODS:
        raw = _first_answer(output)
        selected = _decision_from_available_options(raw, params)
        if selected is not None:
            return {"decision": selected}
        return {"decision": _normalize_decision(raw)}

    # elicitation / dynamic tool call: pass a structured object through, else wrap.
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(output) if isinstance(output, str) else output
        if isinstance(parsed, dict):
            return parsed
    return {"response": output}
