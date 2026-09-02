"""Unit tests for the Codex human-interaction translation (issue #173 §3-4).

Pure functions: native server-request -> canonical AskUserQuestion payload, and
the user's function_call_output string -> native JSON-RPC result. No transport.
"""

from __future__ import annotations

from src.backends.appserver.interactions import (
    answer_result_from_output,
    interaction_arguments,
    interaction_kind,
)


def test_kind_mapping():
    assert (
        interaction_kind("item/commandExecution/requestApproval") == "command_approval"
    )
    assert interaction_kind("item/fileChange/requestApproval") == "file_approval"
    assert interaction_kind("item/permissions/requestApproval") == "permission_approval"
    assert interaction_kind("item/tool/requestUserInput") == "user_input"
    assert interaction_kind("mcpServer/elicitation/request") == "mcp_elicitation"
    assert interaction_kind("item/tool/call") == "tool_call"


def test_command_approval_arguments_carry_command_and_options():
    args = interaction_arguments(
        "item/commandExecution/requestApproval",
        {"command": "rm -rf /", "cwd": "/w", "reason": "cleanup"},
    )
    assert args["kind"] == "command_approval"
    assert "rm -rf /" in args["question"]
    assert args["command"] == "rm -rf /"
    assert args["cwd"] == "/w"
    assert args["reason"] == "cleanup"
    labels = [o["label"] for o in args["options"]]
    assert "accept" in labels and "decline" in labels


def test_available_decisions_are_surfaced_as_options():
    args = interaction_arguments(
        "item/commandExecution/requestApproval",
        {"command": "ls", "availableDecisions": ["accept", "decline"]},
    )
    assert [o["label"] for o in args["options"]] == ["accept", "decline"]


def test_user_input_has_no_options():
    args = interaction_arguments(
        "item/tool/requestUserInput", {"prompt": "What is your name?"}
    )
    assert args["kind"] == "user_input"
    assert args["question"] == "What is your name?"
    assert "options" not in args


def test_answer_command_decision_aliases():
    method = "item/commandExecution/requestApproval"
    assert answer_result_from_output(method, "yes", {}) == {"decision": "accept"}
    assert answer_result_from_output(method, "no", {}) == {"decision": "decline"}
    assert answer_result_from_output(method, "always", {}) == {
        "decision": "acceptForSession"
    }
    assert answer_result_from_output(method, "", {}) == {"decision": "decline"}
    assert answer_result_from_output(method, "accept", {}) == {"decision": "accept"}


def test_answer_permissions_scope():
    method = "item/permissions/requestApproval"
    accept = answer_result_from_output(method, "accept", {"permissions": {"net": True}})
    assert accept == {"permissions": {"net": True}, "scope": "turn"}
    session = answer_result_from_output(
        method, "always", {"permissions": {"net": True}}
    )
    assert session == {"permissions": {"net": True}, "scope": "session"}
    decline = answer_result_from_output(method, "no", {"permissions": {"net": True}})
    assert decline == {"permissions": {}, "scope": "turn"}


def test_answer_selects_structured_available_decision():
    method = "item/commandExecution/requestApproval"
    params = {
        "availableDecisions": [
            {"acceptWithExecpolicyAmendment": {"rule": "x"}},
            "decline",
        ]
    }
    result = answer_result_from_output(method, "acceptWithExecpolicyAmendment", params)
    assert result == {"decision": {"acceptWithExecpolicyAmendment": {"rule": "x"}}}


def test_user_input_passes_structured_json_through():
    method = "item/tool/requestUserInput"
    assert answer_result_from_output(method, '{"response": "Bob"}', {}) == {
        "response": "Bob"
    }
    # Plain text is wrapped.
    assert answer_result_from_output(method, "Bob", {}) == {"response": "Bob"}
