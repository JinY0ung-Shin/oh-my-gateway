"""Subagent selection via ``Task(<agent>)`` allowed_tools entries.

There is no SDK allowlist that hides a plugin/filesystem subagent from the
model (``options.agents`` only *defines* programmatic ones), and the CLI rule
matcher is not a dependable gate for ``Task(<agent>)`` across builds. So the
gateway enforces the client's selection with a PreToolUse hook that denies any
``Task`` call whose ``subagent_type`` is outside the selection.
"""

import pytest

from src.backends.claude import client as client_mod
from src.backends.claude.client import ClaudeCodeCLI, agent_allowlist


class _Options:
    """Minimal stand-in for ClaudeAgentOptions (only the fields touched here)."""

    def __init__(self):
        self.allowed_tools = None
        self.skills = None


@pytest.fixture
def cli():
    return ClaudeCodeCLI.__new__(ClaudeCodeCLI)


class TestAgentAllowlist:
    def test_none_without_granular_entries(self):
        assert agent_allowlist(["Read", "Bash", "Task"]) is None
        assert agent_allowlist(["Read"]) is None
        assert agent_allowlist(None) is None

    def test_collects_granular_entries(self):
        assert agent_allowlist(["Read", "Task(Explore)", "Task(Plan)"]) == {"Explore", "Plan"}

    def test_bare_task_wins_over_granular(self):
        # "allow every subagent" must not be narrowed by a stray granular entry.
        assert agent_allowlist(["Task", "Task(Explore)"]) is None

    def test_agent_spelling_also_accepted(self):
        # DISALLOWED_SUBAGENT_TYPES denies with the Agent(...) spelling.
        assert agent_allowlist(["Agent(Explore)"]) == {"Explore"}


class TestAllowedToolsTranslation:
    def test_granular_entries_replaced_by_bare_task(self, cli):
        options = _Options()
        cli._set_allowed_tools(options, ["Read", "Task(Explore)", "Task(Plan)"])
        # The tool itself stays callable; the hook narrows which subagents run.
        assert "Task" in options.allowed_tools
        assert not any(t.startswith("Task(") for t in options.allowed_tools)
        assert "Read" in options.allowed_tools

    def test_bare_task_kept_and_granular_dropped(self, cli):
        options = _Options()
        cli._set_allowed_tools(options, ["Task", "Task(Explore)"])
        assert options.allowed_tools.count("Task") == 1
        assert not any(t.startswith("Task(") for t in options.allowed_tools)

    def test_no_task_entries_unchanged(self, cli):
        options = _Options()
        cli._set_allowed_tools(options, ["Read", "Bash"])
        assert options.allowed_tools == ["Read", "Bash"]

    def test_skill_translation_still_works(self, cli):
        options = _Options()
        cli._set_allowed_tools(options, ["Skill(weekly-report)", "Task(Explore)"])
        assert options.skills == ["weekly-report"]
        assert "Skill(weekly-report:*)" in options.allowed_tools
        assert "Task" in options.allowed_tools


class TestTaskHook:
    """Selection (deny unlisted) + foreground forcing, in one Task hook."""

    async def _decide(self, cli, allowed, tool_name, tool_input):
        hook = cli._make_task_hook(allowed)
        return await hook({"tool_name": tool_name, "tool_input": tool_input}, None, None)

    async def test_listed_subagent_passes(self, cli):
        out = await self._decide(
            cli, {"Explore"}, "Task", {"subagent_type": "Explore", "run_in_background": False}
        )
        assert out == {}

    async def test_unlisted_subagent_denied(self, cli):
        out = await self._decide(
            cli,
            {"Explore"},
            "Task",
            {"subagent_type": "general-purpose", "run_in_background": False},
        )
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "general-purpose" in decision["permissionDecisionReason"]
        # The reason names what IS available so the model can retry usefully.
        assert "Explore" in decision["permissionDecisionReason"]

    async def test_other_tools_pass_through(self, cli):
        assert await self._decide(cli, {"Explore"}, "Bash", {"command": "ls"}) == {}

    async def test_no_allowlist_allows_any_subagent(self, cli):
        out = await self._decide(
            cli, None, "Task", {"subagent_type": "anything", "run_in_background": False}
        )
        assert out == {}


class TestForegroundForcing:
    """A background subagent's payoff lands after the HTTP turn closes."""

    async def _run(self, cli, tool_input, allowed=None):
        hook = cli._make_task_hook(allowed)
        return await hook({"tool_name": "Task", "tool_input": tool_input}, None, None)

    async def test_explicit_background_is_rewritten(self, cli):
        out = await self._run(cli, {"subagent_type": "Explore", "run_in_background": True})
        updated = out["hookSpecificOutput"]["updatedInput"]
        assert updated["run_in_background"] is False
        # Everything else about the call survives.
        assert updated["subagent_type"] == "Explore"

    async def test_omitted_flag_is_normalized(self, cli):
        """Background is the CLI default — 'not present' is the broken case."""
        out = await self._run(cli, {"subagent_type": "Explore", "prompt": "go"})
        updated = out["hookSpecificOutput"]["updatedInput"]
        assert updated["run_in_background"] is False
        assert updated["prompt"] == "go"

    async def test_explicit_foreground_untouched(self, cli):
        out = await self._run(cli, {"subagent_type": "Explore", "run_in_background": False})
        assert out == {}

    async def test_denial_wins_over_rewrite(self, cli):
        """An unlisted subagent is denied, not quietly made synchronous."""
        out = await self._run(cli, {"subagent_type": "nope"}, allowed={"Explore"})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_disabled_by_env(self, cli, monkeypatch):
        monkeypatch.setattr(client_mod, "FORCE_FOREGROUND_SUBAGENTS", False)
        out = await self._run(cli, {"subagent_type": "Explore", "run_in_background": True})
        assert out == {}


class TestRenamedSubagentTool:
    """The CLI renamed ``Task`` to ``Agent``; a hook bound to the old name is a no-op.

    This is the shape of a real regression: subagents kept running in the
    background (turn ends with "I'll report back", the Task row stays
    "결과 미수신") and the selection allowlist silently stopped enforcing,
    because the PreToolUse matcher never fired on the new tool name.
    """

    async def _run(self, cli, tool_name, tool_input, allowed=None):
        hook = cli._make_task_hook(allowed)
        return await hook({"tool_name": tool_name, "tool_input": tool_input}, None, None)

    async def test_agent_spelling_is_forced_to_foreground(self, cli):
        out = await self._run(cli, "Agent", {"subagent_type": "Explore", "prompt": "go"})
        assert out["hookSpecificOutput"]["updatedInput"]["run_in_background"] is False

    async def test_agent_spelling_enforces_the_allowlist(self, cli):
        out = await self._run(cli, "Agent", {"subagent_type": "nope"}, allowed={"Explore"})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    async def test_legacy_task_spelling_still_governed(self, cli):
        out = await self._run(cli, "Task", {"subagent_type": "Explore"})
        assert out["hookSpecificOutput"]["updatedInput"]["run_in_background"] is False

    def test_both_names_reach_allowed_tools(self, cli):
        """Availability must not depend on which name the installed CLI uses."""
        options = _Options()
        cli._set_allowed_tools(options, ["Read", "Task(Explore)"])
        assert "Task" in options.allowed_tools and "Agent" in options.allowed_tools
        assert not any(t.startswith(("Task(", "Agent(")) for t in options.allowed_tools)


class TestHookWiring:
    """Which tools the gateway governs on every session."""

    def test_skill_and_subagent_tools_are_always_governed(self, cli):
        matchers = [m.matcher for m in cli._pre_tool_use_hooks(None, ["Read"])]
        # Both subagent spellings: hook matchers fire on the tool's real name and
        # current CLI builds renamed Task -> Agent.
        assert matchers[:3] == ["Skill", "Task", "Agent"]

    def test_deferred_tools_are_denied_with_a_reason(self, cli):
        """The CLI's own refusal explains nothing; ours has to."""
        matchers = {m.matcher for m in cli._pre_tool_use_hooks(None, ["Read"])}
        assert {"ScheduleWakeup", "CronCreate"} <= matchers

    async def test_deferred_deny_reason_names_the_constraint(self, cli):
        hook = cli._make_deferred_deny_hook()
        out = await hook({"tool_name": "CronCreate", "tool_input": {}}, None, None)
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "after the" in decision["permissionDecisionReason"]
        # Other tools are untouched
        assert await hook({"tool_name": "Bash", "tool_input": {}}, None, None) == {}

    def test_task_hook_present_without_any_allowlist(self, cli):
        """Foreground forcing must not depend on subagent selection."""
        matchers = cli._pre_tool_use_hooks(None, None)
        for name in ("Task", "Agent"):
            assert any(m.matcher == name and m.hooks for m in matchers)

    def test_sandbox_hook_added_when_enabled(self, cli, monkeypatch):
        monkeypatch.setattr(client_mod, "sandbox_enabled", lambda: True)
        matchers = [m.matcher for m in cli._pre_tool_use_hooks("/tmp/ws", ["Read"])]
        # The catch-all sandbox matcher goes last
        assert matchers[0] == "Skill" and matchers[-1] == ""

    def test_sandbox_hook_skipped_without_cwd(self, cli, monkeypatch):
        monkeypatch.setattr(client_mod, "sandbox_enabled", lambda: True)
        matchers = [m.matcher for m in cli._pre_tool_use_hooks(None, ["Read"])]
        assert "" not in matchers


class TestSessionEffort:
    """``reasoning.effort`` is a session-level knob (the SDK bakes it at create)."""

    def test_request_model_accepts_effort(self):
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(input="hi", reasoning={"effort": "high"})
        assert body.reasoning is not None
        assert body.reasoning.effort == "high"

    def test_request_model_rejects_unknown_level(self):
        import pytest as _pytest
        from pydantic import ValidationError

        from src.response_models import ResponseCreateRequest

        with _pytest.raises(ValidationError):
            ResponseCreateRequest(input="hi", reasoning={"effort": "turbo"})

    def test_absent_reasoning_stays_none(self):
        from src.response_models import ResponseCreateRequest

        assert ResponseCreateRequest(input="hi").reasoning is None
