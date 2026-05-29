"""Unit tests for admin_service — config redaction and session views."""

import os
from unittest.mock import patch

from src.admin_service import (
    get_redacted_config,
    get_session_messages,
)


async def test_admin_backend_health_includes_opencode_metadata(monkeypatch):
    """Backend health includes runtime metadata exposed by OpenCode."""
    from src.admin_service import get_backends_health
    from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel

    def resolve(model):
        if model == "opencode/openai/gpt-5.5":
            return ResolvedModel(model, "opencode", "openai/gpt-5.5")
        return None

    class FakeOpenCodeBackend:
        name = "opencode"

        def supported_models(self):
            return ["opencode/openai/gpt-5.5"]

        async def verify(self):
            return True

        def runtime_metadata(self):
            return {
                "mode": "managed",
                "base_url": "http://127.0.0.1:4096",
                "agent": "general",
                "models": ["opencode/openai/gpt-5.5"],
                "managed_process": True,
            }

    BackendRegistry.clear()
    BackendRegistry.register_descriptor(
        BackendDescriptor("opencode", "opencode", ["opencode/openai/gpt-5.5"], resolve)
    )
    BackendRegistry.register("opencode", FakeOpenCodeBackend())

    health = await get_backends_health()

    opencode = next(item for item in health if item["name"] == "opencode")
    assert opencode["metadata"]["mode"] == "managed"
    assert opencode["metadata"]["base_url"] == "http://127.0.0.1:4096"
    assert opencode["metadata"]["models"] == ["opencode/openai/gpt-5.5"]


# ---------------------------------------------------------------------------
# Config redaction
# ---------------------------------------------------------------------------


class TestRedactedConfig:
    def test_secrets_redacted(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "sk-secret", "API_KEY": "my-key"}):
            config = get_redacted_config()
            env = config["environment"]
            assert env["ANTHROPIC_AUTH_TOKEN"] == "***REDACTED***"
            assert env["API_KEY"] == "***REDACTED***"

    def test_mcp_config_redacted(self):
        mcp_config = '{"servers":{"demo":{"headers":{"Authorization":"Bearer secret-token"}}}}'
        with patch.dict(os.environ, {"MCP_CONFIG": mcp_config}):
            config = get_redacted_config()
            env = config["environment"]
            assert env["MCP_CONFIG"] == "***REDACTED***"

    def test_non_secrets_visible(self):
        config = get_redacted_config()
        assert "runtime" in config
        assert "rate_limits" in config
        assert config["runtime"]["default_model"]  # should have a value


# ---------------------------------------------------------------------------
# Session message history
# ---------------------------------------------------------------------------


class TestGetSessionMessages:
    def test_nonexistent_session(self):
        """Nonexistent session returns None."""
        result = get_session_messages("nonexistent-id")
        assert result is None

    def test_returns_messages(self):
        """Messages are returned from a real session."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-001"
        try:
            session = session_manager.get_or_create_session(sid)
            session.add_messages(
                [
                    Message(role="user", content="Hello"),
                    Message(role="assistant", content="Hi there!"),
                ]
            )

            result = get_session_messages(sid)
            assert result is not None
            assert len(result) == 2
            assert result[0]["role"] == "user"
            assert result[0]["content"] == "Hello"
            assert result[0]["index"] == 0
            assert result[1]["role"] == "assistant"
            assert result[1]["content"] == "Hi there!"
            assert result[1]["thinking"] == []
            assert result[1]["index"] == 1
        finally:
            session_manager.delete_session(sid)

    def test_returns_assistant_thinking(self):
        """Assistant thinking is returned separately from visible content."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-thinking"
        try:
            session = session_manager.get_or_create_session(sid)
            session.add_messages(
                [
                    Message(
                        role="assistant",
                        content="Visible answer",
                        thinking=["Hidden reasoning"],
                    )
                ]
            )

            result = get_session_messages(sid)
            assert result is not None
            assert result[0]["content"] == "Visible answer"
            assert result[0]["thinking"] == ["Hidden reasoning"]
            assert result[0]["thinking_truncated"] is False
        finally:
            session_manager.delete_session(sid)

    def test_truncation(self):
        """Long messages are truncated."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-002"
        try:
            session = session_manager.get_or_create_session(sid)
            long_msg = "x" * 1000
            long_thinking = "y" * 1000
            session.add_messages(
                [Message(role="assistant", content=long_msg, thinking=[long_thinking])]
            )

            result = get_session_messages(sid, truncate=100)
            assert result is not None
            assert len(result[0]["content"]) == 100
            assert result[0]["truncated"] is True
            assert len(result[0]["thinking"][0]) == 100
            assert result[0]["thinking_truncated"] is True

            # No truncation
            result_full = get_session_messages(sid, truncate=0)
            assert len(result_full[0]["content"]) == 1000
            assert result_full[0]["truncated"] is False
            assert len(result_full[0]["thinking"][0]) == 1000
            assert result_full[0]["thinking_truncated"] is False
        finally:
            session_manager.delete_session(sid)

    def test_no_ttl_refresh(self):
        """peek_session does not refresh TTL."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-003"
        try:
            session = session_manager.get_or_create_session(sid)
            session.add_messages([Message(role="user", content="test")])
            original_last_accessed = session.last_accessed

            # Small delay to detect TTL change
            import time

            time.sleep(0.01)

            get_session_messages(sid)
            # peek_session should NOT have changed last_accessed
            assert session.last_accessed == original_last_accessed
        finally:
            session_manager.delete_session(sid)

    def test_string_content(self):
        """String content is displayed correctly."""
        from src.session_manager import session_manager
        from src.models import Message

        sid = "test-admin-history-004"
        try:
            session = session_manager.get_or_create_session(sid)
            session.messages.append(
                Message(role="user", content="Look at this image description")
            )

            result = get_session_messages(sid)
            assert result is not None
            assert "Look at this image description" in result[0]["content"]
        finally:
            session_manager.delete_session(sid)
