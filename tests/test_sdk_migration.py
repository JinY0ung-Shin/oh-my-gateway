#!/usr/bin/env python3
"""
Critical tests for Claude Agent SDK migration.

Tests system prompt formats, message conversion, and basic SDK integration.
"""

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from src.constants import DEFAULT_MODEL


class TestSystemPromptFormats:
    """Test that system prompt formats work correctly with new SDK."""

    def test_preset_append_system_prompt_format(self):
        """Test preset-based system prompt with append field."""
        options = ClaudeAgentOptions(
            max_turns=1,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": "You are a helpful assistant.",
            },
        )
        assert options.system_prompt is not None
        assert isinstance(options.system_prompt, dict)
        assert options.system_prompt["type"] == "preset"
        assert options.system_prompt["append"] == "You are a helpful assistant."

    def test_preset_system_prompt_format(self):
        """Test preset-based system prompt format."""
        options = ClaudeAgentOptions(
            max_turns=1, system_prompt={"type": "preset", "preset": "claude_code"}
        )
        assert options.system_prompt is not None
        assert isinstance(options.system_prompt, dict)
        assert options.system_prompt["type"] == "preset"
        assert options.system_prompt["preset"] == "claude_code"


class TestClaudeAgentOptions:
    """Test ClaudeAgentOptions configuration."""

    def test_basic_options_creation(self):
        """Test creating basic options."""
        options = ClaudeAgentOptions(max_turns=5)
        assert options.max_turns == 5

    def test_options_with_model(self):
        """Test options with model specification."""
        options = ClaudeAgentOptions(max_turns=1, model=DEFAULT_MODEL)
        assert options.model == DEFAULT_MODEL

    def test_options_with_tools(self):
        """Test options with tool restrictions."""
        options = ClaudeAgentOptions(
            max_turns=1, allowed_tools=["Read", "Write"], disallowed_tools=["Bash"]
        )
        assert options.allowed_tools == ["Read", "Write"]
        assert options.disallowed_tools == ["Bash"]


class TestConstants:
    """Test that constants are properly defined."""

    def test_claude_models_defined(self):
        """Test that CLAUDE_MODELS constant exists and has expected models."""
        from src.constants import CLAUDE_MODELS

        assert isinstance(CLAUDE_MODELS, list)
        assert len(CLAUDE_MODELS) > 0

        # Check alias models are included
        assert "opus" in CLAUDE_MODELS
        assert "sonnet" in CLAUDE_MODELS
        assert "haiku" in CLAUDE_MODELS

    def test_default_model_defined(self):
        """Test that DEFAULT_MODEL is set to a valid model."""
        from src.constants import DEFAULT_MODEL, CLAUDE_MODELS

        assert DEFAULT_MODEL in CLAUDE_MODELS

    def test_claude_tools_defined(self):
        """Test that CLAUDE_TOOLS constant exists."""
        from src.constants import CLAUDE_TOOLS

        assert isinstance(CLAUDE_TOOLS, list)
        assert len(CLAUDE_TOOLS) > 0

        # Check common tools are included
        assert "Read" in CLAUDE_TOOLS
        assert "Write" in CLAUDE_TOOLS
        assert "Bash" in CLAUDE_TOOLS


class TestMessageHandling:
    """Test message conversion and handling."""

    def test_message_adapter_import(self):
        """Test that MessageAdapter can be imported."""
        from src.message_adapter import MessageAdapter

        assert MessageAdapter is not None

    def test_filter_content_basic(self):
        """Test basic content filtering."""
        from src.message_adapter import MessageAdapter

        # Test with simple text
        result = MessageAdapter.filter_content("Hello world")
        assert result == "Hello world"

    def test_filter_content_with_images(self):
        """Test content filtering with image references in output."""
        from src.message_adapter import MessageAdapter

        # Test with image reference in Claude's output (string format)
        content = "Here is the result: [Image: example.jpg] as you can see."

        result = MessageAdapter.filter_content(content)
        assert isinstance(result, str)
        # [Image:...] references are now preserved (not stripped)
        assert "[Image: example.jpg]" in result


class TestAPIModels:
    """Test API models and validation."""


class TestClaudeAgentOptionsAllParameters:
    """Test ClaudeAgentOptions with all parameters set at once."""

    def test_all_parameters_at_once(self):
        """Test creating options with system_prompt, model, max_turns, allowed_tools,
        disallowed_tools, permission_mode, cwd, and resume all set simultaneously."""
        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": "Be concise."},
            model="sonnet",
            max_turns=10,
            allowed_tools=["Read", "Write", "Bash"],
            disallowed_tools=["NotebookEdit"],
            permission_mode="bypassPermissions",
            cwd="/tmp",
            resume="session-abc-123",
        )
        assert options.system_prompt == {
            "type": "preset",
            "preset": "claude_code",
            "append": "Be concise.",
        }
        assert options.model == "sonnet"
        assert options.max_turns == 10
        assert options.allowed_tools == ["Read", "Write", "Bash"]
        assert options.disallowed_tools == ["NotebookEdit"]
        assert options.permission_mode == "bypassPermissions"
        assert options.cwd == "/tmp"
        assert options.resume == "session-abc-123"


class TestClaudeAgentOptionsSessionAndExtraArgs:
    """Test ClaudeAgentOptions native session_id and extra_args."""

    def test_extra_args_empty_by_default(self):
        """Test that extra_args defaults to empty dict."""
        options = ClaudeAgentOptions(max_turns=1)
        assert options.extra_args == {}

    def test_native_session_id(self):
        """Test that session_id can be set via the native option."""
        options = ClaudeAgentOptions(
            max_turns=1,
            session_id="my-session-42",
        )
        assert options.session_id == "my-session-42"

    def test_extra_args_with_multiple_entries(self):
        """Test extra_args with multiple key-value pairs."""
        options = ClaudeAgentOptions(
            max_turns=1,
            extra_args={"verbose": None, "debug": "true"},
        )
        assert options.extra_args["verbose"] is None
        assert options.extra_args["debug"] == "true"


class TestMessageAdapterFormatBlocks:
    """Test MessageAdapter.format_block() and format_blocks() with various block types."""

    def setup_method(self):
        from src.message_adapter import MessageAdapter

        self.adapter = MessageAdapter

    def test_format_block_text_string(self):
        """Test format_block with a plain string."""
        result = self.adapter.format_block("hello world")
        assert result == "hello world"

    def test_format_block_text_dict(self):
        """Test format_block with a text dict block."""
        result = self.adapter.format_block({"type": "text", "text": "some text"})
        assert result == "some text"

    def test_format_block_text_object(self):
        """Test format_block with a TextBlock-like object."""

        class FakeTextBlock:
            text = "object text"

        result = self.adapter.format_block(FakeTextBlock())
        assert result == "object text"

    def test_format_block_tool_use_object(self):
        """Test format_block with a ToolUseBlock-like object renders as JSON code block."""
        import json

        class FakeToolUse:
            id = "tool_123"
            name = "Read"
            input = {"path": "/tmp/test.py"}

        result = self.adapter.format_block(FakeToolUse())
        assert result is not None
        assert "```json" in result
        parsed = json.loads(result.strip().strip("`").replace("json\n", "", 1))
        assert parsed["type"] == "tool_use"
        assert parsed["name"] == "Read"
        assert parsed["input"]["path"] == "/tmp/test.py"
        assert parsed["id"] == "tool_123"

    def test_format_block_tool_result_object(self):
        """Test format_block with a ToolResultBlock-like object renders as JSON code block."""
        import json

        class FakeToolResult:
            tool_use_id = "tool_123"
            content = "file contents here"
            is_error = False

        result = self.adapter.format_block(FakeToolResult())
        assert result is not None
        assert "```json" in result
        parsed = json.loads(result.strip().strip("`").replace("json\n", "", 1))
        assert parsed["type"] == "tool_result"
        assert parsed["tool_use_id"] == "tool_123"
        assert parsed["content"] == "file contents here"
        assert parsed["is_error"] is False

    def test_format_block_unrecognized(self):
        """Test format_block returns None for unrecognized block types."""
        result = self.adapter.format_block(12345)
        assert result is None

    def test_format_blocks_mixed(self):
        """Test format_blocks with a mix of text, tool_use, and tool_result blocks."""

        class FakeTextBlock:
            text = "Hello"

        class FakeToolUse:
            id = "t1"
            name = "Bash"
            input = {"command": "ls"}

        blocks = [FakeTextBlock(), FakeToolUse(), "plain string"]
        result = self.adapter.format_blocks(blocks)
        assert result is not None
        assert "Hello" in result
        assert "```json" in result
        assert "plain string" in result

    def test_format_blocks_empty(self):
        """Test format_blocks with empty list returns None."""
        result = self.adapter.format_blocks([])
        assert result is None

    def test_format_blocks_all_unrecognized(self):
        """Test format_blocks with all unrecognized blocks returns None."""
        result = self.adapter.format_blocks([42, 3.14, object()])
        assert result is None


class TestTaskToolCatalog:
    """Task tools must be present in tool catalog after 0.2.82 upgrade."""

    def test_claude_tools_contains_task_tools(self):
        from src.backends.claude.constants import CLAUDE_TOOLS

        for tool in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList"):
            assert tool in CLAUDE_TOOLS, f"{tool} missing from CLAUDE_TOOLS"

    def test_default_allowed_tools_contains_task_tools(self):
        from src.backends.claude.constants import DEFAULT_ALLOWED_TOOLS

        for tool in ("TaskCreate", "TaskUpdate", "TaskGet", "TaskList"):
            assert tool in DEFAULT_ALLOWED_TOOLS, f"{tool} missing from DEFAULT_ALLOWED_TOOLS"

    def test_todowrite_retained_as_default(self):
        """TodoWrite remains the default task-tracking tool; Task* require CLAUDE_CODE_ENABLE_TASKS=1 on the CLI subprocess env."""
        from src.backends.claude.constants import CLAUDE_TOOLS

        assert "TodoWrite" in CLAUDE_TOOLS


class TestCliPathOverride:
    """CLAUDE_CLI_PATH overrides the SDK's bundled CLI, failing safe."""

    def test_unset_returns_none(self, monkeypatch):
        from src.backends.claude import client as client_module

        monkeypatch.setattr(client_module, "CLAUDE_CLI_PATH", None)
        assert client_module._get_cli_path() is None

    def test_executable_path_is_used(self, monkeypatch, tmp_path):
        from src.backends.claude import client as client_module

        cli = tmp_path / "claude"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        monkeypatch.setattr(client_module, "CLAUDE_CLI_PATH", str(cli))
        assert client_module._get_cli_path() == str(cli)

    def test_bogus_path_ignored_with_warning(self, monkeypatch, caplog):
        from src.backends.claude import client as client_module

        monkeypatch.setattr(
            client_module, "CLAUDE_CLI_PATH", "/nonexistent/claude-bin"
        )
        with caplog.at_level("WARNING"):
            assert client_module._get_cli_path() is None
        assert any("CLAUDE_CLI_PATH" in r.message for r in caplog.records)


class TestSkillsOptionMigration:
    """`Skill` allowed_tools entry should be transformed into `skills="all"`."""

    def test_skill_in_allowed_tools_sets_skills_all(self):
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)  # avoid __init__ side effects
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill", "Bash"],
            disallowed_tools=None,
        )

        assert "Skill" not in (options.allowed_tools or [])
        assert getattr(options, "skills", None) == "all"
        # Execution permission needs a *granular* Skill rule; the bare "Skill"
        # rule that skills="all" adds is ignored by the CLI permission matcher.
        assert "Skill(:*)" in (options.allowed_tools or [])

    async def test_hidden_skills_converts_to_allowlist(self, monkeypatch):
        """HIDDEN_SKILLS rewrites skills into discovered-minus-hidden allowlist."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude import client as client_module
        from src.backends.claude import slash_commands
        from src.backends.claude.client import ClaudeCodeCLI

        async def _fake_available(cwd=None, force=False):
            return {"verify", "simplify", "compact", "review"}

        monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset({"verify"}))
        monkeypatch.setattr(slash_commands, "get_available_commands", _fake_available)

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        options.skills = "all"
        await backend._apply_skills_allowlist(options)

        # "verify" hidden, "compact" dropped as an always-blocked builtin.
        assert options.skills == ["review", "simplify"]

    async def test_hidden_skills_unset_is_noop(self, monkeypatch):
        """Without HIDDEN_SKILLS the skills option is left untouched."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude import client as client_module
        from src.backends.claude.client import ClaudeCodeCLI

        monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset())

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        options.skills = "all"
        await backend._apply_skills_allowlist(options)

        assert options.skills == "all"

    def test_granular_skill_rules_set_catalog_allowlist(self):
        """Skill(<name>) entries without bare Skill select exactly those skills."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill(summarize)", "Skill(translate:*)", "Bash"],
            disallowed_tools=None,
        )

        assert getattr(options, "skills", None) == ["summarize", "translate"]
        allowed = options.allowed_tools or []
        # normalized to prefix-matching granular rules; no bare/catch-all rule
        assert "Skill(summarize:*)" in allowed and "Skill(translate:*)" in allowed
        assert "Skill" not in allowed and "Skill(:*)" not in allowed

    def test_bare_skill_wins_over_granular(self):
        """Bare Skill keeps its allow-everything meaning even with granular entries."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Skill", "Skill(summarize)"],
            disallowed_tools=None,
        )

        assert getattr(options, "skills", None) == "all"
        assert options.allowed_tools == ["Skill(:*)"]

    async def test_hidden_skills_narrows_granular_allowlist(self, monkeypatch):
        """A granular skills list is narrowed by HIDDEN_SKILLS, not replaced."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude import client as client_module
        from src.backends.claude import slash_commands
        from src.backends.claude.client import ClaudeCodeCLI

        async def _fake_available(cwd=None, force=False):
            return {"summarize", "translate"}

        monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset({"translate"}))
        monkeypatch.setattr(slash_commands, "get_available_commands", _fake_available)

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        options.skills = ["summarize", "translate"]
        await backend._apply_skills_allowlist(options)

        assert options.skills == ["summarize"]

    def test_no_skill_keeps_skills_unset(self):
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Bash"],
            disallowed_tools=None,
        )

        assert getattr(options, "skills", None) is None
        # No skill access requested → no catch-all skill rule injected.
        assert "Skill(:*)" not in (options.allowed_tools or [])

    def test_skill_in_disallowed_tools_skips_skills_translation(self, monkeypatch):
        """DISALLOWED_TOOLS filter must win over the Skill→skills translation."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        # Operator-configured deny-list takes precedence over the Skill translation.
        monkeypatch.setattr("src.backends.claude.client.DISALLOWED_TOOLS", ["Skill"])

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill", "Bash"],
            disallowed_tools=None,
        )

        assert "Skill" not in (options.allowed_tools or [])
        assert getattr(options, "skills", None) is None
        # Translation skipped entirely → no catch-all skill rule either.
        assert "Skill(:*)" not in (options.allowed_tools or [])

    def test_mcp_default_allowed_tools_translates_skill(self):
        """MCP default allow-list path should not reintroduce deprecated Skill."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_mcp_servers(
            options,
            mcp_servers={"fs": {"type": "stdio", "command": "server"}},
            allowed_tools=None,
        )

        assert "Skill" not in (options.allowed_tools or [])
        # The default-allowlist path allows all MCP tools via ``mcp__*`` (which
        # subsumes ``mcp__fs__*``) so plugin-bundled MCP servers loaded via
        # setting_sources are not locked out when MCP_CONFIG is set.
        assert "mcp__*" in (options.allowed_tools or [])
        assert getattr(options, "skills", None) == "all"
        assert "Skill(:*)" in (options.allowed_tools or [])

    def test_skill_catch_all_alone_does_not_seed_allowlist(self):
        """``Skill(:*)`` carries no name — not a subset request."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill(:*)"],
            disallowed_tools=None,
        )

        assert getattr(options, "skills", None) is None

    def test_qualified_granular_skill_rule_is_recognized(self):
        """A plugin-qualified ``Skill(plugin:name)`` entry is a subset request.

        The CLI registers a plugin skill as ``plugin:skill``, so that is the
        spelling a client naming one has to use. Failing to recognize it would
        leave ``options.skills`` unset — i.e. every skill exposed.
        """
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill(docs-helper:summarize)"],
            disallowed_tools=None,
        )

        assert options.skills == ["docs-helper:summarize"]
        assert "Skill(docs-helper:summarize:*)" in (options.allowed_tools or [])
        assert "Skill(:*)" not in (options.allowed_tools or [])

    def test_qualified_granular_agent_rule_is_recognized(self):
        """``Task(plugin:agent)`` narrows the subagent allowlist.

        Same shape as skills: a plugin subagent's ``subagent_type`` is
        ``plugin:agent``, and an unrecognized rule would fall back to
        allow-every-subagent.
        """
        from src.backends.claude.client import agent_allowlist

        assert agent_allowlist(["Read", "Task(testplugin:reporter)"]) == {
            "testplugin:reporter"
        }
        # A bare Task still means "every subagent".
        assert agent_allowlist(["Task", "Task(testplugin:reporter)"]) is None

    def test_disallowed_skill_strips_granular_rules(self, monkeypatch):
        """Operator Skill deny disables the granular path too."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        monkeypatch.setattr("src.backends.claude.client.DISALLOWED_TOOLS", ["Skill"])

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill(summarize)", "Skill(:*)"],
            disallowed_tools=None,
        )

        assert getattr(options, "skills", None) is None
        assert options.allowed_tools == ["Read"]

    async def test_granular_allowlist_resolves_plugin_qualified(self, monkeypatch):
        """Requested bare names resolve to their plugin-qualified forms."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude import client as client_module
        from src.backends.claude import slash_commands
        from src.backends.claude.client import ClaudeCodeCLI

        async def _fake_available(cwd=None, force=False):
            return {"docs-helper:summarize", "translate", "review"}

        monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset())
        monkeypatch.setattr(slash_commands, "get_available_commands", _fake_available)

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        options.skills = ["summarize", "translate"]
        await backend._apply_skills_allowlist(options)

        # Qualified match joins the allowlist; unselected "review" stays out.
        assert options.skills == [
            "docs-helper:summarize",
            "summarize",
            "translate",
        ]

    async def test_granular_allowlist_subtracts_hidden_by_tail(self, monkeypatch):
        """HIDDEN_SKILLS hides a skill in bare and plugin-qualified form."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude import client as client_module
        from src.backends.claude import slash_commands
        from src.backends.claude.client import ClaudeCodeCLI

        async def _fake_available(cwd=None, force=False):
            return {"docs-helper:verify", "translate"}

        monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset({"verify"}))
        monkeypatch.setattr(slash_commands, "get_available_commands", _fake_available)

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        options.skills = ["verify", "translate"]
        await backend._apply_skills_allowlist(options)

        assert options.skills == ["translate"]

    async def test_granular_allowlist_survives_discovery_failure(self, monkeypatch):
        """Discovery failure keeps the raw requested names (fail closed)."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude import client as client_module
        from src.backends.claude import slash_commands
        from src.backends.claude.client import ClaudeCodeCLI

        async def _boom(cwd=None, force=False):
            raise RuntimeError("no CLI")

        monkeypatch.setattr(client_module, "HIDDEN_SKILLS", frozenset())
        monkeypatch.setattr(slash_commands, "get_available_commands", _boom)

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        options.skills = ["summarize"]
        await backend._apply_skills_allowlist(options)

        assert options.skills == ["summarize"]

    def test_skill_catch_all_rule_not_duplicated(self):
        """A caller that already passes ``Skill(:*)`` must not get it twice."""
        from claude_agent_sdk import ClaudeAgentOptions
        from src.backends.claude.client import ClaudeCodeCLI

        backend = ClaudeCodeCLI.__new__(ClaudeCodeCLI)
        options = ClaudeAgentOptions(max_turns=1)
        backend._configure_tools(
            options,
            allowed_tools=["Read", "Skill", "Skill(:*)"],
            disallowed_tools=None,
        )

        allowed = options.allowed_tools or []
        assert allowed.count("Skill(:*)") == 1
        assert "Skill" not in allowed
        assert getattr(options, "skills", None) == "all"


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
