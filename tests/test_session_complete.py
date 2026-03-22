#!/usr/bin/env python3
"""
Unit tests for session routing and lifecycle.
"""

import contextlib
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.constants import DEFAULT_MODEL
from src.backend_registry import ResolvedModel


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_mock_msg(text="response"):
    """Build an assistant message dict."""
    return {
        "type": "assistant",
        "content": [{"type": "text", "text": text}],
    }


def _make_result_msg(text="response"):
    """Build a success result message dict."""
    return {
        "type": "result",
        "subtype": "success",
        "result": text,
        "stop_reason": "end_turn",
    }


def make_mock_run_completion(reply_text="response", error=None):
    """Factory that returns ``(mock_run_completion, captured_kwargs)``.

    Parameters
    ----------
    reply_text : str
        Text the mock assistant will yield.
    error : dict | None
        If provided, yield this single error message instead of the normal
        assistant + result pair.
    """
    captured_kwargs: dict = {}

    async def mock_run_completion(**kwargs):
        captured_kwargs.update(kwargs)
        if error is not None:
            yield error
            return
        yield _make_mock_msg(reply_text)
        yield _make_result_msg(reply_text)

    return mock_run_completion, captured_kwargs


def _attach_build_options(mock_backend):
    """Attach real ClaudeCodeCLI.build_options to a mock and register it."""
    from src.backends.base import BackendRegistry
    from src.backends.claude.client import ClaudeCodeCLI

    mock_backend.build_options = ClaudeCodeCLI.build_options.__get__(
        mock_backend, type(mock_backend)
    )
    BackendRegistry.register("claude", mock_backend)


@contextlib.contextmanager
def mock_backend_dispatch(mock_run, parse_return="response"):
    """Context manager that patches _resolve_and_get_backend + _validate_backend_auth.

    The mock backend wraps ``mock_run`` as ``run_completion`` and returns
    ``parse_return`` from ``parse_message``.
    """
    mock_backend = MagicMock()
    mock_backend.run_completion = mock_run
    mock_backend.parse_message = MagicMock(return_value=parse_return)
    mock_backend.estimate_token_usage = MagicMock(
        return_value={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )
    resolved = ResolvedModel(
        public_model=DEFAULT_MODEL,
        backend="claude",
        provider_model=DEFAULT_MODEL,
    )
    _attach_build_options(mock_backend)

    with (
        patch(
            "src.main._resolve_and_get_backend",
            return_value=(resolved, mock_backend),
        ),
        patch(
            "src.routes.chat._resolve_and_get_backend",
            return_value=(resolved, mock_backend),
        ),
        patch("src.routes.chat._validate_backend_auth"),
        patch("src.backends.claude.client.get_mcp_servers", return_value={}),
    ):
        yield mock_backend


class TestSessionRouting:
    """Test that session_id routes to fresh query() with session_id/resume."""

    @pytest.mark.asyncio
    async def test_streaming_with_session_id_uses_run_completion(self):
        """Streaming request with session_id uses run_completion with session_id param."""
        mock_run, captured_kwargs = make_mock_run_completion()

        with mock_backend_dispatch(mock_run):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Hi")],
                session_id="test-routing-stream",
                stream=True,
            )

            chunks = []
            async for chunk in generate_streaming_response(req, "test-req-id"):
                chunks.append(chunk)

            # First turn: session_id should be passed (not resume)
            assert captured_kwargs.get("session_id") == "test-routing-stream"
            assert captured_kwargs.get("resume") is None
            assert any("data:" in c for c in chunks)

    @pytest.mark.asyncio
    async def test_streaming_without_session_id_uses_run_completion(self):
        """Streaming request without session_id uses run_completion (stateless)."""
        mock_run, captured_kwargs = make_mock_run_completion("hello")

        with mock_backend_dispatch(mock_run, "hello"):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Hi")],
                stream=True,
            )

            chunks = []
            async for chunk in generate_streaming_response(req, "test-req-id"):
                chunks.append(chunk)

            assert any("data:" in c for c in chunks)
            # No session_id or resume in stateless mode
            assert captured_kwargs.get("session_id") is None
            assert captured_kwargs.get("resume") is None

    @pytest.mark.asyncio
    async def test_session_resume_on_second_turn(self):
        """Second turn with session_id uses resume instead of session_id."""
        from src.session_manager import session_manager

        # Simulate first turn already happened: session exists with messages
        session = session_manager.get_or_create_session("test-resume")
        from src.models import Message as Msg

        session.add_messages([Msg(role="user", content="first")])

        mock_run, captured_kwargs = make_mock_run_completion()

        with mock_backend_dispatch(mock_run):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="second turn")],
                session_id="test-resume",
                stream=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            # Second turn: resume should be set, session_id should be None
            assert captured_kwargs.get("resume") == "test-resume"
            assert captured_kwargs.get("session_id") is None

    @pytest.mark.asyncio
    async def test_streaming_session_passes_system_prompt_only_on_first_turn(self):
        """Session mode forwards system_prompt on first turn and suppresses it on resume."""
        captured_calls = []

        async def mock_run_completion(**kwargs):
            captured_calls.append(dict(kwargs))
            yield _make_mock_msg()
            yield _make_result_msg()

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run_completion
        mock_backend.parse_message = MagicMock(return_value="response")
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )

        from src.backend_registry import ResolvedModel

        _attach_build_options(mock_backend)

        _resolved = ResolvedModel(
            public_model=DEFAULT_MODEL, backend="claude", provider_model=DEFAULT_MODEL
        )
        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(_resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(_resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.backends.claude.client.get_mcp_servers", return_value={}),
        ):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            first_req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[
                    Message(role="system", content="session system prompt"),
                    Message(role="user", content="first turn"),
                ],
                session_id="test-stream-system-prompt",
                stream=True,
            )
            async for _ in generate_streaming_response(first_req, "req-first"):
                pass

            second_req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="second turn")],
                session_id="test-stream-system-prompt",
                stream=True,
            )
            async for _ in generate_streaming_response(second_req, "req-second"):
                pass

            assert len(captured_calls) == 2
            assert captured_calls[0]["system_prompt"] == "session system prompt"
            assert captured_calls[0]["session_id"] == "test-stream-system-prompt"
            assert captured_calls[0]["resume"] is None
            assert captured_calls[1]["system_prompt"] is None
            assert captured_calls[1]["session_id"] is None
            assert captured_calls[1]["resume"] == "test-stream-system-prompt"


class TestAsyncShutdownIntegration:
    """Test that lifespan uses async_shutdown."""

    @pytest.mark.asyncio
    async def test_lifespan_calls_async_shutdown(self):
        """Lifespan shutdown calls session_manager.async_shutdown()."""
        mock_sm = MagicMock()
        mock_sm.start_cleanup_task = MagicMock()
        mock_sm.async_shutdown = AsyncMock()

        with (
            patch("src.main.session_manager", mock_sm),
            patch("src.routes.chat.session_manager", mock_sm),
            patch(
                "src.main.validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ),
            patch("src.main.discover_backends"),
            patch("src.main._verify_backends", new_callable=AsyncMock),
        ):
            from src.main import lifespan, app

            async with lifespan(app):
                pass

            mock_sm.async_shutdown.assert_called_once()


class TestNonStreamingSessionRequest:
    """Test non-streaming (stream=False) with session_id returns proper JSON."""

    @pytest.mark.asyncio
    async def test_non_streaming_session_returns_json_response(self):
        """Non-streaming request with session_id should return ChatCompletionResponse."""
        mock_run, captured_kwargs = make_mock_run_completion("non-stream reply")

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run
        mock_backend.parse_message = MagicMock(return_value="non-stream reply")
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.routes.chat.verify_api_key", new_callable=AsyncMock),
        ):
            from src.main import app
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": False,
                        "session_id": "test-non-stream-session",
                    },
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["id"].startswith("chatcmpl-")
            assert data["choices"][0]["message"]["role"] == "assistant"
            assert data["choices"][0]["message"]["content"] == "non-stream reply"
            assert "usage" in data

            # Verify session_id was passed (first turn, so session_id not resume)
            assert captured_kwargs.get("session_id") == "test-non-stream-session"
            assert captured_kwargs.get("resume") is None
            assert captured_kwargs.get("stream") is False

    @pytest.mark.asyncio
    async def test_non_streaming_session_error_result_returns_500(self):
        """Non-streaming session requests return 500 when SDK yields only an error result."""
        mock_run, captured_kwargs = make_mock_run_completion(
            error={
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "error_message": "SDK failed",
            }
        )

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run
        mock_backend.parse_message = MagicMock(return_value=None)
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.routes.chat.verify_api_key", new_callable=AsyncMock),
        ):
            from src.main import app
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": False,
                        "session_id": "test-non-stream-error",
                    },
                )

            assert resp.status_code == 500
            assert resp.json()["error"]["message"] == "No response from claude backend"
            assert captured_kwargs.get("session_id") == "test-non-stream-error"
            assert captured_kwargs.get("resume") is None
            assert captured_kwargs.get("stream") is False

    @pytest.mark.asyncio
    async def test_non_streaming_session_resume_on_second_turn(self):
        """Second non-streaming turn uses resume instead of session_id."""
        from src.session_manager import session_manager
        from src.models import Message

        session = session_manager.get_or_create_session("test-non-stream-resume")
        session.add_messages([Message(role="user", content="first turn")])

        mock_run, captured_kwargs = make_mock_run_completion("follow-up reply")

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run
        mock_backend.parse_message = MagicMock(return_value="follow-up reply")
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.routes.chat.verify_api_key", new_callable=AsyncMock),
        ):
            from src.main import app
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "second turn"}],
                        "stream": False,
                        "session_id": "test-non-stream-resume",
                    },
                )

            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"] == "follow-up reply"
            assert captured_kwargs.get("resume") == "test-non-stream-resume"
            assert captured_kwargs.get("session_id") is None
            assert captured_kwargs.get("stream") is False


class TestSessionEnableTools:
    """Test that enable_tools=True passes allowed_tools and permission_mode."""

    @pytest.mark.asyncio
    async def test_enable_tools_passes_allowed_tools_and_permission(self):
        """When enable_tools=True, allowed_tools and permission_mode are forwarded."""
        from src.constants import DEFAULT_ALLOWED_TOOLS

        mock_run, captured_kwargs = make_mock_run_completion("tool reply")

        with mock_backend_dispatch(mock_run, "tool reply"):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Use tools")],
                session_id="test-tools-session",
                stream=True,
                enable_tools=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            allowed = captured_kwargs.get("allowed_tools", [])
            for tool in DEFAULT_ALLOWED_TOOLS:
                assert tool in allowed, f"{tool} missing from allowed_tools"
            assert captured_kwargs.get("permission_mode") is not None
            # disallowed_tools should NOT be set when tools are enabled
            assert captured_kwargs.get("disallowed_tools") is None

    @pytest.mark.asyncio
    async def test_disable_tools_passes_disallowed_tools(self):
        """When enable_tools=False (default), disallowed_tools are set."""
        mock_run, captured_kwargs = make_mock_run_completion("no-tool reply")

        with mock_backend_dispatch(mock_run, "no-tool reply"):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="No tools")],
                session_id="test-no-tools-session",
                stream=True,
                enable_tools=False,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            assert captured_kwargs.get("disallowed_tools") is not None
            assert captured_kwargs.get("allowed_tools") is None
            assert captured_kwargs.get("max_turns") == 1


class TestStreamingErrorHandling:
    """Test that errors in session mode still emit valid SSE."""

    @pytest.mark.asyncio
    async def test_streaming_error_emits_valid_sse(self):
        """When run_completion raises, the stream should emit a valid SSE error event."""

        async def mock_run_completion(**kwargs):
            raise RuntimeError("SDK connection failed")
            # Make it an async generator
            yield  # pragma: no cover

        with mock_backend_dispatch(mock_run_completion):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Hi")],
                session_id="test-error-session",
                stream=True,
            )

            chunks = []
            async for chunk in generate_streaming_response(req, "test-req-id"):
                chunks.append(chunk)

            # Should have at least one SSE data line with error
            assert len(chunks) > 0
            error_chunks = [c for c in chunks if "error" in c]
            assert len(error_chunks) > 0
            # Verify it is valid SSE format (starts with "data: ")
            for ec in error_chunks:
                assert ec.startswith("data: ")


class TestAssistantResponseStoredInSession:
    """Test that assistant response is stored via session_manager.add_assistant_response."""

    @pytest.mark.asyncio
    async def test_streaming_stores_assistant_response(self):
        """After streaming completes, add_assistant_response is called with the right session_id."""
        mock_run, _ = make_mock_run_completion("stored reply")

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run
        mock_backend.parse_message = MagicMock(return_value="stored reply")
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.routes.chat.session_manager") as mock_sm,
        ):
            # get_or_create_session must return a mock session
            mock_session = MagicMock()
            mock_session.messages = []  # first turn (new session)
            mock_session.lock = asyncio.Lock()
            mock_session.backend = "claude"
            mock_sm.get_or_create_session.return_value = mock_session

            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="Store this")],
                session_id="test-store-session",
                stream=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            # Verify add_assistant_response was called with correct session_id
            mock_sm.add_assistant_response.assert_called_once()
            call_args = mock_sm.add_assistant_response.call_args
            assert call_args[0][0] == "test-store-session"
            assert call_args[0][1].role == "assistant"
            assert call_args[0][1].content == "stored reply"

    @pytest.mark.asyncio
    async def test_no_session_skips_add_assistant_response(self):
        """Stateless request (no session_id) should NOT call add_assistant_response."""
        mock_run, _ = make_mock_run_completion("stateless")

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run
        mock_backend.parse_message = MagicMock(return_value="stateless")
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.routes.chat.session_manager") as mock_sm,
        ):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="No session")],
                stream=True,
            )

            async for _ in generate_streaming_response(req, "test-req-id"):
                pass

            mock_sm.add_assistant_response.assert_not_called()


class TestMultipleSessionsIsolation:
    """Test that two different session_ids do not interfere with each other."""

    @pytest.mark.asyncio
    async def test_two_sessions_are_isolated(self):
        """Messages from session A should not appear in session B."""
        from src.session_manager import session_manager
        from src.models import Message

        # Create two independent sessions
        session_a = session_manager.get_or_create_session("test-iso-a")
        session_b = session_manager.get_or_create_session("test-iso-b")

        session_a.add_messages([Message(role="user", content="I am session A")])
        session_b.add_messages([Message(role="user", content="I am session B")])

        # Verify isolation
        assert len(session_a.messages) == 1
        assert len(session_b.messages) == 1
        assert session_a.messages[0].content == "I am session A"
        assert session_b.messages[0].content == "I am session B"

        # Add more to A, B should be unaffected
        session_a.add_messages([Message(role="assistant", content="Hello A")])
        assert len(session_a.messages) == 2
        assert len(session_b.messages) == 1

    @pytest.mark.asyncio
    async def test_deleting_one_session_does_not_affect_other(self):
        """Deleting session A should not affect session B."""
        from src.session_manager import session_manager
        from src.models import Message

        session_manager.get_or_create_session("test-del-a")
        session_b = session_manager.get_or_create_session("test-del-b")
        session_b.add_messages([Message(role="user", content="B content")])

        session_manager.delete_session("test-del-a")

        # Session B should still exist and be intact
        retrieved_b = session_manager.get_session("test-del-b")
        assert retrieved_b is not None
        assert len(retrieved_b.messages) == 1
        assert retrieved_b.messages[0].content == "B content"

        # Session A should be gone
        assert session_manager.get_session("test-del-a") is None

    @pytest.mark.asyncio
    async def test_concurrent_sessions_streaming_isolation(self, isolated_session_manager):
        """Two sessions streaming concurrently stay isolated under asyncio.gather()."""

        captured_kwargs = {}
        max_inflight = 0
        inflight_sessions = set()
        state_lock = asyncio.Lock()
        both_started = asyncio.Event()

        async def mock_run_completion(**kwargs):
            nonlocal max_inflight

            session_key = kwargs.get("session_id") or kwargs.get("resume")
            reply_text = f"reply-for-{session_key}"

            captured_kwargs[session_key] = dict(kwargs)

            async with state_lock:
                inflight_sessions.add(session_key)
                max_inflight = max(max_inflight, len(inflight_sessions))
                if len(inflight_sessions) == 2:
                    both_started.set()

            await asyncio.wait_for(both_started.wait(), timeout=1)

            try:
                yield {
                    "type": "assistant",
                    "content": [{"type": "text", "text": reply_text}],
                }
                await asyncio.sleep(0)
                yield {
                    "type": "result",
                    "subtype": "success",
                    "result": reply_text,
                    "stop_reason": "end_turn",
                }
            finally:
                async with state_lock:
                    inflight_sessions.discard(session_key)

        def parse_message_side_effect(messages):
            for message in reversed(messages):
                if message.get("subtype") == "success":
                    return message["result"]
            return None

        async def collect_chunks(request, request_id):
            from src.main import generate_streaming_response

            return [chunk async for chunk in generate_streaming_response(request, request_id)]

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run_completion
        mock_backend.parse_message = MagicMock(side_effect=parse_message_side_effect)
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
        ):
            from src.models import ChatCompletionRequest, Message

            req_a = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="From A")],
                session_id="test-concurrent-a",
                stream=True,
            )
            req_b = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="From B")],
                session_id="test-concurrent-b",
                stream=True,
            )

            chunks_a, chunks_b = await asyncio.gather(
                collect_chunks(req_a, "req-a"),
                collect_chunks(req_b, "req-b"),
            )

        assert max_inflight == 2

        assert captured_kwargs["test-concurrent-a"]["session_id"] == "test-concurrent-a"
        assert captured_kwargs["test-concurrent-b"]["session_id"] == "test-concurrent-b"
        assert captured_kwargs["test-concurrent-a"]["prompt"] == "From A"
        assert captured_kwargs["test-concurrent-b"]["prompt"] == "From B"

        joined_a = "".join(chunks_a)
        joined_b = "".join(chunks_b)
        assert "reply-for-test-concurrent-a" in joined_a
        assert "reply-for-test-concurrent-b" not in joined_a
        assert "reply-for-test-concurrent-b" in joined_b
        assert "reply-for-test-concurrent-a" not in joined_b

        session_a = isolated_session_manager.get_session("test-concurrent-a")
        session_b = isolated_session_manager.get_session("test-concurrent-b")
        assert session_a is not None
        assert session_b is not None
        assert [msg.content for msg in session_a.messages] == [
            "From A",
            "reply-for-test-concurrent-a",
        ]
        assert [msg.content for msg in session_b.messages] == [
            "From B",
            "reply-for-test-concurrent-b",
        ]

    @pytest.mark.asyncio
    async def test_streaming_lock_ownership_safety(self, isolated_session_manager):
        """Verify that a failing request does not release another request's lock.

        Regression test: the old code used ``session.lock.locked()`` to decide
        whether to release in ``finally``, but ``asyncio.Lock`` has no ownership
        tracking.  If request A holds the lock and request B fails before acquiring
        it, B's ``finally`` could mistakenly release A's lock.

        The fix uses a ``lock_acquired`` boolean so only the coroutine that
        successfully acquired the lock will release it.
        """
        from src.main import generate_streaming_response
        from src.models import ChatCompletionRequest, Message

        session_id = "test-lock-ownership"
        session = isolated_session_manager.get_or_create_session(session_id)

        # Pre-acquire the lock to simulate request A holding it
        await session.lock.acquire()
        assert session.lock.locked()

        # Build a request that will fail because _resolve_and_get_backend raises
        req = ChatCompletionRequest(
            model=DEFAULT_MODEL,
            messages=[Message(role="user", content="should fail")],
            session_id=session_id,
            stream=True,
        )

        # Request B: fails inside try, should NOT release A's lock
        with (
            patch(
                "src.main._resolve_and_get_backend",
                side_effect=ValueError("forced failure"),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                side_effect=ValueError("forced failure"),
            ),
        ):
            chunks = []
            async for chunk in generate_streaming_response(req, "req-lock-test"):
                chunks.append(chunk)

        # The lock must still be held (by request A)
        assert session.lock.locked(), (
            "Lock was released by a request that never acquired it — ownership tracking is broken"
        )

        # Clean up: release A's lock
        session.lock.release()


class TestMixedBackendSessionInvariant:
    """Verify that a mixed-backend request is rejected WITHOUT polluting session history."""

    @pytest.mark.asyncio
    async def test_streaming_mixed_backend_rejected_without_session_pollution(
        self, isolated_session_manager
    ):
        """A session created with Claude rejects a Codex model without adding messages."""
        mock_run, _ = make_mock_run_completion()

        # First turn: create session with Claude backend
        with mock_backend_dispatch(mock_run):
            from src.main import generate_streaming_response
            from src.models import ChatCompletionRequest, Message

            first_req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="first turn")],
                session_id="test-mixed-backend",
                stream=True,
            )
            async for _ in generate_streaming_response(first_req, "req-1"):
                pass

        session = isolated_session_manager.get_session("test-mixed-backend")
        assert session is not None
        assert session.backend == "claude"
        messages_before = len(session.messages)

        # Second turn: attempt with a different backend (Codex) — should be rejected
        codex_resolved = ResolvedModel(
            public_model="codex",
            backend="codex",
            provider_model=None,
        )
        mock_codex_backend = MagicMock()

        from fastapi import HTTPException

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
        ):
            second_req = ChatCompletionRequest(
                model="codex",
                messages=[Message(role="user", content="should not be stored")],
                session_id="test-mixed-backend",
                stream=True,
            )
            with pytest.raises(HTTPException) as exc_info:
                async for _ in generate_streaming_response(second_req, "req-2"):
                    pass

        assert exc_info.value.status_code == 400
        assert "Cannot mix backends" in str(exc_info.value.detail)

        # Key assertion: session messages must NOT have been polluted
        assert len(session.messages) == messages_before
        assert session.backend == "claude"

    @pytest.mark.asyncio
    async def test_non_streaming_mixed_backend_rejected_without_session_pollution(
        self, isolated_session_manager
    ):
        """Non-streaming path also rejects mixed-backend without polluting session."""
        # Create session and simulate a completed first turn
        session = isolated_session_manager.get_or_create_session("test-mixed-ns")
        from src.models import Message as Msg

        session.add_messages([Msg(role="user", content="first")])
        session.backend = "claude"
        messages_before = len(session.messages)

        # Attempt non-streaming with different backend
        codex_resolved = ResolvedModel(
            public_model="codex",
            backend="codex",
            provider_model=None,
        )

        import src.main as main

        _mock_codex = MagicMock()
        with (
            patch.object(
                main,
                "_resolve_and_get_backend",
                return_value=(codex_resolved, _mock_codex),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(codex_resolved, _mock_codex),
            ),
            patch.object(main, "_validate_backend_auth"),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.routes.chat.verify_api_key", new_callable=AsyncMock),
        ):
            from httpx import AsyncClient, ASGITransport

            transport = ASGITransport(app=main.app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "should not be stored"}],
                        "stream": False,
                        "session_id": "test-mixed-ns",
                    },
                )

            assert resp.status_code == 400
            assert "Cannot mix backends" in str(resp.json()["error"]["message"])

        # Key assertion: session messages must NOT have been polluted
        assert len(session.messages) == messages_before
        assert session.backend == "claude"


class TestConcurrentFirstTurnRace:
    """Verify that concurrent first-turn requests don't both see is_new=True."""

    @pytest.mark.asyncio
    async def test_concurrent_first_turns_only_one_sees_is_new(self, isolated_session_manager):
        """Two concurrent streaming requests to the same new session_id:
        only one should pass system_prompt (is_new=True), the other should resume.

        Regression: is_new was computed before lock acquire, so both could see
        is_new=True and fork the provider conversation.
        """
        from src.main import generate_streaming_response
        from src.models import ChatCompletionRequest, Message

        captured_calls = []
        call_count = 0

        async def mock_run_completion(**kwargs):
            nonlocal call_count
            call_count += 1
            captured_calls.append(dict(kwargs))
            yield _make_mock_msg(f"reply-{call_count}")
            yield _make_result_msg(f"reply-{call_count}")

        mock_backend = MagicMock()
        mock_backend.run_completion = mock_run_completion
        mock_backend.parse_message = MagicMock(return_value="reply")
        mock_backend.estimate_token_usage = MagicMock(
            return_value={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        resolved = ResolvedModel(
            public_model=DEFAULT_MODEL,
            backend="claude",
            provider_model=DEFAULT_MODEL,
        )

        session_id = "test-concurrent-first-turn"

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
        ):
            req_a = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[
                    Message(role="system", content="sys prompt"),
                    Message(role="user", content="from A"),
                ],
                session_id=session_id,
                stream=True,
            )
            req_b = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="from B")],
                session_id=session_id,
                stream=True,
            )

            # Run both concurrently — lock serializes them
            async def consume(req, req_id):
                async for _ in generate_streaming_response(req, req_id):
                    pass

            await asyncio.gather(
                consume(req_a, "req-a"),
                consume(req_b, "req-b"),
            )

        # Exactly one call should have session_id set (is_new=True),
        # the other should have resume set (is_new=False)
        assert len(captured_calls) == 2
        new_calls = [c for c in captured_calls if c.get("session_id") is not None]
        resume_calls = [c for c in captured_calls if c.get("resume") is not None]
        assert len(new_calls) == 1, f"Expected exactly 1 new-session call, got {len(new_calls)}"
        assert len(resume_calls) == 1, f"Expected exactly 1 resume call, got {len(resume_calls)}"

        # The new-session call should have system_prompt
        assert new_calls[0]["system_prompt"] == "sys prompt"
        # The resume call should NOT have system_prompt
        assert resume_calls[0]["system_prompt"] is None


class TestCodexProviderSessionIdFallback:
    """Verify that Codex resume rejects when provider_session_id is missing."""

    @pytest.mark.asyncio
    async def test_codex_resume_without_provider_session_id_returns_409(
        self, isolated_session_manager
    ):
        """If a Codex first turn failed before yielding thread_id,
        the second turn must NOT fall back to gateway session_id for resume.

        Regression: the old code used ``session.provider_session_id or request.session_id``
        which would send a meaningless gateway UUID to Codex as resume token.
        """
        from fastapi import HTTPException
        from src.main import generate_streaming_response
        from src.models import ChatCompletionRequest, Message

        # Simulate a Codex session whose first turn failed (no provider_session_id)
        session = isolated_session_manager.get_or_create_session("test-codex-no-thread")
        session.backend = "codex"
        session.add_messages([Message(role="user", content="first turn")])
        # provider_session_id is deliberately None (first turn timed out)

        codex_resolved = ResolvedModel(
            public_model="codex",
            backend="codex",
            provider_model=None,
        )
        mock_codex_backend = MagicMock()

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
        ):
            second_req = ChatCompletionRequest(
                model="codex",
                messages=[Message(role="user", content="second turn")],
                session_id="test-codex-no-thread",
                stream=True,
            )
            with pytest.raises(HTTPException) as exc_info:
                async for _ in generate_streaming_response(second_req, "req-codex-2"):
                    pass

        assert exc_info.value.status_code == 409
        assert "thread_id" in str(exc_info.value.detail)

        # Backend should never have been called
        mock_codex_backend.run_completion.assert_not_called()

    @pytest.mark.asyncio
    async def test_claude_resume_without_provider_session_id_falls_back_normally(
        self, isolated_session_manager
    ):
        """Claude backend should still fall back to gateway session_id for resume."""
        from src.models import ChatCompletionRequest, Message

        mock_run, captured_kwargs = make_mock_run_completion()

        # Simulate a Claude session (no provider_session_id — normal for Claude)
        session = isolated_session_manager.get_or_create_session("test-claude-fallback")
        session.backend = "claude"
        session.add_messages([Message(role="user", content="first")])

        with mock_backend_dispatch(mock_run):
            from src.main import generate_streaming_response

            req = ChatCompletionRequest(
                model=DEFAULT_MODEL,
                messages=[Message(role="user", content="second")],
                session_id="test-claude-fallback",
                stream=True,
            )
            async for _ in generate_streaming_response(req, "req-claude-2"):
                pass

        # Claude should have used gateway session_id as resume
        assert captured_kwargs.get("resume") == "test-claude-fallback"


class TestCodex409GuardSessionPollution:
    """Verify that a Codex 409 rejection does NOT pollute session.messages.

    Regression: if the 409 guard fires after session.add_messages(), the
    rejected turn's messages remain in the session and corrupt future turns.
    The fix moves the guard before add_messages().
    """

    @pytest.mark.asyncio
    async def test_streaming_409_does_not_pollute_session(self, isolated_session_manager):
        """Streaming: session.messages must be unchanged after 409 rejection."""
        from fastapi import HTTPException
        from src.main import generate_streaming_response
        from src.models import ChatCompletionRequest, Message

        session = isolated_session_manager.get_or_create_session("codex-409-poll-s")
        session.backend = "codex"
        session.add_messages([Message(role="user", content="turn-1")])
        # provider_session_id deliberately None (first turn failed)
        msgs_before = len(session.messages)

        codex_resolved = ResolvedModel(public_model="codex", backend="codex", provider_model=None)
        mock_codex = MagicMock()

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
        ):
            req = ChatCompletionRequest(
                model="codex",
                messages=[Message(role="user", content="rejected turn")],
                session_id="codex-409-poll-s",
                stream=True,
            )
            with pytest.raises(HTTPException) as exc_info:
                async for _ in generate_streaming_response(req, "req-poll-s"):
                    pass

        assert exc_info.value.status_code == 409
        assert len(session.messages) == msgs_before

    @pytest.mark.asyncio
    async def test_non_streaming_409_does_not_pollute_session(self, isolated_session_manager):
        """Non-streaming: session.messages must be unchanged after 409 rejection."""
        from fastapi.testclient import TestClient
        from src.models import Message
        from src import main

        session = isolated_session_manager.get_or_create_session("codex-409-poll-ns")
        session.backend = "codex"
        session.add_messages([Message(role="user", content="turn-1")])
        msgs_before = len(session.messages)

        codex_resolved = ResolvedModel(public_model="codex", backend="codex", provider_model=None)
        mock_codex = MagicMock()

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(codex_resolved, mock_codex),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.main.discover_backends"),
            patch("src.main._verify_backends"),
            patch("src.routes.chat.verify_api_key", new=AsyncMock(return_value=True)),
            patch.object(
                main,
                "validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ),
        ):
            with TestClient(main.app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "rejected turn"}],
                        "session_id": "codex-409-poll-ns",
                        "stream": False,
                    },
                )

        assert resp.status_code == 409
        assert len(session.messages) == msgs_before


class TestStreamingPreflightHTTPStatus:
    """Verify that session preflight errors return correct HTTP status codes
    even for streaming requests.

    Regression: when guards ran inside the async generator, Starlette had
    already committed the 200 status line so HTTPExceptions were swallowed.
    The fix moves preflight to the endpoint before StreamingResponse creation.
    """

    @pytest.mark.asyncio
    async def test_streaming_backend_mismatch_returns_400_http(self, isolated_session_manager):
        """Streaming request with backend mismatch returns HTTP 400, not 200."""
        import httpx
        from src.models import Message
        from src import main

        session = isolated_session_manager.get_or_create_session("preflight-400")
        session.backend = "codex"
        session.add_messages([Message(role="user", content="turn-1")])

        claude_resolved = ResolvedModel(
            public_model=DEFAULT_MODEL, backend="claude", provider_model=DEFAULT_MODEL
        )
        mock_backend = MagicMock()

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(claude_resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(claude_resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.main.discover_backends"),
            patch("src.main._verify_backends"),
            patch("src.routes.chat.verify_api_key", new=AsyncMock(return_value=True)),
            patch.object(
                main,
                "validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ),
        ):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": DEFAULT_MODEL,
                        "messages": [{"role": "user", "content": "turn-2"}],
                        "session_id": "preflight-400",
                        "stream": True,
                    },
                )

        assert resp.status_code == 400
        assert "backend" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_streaming_codex_409_returns_409_http(self, isolated_session_manager):
        """Streaming Codex 409 guard returns HTTP 409, not 200."""
        import httpx
        from src.models import Message
        from src import main

        session = isolated_session_manager.get_or_create_session("preflight-409")
        session.backend = "codex"
        session.add_messages([Message(role="user", content="turn-1")])
        # provider_session_id deliberately None

        codex_resolved = ResolvedModel(public_model="codex", backend="codex", provider_model=None)
        mock_backend = MagicMock()

        with (
            patch(
                "src.main._resolve_and_get_backend",
                return_value=(codex_resolved, mock_backend),
            ),
            patch(
                "src.routes.chat._resolve_and_get_backend",
                return_value=(codex_resolved, mock_backend),
            ),
            patch("src.routes.chat._validate_backend_auth"),
            patch("src.routes.chat._build_backend_options", return_value={"model": "sonnet"}),
            patch("src.main.discover_backends"),
            patch("src.main._verify_backends"),
            patch("src.routes.chat.verify_api_key", new=AsyncMock(return_value=True)),
            patch.object(
                main,
                "validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ),
        ):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "codex",
                        "messages": [{"role": "user", "content": "turn-2"}],
                        "session_id": "preflight-409",
                        "stream": True,
                    },
                )

        assert resp.status_code == 409
        assert "thread_id" in resp.text
