#!/usr/bin/env python3
"""
Unit tests for src/models.py

Tests all Pydantic models including validators and methods.
These are pure unit tests that don't require a running server.
"""

from datetime import datetime, timezone

from src.models import (
    ContentPart,
    Message,
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
