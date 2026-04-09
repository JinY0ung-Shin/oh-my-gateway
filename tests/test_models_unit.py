#!/usr/bin/env python3
"""
Unit tests for src/models.py

Tests all Pydantic models including validators and methods.
These are pure unit tests that don't require a running server.
"""

import pytest
from src.constants import DEFAULT_MODEL
from datetime import datetime, timezone

from src.models import (
    ContentPart,
    Message,
    StreamOptions,
    ChatCompletionRequest,
    SessionInfo,
    SessionListResponse,
)


class TestContentPart:
    """Test ContentPart model."""

    def test_create_text_content_part(self):
        """Can create a text content part."""
        part = ContentPart(type="text", text="Hello world")
        assert part.type == "text"
        assert part.text == "Hello world"


class TestMessage:
    """Test Message model."""

    def test_create_user_message(self):
        """Can create a user message."""
        msg = Message(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_create_assistant_message(self):
        """Can create an assistant message."""
        msg = Message(role="assistant", content="Hi there")
        assert msg.role == "assistant"
        assert msg.content == "Hi there"

    def test_create_system_message(self):
        """Can create a system message."""
        msg = Message(role="system", content="You are helpful")
        assert msg.role == "system"
        assert msg.content == "You are helpful"

    def test_message_with_name(self):
        """Can create a message with a name."""
        msg = Message(role="user", content="Hello", name="alice")
        assert msg.name == "alice"

    def test_message_normalizes_array_content(self):
        """Array content is normalized to string."""
        content_parts = [
            ContentPart(type="text", text="Part 1"),
            ContentPart(type="text", text="Part 2"),
        ]
        msg = Message(role="user", content=content_parts)
        assert msg.content == "Part 1\nPart 2"

    def test_message_normalizes_dict_content(self):
        """Dict content parts are normalized to string."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": "World"},
        ]
        msg = Message(role="user", content=content)
        assert msg.content == "Hello\nWorld"

    def test_empty_array_content_becomes_empty_string(self):
        """Empty array content becomes empty string."""
        msg = Message(role="user", content=[])
        assert msg.content == ""


class TestStreamOptions:
    """Test StreamOptions model."""

    def test_default_include_usage_is_false(self):
        """Default include_usage is False."""
        options = StreamOptions()
        assert options.include_usage is False

    def test_can_set_include_usage(self):
        """Can set include_usage to True."""
        options = StreamOptions(include_usage=True)
        assert options.include_usage is True


class TestChatCompletionRequest:
    """Test ChatCompletionRequest model."""

    def test_minimal_request(self):
        """Can create request with just messages."""
        request = ChatCompletionRequest(messages=[Message(role="user", content="Hi")])
        assert len(request.messages) == 1

    def test_default_model(self):
        """Default model is set from constants."""
        request = ChatCompletionRequest(messages=[Message(role="user", content="Hi")])
        assert request.model is not None

    def test_temperature_range_validation(self):
        """Temperature must be between 0 and 2."""
        # Valid range
        request = ChatCompletionRequest(
            messages=[Message(role="user", content="Hi")], temperature=1.5
        )
        assert request.temperature == 1.5

        # Invalid - too high
        with pytest.raises(ValueError):
            ChatCompletionRequest(messages=[Message(role="user", content="Hi")], temperature=3.0)

        # Invalid - too low
        with pytest.raises(ValueError):
            ChatCompletionRequest(messages=[Message(role="user", content="Hi")], temperature=-1.0)

    def test_top_p_range_validation(self):
        """top_p must be between 0 and 1."""
        request = ChatCompletionRequest(messages=[Message(role="user", content="Hi")], top_p=0.5)
        assert request.top_p == 0.5

        with pytest.raises(ValueError):
            ChatCompletionRequest(messages=[Message(role="user", content="Hi")], top_p=1.5)

    def test_n_must_be_1(self):
        """n > 1 raises validation error."""
        with pytest.raises(ValueError) as exc_info:
            ChatCompletionRequest(messages=[Message(role="user", content="Hi")], n=3)
        assert "multiple choices" in str(exc_info.value).lower()

    def test_presence_penalty_range(self):
        """presence_penalty must be between -2 and 2."""
        request = ChatCompletionRequest(
            messages=[Message(role="user", content="Hi")], presence_penalty=1.0
        )
        assert request.presence_penalty == 1.0

        with pytest.raises(ValueError):
            ChatCompletionRequest(
                messages=[Message(role="user", content="Hi")], presence_penalty=3.0
            )

    def test_frequency_penalty_range(self):
        """frequency_penalty must be between -2 and 2."""
        request = ChatCompletionRequest(
            messages=[Message(role="user", content="Hi")], frequency_penalty=-1.0
        )
        assert request.frequency_penalty == -1.0

        with pytest.raises(ValueError):
            ChatCompletionRequest(
                messages=[Message(role="user", content="Hi")], frequency_penalty=5.0
            )

    def test_stream_options(self):
        """Can set stream_options."""
        request = ChatCompletionRequest(
            messages=[Message(role="user", content="Hi")],
            stream_options=StreamOptions(include_usage=True),
        )
        assert request.stream_options.include_usage is True

    def test_to_claude_options_basic(self):
        """to_claude_options() returns model."""
        request = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="Hi")],
        )
        options = request.to_claude_options()
        assert options["model"] == DEFAULT_MODEL

    def test_to_claude_options_ignores_max_tokens(self):
        """to_claude_options() ignores max_tokens (unsupported)."""
        request = ChatCompletionRequest(
            messages=[Message(role="user", content="Hi")], max_tokens=500
        )
        options = request.to_claude_options()
        assert "thinking" not in options
        assert "max_tokens" not in options

    def test_to_claude_options_with_task_budget(self):
        """to_claude_options() includes task_budget when set."""
        request = ChatCompletionRequest(
            messages=[Message(role="user", content="Hi")], task_budget=50000
        )
        options = request.to_claude_options()
        assert options["task_budget"] == 50000

    def test_to_claude_options_without_task_budget(self):
        """to_claude_options() omits task_budget when not set."""
        request = ChatCompletionRequest(messages=[Message(role="user", content="Hi")])
        options = request.to_claude_options()
        assert "task_budget" not in options

    def test_task_budget_rejects_non_positive(self):
        """task_budget rejects zero and negative values."""
        with pytest.raises(Exception):
            ChatCompletionRequest(messages=[Message(role="user", content="Hi")], task_budget=0)
        with pytest.raises(Exception):
            ChatCompletionRequest(messages=[Message(role="user", content="Hi")], task_budget=-100)


class TestSessionModels:
    """Test session-related models."""

    def test_session_info(self):
        """Can create SessionInfo."""
        now = datetime.now(timezone.utc)
        info = SessionInfo(
            session_id="test-123",
            created_at=now,
            last_accessed=now,
            message_count=5,
            expires_at=now,
        )
        assert info.session_id == "test-123"
        assert info.message_count == 5

    def test_session_list_response(self):
        """Can create SessionListResponse."""
        now = datetime.now(timezone.utc)
        response = SessionListResponse(
            sessions=[
                SessionInfo(
                    session_id="s1",
                    created_at=now,
                    last_accessed=now,
                    message_count=1,
                    expires_at=now,
                )
            ],
            total=1,
        )
        assert response.total == 1
