"""Subagent selection via ``Task(<agent>)`` allowed_tools entries.

There is no SDK allowlist that hides a plugin/filesystem subagent from the
model (``options.agents`` only *defines* programmatic ones), and the CLI rule
matcher is not a dependable gate for ``Task(<agent>)`` across builds. So the
gateway enforces the client's selection with a PreToolUse hook that denies any
``Task`` call whose ``subagent_type`` is outside the selection.
"""

import pytest

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


class TestAgentAllowlistHook:
    async def _decide(self, cli, allowed, tool_name, tool_input):
        hook = cli._make_agent_allowlist_hook(allowed)
        return await hook({"tool_name": tool_name, "tool_input": tool_input}, None, None)

    async def test_listed_subagent_passes(self, cli):
        assert await self._decide(cli, {"Explore"}, "Task", {"subagent_type": "Explore"}) == {}

    async def test_unlisted_subagent_denied(self, cli):
        out = await self._decide(cli, {"Explore"}, "Task", {"subagent_type": "general-purpose"})
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "general-purpose" in decision["permissionDecisionReason"]
        # The reason names what IS available so the model can retry usefully.
        assert "Explore" in decision["permissionDecisionReason"]

    async def test_other_tools_pass_through(self, cli):
        assert await self._decide(cli, {"Explore"}, "Bash", {"command": "ls"}) == {}

    async def test_missing_subagent_type_passes(self, cli):
        # Nothing to check against — leave the CLI's own handling in charge.
        assert await self._decide(cli, {"Explore"}, "Task", {}) == {}
