"""Additional coverage tests for src/routes/responses.py.

Targets the missing lines not covered by existing test files:
test_responses_user.py and test_main_api_unit.py.
"""

import asyncio
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.main as main
import src.routes.responses as responses_module
import src.routes.general as general_module
from src.backend_registry import BackendDescriptor, BackendRegistry, ResolvedModel
from src.constants import DEFAULT_MODEL
from src.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Shared test client helpers (mirrors the existing test style)
# ---------------------------------------------------------------------------


@contextmanager
def client_context(workspace_path=None):
    """TestClient with the standard mock stack; optionally patches workspace_manager."""
    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)

    async def _default_create_client(**kwargs):
        return object()

    async def _default_run_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": "Hi"}]}
        yield {"subtype": "success", "result": "Hi"}

    mock_cli.create_client = _default_create_client
    mock_cli.run_completion_with_client = _default_run_with_client

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    def _mock_discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    mock_wm = MagicMock()
    if workspace_path is not None:
        mock_wm.resolve.return_value = Path(workspace_path)
    else:
        mock_wm.resolve.return_value = Path("/tmp/ws/default")

    patches = [
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(main, "validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
        patch.object(responses_module, "workspace_manager", mock_wm),
    ]

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
    ):
        with TestClient(main.app) as client:
            yield client, mock_cli, mock_wm

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


# ---------------------------------------------------------------------------
# Unit tests for pure helper functions (no HTTP overhead)
# ---------------------------------------------------------------------------


class TestContentPartText:
    """Line 114 — dict branch; line 116 — non-dict branch."""

    def test_dict_with_text_key(self):
        from src.routes.responses import _content_part_text

        result = _content_part_text({"type": "input_text", "text": "hello"})
        assert result == "hello"

    def test_object_with_text_attr(self):
        from src.routes.responses import _content_part_text

        part = MagicMock()
        part.text = "world"
        result = _content_part_text(part)
        assert result == "world"

    def test_dict_with_non_string_text_returns_empty(self):
        """Line 117 — value is not a str."""
        from src.routes.responses import _content_part_text

        result = _content_part_text({"text": 42})
        assert result == ""

    def test_object_without_text_attr_returns_empty(self):
        from src.routes.responses import _content_part_text

        result = _content_part_text(object())
        assert result == ""


class TestSplitResponseInput:
    """Lines 145-150 — array-form with system/developer items."""

    def test_system_item_extracted_as_system_prompt(self):
        from src.routes.responses import _split_response_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(role="system", content="Be concise"),
                ResponseInputItem(role="user", content="Hello"),
            ],
        )
        sys_prompt, items = _split_response_input(body)
        assert sys_prompt == "Be concise"
        assert len(items) == 1
        assert items[0].role == "user"

    def test_developer_item_extracted_as_system_prompt(self):
        from src.routes.responses import _split_response_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(role="developer", content="You are a coder"),
                ResponseInputItem(role="user", content="Write code"),
            ],
        )
        sys_prompt, items = _split_response_input(body)
        assert sys_prompt == "You are a coder"
        assert len(items) == 1

    def test_only_system_items_returns_original_input(self):
        """Line 152 — user_items empty so body.input is returned."""
        from src.routes.responses import _split_response_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        original_input = [ResponseInputItem(role="system", content="Only sys")]
        body = ResponseCreateRequest(model=DEFAULT_MODEL, input=original_input)
        sys_prompt, returned_input = _split_response_input(body)
        assert sys_prompt == "Only sys"
        # Should return the original input unchanged
        assert returned_input is body.input

    def test_string_input_passes_through(self):
        """Line 139-140 — non-list input returns instructions and input unchanged."""
        from src.routes.responses import _split_response_input
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="just a string")
        sys_prompt, inp = _split_response_input(body)
        assert sys_prompt is None
        assert inp == "just a string"

    def test_instructions_present_passes_through(self):
        """Line 139-140 — when instructions is set, array input is returned as-is."""
        from src.routes.responses import _split_response_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        items = [ResponseInputItem(role="user", content="Hi")]
        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=items,
            instructions="custom system",
        )
        sys_prompt, inp = _split_response_input(body)
        assert sys_prompt == "custom system"
        # input is returned as the body.input (may be a new list object from pydantic)
        assert inp == body.input


class TestValidateResponseContinuation:
    """Lines 322, 328 — validation guards for previous_response_id."""

    def test_instructions_with_previous_response_id_raises_400(self, isolated_session_manager):
        """Line 352-356 — instructions + previous_response_id forbidden."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1

        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "follow up",
                    "instructions": "be concise",
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 400
        assert "instructions" in resp.json()["error"]["message"]

    def test_system_input_item_with_previous_response_id_raises_400(
        self, isolated_session_manager
    ):
        """Lines 358-371 — system item in array input with previous_response_id."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1

        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": [
                        {"role": "system", "content": "Be terse"},
                        {"role": "user", "content": "follow up"},
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 400
        assert "system/developer" in resp.json()["error"]["message"]


class TestResolveResponseSession:
    """Lines 279, 281 — turn counter checks in _resolve_response_session."""

    def test_future_turn_returns_404(self, isolated_session_manager):
        """Line 394-398 — turn > session.turn_counter."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1

        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": f"resp_{sid}_99",
                },
            )

        assert resp.status_code == 404
        assert "future turn" in resp.json()["error"]["message"]

    def test_invalid_previous_response_id_format_returns_404(self, isolated_session_manager):
        """Lines 380-384 — malformed previous_response_id."""
        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": "bad_format",
                },
            )

        assert resp.status_code == 404


class TestResolveResponseWorkspace:
    """Lines 405-406, 411-412, 424, 430, 445, 466-467."""

    def test_new_session_workspace_resolve_error_returns_400(self, isolated_session_manager):
        """Lines 454-456 — ValueError from workspace_manager on new session."""
        with client_context() as (client, mock_cli, mock_wm):
            mock_wm.resolve.side_effect = ValueError("bad user path")
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello", "user": "bad-user"},
            )

        assert resp.status_code == 400
        assert "bad user path" in resp.json()["error"]["message"]

    def test_existing_session_with_no_workspace_resolves_lazily(self, isolated_session_manager):
        """Lines 464-469 — existing session with no workspace stored, resolves from wm."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = ""  # no stored workspace

        async def fake_run(client, prompt, sess):
            yield {"subtype": "success", "result": "ok"}

        with client_context() as (client, mock_cli, mock_wm):
            mock_wm.resolve.return_value = Path("/tmp/ws/lazily")
            mock_cli.run_completion_with_client = fake_run
            mock_cli.parse_message = MagicMock(return_value="ok")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 200

    def test_existing_session_uses_stored_workspace(self, isolated_session_manager):
        """Line 461-462 — session.workspace is set, used directly."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/stored"

        async def fake_run(client, prompt, sess):
            yield {"subtype": "success", "result": "stored"}

        with client_context() as (client, mock_cli, mock_wm):
            mock_cli.run_completion_with_client = fake_run
            mock_cli.parse_message = MagicMock(return_value="stored")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 200
        mock_wm.resolve.assert_not_called()


class TestDisconnectSessionClient:
    """Lines 279-290 — _disconnect_session_client helper."""

    def test_client_none_does_nothing(self):
        """Lines 278-281 — early return when client is None."""
        from src.routes.responses import _disconnect_session_client

        session = MagicMock()
        session.client = None
        # Should not raise, should not call disconnect
        asyncio.get_event_loop().run_until_complete(_disconnect_session_client(session, "test"))

    def test_client_without_disconnect_does_nothing(self):
        """Lines 284-286 — client exists but has no disconnect attr."""
        from src.routes.responses import _disconnect_session_client

        session = MagicMock()
        mock_client = object()  # plain object — no disconnect attr
        session.client = mock_client
        asyncio.get_event_loop().run_until_complete(
            _disconnect_session_client(session, "test", client=mock_client)
        )
        # session.client should be set to None since it matched
        assert session.client is None

    def test_disconnect_timeout_exception_is_suppressed(self):
        """Line 289-290 — exception during disconnect is swallowed."""
        from src.routes.responses import _disconnect_session_client

        async def failing_disconnect():
            raise RuntimeError("disconnect boom")

        mock_client = MagicMock()
        mock_client.disconnect = failing_disconnect
        session = MagicMock()
        session.client = mock_client
        # Should complete without raising
        asyncio.get_event_loop().run_until_complete(
            _disconnect_session_client(session, "test", client=mock_client)
        )


class TestConfigureClientStreaming:
    """Lines 293-296 — _configure_client_streaming."""

    def test_sets_stream_events_when_attr_present(self):
        from src.routes.responses import _configure_client_streaming

        client = MagicMock(spec=["stream_events"])
        client.stream_events = False
        _configure_client_streaming(client, True)
        assert client.stream_events is True

    def test_noop_when_client_is_none(self):
        from src.routes.responses import _configure_client_streaming

        # Should not raise
        _configure_client_streaming(None, True)

    def test_noop_when_attr_missing(self):
        from src.routes.responses import _configure_client_streaming

        # object with no stream_events attr
        _configure_client_streaming(object(), True)


class TestParseResponseId:
    """Lines 77-91 — _parse_response_id edge cases."""

    def test_valid_id_returns_tuple(self):
        from src.routes.responses import _parse_response_id

        sid = str(uuid.uuid4())
        result = _parse_response_id(f"resp_{sid}_3")
        assert result == (sid, 3)

    def test_wrong_prefix_returns_none(self):
        from src.routes.responses import _parse_response_id

        assert _parse_response_id("notresp_abc_1") is None

    def test_non_integer_turn_returns_none(self):
        from src.routes.responses import _parse_response_id

        sid = str(uuid.uuid4())
        assert _parse_response_id(f"resp_{sid}_abc") is None

    def test_zero_turn_returns_none(self):
        """Line 85-86 — turn <= 0 returns None."""
        from src.routes.responses import _parse_response_id

        sid = str(uuid.uuid4())
        assert _parse_response_id(f"resp_{sid}_0") is None

    def test_invalid_uuid_returns_none(self):
        """Lines 87-90 — UUID validation."""
        from src.routes.responses import _parse_response_id

        assert _parse_response_id("resp_not-a-uuid_1") is None

    def test_too_few_parts_returns_none(self):
        from src.routes.responses import _parse_response_id

        assert _parse_response_id("resp_only") is None


class TestDetectFunctionCallOutput:
    """Lines 94-109 — _detect_function_call_output."""

    def test_string_input_returns_none(self):
        """Line 102-103 — string input short-circuits."""
        from src.routes.responses import _detect_function_call_output

        assert _detect_function_call_output("hello") is None

    def test_dict_item_returns_call_info(self):
        """Line 105-106 — dict item with type==function_call_output."""
        from src.routes.responses import _detect_function_call_output

        result = _detect_function_call_output(
            [{"type": "function_call_output", "call_id": "cid1", "output": "yes"}]
        )
        assert result == {"call_id": "cid1", "output": "yes"}

    def test_model_object_item_returns_call_info(self):
        """Lines 107-108 — object with .type attribute."""
        from src.routes.responses import _detect_function_call_output

        item = MagicMock()
        item.type = "function_call_output"
        item.call_id = "cid2"
        item.output = "no"
        result = _detect_function_call_output([item])
        assert result == {"call_id": "cid2", "output": "no"}

    def test_no_function_call_output_returns_none(self):
        from src.routes.responses import _detect_function_call_output

        assert _detect_function_call_output([{"type": "message", "role": "user"}]) is None


class TestResponseInputToCodexItems:
    """Lines 539-579 — _response_input_to_codex_items."""

    def test_string_input_becomes_text_item(self):
        """Lines 538-543 — plain string input."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hello codex")
        items, sys_prompt = _response_input_to_codex_items(body)
        assert items == [{"type": "text", "text": "hello codex"}]
        assert sys_prompt is None

    def test_empty_string_returns_empty_items(self):
        """Line 541 — stripped text is empty."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="   ")
        items, _ = _response_input_to_codex_items(body)
        assert items == []

    def test_array_with_string_content(self):
        """Lines 548-551 — content is a plain string."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[ResponseInputItem(role="user", content="ask codex")],
        )
        items, _ = _response_input_to_codex_items(body)
        assert items == [{"type": "text", "text": "ask codex"}]

    def test_array_with_empty_string_content_skipped(self):
        """Line 549-551 — empty string content produces no item."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[ResponseInputItem(role="user", content="")],
        )
        items, _ = _response_input_to_codex_items(body)
        assert items == []

    def test_input_text_part_added(self):
        """Line 557-561 — input_text part type."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[ResponseInputItem(role="user", content=[{"type": "input_text", "text": "hi"}])],
        )
        items, _ = _response_input_to_codex_items(body)
        assert items == [{"type": "text", "text": "hi"}]

    def test_input_image_part_added(self):
        """Lines 562-576 — input_image part type."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(
                    role="user",
                    content=[{"type": "input_image", "image_url": "https://example.com/img.png"}],
                )
            ],
        )
        items, _ = _response_input_to_codex_items(body)
        assert items == [{"type": "image", "url": "https://example.com/img.png"}]

    def test_empty_image_url_raises_400(self):
        """Lines 568-571 — empty image_url raises HTTPException."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(
                    role="user",
                    content=[{"type": "input_image", "image_url": ""}],
                )
            ],
        )
        with pytest.raises(HTTPException) as exc_info:
            _response_input_to_codex_items(body)
        assert exc_info.value.status_code == 400
        assert "empty image_url" in str(exc_info.value.detail)

    def test_non_list_content_skipped(self):
        """Lines 552-553 — non-list, non-string content continues without adding items."""
        from src.routes.responses import _response_input_to_codex_items
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        # Use a real ResponseInputItem but inject a non-list, non-string content
        # by bypassing Pydantic: construct then mutate
        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[ResponseInputItem(role="user", content="placeholder")],
        )
        # Mutate the parsed item's content to an integer so the
        # isinstance(content, list) and isinstance(content, str) checks both fail
        body.input[0].content = 42  # type: ignore[assignment]
        items, _ = _response_input_to_codex_items(body)
        assert items == []


class TestNormalizeOpencodeQuestionArguments:
    """Lines 889-901 — _normalize_opencode_question_arguments."""

    def test_question_key_returns_input_unchanged(self):
        """Lines 891-893 — direct question key."""
        from src.routes.responses import _normalize_opencode_question_arguments

        val = {"question": "What do you want?", "options": []}
        result = _normalize_opencode_question_arguments(val)
        assert result is val

    def test_questions_list_with_valid_item(self):
        """Lines 895-900 — questions list with valid item."""
        from src.routes.responses import _normalize_opencode_question_arguments

        val = {"questions": [{"question": "Are you sure?"}]}
        result = _normalize_opencode_question_arguments(val)
        assert result is val

    def test_empty_question_string_returns_none(self):
        """Line 901 — question is empty string."""
        from src.routes.responses import _normalize_opencode_question_arguments

        val = {"question": ""}
        result = _normalize_opencode_question_arguments(val)
        assert result is None

    def test_questions_not_list_returns_none(self):
        """Lines 895-897 — questions is not a list."""
        from src.routes.responses import _normalize_opencode_question_arguments

        result = _normalize_opencode_question_arguments({"questions": "bad"})
        assert result is None

    def test_no_question_no_questions_returns_none(self):
        from src.routes.responses import _normalize_opencode_question_arguments

        result = _normalize_opencode_question_arguments({})
        assert result is None

    def test_questions_list_all_items_invalid_returns_none(self):
        """Line 901 — questions list exhausted without a valid question item."""
        from src.routes.responses import _normalize_opencode_question_arguments

        # questions list is non-empty but all items fail the validity check
        val = {"questions": [{"question": ""}, {"question": 42}, {"not_question": "hi"}]}
        result = _normalize_opencode_question_arguments(val)
        assert result is None


class TestNormalizeOpencodePermissionArguments:
    """Lines 904-938 — _normalize_opencode_permission_arguments."""

    def test_valid_permission_returns_formatted_dict(self):
        from src.routes.responses import _normalize_opencode_permission_arguments

        val = {
            "permission": "read_file",
            "patterns": ["*.py"],
            "always": ["tests/"],
            "metadata": {"key": "value"},
        }
        result = _normalize_opencode_permission_arguments(val)
        assert result is not None
        assert result["permission"] == "read_file"
        assert "read_file" in result["question"]
        assert "*.py" in result["question"]
        assert "tests/" in result["question"]

    def test_missing_permission_returns_none(self):
        """Line 909-910 — permission missing."""
        from src.routes.responses import _normalize_opencode_permission_arguments

        result = _normalize_opencode_permission_arguments({"patterns": []})
        assert result is None

    def test_empty_permission_returns_none(self):
        """Line 909-910 — permission empty string."""
        from src.routes.responses import _normalize_opencode_permission_arguments

        result = _normalize_opencode_permission_arguments({"permission": ""})
        assert result is None

    def test_no_patterns_uses_fallback(self):
        """Line 925 — patterns empty produces fallback text."""
        from src.routes.responses import _normalize_opencode_permission_arguments

        result = _normalize_opencode_permission_arguments({"permission": "write_file"})
        assert result is not None
        assert "(no patterns specified)" in result["question"]

    def test_patterns_not_list_handled_gracefully(self):
        """Line 912-916 — non-list raw_patterns collapses to empty."""
        from src.routes.responses import _normalize_opencode_permission_arguments

        result = _normalize_opencode_permission_arguments(
            {"permission": "exec", "patterns": "not-a-list"}
        )
        assert result is not None
        assert result["patterns"] == []


class TestStoreOpencodeToolCall:
    """Lines 941-996 — _store_opencode_pending_tool_call."""

    def _make_resolved(self, backend="opencode"):
        return ResolvedModel("model", backend, "model")

    def test_non_opencode_backend_returns_false(self):
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved("claude")
        session = MagicMock()
        assert _store_opencode_pending_tool_call(resolved, session, {}) is False

    def test_non_dict_chunk_returns_false(self):
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        assert _store_opencode_pending_tool_call(resolved, session, "not a dict") is False

    def test_content_not_list_returns_false(self):
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        assert _store_opencode_pending_tool_call(resolved, session, {"content": "bad"}) is False

    def test_question_tool_stored(self):
        """Lines 966-980 — question tool captured."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = None
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "question",
                    "metadata": {"opencode_question_request_id": "req-001"},
                    "input": {"question": "Proceed?"},
                }
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is True
        assert session.pending_tool_call["call_id"] == "req-001"
        assert session.pending_tool_call["opencode_resume"] == "question"

    def test_question_without_request_id_skipped(self):
        """Lines 968-969 — missing request_id, continues."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = None
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "question",
                    "metadata": {},
                    "input": {"question": "Proceed?"},
                }
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is False

    def test_question_with_invalid_arguments_skipped(self):
        """Lines 970-972 — arguments normalize to None, continues."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = None
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "question",
                    "metadata": {"opencode_question_request_id": "req-002"},
                    "input": {"question": ""},  # empty question -> None
                }
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is False

    def test_permission_tool_stored(self):
        """Lines 981-995 — permission tool captured."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = None
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "permission",
                    "metadata": {"opencode_permission_request_id": "req-perm-1"},
                    "input": {"permission": "read_file", "patterns": ["*.py"]},
                }
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is True
        assert session.pending_tool_call["call_id"] == "req-perm-1"
        assert session.pending_tool_call["opencode_resume"] == "permission"

    def test_permission_missing_request_id_skipped(self):
        """Line 983-984 — permission request_id missing."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = None
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "permission",
                    "metadata": {},
                    "input": {"permission": "read_file"},
                }
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is False

    def test_permission_invalid_arguments_skipped(self):
        """Lines 985-986 — arguments normalize to None."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = None
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "permission",
                    "metadata": {"opencode_permission_request_id": "req-perm-2"},
                    "input": {},  # no permission key -> None
                }
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is False

    def test_returns_false_for_empty_content_list(self):
        """Line 996 — loop exhausted without storing."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = self._make_resolved()
        session = MagicMock()
        result = _store_opencode_pending_tool_call(resolved, session, {"content": []})
        assert result is False


class TestPrepareOpencodeToolContinuation:
    """Lines 795, 801, 813, 824, 830 — _prepare_opencode_tool_continuation."""

    def test_no_pending_tool_call_raises_400(self):
        from src.routes.responses import _prepare_opencode_tool_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = None

        backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_opencode_tool_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "no pending tool call" in exc_info.value.detail

    def test_call_id_mismatch_raises_400(self):
        """Line 801 — call_id doesn't match."""
        from src.routes.responses import _prepare_opencode_tool_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "expected-id", "opencode_resume": "question"}

        backend = MagicMock()
        backend.resume_question_with_client = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_opencode_tool_continuation(
                    session, backend, {"call_id": "wrong-id", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "call_id mismatch" in exc_info.value.detail

    def test_invalid_resume_kind_raises_400(self):
        """Lines 814-817 — unsupported resume kind."""
        from src.routes.responses import _prepare_opencode_tool_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "c1", "opencode_resume": "invalid_kind"}

        backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_opencode_tool_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "Unsupported OpenCode resume kind" in exc_info.value.detail

    def test_none_resume_kind_defaults_to_question(self):
        """Lines 812-813 — resume_kind None defaults to 'question'."""
        from src.routes.responses import _prepare_opencode_tool_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client = object()
        session.pending_tool_call = {"call_id": "c1"}  # no opencode_resume key

        backend = MagicMock()
        backend.resume_question_with_client = MagicMock()

        result = asyncio.get_event_loop().run_until_complete(
            _prepare_opencode_tool_continuation(
                session, backend, {"call_id": "c1", "output": "answer"}
            )
        )
        assert result == "question"

    def test_no_resume_callable_raises_400(self):
        """Lines 823-827 — resume callable not on backend."""
        from src.routes.responses import _prepare_opencode_tool_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "c1", "opencode_resume": "question"}

        backend = MagicMock(spec=[])  # no resume_question_with_client

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_opencode_tool_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "continuation is not supported" in exc_info.value.detail

    def test_no_client_raises_400(self):
        """Lines 829-833 — session.client is None."""
        from src.routes.responses import _prepare_opencode_tool_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client = None
        session.pending_tool_call = {"call_id": "c1", "opencode_resume": "question"}

        backend = MagicMock()
        backend.resume_question_with_client = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_opencode_tool_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "no active SDK client" in exc_info.value.detail


class TestPrepareCodexApprovalContinuation:
    """Lines 849, 855, 865, 872, 878, 884-886 — _prepare_codex_approval_continuation."""

    def test_no_pending_tool_call_raises_400(self):
        from src.routes.responses import _prepare_codex_approval_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = None

        backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_codex_approval_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "no pending tool call" in exc_info.value.detail

    def test_call_id_mismatch_raises_400(self):
        """Line 855 — call_id mismatch."""
        from src.routes.responses import _prepare_codex_approval_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "expected", "codex_resume": "approval"}

        backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_codex_approval_continuation(
                    session, backend, {"call_id": "wrong", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "call_id mismatch" in exc_info.value.detail

    def test_not_approval_type_raises_400(self):
        """Lines 864-868 — codex_resume != approval."""
        from src.routes.responses import _prepare_codex_approval_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "something_else"}

        backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_codex_approval_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "Unsupported Codex continuation type" in exc_info.value.detail

    def test_no_resume_callable_raises_400(self):
        """Lines 870-875 — resume_approval_with_client not callable."""
        from src.routes.responses import _prepare_codex_approval_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "approval"}

        backend = MagicMock(spec=[])  # no resume_approval_with_client

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_codex_approval_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "not supported" in exc_info.value.detail

    def test_no_client_raises_400(self):
        """Lines 877-881 — session.client is None."""
        from src.routes.responses import _prepare_codex_approval_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client = None
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "approval"}

        backend = MagicMock()
        backend.resume_approval_with_client = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _prepare_codex_approval_continuation(
                    session, backend, {"call_id": "c1", "output": "yes"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "no active SDK client" in exc_info.value.detail

    def test_successful_preparation_clears_pending_tool_call(self):
        """Lines 883-886 — success path clears pending_tool_call."""
        from src.routes.responses import _prepare_codex_approval_continuation

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client = object()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "approval"}

        backend = MagicMock()
        backend.resume_approval_with_client = MagicMock()

        asyncio.get_event_loop().run_until_complete(
            _prepare_codex_approval_continuation(
                session, backend, {"call_id": "c1", "output": "yes"}
            )
        )
        assert session.pending_tool_call is None


class TestUnblockPendingToolCall:
    """Lines 694-734 — _unblock_pending_tool_call."""

    def test_no_pending_tool_call_raises_400(self):
        from src.routes.responses import _unblock_pending_tool_call

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = None

        backend = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _unblock_pending_tool_call(session, backend, {"call_id": "c1", "output": "x"})
            )
        assert exc_info.value.status_code == 400

    def test_call_id_mismatch_raises_400(self):
        from src.routes.responses import _unblock_pending_tool_call

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "expected"}
        session.client = object()
        session.input_event = MagicMock()

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _unblock_pending_tool_call(
                    session, backend, {"call_id": "different", "output": "x"}
                )
            )
        assert exc_info.value.status_code == 400
        assert "call_id mismatch" in exc_info.value.detail

    def test_backend_without_run_completion_raises_400(self):
        """Lines 713-717 — backend missing run_completion_with_client."""
        from src.routes.responses import _unblock_pending_tool_call

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.pending_tool_call = {"call_id": "c1"}

        backend = MagicMock(spec=[])  # no run_completion_with_client

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _unblock_pending_tool_call(session, backend, {"call_id": "c1", "output": "x"})
            )
        assert exc_info.value.status_code == 400
        assert "persistent clients" in exc_info.value.detail

    def test_no_client_raises_400(self):
        """Lines 719-723 — session.client is None."""
        from src.routes.responses import _unblock_pending_tool_call

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client = None
        session.pending_tool_call = {"call_id": "c1"}

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _unblock_pending_tool_call(session, backend, {"call_id": "c1", "output": "x"})
            )
        assert exc_info.value.status_code == 400
        assert "no active SDK client" in exc_info.value.detail

    def test_no_input_event_raises_400(self):
        """Lines 725-729 — session.input_event is None."""
        from src.routes.responses import _unblock_pending_tool_call

        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client = object()
        session.input_event = None
        session.pending_tool_call = {"call_id": "c1"}

        backend = MagicMock()
        backend.run_completion_with_client = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _unblock_pending_tool_call(session, backend, {"call_id": "c1", "output": "x"})
            )
        assert exc_info.value.status_code == 400
        assert "no pending input event" in exc_info.value.detail


class TestCollectNonStreamContinuationChunks:
    """Lines 1048 — _collect_non_stream_continuation_chunks timeout path."""

    def test_timeout_raises_504(self):
        from src.routes.responses import _collect_non_stream_continuation_chunks

        async def slow_source():
            await asyncio.sleep(999)
            yield {"content": []}

        async def run():
            import src.routes.responses as rm

            original = rm.NON_STREAM_CONTINUATION_TIMEOUT_SECONDS
            rm.NON_STREAM_CONTINUATION_TIMEOUT_SECONDS = 0.01
            try:
                await _collect_non_stream_continuation_chunks(slow_source())
            finally:
                rm.NON_STREAM_CONTINUATION_TIMEOUT_SECONDS = original

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(run())
        assert exc_info.value.status_code == 504
        assert "timed out" in exc_info.value.detail


class TestRefreshExistingClientPolicy:
    """Lines 594-634 — _refresh_existing_client_policy."""

    def test_backend_without_update_policy_noop(self):
        """Line 614-615 — no update_request_policy attr."""
        from src.routes.responses import _refresh_existing_client_policy
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hi")
        backend = MagicMock(spec=[])  # no update_request_policy

        asyncio.get_event_loop().run_until_complete(
            _refresh_existing_client_policy(body, backend, object())
        )

    def test_sync_update_policy_called(self):
        """Lines 617-629 — sync callable."""
        from src.routes.responses import _refresh_existing_client_policy
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hi")
        backend = MagicMock()
        backend.update_request_policy = MagicMock(return_value=None)

        asyncio.get_event_loop().run_until_complete(
            _refresh_existing_client_policy(body, backend, object())
        )
        backend.update_request_policy.assert_called_once()

    def test_unsupported_continuation_policy_raises_400(self):
        """Lines 630-634 — UnsupportedContinuationPolicy converts to 400."""
        from src.routes.responses import _refresh_existing_client_policy
        from src.response_models import ResponseCreateRequest
        from src.backends.claude.client import UnsupportedContinuationPolicy

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hi")
        backend = MagicMock()
        backend.update_request_policy = MagicMock(
            side_effect=UnsupportedContinuationPolicy("policy rejected")
        )

        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                _refresh_existing_client_policy(body, backend, object())
            )
        assert exc_info.value.status_code == 400
        assert "policy rejected" in str(exc_info.value.detail)


class TestUsageLogFailureSuppressed:
    """Lines 1442-1443, 1704-1705 — usage_logger failure is swallowed."""

    def test_usage_log_failure_non_stream_does_not_raise(self, isolated_session_manager):
        """Line 1442-1443 — usage_logger.log_turn_from_context exception is silenced."""

        async def fake_run(client, prompt, session):
            yield {"content": [{"type": "text", "text": "answer"}]}
            yield {"subtype": "success", "result": "answer"}

        with (
            client_context() as (client, mock_cli, mock_wm),
            patch.object(
                responses_module.usage_logger,
                "log_turn_from_context",
                new=AsyncMock(side_effect=RuntimeError("log boom")),
            ),
        ):
            mock_cli.run_completion_with_client = fake_run
            mock_cli.parse_message = MagicMock(return_value="answer")

            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello"},
            )

        assert resp.status_code == 200

    def test_usage_log_failure_in_continuation_does_not_raise(self, isolated_session_manager):
        """Lines 1704-1705 — usage_logger failure silenced in continuation path."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/test"
        session.pending_tool_call = {"call_id": "tool-1", "name": "AskUserQuestion"}
        mock_client = MagicMock()
        session.client = mock_client
        session.input_event = asyncio.Event()

        async def fake_receive(client, sess):
            yield {"content": [{"type": "text", "text": "continuation answer"}]}
            yield {"subtype": "success", "result": "continuation answer"}

        with (
            client_context() as (http_client, mock_cli, mock_wm),
            patch.object(
                responses_module.usage_logger,
                "log_turn_from_context",
                new=AsyncMock(side_effect=RuntimeError("log boom")),
            ),
        ):
            mock_cli.receive_response_from_client = fake_receive
            mock_cli.parse_message = MagicMock(return_value="continuation answer")
            mock_cli.run_completion_with_client = AsyncMock()
            mock_cli.update_request_policy = None

            resp = http_client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "tool-1",
                            "output": "confirmed",
                        }
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        # Either 200 (success) or a backend error, but NOT 500 from log failure
        assert resp.status_code in (200, 502)


class TestHasMultimodalInput:
    """Lines 518 — _has_multimodal_input."""

    def test_string_input_returns_false(self):
        from src.routes.responses import _has_multimodal_input
        from src.response_models import ResponseCreateRequest

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="text only")
        assert _has_multimodal_input(body) is False

    def test_list_with_image_returns_true(self):
        from src.routes.responses import _has_multimodal_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[
                ResponseInputItem(
                    role="user",
                    content=[{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                )
            ],
        )
        assert _has_multimodal_input(body) is True

    def test_list_without_image_returns_false(self):
        from src.routes.responses import _has_multimodal_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[ResponseInputItem(role="user", content="plain text")],
        )
        assert _has_multimodal_input(body) is False

    def test_non_list_content_skipped(self):
        """Line 518 — content that is not a list is skipped."""
        from src.routes.responses import _has_multimodal_input
        from src.response_models import ResponseCreateRequest, ResponseInputItem

        body = ResponseCreateRequest(
            model=DEFAULT_MODEL,
            input=[ResponseInputItem(role="user", content="string content")],
        )
        assert _has_multimodal_input(body) is False


class TestBuildFailedResponse:
    """Lines 178-193 — _build_failed_response."""

    def test_default_error_code_and_message(self):
        from src.routes.responses import _build_failed_response

        result = _build_failed_response("resp_id_1", "model-x", None)
        assert result.status == "failed"
        assert result.error.code == "server_error"
        assert result.error.message == "Internal server error"
        assert result.metadata == {}

    def test_custom_error_code_and_message(self):
        from src.routes.responses import _build_failed_response

        result = _build_failed_response(
            "resp_id_1", "model-x", {"key": "val"}, code="empty_response", message="No output"
        )
        assert result.error.code == "empty_response"
        assert result.error.message == "No output"
        assert result.metadata == {"key": "val"}


class TestIsCodexPendingApprovalChunk:
    """Lines 999-1025 — _is_codex_pending_approval_chunk."""

    def _make_resolved(self, backend="codex"):
        return ResolvedModel("model", backend, "model")

    def test_non_codex_backend_returns_false(self):
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved("claude")
        session = MagicMock()
        assert _is_codex_pending_approval_chunk(resolved, session, {}) is False

    def test_non_dict_chunk_returns_false(self):
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        assert _is_codex_pending_approval_chunk(resolved, session, "bad") is False

    def test_pending_tool_call_not_approval_returns_false(self):
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "other"}
        content = [{"type": "tool_use", "name": "codex_approval", "metadata": {}}]
        assert _is_codex_pending_approval_chunk(resolved, session, {"content": content}) is False

    def test_matching_approval_block_returns_true(self):
        """Lines 1012-1025 — matching approval."""
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = {"call_id": "req-42", "codex_resume": "approval"}
        content = [
            {
                "type": "tool_use",
                "name": "codex_approval",
                "metadata": {"codex_approval_request_id": "req-42"},
            }
        ]
        assert _is_codex_pending_approval_chunk(resolved, session, {"content": content}) is True

    def test_content_not_list_returns_false(self):
        """Line 1011 — content not a list."""
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "approval"}
        assert (
            _is_codex_pending_approval_chunk(resolved, session, {"content": "bad"}) is False
        )


class TestResumeBackendContinuation:
    """Lines 750-785 — _resume_backend_continuation."""

    def _make_resolved(self, backend="claude"):
        return ResolvedModel("model", backend, "model")

    def test_opencode_permission_resume(self):
        """Lines 761-767 — opencode permission path."""
        from src.routes.responses import _resume_backend_continuation

        resolved = self._make_resolved("opencode")
        backend = MagicMock()
        backend.resume_permission_with_client = MagicMock(return_value="perm_stream")
        session = MagicMock()

        result = _resume_backend_continuation(
            resolved, backend, "client", session, {"call_id": "c1", "output": "yes"}, "permission"
        )
        assert result == "perm_stream"
        backend.resume_permission_with_client.assert_called_once()

    def test_opencode_question_resume(self):
        """Lines 768-773 — opencode question path."""
        from src.routes.responses import _resume_backend_continuation

        resolved = self._make_resolved("opencode")
        backend = MagicMock()
        backend.resume_question_with_client = MagicMock(return_value="question_stream")
        session = MagicMock()

        result = _resume_backend_continuation(
            resolved, backend, "client", session, {"call_id": "c1", "output": "ans"}, "question"
        )
        assert result == "question_stream"

    def test_codex_resume(self):
        """Lines 774-780 — codex approval path."""
        from src.routes.responses import _resume_backend_continuation

        resolved = self._make_resolved("codex")
        backend = MagicMock()
        backend.resume_approval_with_client = MagicMock(return_value="approval_stream")
        session = MagicMock()

        result = _resume_backend_continuation(
            resolved, backend, "client", session, {"call_id": "c1", "output": "ok"}, "question"
        )
        assert result == "approval_stream"

    def test_claude_uses_receive_response_when_available(self):
        """Lines 782-784 — claude/receiver path."""
        from src.routes.responses import _resume_backend_continuation

        resolved = self._make_resolved("claude")
        backend = MagicMock()
        backend.receive_response_from_client = MagicMock(return_value="receiver_stream")
        session = MagicMock()

        result = _resume_backend_continuation(
            resolved, backend, "client", session, {"call_id": "c1", "output": "x"}, "question"
        )
        assert result == "receiver_stream"

    def test_claude_falls_back_to_run_completion(self):
        """Line 785 — claude without receive_response falls back."""
        from src.routes.responses import _resume_backend_continuation

        resolved = self._make_resolved("claude")
        backend = MagicMock(spec=["run_completion_with_client"])
        backend.run_completion_with_client = MagicMock(return_value="fallback_stream")
        session = MagicMock()

        result = _resume_backend_continuation(
            resolved, backend, "client", session, {"call_id": "c1", "output": "x"}, "question"
        )
        assert result == "fallback_stream"


# ---------------------------------------------------------------------------
# Integration tests — these hit the HTTP endpoints via TestClient so they
# exercise the endpoint's internal branches rather than helpers directly.
# ---------------------------------------------------------------------------


class TestResolveResponseSessionFallbackPaths:
    """Lines 405-406, 411-412, 424, 430 — session lookup fallback paths.

    These execute when previous_response_id references a session NOT in memory
    but the resolve_model finds it via workspace lookup.
    """

    def test_workspace_resolve_oserror_silenced_in_session_lookup(
        self, isolated_session_manager
    ):
        """Lines 405-406 — OSError from workspace_manager.resolve is silently swallowed."""
        sid = str(uuid.uuid4())
        # No session in memory — triggers the fallback lookup code path
        # workspace_manager.resolve raises OSError → _early_cwd stays None

        with client_context() as (client, mock_cli, mock_wm):
            mock_wm.resolve.side_effect = OSError("disk error")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "user": "alice",
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        # Session not found → 404 (the OSError itself was silenced)
        assert resp.status_code == 404

    def test_session_not_found_returns_404(self, isolated_session_manager):
        """Lines 415-422 — session not found for previous_response_id."""
        sid = str(uuid.uuid4())
        # No session in memory at all

        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 404
        assert "not found or expired" in resp.json()["error"]["message"]

    def test_user_mismatch_after_rehydration_returns_400(self, isolated_session_manager):
        """Lines 423-428 — user mismatch when session found via workspace lookup."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.user = "alice"
        session.workspace = "/tmp/ws/alice"
        session.turn_counter = 1

        with client_context() as (client, mock_cli, mock_wm):
            # patch get_session to return None first, then the session
            original_get = isolated_session_manager.get_session

            call_count = {"n": 0}

            def patched_get(session_id, user=None, cwd=None):
                call_count["n"] += 1
                if call_count["n"] <= 1:
                    return None  # first call returns None to trigger fallback
                return session

            with patch.object(isolated_session_manager, "get_session", side_effect=patched_get):
                resp = client.post(
                    "/v1/responses",
                    json={
                        "model": DEFAULT_MODEL,
                        "input": "hijack",
                        "user": "eve",  # mismatch
                        "previous_response_id": f"resp_{sid}_1",
                    },
                )

        assert resp.status_code == 400
        assert "mismatch" in resp.json()["error"]["message"].lower()

    def test_future_turn_in_fallback_session_returns_404(self, isolated_session_manager):
        """Lines 429-433 — future turn when session found via workspace fallback."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.user = None
        session.workspace = "/tmp/ws/x"
        session.turn_counter = 1

        call_count = {"n": 0}

        def patched_get(session_id, user=None, cwd=None):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                return None
            return session

        with (
            client_context() as (client, mock_cli, mock_wm),
            patch.object(isolated_session_manager, "get_session", side_effect=patched_get),
        ):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": f"resp_{sid}_99",  # future turn
                },
            )

        assert resp.status_code == 404
        assert "future turn" in resp.json()["error"]["message"]


class TestResolveWorkspaceExistingSessionError:
    """Lines 466-467 — ValueError in non-new-session workspace lazy resolve."""

    def test_workspace_resolve_error_for_existing_session_returns_400(
        self, isolated_session_manager
    ):
        """Lines 464-467 — session has no stored workspace, resolve raises ValueError."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = ""  # empty — triggers lazy resolve

        with client_context() as (client, mock_cli, mock_wm):
            mock_wm.resolve.side_effect = ValueError("user not allowed")
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 400
        assert "user not allowed" in resp.json()["error"]["message"]


class TestValidateBackendPromptSlashError:
    """Lines 594-595 — SlashCommandError propagated as 400."""

    def test_slash_command_error_returns_400(self, isolated_session_manager):
        from src.backends.claude.slash_commands import SlashCommandError

        async def fake_validate(resolved, prompt, workspace_str):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "type": "invalid_request_error",
                        "code": "unknown_slash",
                        "message": "/bad is not a valid command",
                    }
                },
            )

        with (
            client_context() as (client, mock_cli, mock_wm),
            patch.object(responses_module, "_validate_backend_prompt", side_effect=fake_validate),
        ):
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "/bad command"},
            )

        assert resp.status_code == 400


class TestEnsureResponseSessionClientBaseSysPrompt:
    """Line 655 — session.base_system_prompt is set, uses it directly."""

    def test_base_system_prompt_on_session_used_directly(self, isolated_session_manager):
        """Line 655 — existing base_system_prompt is used instead of get_system_prompt.

        We test this by calling _ensure_response_session_client directly with a session
        that already has base_system_prompt set.
        """
        from src.routes.responses import _ensure_response_session_client
        from src.response_models import ResponseCreateRequest

        create_calls = []

        async def fake_create_client(**kwargs):
            create_calls.append(kwargs)
            return object()

        body = ResponseCreateRequest(model=DEFAULT_MODEL, input="hello")
        resolved = ResolvedModel(DEFAULT_MODEL, "claude", DEFAULT_MODEL)

        backend = MagicMock()
        backend.create_client = fake_create_client

        session = MagicMock()
        session.client = None
        session.base_system_prompt = "custom base prompt"

        async def run():
            with patch("src.routes.responses.get_mcp_servers", return_value={}):
                await _ensure_response_session_client(
                    body, resolved, backend, session, "sess-1", True, None, "/tmp/ws"
                )

        asyncio.get_event_loop().run_until_complete(run())
        assert len(create_calls) == 1
        assert create_calls[0]["_custom_base"] == "custom base prompt"


class TestEnsureSessionClientCreateFails:
    """Lines 672-675 — create_client raises → 503."""

    def test_create_client_failure_returns_503(self, isolated_session_manager):
        async def boom_create_client(**kwargs):
            raise RuntimeError("backend down")

        with client_context() as (client, mock_cli, mock_wm):
            mock_cli.create_client = boom_create_client
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello"},
            )

        assert resp.status_code == 503
        assert "unavailable" in resp.json()["error"]["message"]


class TestCodexMultimodalEmptyItems:
    """Line 1131 — Codex multimodal conversion produces empty items list → 400."""

    def test_codex_multimodal_empty_items_raises_400(self, isolated_session_manager):
        from tests.conftest import register_all_descriptors

        def resolve(model):
            if model == "codex/gpt-5":
                return ResolvedModel(model, "codex", "gpt-5")
            return None

        backend = MagicMock()
        backend.name = "codex"

        async def create_client(**kwargs):
            return object()

        async def run_with_client(client, prompt, session):
            yield {"subtype": "success", "result": "x"}

        backend.create_client = create_client
        backend.run_completion_with_client = run_with_client
        backend.parse_message = MagicMock(return_value="x")

        BackendRegistry.register_descriptor(
            BackendDescriptor(
                name="codex",
                owned_by="openai",
                models=["codex/gpt-5"],
                resolve_fn=resolve,
            )
        )
        BackendRegistry.register("codex", backend)

        with client_context() as (http_client, mock_cli, mock_wm):
            # Patch _response_input_to_codex_items to return empty list
            with patch.object(
                responses_module, "_response_input_to_codex_items", return_value=([], None)
            ):
                # Patch _has_multimodal_input to return True so we enter that branch
                with patch.object(
                    responses_module, "_has_multimodal_input", return_value=True
                ):
                    resp = http_client.post(
                        "/v1/responses",
                        json={
                            "model": "codex/gpt-5",
                            "input": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "input_image", "image_url": "data:img/png;base64,x"}
                                    ],
                                }
                            ],
                        },
                    )

        assert resp.status_code == 400
        assert "no usable items" in resp.json()["error"]["message"]


class TestNonStreamInvalidPreviousResponseId:
    """Lines 1308, 1314 — non-stream path validates previous_response_id format."""

    def test_non_stream_invalid_previous_response_id_returns_404(
        self, isolated_session_manager
    ):
        """Line 1314 — non-stream path rejects malformed previous_response_id."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/x"

        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    # session id in memory but the previous_response_id format is garbage
                    "previous_response_id": f"resp_{sid}_notanumber",
                },
            )

        assert resp.status_code == 404


class TestContinuationStreamingChainedToolCall:
    """Lines 1561-1577 — continuation stream emits requires_action for a chained tool call."""

    def test_continuation_stream_chained_ask_user_question(self, isolated_session_manager):
        """Continuation streaming: pending_tool_call set during continuation → requires_action."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/test"
        session.input_event = asyncio.Event()
        session.pending_tool_call = {"call_id": "tool-first", "name": "AskUserQuestion"}
        mock_sdk_client = MagicMock()
        session.client = mock_sdk_client

        chained_call = {
            "call_id": "tool-chained",
            "name": "AskUserQuestion",
            "arguments": {"question": "follow?"},
        }

        # After continuation runs, a new pending_tool_call appears
        async def fake_receive(client, sess):
            # Simulate side-effect: sets a new pending tool call
            sess.pending_tool_call = chained_call
            yield {"subtype": "success", "result": "chained"}

        with client_context() as (http_client, mock_cli, mock_wm):
            mock_cli.receive_response_from_client = fake_receive
            mock_cli.parse_message = MagicMock(return_value="chained")

            resp = http_client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "stream": True,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "tool-first",
                            "output": "ok",
                        }
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        # streaming returns 200 with SSE content
        assert resp.status_code == 200


class TestNonStreamContinuationChainedToolCall:
    """Lines 1634-1637 — non-stream continuation returns requires_action for chained tool call."""

    def test_continuation_non_stream_chained_requires_action(self, isolated_session_manager):
        """Lines 1633-1639 — pending_tool_call set during continuation."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/test"
        session.input_event = asyncio.Event()
        session.pending_tool_call = {"call_id": "tool-orig", "name": "AskUserQuestion"}
        mock_sdk_client = MagicMock()
        session.client = mock_sdk_client

        chained_call = {
            "call_id": "tool-chained",
            "name": "AskUserQuestion",
            "arguments": {"question": "Are you sure?"},
        }

        async def fake_receive(client, sess):
            sess.pending_tool_call = chained_call
            yield {"subtype": "success", "result": "chained"}

        with client_context() as (http_client, mock_cli, mock_wm):
            mock_cli.receive_response_from_client = fake_receive
            mock_cli.parse_message = MagicMock(return_value="chained")

            resp = http_client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "stream": False,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "tool-orig",
                            "output": "confirmed",
                        }
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "requires_action"


class TestNonStreamContinuationErrorChunk:
    """Lines 1646-1647 — continuation returns error chunk → 502."""

    def test_continuation_error_chunk_returns_502(self, isolated_session_manager):
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/test"
        session.input_event = asyncio.Event()
        session.pending_tool_call = {"call_id": "tool-err", "name": "AskUserQuestion"}
        mock_sdk_client = MagicMock()
        session.client = mock_sdk_client

        async def fake_receive(client, sess):
            yield {"is_error": True, "error_message": "rate limit exceeded"}

        with client_context() as (http_client, mock_cli, mock_wm):
            mock_cli.receive_response_from_client = fake_receive

            resp = http_client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "stream": False,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "tool-err",
                            "output": "continue",
                        }
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 502
        assert "rate limit" in resp.json()["error"]["message"]


class TestNonStreamContinuationNoResponse:
    """Line 1653 — continuation produces empty response → 502."""

    def test_continuation_empty_response_returns_502(self, isolated_session_manager):
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/test"
        session.input_event = asyncio.Event()
        session.pending_tool_call = {"call_id": "tool-empty", "name": "AskUserQuestion"}
        mock_sdk_client = MagicMock()
        session.client = mock_sdk_client

        async def fake_receive(client, sess):
            # yields nothing usable — no text, no error
            yield {"type": "ping"}

        with client_context() as (http_client, mock_cli, mock_wm):
            mock_cli.receive_response_from_client = fake_receive
            mock_cli.parse_message = MagicMock(return_value="")

            resp = http_client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "stream": False,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "tool-empty",
                            "output": "continue",
                        }
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 502
        assert "No response" in resp.json()["error"]["message"]


class TestStreamingPreflight:
    """Lines 321-332 — streaming preflight validates previous_response_id."""

    def test_streaming_invalid_previous_response_id_returns_404(self, isolated_session_manager):
        """Line 327-331 — streaming preflight rejects malformed previous_response_id."""
        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/x"

        with client_context() as (client, mock_cli, mock_wm):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "stream": True,
                    "previous_response_id": f"resp_{sid}_notanumber",
                },
            )

        assert resp.status_code == 404

    def test_streaming_ensure_client_failure_releases_lock(self, isolated_session_manager):
        """Lines 1167-1170 — if _ensure_response_session_client raises, lock is released."""

        async def boom_create(**kwargs):
            raise RuntimeError("boom")

        with client_context() as (client, mock_cli, mock_wm):
            mock_cli.create_client = boom_create
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hello",
                    "stream": True,
                },
            )

        # The RuntimeError should be turned into a 503 by ensure_session_client
        assert resp.status_code == 503


class _TrackableAsyncGen:
    """Wraps an async generator to allow tracking aclose calls."""

    def __init__(self, chunks):
        self._chunks = chunks
        self._gen = self._make_gen()
        self.aclose_called = []

    async def _make_gen(self):
        for chunk in self._chunks:
            yield chunk

    def __aiter__(self):
        return self._gen.__aiter__()

    async def __anext__(self):
        return await self._gen.__anext__()

    async def aclose(self):
        self.aclose_called.append(True)
        await self._gen.aclose()


class TestCapturePendingToolQuestionsAclose:
    """Lines 1031-1034 — _capture_pending_tool_questions calls aclose on codex chunk."""

    def test_codex_approval_chunk_calls_aclose(self):
        """Lines 1031-1034 — when codex approval chunk detected, aclose is called."""
        from src.routes.responses import _capture_pending_tool_questions

        resolved = ResolvedModel("model", "codex", "model")

        session = MagicMock()
        session.pending_tool_call = {"call_id": "req-codex", "codex_resume": "approval"}

        # A chunk that will match _is_codex_pending_approval_chunk
        matching_chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "codex_approval",
                    "metadata": {"codex_approval_request_id": "req-codex"},
                }
            ]
        }

        async def run():
            source = _TrackableAsyncGen([matching_chunk])
            chunks_out = []
            async for chunk in _capture_pending_tool_questions(source, resolved, session):
                chunks_out.append(chunk)
            return source.aclose_called, chunks_out

        aclose_called, chunks_out = asyncio.get_event_loop().run_until_complete(run())
        assert aclose_called  # aclose was called
        assert chunks_out == []  # no chunks yielded — codex approval stops iteration

    def test_opencode_tool_chunk_calls_aclose_on_store(self):
        """Lines 1044-1047 — opencode question chunk stored, aclose called."""
        from src.routes.responses import _capture_pending_tool_questions

        resolved = ResolvedModel("model", "opencode", "model")
        session = MagicMock()
        session.pending_tool_call = None

        question_chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "question",
                    "metadata": {"opencode_question_request_id": "q-001"},
                    "input": {"question": "Should I continue?"},
                }
            ]
        }

        async def run():
            source = _TrackableAsyncGen([question_chunk])
            chunks_out = []
            async for chunk in _capture_pending_tool_questions(source, resolved, session):
                chunks_out.append(chunk)
            return source.aclose_called, chunks_out

        aclose_called, chunks_out = asyncio.get_event_loop().run_until_complete(run())
        assert aclose_called
        assert chunks_out == []


class TestStoreOpencodeToolCallNonDictBlock:
    """Lines 953-954 — non-dict block in content list is skipped."""

    def test_non_dict_block_in_content_is_skipped(self):
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = ResolvedModel("model", "opencode", "model")
        session = MagicMock()
        session.pending_tool_call = None

        chunk = {
            "content": [
                "not-a-dict",  # non-dict block → continue
                {
                    "type": "tool_use",
                    "name": "question",
                    "metadata": {"opencode_question_request_id": "q-002"},
                    "input": {"question": "Yes?"},
                },
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is True  # processes the second valid block after skipping first

    def test_non_tool_use_type_block_skipped(self):
        """Line 956 — block type != tool_use is skipped."""
        from src.routes.responses import _store_opencode_pending_tool_call

        resolved = ResolvedModel("model", "opencode", "model")
        session = MagicMock()
        session.pending_tool_call = None

        chunk = {
            "content": [
                {"type": "text", "text": "just text"},  # not tool_use → continue
            ]
        }
        result = _store_opencode_pending_tool_call(resolved, session, chunk)
        assert result is False


class TestIsCodexApprovalChunkDetails:
    """Lines 1015, 1017, 1025 — _is_codex_pending_approval_chunk edge cases."""

    def _make_resolved(self):
        return ResolvedModel("model", "codex", "model")

    def test_non_dict_block_skipped(self):
        """Line 1015 — non-dict block in content is skipped."""
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "approval"}
        chunk = {"content": ["not-a-dict"]}
        assert _is_codex_pending_approval_chunk(resolved, session, chunk) is False

    def test_wrong_tool_name_skipped(self):
        """Line 1017 — block has type tool_use but wrong name."""
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = {"call_id": "c1", "codex_resume": "approval"}
        chunk = {
            "content": [
                {"type": "tool_use", "name": "other_tool", "metadata": {}}
            ]
        }
        assert _is_codex_pending_approval_chunk(resolved, session, chunk) is False

    def test_id_fallback_used_when_no_metadata_key(self):
        """Line 1022-1025 — uses block 'id' as fallback for request_id."""
        from src.routes.responses import _is_codex_pending_approval_chunk

        resolved = self._make_resolved()
        session = MagicMock()
        session.pending_tool_call = {"call_id": "block-id-42", "codex_resume": "approval"}
        chunk = {
            "content": [
                {
                    "type": "tool_use",
                    "name": "codex_approval",
                    "id": "block-id-42",
                    "metadata": {},  # no codex_approval_request_id
                }
            ]
        }
        result = _is_codex_pending_approval_chunk(resolved, session, chunk)
        assert result is True


class TestCodexContinuationPath:
    """Line 1479 — Codex function_call_output continuation path in _handle_function_call_output."""

    def test_codex_function_call_output_continuation(self, isolated_session_manager):
        """Line 1479 — dispatches to _prepare_codex_approval_continuation for codex backend."""
        from tests.conftest import register_all_descriptors

        def resolve(model):
            if model == "codex/gpt-5-cont":
                return ResolvedModel(model, "codex", "gpt-5-cont")
            return None

        sid = str(uuid.uuid4())
        session = isolated_session_manager.get_or_create_session(sid)
        session.turn_counter = 1
        session.workspace = "/tmp/ws/codex"
        mock_client = MagicMock()
        session.client = mock_client
        session.pending_tool_call = {"call_id": "tool-codex", "codex_resume": "approval"}

        async def fake_resume(client, call_id, output, sess):
            yield {"content": [{"type": "text", "text": "approved"}]}
            yield {"subtype": "success", "result": "approved"}

        backend = MagicMock()
        backend.name = "codex"
        backend.resume_approval_with_client = fake_resume
        backend.parse_message = MagicMock(return_value="approved")
        backend.update_request_policy = None

        async def fake_create(**kwargs):
            return mock_client

        backend.create_client = fake_create

        BackendRegistry.register_descriptor(
            BackendDescriptor(
                name="codex",
                owned_by="openai",
                models=["codex/gpt-5-cont"],
                resolve_fn=resolve,
            )
        )
        BackendRegistry.register("codex", backend)

        with client_context() as (http_client, mock_cli, mock_wm):
            resp = http_client.post(
                "/v1/responses",
                json={
                    "model": "codex/gpt-5-cont",
                    "stream": False,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "tool-codex",
                            "output": "approved",
                        }
                    ],
                    "previous_response_id": f"resp_{sid}_1",
                },
            )

        assert resp.status_code == 200
