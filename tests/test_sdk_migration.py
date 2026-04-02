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

    def test_chat_completion_request_import(self):
        """Test that ChatCompletionRequest can be imported."""
        from src.models import ChatCompletionRequest

        assert ChatCompletionRequest is not None

    def test_chat_completion_request_creation(self):
        """Test creating a ChatCompletionRequest."""
        from src.models import ChatCompletionRequest

        request = ChatCompletionRequest(
            model=DEFAULT_MODEL, messages=[{"role": "user", "content": "Hello"}]
        )

        assert request.model == DEFAULT_MODEL
        assert len(request.messages) == 1


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


class TestMessageAdapterMessagesToPrompt:
    """Test MessageAdapter.messages_to_prompt() conversion logic."""

    def setup_method(self):
        from src.message_adapter import MessageAdapter
        from src.models import Message

        self.adapter = MessageAdapter
        self.Message = Message

    def test_single_user_message(self):
        """Single user message: prompt only, no system."""
        messages = [self.Message(role="user", content="What is 2+2?")]
        prompt, system = self.adapter.messages_to_prompt(messages)
        assert prompt == "What is 2+2?"
        assert system is None

    def test_system_and_user(self):
        """System + user: system extracted, user as prompt."""
        messages = [
            self.Message(role="system", content="You are a calculator."),
            self.Message(role="user", content="What is 2+2?"),
        ]
        prompt, system = self.adapter.messages_to_prompt(messages)
        assert prompt == "What is 2+2?"
        assert system == "You are a calculator."

    def test_multi_turn_conversation(self):
        """Multi-turn (system, user, assistant, user): all parts joined, system extracted."""
        messages = [
            self.Message(role="system", content="Be helpful."),
            self.Message(role="user", content="Hi"),
            self.Message(role="assistant", content="Hello! How can I help?"),
            self.Message(role="user", content="Tell me a joke."),
        ]
        prompt, system = self.adapter.messages_to_prompt(messages)
        assert system == "Be helpful."
        # All conversation parts joined
        assert "Hi" in prompt
        assert "Hello! How can I help?" in prompt
        assert "Tell me a joke." in prompt
        # Parts are separated by double newlines
        assert prompt == "Hi\n\nHello! How can I help?\n\nTell me a joke."

    def test_multiple_system_messages_last_wins(self):
        """When multiple system messages exist, the last one is used."""
        messages = [
            self.Message(role="system", content="First system"),
            self.Message(role="system", content="Second system"),
            self.Message(role="user", content="Hello"),
        ]
        prompt, system = self.adapter.messages_to_prompt(messages)
        assert system == "Second system"

    def test_no_messages(self):
        """Empty message list returns empty prompt and no system."""
        prompt, system = self.adapter.messages_to_prompt([])
        assert prompt == ""
        assert system is None


class TestChatCompletionRequestToClaudeOptions:
    """Test ChatCompletionRequest.to_claude_options() method."""

    def setup_method(self):
        from src.models import ChatCompletionRequest

        self.Request = ChatCompletionRequest

    def test_basic_model_passthrough(self):
        """Test that model is included in options."""
        req = self.Request(
            model="opus",
            messages=[{"role": "user", "content": "hi"}],
        )
        opts = req.to_claude_options()
        assert opts["model"] == "opus"

    def test_no_response_format(self):
        """Test options without response_format only has model."""
        req = self.Request(
            model="sonnet",
            messages=[{"role": "user", "content": "hi"}],
        )
        opts = req.to_claude_options()
        assert "output_format" not in opts
        assert opts["model"] == "sonnet"

    def test_response_format_json_schema(self):
        """Test that json_schema response_format maps to output_format."""
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
        req = self.Request(
            model="opus",
            messages=[{"role": "user", "content": "hi"}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "answer_schema", "schema": schema},
            },
        )
        opts = req.to_claude_options()
        assert "output_format" in opts
        assert opts["output_format"]["type"] == "json_schema"
        assert opts["output_format"]["schema"] == schema

    def test_response_format_non_json_schema_ignored(self):
        """Test that non-json_schema response_format types don't produce output_format."""
        req = self.Request(
            model="opus",
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "text"},
        )
        opts = req.to_claude_options()
        assert "output_format" not in opts

    def test_unsupported_params_not_passed(self):
        """Test that temperature, top_p, max_tokens etc. are NOT in options."""
        req = self.Request(
            model="opus",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.5,
            top_p=0.9,
            max_tokens=1000,
            presence_penalty=0.5,
            frequency_penalty=0.5,
        )
        opts = req.to_claude_options()
        assert "temperature" not in opts
        assert "top_p" not in opts
        assert "max_tokens" not in opts
        assert "presence_penalty" not in opts
        assert "frequency_penalty" not in opts


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
