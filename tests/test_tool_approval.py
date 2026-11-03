"""MCP tool approval gate (ChatDRAGON #798): policy, digest, decision, wire shape.

The bundled-CLI behavior (the gate actually parks and runs once / zero times) is pinned in
``tests/test_cli_tool_approval.py``; this file covers the gateway-side contract around it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.backends.claude import tool_approval
from src.backends.claude.tool_approval import ApprovalPolicy, split_approval
from src.response_models import FunctionCallOutputItem
from src.sse_builders import make_function_call_response_sse

# -- policy -------------------------------------------------------------------


def test_the_key_is_stripped_and_star_or_names_are_read():
    cleaned, rules = split_approval(
        {
            "knob": {"type": "http", "url": "https://k/mcp", "approvalTools": "*"},
            "docs": {
                "type": "http",
                "url": "https://d/mcp",
                "approvalTools": ["publish"],
            },
            "plain": {"type": "http", "url": "https://p/mcp"},
        }
    )
    assert all("approvalTools" not in c for c in cleaned.values())
    assert rules == {"knob": frozenset({"*"}), "docs": frozenset({"publish"})}


@pytest.mark.parametrize("value", [True, 1, {}, [], ["ok", 3], ["*"], "all", "", None])
def test_a_malformed_value_gates_the_whole_server(value, caplog):
    """Fail closed the other way from readOnlyTools: a typo must not drop the approval."""
    cleaned, rules = split_approval({"knob": {"type": "http", "approvalTools": value}})
    assert (
        rules == {"knob": frozenset({"*"})} and "approvalTools" not in cleaned["knob"]
    )
    assert any("approvalTools" in r.getMessage() for r in caplog.records)


def test_star_gates_everything_but_the_operators_read_only_tools():
    policy = ApprovalPolicy(
        {"knob": frozenset({"*"})}, {"knob": frozenset({"list_mail"})}
    )
    assert policy.target("mcp__knob__send_mail") == ("knob", "send_mail")
    assert policy.target("mcp__knob__send_mail_v2") == (
        "knob",
        "send_mail_v2",
    ), "renamed/new writes park"
    assert policy.target("mcp__knob__list_mail") is None
    assert policy.target("mcp__other__send_mail") is None
    assert policy.target("Bash") is None
    assert policy.target("mcp__knob__") is None


def test_an_explicit_list_gates_only_those_tools():
    policy = ApprovalPolicy({"docs": frozenset({"publish"})}, {})
    assert policy.target("mcp__docs__publish") == ("docs", "publish")
    assert policy.target("mcp__docs__search") is None


def test_read_only_star_does_not_cancel_approval_star():
    """Contradictory config (everything read-only AND everything approved) fails closed."""
    policy = ApprovalPolicy({"knob": frozenset({"*"})}, {"knob": frozenset({"*"})})
    assert policy.target("mcp__knob__send_mail") == ("knob", "send_mail")


def test_the_longest_configured_server_name_wins():
    policy = ApprovalPolicy({"a": frozenset({"x__y"}), "a__x": frozenset({"*"})}, {})
    assert policy.target("mcp__a__x__y") == ("a__x", "y")


def test_an_empty_policy_is_falsy():
    assert not ApprovalPolicy({}, {})
    assert ApprovalPolicy({"knob": frozenset({"*"})}, {})


# -- digest and decision --------------------------------------------------------


def test_the_digest_is_canonical_and_follows_the_arguments():
    a = tool_approval.args_digest({"to": "a", "subject": "s", "cc": ["b", "c"]})
    b = tool_approval.args_digest({"cc": ["b", "c"], "subject": "s", "to": "a"})
    assert a == b and a.startswith("sha256:")
    assert tool_approval.args_digest({"to": "a", "subject": "S", "cc": ["b", "c"]}) != a
    assert tool_approval.args_digest({"to": "a", "subject": "s", "cc": ["c", "b"]}) != a


def test_only_an_approval_for_the_same_digest_approves():
    digest = tool_approval.args_digest({"to": "a"})
    ok = json.dumps({"decision": "approve", "argsDigest": digest})
    assert tool_approval.is_approved(ok, digest)
    for answer in [
        None,
        "",
        "approve",
        "yes",
        json.dumps({"decision": "approve"}),
        json.dumps(
            {
                "decision": "approve",
                "argsDigest": tool_approval.args_digest({"to": "b"}),
            }
        ),
        json.dumps({"decision": "deny", "argsDigest": digest}),
        json.dumps({"decision": "APPROVE", "argsDigest": digest}),
        json.dumps(["approve", digest]),
    ]:
        assert not tool_approval.is_approved(answer, digest), answer


@pytest.mark.parametrize(
    ("header", "capable"),
    [
        (None, False),
        ("", False),
        ("tool-approval", True),
        ("Tool-Approval", True),
        ("something, tool-approval", True),
        ("tool-approvals", False),
        ("x tool-approval", False),
    ],
)
def test_the_capability_header(header, capable):
    assert tool_approval.client_supports_approval(header) is capable


# -- wire shape ---------------------------------------------------------------


_APPROVAL = tool_approval.approval_payload("knob", "send_mail", "sha256:" + "a" * 64)


def test_the_function_call_item_carries_approval_only_when_parked_for_one():
    plain = FunctionCallOutputItem(
        id="fc_1", call_id="1", name="AskUserQuestion", arguments="{}"
    )
    assert "approval" not in plain.model_dump()
    assert "approval" not in plain.model_dump(mode="json", exclude_none=True)
    gated = FunctionCallOutputItem(
        id="fc_2",
        call_id="2",
        name="mcp__knob__send_mail",
        arguments="{}",
        approval=_APPROVAL,
    )
    assert gated.model_dump()["approval"] == _APPROVAL


def test_the_sse_function_call_carries_approval_only_when_given():
    def item(sse: str):
        data = json.loads(sse.split("data: ", 1)[1])
        return data["item"]

    assert "approval" not in item(
        make_function_call_response_sse("r", "c", "AskUserQuestion", "{}", 3)
    )
    assert (
        item(
            make_function_call_response_sse(
                "r", "c", "mcp__knob__x", "{}", 3, approval=_APPROVAL
            )
        )["approval"]
        == _APPROVAL
    )


def test_the_requires_action_response_carries_the_pending_approval():
    from src.routes.responses import _build_requires_action_response

    tc = {
        "call_id": "c1",
        "name": "mcp__knob__send_mail",
        "arguments": {"to": "a"},
        "approval": _APPROVAL,
    }
    resp = _build_requires_action_response("resp_1", "sonnet", tc, None)
    assert resp.status == "requires_action"
    assert resp.output[0].approval == _APPROVAL
    assert json.loads(resp.output[0].arguments) == {"to": "a"}


def test_every_options_build_strips_approval_tools_and_keeps_the_policy():
    from claude_agent_sdk import ClaudeAgentOptions

    from src.backends.claude.client import ClaudeCodeCLI

    cli = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
    options = ClaudeAgentOptions()
    with (
        patch(
            "src.backends.claude.client.resolve_mcp_servers", side_effect=lambda s: s
        ),
        patch.object(
            ClaudeCodeCLI,
            "_set_allowed_tools",
            lambda self, o, tools: setattr(o, "allowed_tools", tools),
        ),
    ):
        cli._configure_mcp_servers(
            options,
            {
                "knob": {
                    "type": "http",
                    "url": "https://k/mcp",
                    "approvalTools": "*",
                    "readOnlyTools": ["list_mail"],
                }
            },
            None,
            None,
        )
    assert "approvalTools" not in options.mcp_servers["knob"]
    assert "readOnlyTools" not in options.mcp_servers["knob"]
    policy = options._gateway_approval_policy
    assert policy.target("mcp__knob__send_mail") == ("knob", "send_mail")
    assert policy.target("mcp__knob__list_mail") is None


async def test_the_hook_has_no_opinion_on_tools_it_does_not_gate():
    from src.backends.claude.client import make_tool_approval_hook
    from src.session_manager import Session

    session = Session(session_id="s", backend="claude")
    session.tool_approval_capable = True
    hook = make_tool_approval_hook(
        session, ApprovalPolicy({"knob": frozenset({"send_mail"})}, {})
    )
    assert (
        await hook({"tool_name": "mcp__knob__list_mail", "tool_input": {}}, "t1", None)
        == {}
    )
    assert await hook({"tool_name": "Bash", "tool_input": {}}, "t2", None) == {}
    assert session.pending_tool_call is None


async def test_the_hook_denies_without_parking_when_the_client_cannot_approve():
    from src.backends.claude.client import make_tool_approval_hook
    from src.session_manager import Session

    session = Session(session_id="s", backend="claude")
    hook = make_tool_approval_hook(
        session, ApprovalPolicy({"knob": frozenset({"*"})}, {})
    )
    out = await hook(
        {"tool_name": "mcp__knob__send_mail", "tool_input": {"to": "a"}}, "t1", None
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert session.pending_tool_call is None


def test_the_route_records_the_capability_on_every_request(monkeypatch):
    """A continuation from an older client must not inherit a newer client's promise."""
    from fastapi.testclient import TestClient

    from src.main import app
    from src.routes import responses as responses_route
    from src.session_manager import Session

    session = Session(session_id="cap", backend="claude")
    captured = []

    async def fake_resolve(body, backend):
        return "cap", session

    async def stop_after_capture(*args, **kwargs):
        captured.append(session.tool_approval_capable)
        raise RuntimeError("stop")

    monkeypatch.setattr(responses_route, "_resolve_response_session", fake_resolve)
    monkeypatch.setattr(
        responses_route, "_resolve_response_workspace", stop_after_capture
    )
    monkeypatch.setattr(responses_route, "verify_api_key", lambda *a, **k: _noop())
    with TestClient(app, raise_server_exceptions=False) as client:
        for header, expected in [
            ({"X-Gateway-Capabilities": "tool-approval"}, True),
            ({}, False),
        ]:
            client.post(
                "/v1/responses", json={"model": "sonnet", "input": "hi"}, headers=header
            )
            assert captured[-1] is expected


async def _noop():
    return None
