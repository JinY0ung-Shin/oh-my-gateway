"""Tests for AskUserQuestion / function_call flow.

Covers:
- SSE emission (make_function_call_response_sse)
- Detection helper (_detect_function_call_output)
- Validation error cases in _handle_function_call_output
- ResponseCreateRequest accepting function_call_output items
- Integration tests via FastAPI TestClient
- Continuation paths (non-streaming and streaming) in _handle_function_call_output
"""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import src.main as main
import src.routes.general as general_module
import src.routes.responses as responses_module
from src.backend_registry import BackendRegistry
from src.constants import DEFAULT_MODEL
from src.response_models import (
    FunctionCallOutputInput,
    FunctionCallOutputItem,
    ResponseCreateRequest,
    ResponseObject,
)
from src.routes.responses import _detect_function_call_output
from src.streaming_utils import make_function_call_response_sse


# ---------------------------------------------------------------------------
# SSE emission tests (Phase 5)
# ---------------------------------------------------------------------------


def test_make_function_call_response_sse():
    result = make_function_call_response_sse(
        response_id="resp_123_1",
        call_id="toolu_abc",
        name="AskUserQuestion",
        arguments='{"question": "Overwrite?"}',
    )
    assert "event: response.output_item.added" in result
    parsed_lines = [line for line in result.strip().split("\n") if line.startswith("data: ")]
    data = json.loads(parsed_lines[0].removeprefix("data: "))
    assert data["item"]["type"] == "function_call"
    assert data["item"]["name"] == "AskUserQuestion"
    assert data["item"]["call_id"] == "toolu_abc"


def test_make_function_call_response_sse_id_format():
    result = make_function_call_response_sse(
        response_id="resp_abc_2",
        call_id="toolu_xyz",
        name="AskUserQuestion",
        arguments="{}",
    )
    data = json.loads(result.split("data: ")[1].split("\n")[0])
    assert data["item"]["id"] == "fc_toolu_xyz"
    assert data["response_id"] == "resp_abc_2"


# ---------------------------------------------------------------------------
# _detect_function_call_output tests
# ---------------------------------------------------------------------------


class TestDetectFunctionCallOutput:
    """Unit tests for _detect_function_call_output helper."""

    def test_returns_none_for_string_input(self):
        assert _detect_function_call_output("hello world") is None

    def test_returns_none_for_empty_list(self):
        assert _detect_function_call_output([]) is None

    def test_returns_none_for_regular_messages(self):
        """Regular message items (with role, no type=function_call_output) return None."""
        from src.response_models import ResponseInputItem

        items = [ResponseInputItem(role="user", content="hello")]
        assert _detect_function_call_output(items) is None

    def test_detects_dict_function_call_output(self):
        items = [
            {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"},
        ]
        result = _detect_function_call_output(items)
        assert result == {"call_id": "toolu_abc", "output": "yes"}

    def test_detects_pydantic_function_call_output(self):
        items = [
            FunctionCallOutputInput(call_id="toolu_xyz", output="no"),
        ]
        result = _detect_function_call_output(items)
        assert result == {"call_id": "toolu_xyz", "output": "no"}

    def test_first_function_call_output_wins(self):
        """When multiple function_call_output items exist, the first is returned."""
        items = [
            {"type": "function_call_output", "call_id": "first", "output": "a"},
            {"type": "function_call_output", "call_id": "second", "output": "b"},
        ]
        result = _detect_function_call_output(items)
        assert result["call_id"] == "first"

    def test_mixed_items_detects_output(self):
        """function_call_output is detected even when mixed with regular items."""
        from src.response_models import ResponseInputItem

        items = [
            ResponseInputItem(role="user", content="context"),
            {"type": "function_call_output", "call_id": "toolu_mix", "output": "ok"},
        ]
        result = _detect_function_call_output(items)
        assert result == {"call_id": "toolu_mix", "output": "ok"}

    def test_dict_without_matching_type_ignored(self):
        items = [{"type": "some_other_type", "call_id": "x", "output": "y"}]
        assert _detect_function_call_output(items) is None


# ---------------------------------------------------------------------------
# FunctionCallOutputInput model tests
# ---------------------------------------------------------------------------


class TestFunctionCallOutputInput:
    """Pydantic model validation for function_call_output items."""

    def test_valid_creation(self):
        item = FunctionCallOutputInput(call_id="toolu_abc", output="yes")
        assert item.type == "function_call_output"
        assert item.call_id == "toolu_abc"
        assert item.output == "yes"

    def test_from_dict(self):
        data = {"type": "function_call_output", "call_id": "c1", "output": "val"}
        item = FunctionCallOutputInput(**data)
        assert item.call_id == "c1"


# ---------------------------------------------------------------------------
# ResponseCreateRequest with function_call_output input
# ---------------------------------------------------------------------------


class TestResponseCreateRequestFunctionCallOutput:
    """Verify ResponseCreateRequest accepts function_call_output items in input."""

    def test_accepts_function_call_output_in_input(self):
        body = ResponseCreateRequest(
            model="opus",
            input=[
                {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"},
            ],
            previous_response_id="resp_00000000-0000-0000-0000-000000000000_1",
        )
        assert len(body.input) == 1
        item = body.input[0]
        assert isinstance(item, FunctionCallOutputInput)
        assert item.call_id == "toolu_abc"

    def test_accepts_mixed_input_with_function_call_output(self):
        body = ResponseCreateRequest(
            model="opus",
            input=[
                {"role": "user", "content": "hello"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ],
        )
        assert len(body.input) == 2

    def test_string_input_still_works(self):
        body = ResponseCreateRequest(model="opus", input="hello")
        assert body.input == "hello"


# ---------------------------------------------------------------------------
# FunctionCallOutputItem and ResponseObject tests
# ---------------------------------------------------------------------------


class TestFunctionCallOutputItem:
    """FunctionCallOutputItem model validation."""

    def test_creation(self):
        item = FunctionCallOutputItem(
            id="fc_toolu_abc",
            call_id="toolu_abc",
            name="AskUserQuestion",
            arguments='{"question": "ok?"}',
        )
        assert item.type == "function_call"
        assert item.status == "completed"

    def test_response_object_with_requires_action(self):
        resp = ResponseObject(
            id="resp_123_1",
            status="requires_action",
            model="opus",
            output=[
                FunctionCallOutputItem(
                    id="fc_toolu_abc",
                    call_id="toolu_abc",
                    name="AskUserQuestion",
                    arguments='{"question": "ok?"}',
                )
            ],
        )
        dumped = resp.model_dump()
        assert dumped["status"] == "requires_action"
        assert dumped["output"][0]["type"] == "function_call"


# ---------------------------------------------------------------------------
# Validation error path tests (function_call_output handling)
# ---------------------------------------------------------------------------


class TestHandleFunctionCallOutputValidation:
    """Test validation error cases in _handle_function_call_output.

    These test the handler indirectly by calling it with mocked dependencies.
    """

    async def test_no_pending_tool_call_raises(self):
        """function_call_output without a pending_tool_call should 400."""
        from unittest.mock import MagicMock

        from src.routes.responses import _handle_function_call_output

        session = MagicMock()
        session.pending_tool_call = None
        session.lock = asyncio.Lock()
        body = MagicMock()
        resolved = MagicMock()
        backend = MagicMock()

        with pytest.raises(Exception) as exc_info:
            await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                "sid",
                "/tmp",
                {"call_id": "toolu_abc", "output": "yes"},
            )
        assert "no pending tool call" in str(exc_info.value.detail)

    async def test_call_id_mismatch_raises(self):
        """function_call_output with mismatched call_id should 400."""
        from unittest.mock import MagicMock

        from src.routes.responses import _handle_function_call_output

        session = MagicMock()
        session.pending_tool_call = {
            "call_id": "toolu_expected",
            "name": "AskUserQuestion",
            "arguments": {},
        }
        session.lock = asyncio.Lock()
        body = MagicMock()
        resolved = MagicMock()
        backend = MagicMock()

        with pytest.raises(Exception) as exc_info:
            await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                "sid",
                "/tmp",
                {"call_id": "toolu_wrong", "output": "yes"},
            )
        assert "call_id mismatch" in str(exc_info.value.detail)

    async def test_no_persistent_client_support_raises(self):
        """Backend without run_completion_with_client should 400."""
        from unittest.mock import MagicMock

        from src.routes.responses import _handle_function_call_output

        session = MagicMock()
        session.pending_tool_call = {
            "call_id": "toolu_abc",
            "name": "AskUserQuestion",
            "arguments": {},
        }
        session.lock = asyncio.Lock()
        body = MagicMock()
        resolved = MagicMock()
        # Backend without run_completion_with_client
        backend = MagicMock(spec=["name", "run_completion"])

        with pytest.raises(Exception) as exc_info:
            await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                "sid",
                "/tmp",
                {"call_id": "toolu_abc", "output": "yes"},
            )
        assert "persistent clients" in str(exc_info.value.detail)

    async def test_no_active_client_raises(self):
        """Session with no client reference should 400."""
        from unittest.mock import MagicMock

        from src.routes.responses import _handle_function_call_output

        session = MagicMock()
        session.pending_tool_call = {
            "call_id": "toolu_abc",
            "name": "AskUserQuestion",
            "arguments": {},
        }
        session.client = None
        session.lock = asyncio.Lock()
        body = MagicMock()
        resolved = MagicMock()
        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        with pytest.raises(Exception) as exc_info:
            await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                "sid",
                "/tmp",
                {"call_id": "toolu_abc", "output": "yes"},
            )
        assert "no active SDK client" in str(exc_info.value.detail)

    async def test_no_input_event_raises(self):
        """Session with pending_tool_call but no input_event should 400."""
        from unittest.mock import MagicMock

        from src.routes.responses import _handle_function_call_output

        session = MagicMock()
        session.pending_tool_call = {
            "call_id": "toolu_abc",
            "name": "AskUserQuestion",
            "arguments": {},
        }
        session.client = MagicMock()
        session.input_event = None
        session.lock = asyncio.Lock()
        body = MagicMock()
        resolved = MagicMock()
        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        with pytest.raises(Exception) as exc_info:
            await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                "sid",
                "/tmp",
                {"call_id": "toolu_abc", "output": "yes"},
            )
        assert "no pending input event" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Integration tests (FastAPI TestClient)
# ---------------------------------------------------------------------------


@contextmanager
def _integration_client_context():
    """Create a TestClient with startup/shutdown side effects patched out."""
    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    def _mock_discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    mock_wm = MagicMock()
    mock_wm.resolve.return_value = Path("/tmp/ws/test")

    with (
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(responses_module, "workspace_manager", mock_wm),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


class TestIntegrationFunctionCallOutput:
    """Integration tests exercising the /v1/responses endpoint via TestClient."""

    def test_function_call_output_without_session_returns_error(self, isolated_session_manager):
        """function_call_output with non-existent session returns 404."""
        with _integration_client_context() as (client, _mock_cli):
            response = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "toolu_abc",
                            "output": "yes",
                        }
                    ],
                    "previous_response_id": "resp_00000000-0000-0000-0000-000000000001_1",
                },
                headers={"Authorization": "Bearer test"},
            )
        assert response.status_code in (400, 404)

    def test_function_call_output_no_pending_tool_call_returns_400(self, isolated_session_manager):
        """function_call_output with a real session but no pending tool call returns 400."""

        async def fake_run_completion(**kwargs):
            yield {"subtype": "success", "result": "Hello"}

        with _integration_client_context() as (client, mock_cli):
            mock_cli.run_completion = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello")

            # Step 1: Create a session via a normal request
            r1 = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "stream": False,
                },
                headers={"Authorization": "Bearer test"},
            )
            assert r1.status_code == 200
            resp_id = r1.json()["id"]

            # Step 2: Send function_call_output against that session
            r2 = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "toolu_abc",
                            "output": "yes",
                        }
                    ],
                    "previous_response_id": resp_id,
                },
                headers={"Authorization": "Bearer test"},
            )
        assert r2.status_code == 400
        assert "no pending tool call" in r2.json()["error"]["message"]

    def test_removed_chat_completions_returns_404(self):
        """Removed /v1/chat/completions returns 404 or 405."""
        with _integration_client_context() as (client, _mock_cli):
            r = client.post(
                "/v1/chat/completions",
                json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code in (404, 405)

    def test_removed_messages_returns_404(self):
        """Removed /v1/messages returns 404 or 405."""
        with _integration_client_context() as (client, _mock_cli):
            r = client.post(
                "/v1/messages",
                json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert r.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Continuation path tests for _handle_function_call_output (lines 600-759)
# ---------------------------------------------------------------------------


def _make_continuation_session(session_id: str, turn: int = 1):
    """Build a session pre-configured for function_call_output continuation.

    The returned session has:
    - pending_tool_call with call_id 'toolu_abc'
    - input_event (an unset asyncio.Event)
    - client set to a truthy MagicMock
    - turn_counter = turn
    - lock = asyncio.Lock()
    """
    from src.session_manager import Session

    session = Session(session_id=session_id, backend="claude")
    session.pending_tool_call = {
        "call_id": "toolu_abc",
        "name": "AskUserQuestion",
        "arguments": {"question": "Overwrite file?"},
    }
    session.input_event = asyncio.Event()
    session.client = MagicMock(name="mock_sdk_client")
    session.turn_counter = turn
    session.workspace = "/tmp/ws/test"
    return session


def _make_body(stream: bool = False, model: str = DEFAULT_MODEL):
    """Build a ResponseCreateRequest for function_call_output."""
    return ResponseCreateRequest(
        model=model,
        input=[
            {"type": "function_call_output", "call_id": "toolu_abc", "output": "yes"},
        ],
        previous_response_id="resp_00000000-0000-0000-0000-000000000000_1",
        stream=stream,
    )


def _make_resolved(model: str = DEFAULT_MODEL):
    """Build a ResolvedModel for tests."""
    from src.backends import ResolvedModel

    return ResolvedModel(public_model=model, backend="claude", provider_model=None)


class TestContinuationNonStreaming:
    """Test non-streaming continuation paths in _handle_function_call_output."""

    async def test_happy_path_returns_completed_response(self):
        """Non-streaming continuation returns completed response with assistant text."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=False)
        resolved = _make_resolved()

        # Mock backend
        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            yield {"type": "result", "subtype": "success", "result": "Done!"}

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value="Done!")
        backend.estimate_token_usage = MagicMock(
            return_value={"prompt_tokens": 10, "completion_tokens": 5}
        )

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            result = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )

        assert result["status"] == "completed"
        assert result["id"].startswith("resp_")
        assert any(
            "Done!" in part.get("text", "")
            for item in result["output"]
            for part in item.get("content", [])
        )
        assert session.turn_counter == 2
        assert session.pending_tool_call is None
        assert session.input_response == "yes"

    async def test_continuation_keeps_session_lock_until_backend_receives_response(self):
        """The pending-tool unblock and continuation read stay in one lock section."""
        from src.routes.responses import _handle_function_call_output

        class TrackingLock:
            def __init__(self):
                self._lock = asyncio.Lock()
                self.backend_started = False
                self.releases_before_backend = 0

            async def acquire(self):
                await self._lock.acquire()

            def release(self):
                if not self.backend_started:
                    self.releases_before_backend += 1
                self._lock.release()

            def locked(self):
                return self._lock.locked()

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        tracking_lock = TrackingLock()
        session.lock = tracking_lock
        body = _make_body(stream=False)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            tracking_lock.backend_started = True
            assert tracking_lock.locked()
            yield {"type": "result", "subtype": "success", "result": "Done!"}

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value="Done!")

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            result = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )

        assert result["status"] == "completed"
        assert tracking_lock.releases_before_backend == 0

    async def test_chained_ask_user_question_returns_requires_action(self):
        """When continuation yields another AskUserQuestion, returns requires_action."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=False)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            # Simulate chunks that set up a new pending_tool_call
            yield {"type": "assistant", "content": [{"type": "text", "text": "partial"}]}
            # The SDK hook sets pending_tool_call during iteration
            sess.pending_tool_call = {
                "call_id": "toolu_second",
                "name": "AskUserQuestion",
                "arguments": {"question": "Are you sure?"},
            }

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value="partial")

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            result = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )

        assert result["status"] == "requires_action"
        assert result["output"][0]["type"] == "function_call"
        assert result["output"][0]["call_id"] == "toolu_second"
        assert result["output"][0]["name"] == "AskUserQuestion"
        assert session.turn_counter == 2

    async def test_backend_error_chunk_returns_502(self):
        """When backend yields an error chunk, returns 502."""
        from fastapi import HTTPException

        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=False)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            yield {"is_error": True, "error_message": "SDK subprocess crashed"}

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value=None)

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            with pytest.raises(HTTPException) as exc_info:
                await _handle_function_call_output(
                    body,
                    resolved,
                    backend,
                    session,
                    session_id,
                    "/tmp/ws/test",
                    {"call_id": "toolu_abc", "output": "yes"},
                )

        assert exc_info.value.status_code == 502
        assert "SDK subprocess crashed" in exc_info.value.detail

    async def test_backend_error_disconnects_cleared_persistent_client(self):
        """Non-streaming continuation disconnects even if backend clears session.client."""
        from fastapi import HTTPException

        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        sdk_client = AsyncMock()
        sdk_client.disconnect = AsyncMock()
        session.client = sdk_client
        body = _make_body(stream=False)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            sess.client = None
            yield {"is_error": True, "error_message": "SDK subprocess crashed"}

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value=None)

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            with pytest.raises(HTTPException) as exc_info:
                await _handle_function_call_output(
                    body,
                    resolved,
                    backend,
                    session,
                    session_id,
                    "/tmp/ws/test",
                    {"call_id": "toolu_abc", "output": "yes"},
                )

        assert exc_info.value.status_code == 502
        sdk_client.disconnect.assert_awaited_once()

    async def test_empty_response_returns_502(self):
        """When backend yields no text, returns 502."""
        from fastapi import HTTPException

        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=False)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            yield {"type": "assistant", "content": []}

        backend.receive_response_from_client = fake_receive
        backend.parse_message = MagicMock(return_value=None)

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            with pytest.raises(HTTPException) as exc_info:
                await _handle_function_call_output(
                    body,
                    resolved,
                    backend,
                    session,
                    session_id,
                    "/tmp/ws/test",
                    {"call_id": "toolu_abc", "output": "yes"},
                )

        assert exc_info.value.status_code == 502
        assert "No response" in exc_info.value.detail

    async def test_fallback_to_run_completion_with_client(self):
        """Uses run_completion_with_client when receive_response_from_client is absent."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=False)
        resolved = _make_resolved()

        backend = MagicMock(
            spec=["run_completion_with_client", "parse_message", "estimate_token_usage", "name"]
        )

        async def fake_run(client, prompt, sess):
            yield {"type": "result", "subtype": "success", "result": "Fallback works!"}

        backend.run_completion_with_client = fake_run
        backend.parse_message = MagicMock(return_value="Fallback works!")
        backend.estimate_token_usage = MagicMock(
            return_value={"prompt_tokens": 5, "completion_tokens": 3}
        )

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            result = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )

        assert result["status"] == "completed"
        assert any(
            "Fallback works!" in part.get("text", "")
            for item in result["output"]
            for part in item.get("content", [])
        )


class TestContinuationStreaming:
    """Test streaming continuation paths in _handle_function_call_output."""

    async def test_happy_path_emits_sse_events(self):
        """Streaming continuation emits proper SSE events with content."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=True)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            yield {"type": "result", "subtype": "success", "result": "Streamed reply"}

        backend.receive_response_from_client = fake_receive

        with patch.object(responses_module, "session_manager") as mock_sm:
            mock_sm.add_assistant_response = MagicMock()

            response = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )

        # Response should be a StreamingResponse
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"

        # Collect SSE lines
        lines = []
        async for chunk in response.body_iterator:
            lines.append(chunk)
        all_sse = "".join(lines)

        assert "response.created" in all_sse
        assert "response.in_progress" in all_sse
        # Should have either content or completed/failed events
        has_completed = "response.completed" in all_sse
        has_failed = "response.failed" in all_sse
        assert has_completed or has_failed

    async def test_chained_ask_user_question_emits_function_call_sse(self):
        """Streaming continuation with chained AskUserQuestion emits function_call SSE."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=True)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        async def fake_receive(client, sess):
            yield {"type": "result", "result": "partial"}

        backend.receive_response_from_client = fake_receive

        # bridge_sse_stream yields some SSE content, then sets pending_tool_call
        # to simulate the SDK hook firing a chained AskUserQuestion.
        async def fake_bridge(sse_source, chunk_source):
            async for line in sse_source:
                yield line
            # After bridge completes, simulate a chained tool call
            session.pending_tool_call = {
                "call_id": "toolu_chained",
                "name": "AskUserQuestion",
                "arguments": {"question": "Continue?"},
            }

        # Patches must remain active while the streaming generator runs,
        # so consume the body_iterator inside the `with` block.
        with (
            patch.object(responses_module, "session_manager") as mock_sm,
            patch.object(
                responses_module.streaming_utils,
                "bridge_sse_stream",
                side_effect=fake_bridge,
            ),
        ):
            mock_sm.add_assistant_response = MagicMock()

            response = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )
            assert isinstance(response, StreamingResponse)

            lines = []
            async for chunk in response.body_iterator:
                lines.append(chunk)

        all_sse = "".join(lines)
        assert "function_call" in all_sse
        assert "toolu_chained" in all_sse
        assert "response.completed" in all_sse
        assert session.turn_counter == 2

    async def test_error_during_streaming_emits_failed_event(self):
        """Exception during streaming emits response.failed SSE event."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        sdk_client = AsyncMock()
        sdk_client.disconnect = AsyncMock()
        session.client = sdk_client
        body = _make_body(stream=True)
        resolved = _make_resolved()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        # bridge_sse_stream as an async generator that raises during iteration
        async def fake_bridge(sse_source, chunk_source):
            raise RuntimeError("Connection lost")
            yield ""  # noqa: E501 — makes this an async generator

        async def fake_receive(client, sess):
            yield {"type": "result", "result": "partial"}

        backend.receive_response_from_client = fake_receive

        # Patches must remain active while the streaming generator runs.
        with patch.object(
            responses_module.streaming_utils,
            "bridge_sse_stream",
            side_effect=fake_bridge,
        ):
            response = await _handle_function_call_output(
                body,
                resolved,
                backend,
                session,
                session_id,
                "/tmp/ws/test",
                {"call_id": "toolu_abc", "output": "yes"},
            )
            assert isinstance(response, StreamingResponse)

            lines = []
            async for chunk in response.body_iterator:
                lines.append(chunk)

        all_sse = "".join(lines)
        assert "response.failed" in all_sse
        assert "server_error" in all_sse
        sdk_client.disconnect.assert_awaited_once()
        assert session.client is None

    async def test_streaming_fallback_to_run_completion_with_client(self):
        """Streaming uses run_completion_with_client when receive_response_from_client is absent."""
        from src.routes.responses import _handle_function_call_output

        session_id = "00000000-0000-0000-0000-000000000000"
        session = _make_continuation_session(session_id, turn=1)
        body = _make_body(stream=True)
        resolved = _make_resolved()

        # Backend without receive_response_from_client attribute
        backend = MagicMock(
            spec=["run_completion_with_client", "parse_message", "estimate_token_usage", "name"]
        )

        async def fake_run(client, prompt, sess):
            yield {"type": "result", "subtype": "success", "result": "Streamed fallback!"}

        backend.run_completion_with_client = fake_run

        response = await _handle_function_call_output(
            body,
            resolved,
            backend,
            session,
            session_id,
            "/tmp/ws/test",
            {"call_id": "toolu_abc", "output": "yes"},
        )
        assert isinstance(response, StreamingResponse)

        lines = []
        async for chunk in response.body_iterator:
            lines.append(chunk)
        all_sse = "".join(lines)

        assert "response.created" in all_sse
        assert "response.in_progress" in all_sse


# ---------------------------------------------------------------------------
# Turn 1 AskUserQuestion support tests
# ---------------------------------------------------------------------------


class TestTurn1PersistentClient:
    """Verify that Turn 1 (no previous_response_id) creates and uses
    a persistent ClaudeSDKClient when the backend supports it."""

    def test_turn1_calls_create_client(self, isolated_session_manager):
        """Turn 1 request should call create_client() on the backend."""
        create_called = False

        async def fake_create_client(**kwargs):
            nonlocal create_called
            create_called = True
            return MagicMock()  # Return a mock client

        async def fake_run_completion_with_client(client, prompt, session):
            yield {"subtype": "success", "result": "Hello from SDK client"}

        with _integration_client_context() as (client, mock_cli):
            mock_cli.create_client = fake_create_client
            mock_cli.run_completion_with_client = fake_run_completion_with_client
            mock_cli.parse_message = MagicMock(return_value="Hello from SDK client")

            r = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "stream": False,
                },
                headers={"Authorization": "Bearer test"},
            )
        assert r.status_code == 200
        assert create_called, "create_client() should be called on Turn 1"

    def test_turn1_uses_sdk_client_path(self, isolated_session_manager):
        """Turn 1 should use run_completion_with_client when create_client succeeds."""
        sdk_path_used = False

        async def fake_create_client(**kwargs):
            return MagicMock()

        async def fake_run_completion_with_client(client, prompt, session):
            nonlocal sdk_path_used
            sdk_path_used = True
            yield {"subtype": "success", "result": "Hello via SDK"}

        with _integration_client_context() as (client, mock_cli):
            mock_cli.create_client = fake_create_client
            mock_cli.run_completion_with_client = fake_run_completion_with_client
            mock_cli.parse_message = MagicMock(return_value="Hello via SDK")

            r = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "stream": False,
                },
                headers={"Authorization": "Bearer test"},
            )
        assert r.status_code == 200
        assert sdk_path_used, "run_completion_with_client() should be used on Turn 1"

    def test_turn1_falls_back_on_create_client_failure(self, isolated_session_manager):
        """Turn 1 should fall back to run_completion() when create_client() fails."""
        fallback_used = False

        async def failing_create_client(**kwargs):
            raise RuntimeError("Client creation failed")

        async def fake_run_completion(**kwargs):
            nonlocal fallback_used
            fallback_used = True
            yield {"subtype": "success", "result": "Hello via fallback"}

        with _integration_client_context() as (client, mock_cli):
            mock_cli.create_client = failing_create_client
            mock_cli.run_completion = fake_run_completion
            mock_cli.parse_message = MagicMock(return_value="Hello via fallback")

            r = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "stream": False,
                },
                headers={"Authorization": "Bearer test"},
            )
        assert r.status_code == 200
        assert fallback_used, "run_completion() fallback should be used when create_client() fails"

    def test_reconnect_uses_session_base_system_prompt(self, isolated_session_manager):
        """When the persistent client was lost (session.client=None) but the
        session already has a frozen base_system_prompt from prior preflight,
        create_client must receive that frozen value as _custom_base — not
        whatever the global system prompt currently returns. This protects
        in-flight sessions from admin prompt changes mid-conversation.
        """
        captured: dict = {}

        async def fake_create_client(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        async def fake_run_completion_with_client(client, prompt, session):
            yield {"subtype": "success", "result": "OK"}

        import uuid as _uuid
        from src import system_prompt as sp

        session_id = str(_uuid.uuid4())

        # Simulate admin changing the global prompt mid-conversation.
        original_runtime = sp._runtime_prompt
        original_runtime_raw = sp._runtime_prompt_raw
        sp._runtime_prompt = "NEW_GLOBAL_AFTER_ADMIN_CHANGE: {{WORKING_DIRECTORY}}"
        sp._runtime_prompt_raw = "NEW_GLOBAL_AFTER_ADMIN_CHANGE: {{WORKING_DIRECTORY}}"

        try:
            with _integration_client_context() as (test_client, mock_cli):
                mock_cli.create_client = fake_create_client
                mock_cli.run_completion_with_client = fake_run_completion_with_client
                mock_cli.parse_message = MagicMock(return_value="OK")

                # Pre-create a session that has already gone through one turn:
                # base_system_prompt is frozen, client was lost. Created inside
                # the TestClient context so it survives any startup state mgmt.
                session = isolated_session_manager.get_or_create_session(session_id)
                session.base_system_prompt = "FROZEN_AT_SESSION_START"
                session.workspace = "/tmp/ws/test"
                session.turn_counter = 1
                session.backend = "claude"
                session.client = None  # client was lost
                session.user = None

                r = test_client.post(
                    "/v1/responses",
                    json={
                        "model": DEFAULT_MODEL,
                        "input": "next turn",
                        "previous_response_id": f"resp_{session_id}_1",
                        "stream": False,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        finally:
            sp._runtime_prompt = original_runtime
            sp._runtime_prompt_raw = original_runtime_raw

        assert r.status_code == 200, r.text
        assert captured.get("_custom_base") == "FROZEN_AT_SESSION_START", (
            f"Expected frozen session base_system_prompt to win, got "
            f"{captured.get('_custom_base')!r}"
        )
