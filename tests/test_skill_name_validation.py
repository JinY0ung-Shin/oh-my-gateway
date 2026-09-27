"""Skill names the Claude SDK rejects never fail a session (SDK 0.2.129+).

The SDK validates every ``ClaudeAgentOptions.skills`` entry while building the
CLI command and raises ``ValueError`` out of ``connect()`` for a bad one, which
used to surface as a retry-forever 503. The gateway checks per source:

* request ``Skill(<name>)`` rules → 400 ``invalid_skill_rule`` before any
  client work; ``Skill(*)`` / ``Skill(*:*)`` mean "every skill";
* catalog-derived names (skill dirs, MCP prompt commands) → left out.
"""

import logging
import uuid
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import src.main as main
import src.routes.responses as responses_module
from src.backends.claude import client as client_module
from src.backends.claude import slash_commands
from src.backends.claude.client import ClaudeCodeCLI
from src.backends.claude.skill_names import (
    SKILL_WILDCARD_RULES,
    invalid_skill_rule,
    skill_name_problem,
)
from src.constants import DEFAULT_MODEL
from tests.test_main_api_unit import client_context

try:
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
        _validate_skill_name as sdk_validate_skill_name,
    )
except ImportError:  # pragma: no cover - the SDK moved its private validator
    SubprocessCLITransport = None
    sdk_validate_skill_name = None

VALID_NAMES = [
    "summarize",
    "docs-helper:summarize",
    "a:b:c",
    "my skill",
    "x*y",
    "trailing:",
    "weird v2",
    "ünïcode",
    "日本語",
    "one\\backslash",
    "tab nbsp",
]
INVALID_NAMES = [
    "",
    "   ",
    "weird (v2)",
    "probe-srv:greet (MCP)",
    "a,b",
    "x(y",
    "x)y",
    "*",
    "plugin:*",
    "x *",
    "/summarize",
    " leading",
    "trailing ",
    "\tx",
    "x\ny",
    "del\x7f",
    "c1\x85",
    "﻿bom",
    "two\\\\backslashes",
    "ends\\",
    "\ud800lone",
]


def _backend() -> ClaudeCodeCLI:
    return ClaudeCodeCLI.__new__(ClaudeCodeCLI)  # avoid __init__ side effects


@pytest.fixture(autouse=True)
def _fresh_warning_memo(monkeypatch):
    monkeypatch.setattr(client_module, "_WARNED_UNUSABLE_SKILL_NAMES", set())


# ---------------------------------------------------------------------------
# The gateway's copy of the rules tracks the installed SDK
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sdk_validate_skill_name is None, reason="SDK private skill-name validator moved"
)
@pytest.mark.parametrize("name", VALID_NAMES + INVALID_NAMES, ids=ascii)
def test_rules_match_the_installed_sdk(name):
    try:
        sdk_validate_skill_name(name)
        sdk_accepts = True
    except ValueError:
        sdk_accepts = False
    assert (skill_name_problem(name) is None) is sdk_accepts


@pytest.mark.parametrize("name", VALID_NAMES, ids=ascii)
def test_valid_names_have_no_problem(name):
    assert skill_name_problem(name) is None


@pytest.mark.parametrize("name", INVALID_NAMES, ids=ascii)
def test_invalid_names_report_a_reason(name):
    assert isinstance(skill_name_problem(name), str)


def test_invalid_skill_rule_reports_first_bad_granular_rule():
    tools = ["Read", "Skill(ok)", "Skill(a,b)", "Skill(/x)"]
    assert invalid_skill_rule(tools) == ("Skill(a,b)", skill_name_problem("a,b"))


@pytest.mark.parametrize(
    "rule, name",
    [
        ("Skill(weird (v2))", "weird (v2)"),  # the granular regex cannot parse it
        ("Skill(a)b)", "a)b"),
        ("Skill(x *:*)", "x *"),
        ("Skill()", ""),
    ],
)
def test_invalid_skill_rule_reads_the_whole_parenthesised_name(rule, name):
    assert invalid_skill_rule([rule]) == (rule, skill_name_problem(name))


@pytest.mark.parametrize(
    "tools",
    [
        None,
        [],
        ["Read", "Skill", "Skill(:*)", "Bash(ls *)"],
        ["Skill(*)"],
        ["Skill(*:*)"],
        ["Skill(summarize)", "Skill(docs-helper:summarize:*)"],
    ],
)
def test_invalid_skill_rule_accepts_usable_rules(tools):
    assert invalid_skill_rule(tools) is None


def test_wildcard_rules_are_the_star_spellings():
    assert SKILL_WILDCARD_RULES == {"Skill(*)", "Skill(*:*)"}


# ---------------------------------------------------------------------------
# Backend: request rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", sorted(SKILL_WILDCARD_RULES))
def test_star_rule_means_every_skill(rule):
    options = ClaudeAgentOptions(max_turns=1)
    _backend()._configure_tools(
        options, allowed_tools=["Read", rule], disallowed_tools=None
    )

    assert options.skills == "all"
    assert options.allowed_tools == ["Read", "Skill(:*)"]


def test_star_rule_honours_the_skill_kill_switch(monkeypatch):
    monkeypatch.setattr(client_module, "DISALLOWED_TOOLS", ["Skill"])
    options = ClaudeAgentOptions(max_turns=1)
    _backend()._set_allowed_tools(options, ["Read", "Skill(*)"])

    assert options.skills is None
    assert options.allowed_tools == ["Read"]


def test_unusable_granular_rule_never_reaches_the_cli(caplog):
    options = ClaudeAgentOptions(max_turns=1)
    with caplog.at_level(logging.WARNING, logger=client_module.logger.name):
        _backend()._set_allowed_tools(options, ["Read", "Skill(a,b)", "Skill(ok)"])

    assert options.skills == ["ok"]
    assert options.allowed_tools == ["Read", "Skill(ok:*)"]
    assert "'a,b'" in caplog.text


def test_all_granular_rules_unusable_fails_closed():
    options = ClaudeAgentOptions(max_turns=1)
    _backend()._set_allowed_tools(options, ["Read", "Skill(/x)", "Skill( y)"])

    # An empty allowlist hides every skill; unset would expose them all.
    assert options.skills == []
    assert options.allowed_tools == ["Read"]


# ---------------------------------------------------------------------------
# Backend: catalog-derived names
# ---------------------------------------------------------------------------

CATALOG = {
    "review",
    "simplify",
    "weird (v2)",
    "probe-srv:greet (MCP)",
    "plug(1):summarize",
    "summarize",
}


@pytest.fixture
def fake_catalog(monkeypatch):
    async def _fake_available(cwd=None, force=False):
        return set(CATALOG)

    monkeypatch.setattr(slash_commands, "get_available_commands", _fake_available)


async def test_hidden_skills_allowlist_leaves_out_unusable_names(
    monkeypatch, fake_catalog, caplog
):
    monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset({"simplify"}))
    options = ClaudeAgentOptions(max_turns=1)
    options.skills = "all"
    with caplog.at_level(logging.WARNING, logger=client_module.logger.name):
        await _backend()._apply_skills_allowlist(options)

    assert options.skills == ["review", "summarize"]
    assert caplog.text.count("Leaving skills") == 1
    for name in ("weird (v2)", "probe-srv:greet (MCP)", "plug(1):summarize"):
        assert repr(name) in caplog.text


async def test_unusable_names_are_warned_about_once(monkeypatch, fake_catalog, caplog):
    monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset({"simplify"}))
    with caplog.at_level(logging.WARNING, logger=client_module.logger.name):
        for _ in range(3):
            options = ClaudeAgentOptions(max_turns=1)
            options.skills = "all"
            await _backend()._apply_skills_allowlist(options)

    assert caplog.text.count("Leaving skills") == 1


async def test_granular_resolution_leaves_out_unusable_names(monkeypatch, fake_catalog):
    monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset())
    options = ClaudeAgentOptions(max_turns=1)
    options.skills = ["summarize"]
    await _backend()._apply_skills_allowlist(options)

    # "plug(1):summarize" is the plugin-qualified match, but unusable.
    assert options.skills == ["summarize"]


@pytest.mark.skipif(SubprocessCLITransport is None, reason="SDK transport moved")
async def test_sdk_command_builder_accepts_gateway_built_options(
    monkeypatch, fake_catalog
):
    monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset({"simplify"}))
    options = ClaudeAgentOptions(max_turns=1, cli_path="/nonexistent/claude")
    backend = _backend()
    backend._configure_tools(
        options,
        allowed_tools=["Read", "Skill(a,b)", "Skill(summarize)"],
        disallowed_tools=None,
    )
    await backend._apply_skills_allowlist(options)

    cmd = SubprocessCLITransport(prompt="hi", options=options)._build_command()
    assert "--allowedTools" in cmd

    # Control: the same catalog handed to the SDK raw is what used to fail.
    raw = ClaudeAgentOptions(
        max_turns=1, cli_path="/nonexistent/claude", skills=sorted(CATALOG)
    )
    with pytest.raises(ValueError):
        SubprocessCLITransport(prompt="hi", options=raw)._build_command()


# ---------------------------------------------------------------------------
# Route: request rules are a 400, before any client work
# ---------------------------------------------------------------------------


def _assert_invalid_skill_rule(response, rule):
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_skill_rule"
    assert repr(rule) in error["message"]


@pytest.mark.parametrize("rule", ["Skill(a,b)", "Skill(/review)", "Skill(x *)"])
def test_new_session_rejects_unusable_skill_rule(isolated_session_manager, rule):
    create_calls = []

    async def fake_create_client(**kwargs):
        create_calls.append(kwargs)
        return object()

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(responses_module, "get_mcp_servers", return_value={}),
    ):
        mock_cli.create_client = fake_create_client
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "hi",
                "allowed_tools": ["Read", rule],
            },
        )

    _assert_invalid_skill_rule(response, rule)
    assert create_calls == []


def test_continuation_rejects_unusable_skill_rule(isolated_session_manager):
    session_id = str(uuid.uuid4())
    session = isolated_session_manager.get_or_create_session(session_id)
    session.backend = "claude"
    session.turn_counter = 1
    session.client = object()
    update_calls = []

    async def fake_update_request_policy(client, **kwargs):
        update_calls.append(kwargs)

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(responses_module, "get_mcp_servers", return_value={}),
    ):
        mock_cli.update_request_policy = fake_update_request_policy
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "follow up",
                "previous_response_id": responses_module._make_response_id(
                    session_id, 1
                ),
                "allowed_tools": ["Skill(weird (v2))"],
            },
        )

    _assert_invalid_skill_rule(response, "Skill(weird (v2))")
    assert update_calls == []


@pytest.mark.parametrize("rule", sorted(SKILL_WILDCARD_RULES))
def test_star_rule_is_accepted_by_the_route(isolated_session_manager, rule):
    create_calls = []

    async def fake_create_client(**kwargs):
        create_calls.append(kwargs)
        return object()

    with (
        client_context() as (client, mock_cli),
        patch.object(main, "get_mcp_servers", return_value={}),
        patch.object(responses_module, "get_mcp_servers", return_value={}),
    ):
        mock_cli.create_client = fake_create_client
        mock_cli.parse_message.return_value = "Hi"
        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "hi", "allowed_tools": [rule]},
        )

    assert response.status_code == 200
    assert create_calls[0]["allowed_tools"] == [rule]
