"""Tests for AskUserQuestion / function_call flow.

Covers:
- SSE emission (make_function_call_response_sse)
- Detection helper (_detect_function_call_output)
- Validation error cases in _handle_function_call_output
- ResponseCreateRequest accepting function_call_output items
- Integration tests via FastAPI TestClient
"""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    from src.backends.claude.client import ClaudeCodeCLI

    mock_cli.build_options = ClaudeCodeCLI.build_options.__get__(mock_cli, type(mock_cli))

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
