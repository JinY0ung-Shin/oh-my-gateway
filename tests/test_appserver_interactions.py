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


def test_command_approval_is_a_single_canonical_question_with_decision_options():
    # The canonical arguments MUST carry a top-level questions[] the UI parser
    # (parseAskUserQuestion) can consume (#174 review §1).
    args = interaction_arguments(
        "item/commandExecution/requestApproval",
        {"command": "rm -rf /", "cwd": "/w", "reason": "cleanup"},
    )
    assert args["kind"] == "command_approval"
    assert isinstance(args["questions"], list) and len(args["questions"]) == 1
    q = args["questions"][0]
    assert "rm -rf /" in q["question"]
    labels = [o["label"] for o in q["options"]]
    assert "accept" in labels and "decline" in labels
    # Approval context is carried as harmless extras for a richer renderer.
    assert args["command"] == "rm -rf /"
    assert args["cwd"] == "/w"


def test_available_decisions_are_surfaced_as_options():
    args = interaction_arguments(
        "item/commandExecution/requestApproval",
        {"command": "ls", "availableDecisions": ["accept", "decline"]},
    )
    assert [o["label"] for o in args["questions"][0]["options"]] == [
        "accept",
        "decline",
    ]


def test_request_user_input_maps_native_questions_to_canonical_questions():
    # Real v2 requestUserInput params (#174 review §1).
    args = interaction_arguments(
        "item/tool/requestUserInput",
        {
            "threadId": "t",
            "turnId": "u",
            "itemId": "i",
            "isBlocking": True,
            "questions": [
                {
                    "id": "q1",
                    "header": "Name",
                    "question": "What is your name?",
                    "isOther": False,
                    "isSecret": False,
                    "options": ["Alice", "Bob"],
                },
                {
                    "id": "q2",
                    "header": "Secret",
                    "question": "API key?",
                    "isSecret": True,
                    "options": [],
                },
            ],
        },
    )
    assert args["kind"] == "user_input"
    questions = args["questions"]
    assert [q["question"] for q in questions] == ["What is your name?", "API key?"]
    assert questions[0]["header"] == "Name"
    assert [o["label"] for o in questions[0]["options"]] == ["Alice", "Bob"]
    assert questions[1]["isSecret"] is True
    # Native question ids are NOT leaked into the canonical question shape.
    assert "id" not in questions[0]


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


def test_request_user_input_answer_maps_positional_answers_to_native_qids():
    # The BFF submits function_call_output.output = {"answers": string[][]}
    # positional by question index; translate back to native
    # {answers: {[questionId]: {answers: [...]}}} (#174 review §1).
    method = "item/tool/requestUserInput"
    params = {
        "questions": [
            {"id": "q1", "question": "Name?"},
            {"id": "q2", "question": "Colors?"},
        ]
    }
    output = '{"answers": [["Bob"], ["red", "blue"]]}'
    result = answer_result_from_output(method, output, params)
    assert result == {
        "answers": {
            "q1": {"answers": ["Bob"]},
            "q2": {"answers": ["red", "blue"]},
        }
    }


def test_command_approval_answer_round_trips_from_positional_answers():
    # An approval renders as one question, so the UI answer is [["accept"]].
    method = "item/commandExecution/requestApproval"
    assert answer_result_from_output(method, '{"answers": [["accept"]]}', {}) == {
        "decision": "accept"
    }
    assert answer_result_from_output(method, '{"answers": [["no"]]}', {}) == {
        "decision": "decline"
    }
