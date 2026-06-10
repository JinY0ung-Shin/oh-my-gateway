"""Tests for Structured Outputs (`text.format` json_schema) support.

Covers:
- request-model validation of the OpenAI-style ``text`` field
- option pass-through to the (mocked) Claude Agent SDK
- 400 rejection for backends that don't support structured outputs
- structured result surfacing in non-streaming and streaming responses
- continuation-turn schema-change guard
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.routes.responses as responses_module
from src.backend_registry import BackendDescriptor, BackendRegistry, ResolvedModel
from src.constants import DEFAULT_MODEL
from src.response_models import (
    ResponseCreateRequest,
    ResponseObject,
    TextFormatJSONSchema,
)
from src.routes.responses import (
    _response_output_format,
    _validate_continuation_output_format,
    _validate_output_format_backend,
)
from src.session_manager import Session
from src.streaming_utils import extract_structured_output
from tests.test_main_api_unit import client_context

PERSON_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name", "age"],
}


# ---------------------------------------------------------------------------
# Request model validation
# ---------------------------------------------------------------------------


def test_request_accepts_json_schema_text_format():
    req = ResponseCreateRequest(
        input="hi",
        text={
            "format": {
                "type": "json_schema",
                "name": "person",
                "schema": PERSON_SCHEMA,
                "strict": True,
            }
        },
    )
    assert isinstance(req.text.format, TextFormatJSONSchema)
    assert req.text.format.name == "person"
    assert req.text.format.json_schema == PERSON_SCHEMA
    assert req.text.format.strict is True


def test_request_accepts_plain_text_format_as_noop():
    req = ResponseCreateRequest(input="hi", text={"format": {"type": "text"}})
    assert req.text.format.type == "text"
    assert _response_output_format(req) is None


def test_request_text_defaults_to_none():
    req = ResponseCreateRequest(input="hi")
    assert req.text is None
    assert _response_output_format(req) is None


def test_request_accepts_empty_text_config():
    req = ResponseCreateRequest(input="hi", text={})
    assert req.text.format is None
    assert _response_output_format(req) is None


def test_request_rejects_json_schema_without_schema():
    with pytest.raises(ValidationError):
        ResponseCreateRequest(
            input="hi", text={"format": {"type": "json_schema", "name": "person"}}
        )


def test_request_rejects_unknown_format_type():
    with pytest.raises(ValidationError):
        ResponseCreateRequest(input="hi", text={"format": {"type": "json_object"}})


def test_response_output_format_maps_to_sdk_shape():
    """The SDK payload is {"type": "json_schema", "schema": ...} — schema passed as-is."""
    req = ResponseCreateRequest(
        input="hi",
        text={"format": {"type": "json_schema", "schema": PERSON_SCHEMA}},
    )
    assert _response_output_format(req) == {
        "type": "json_schema",
        "schema": PERSON_SCHEMA,
    }


def test_response_object_structured_output_defaults_none():
    resp = ResponseObject(id="resp_x")
    assert resp.model_dump()["structured_output"] is None


# ---------------------------------------------------------------------------
# extract_structured_output
# ---------------------------------------------------------------------------


def test_extract_structured_output_from_result_chunk():
    chunks = [
        {"type": "assistant", "content": [{"type": "text", "text": "{}"}]},
        {"type": "result", "subtype": "success", "structured_output": {"name": "Ada"}},
    ]
    assert extract_structured_output(chunks) == {"name": "Ada"}


def test_extract_structured_output_returns_none_without_result_payload():
    assert extract_structured_output([]) is None
    assert extract_structured_output([{"type": "result", "subtype": "success"}]) is None
    assert extract_structured_output([{"type": "assistant", "content": []}]) is None


# ---------------------------------------------------------------------------
# Backend rejection (route/validation layer)
# ---------------------------------------------------------------------------


def test_validate_output_format_backend_rejects_non_claude():
    fmt = {"type": "json_schema", "schema": PERSON_SCHEMA}
    for backend_name in ("codex", "opencode"):
        with pytest.raises(HTTPException) as exc_info:
            _validate_output_format_backend(fmt, backend_name)
        assert exc_info.value.status_code == 400
        assert "json_schema" in exc_info.value.detail


def test_validate_output_format_backend_allows_claude_and_noop():
    _validate_output_format_backend({"type": "json_schema", "schema": {}}, "claude")
    _validate_output_format_backend(None, "codex")
    _validate_output_format_backend(None, "opencode")


def _register_fake_codex_backend():
    def resolve(model):
        if model == "codex/gpt-5.5":
            return ResolvedModel(model, "codex", "gpt-5.5")
        return None

    backend = MagicMock()
    backend.name = "codex"
    BackendRegistry.register_descriptor(
        BackendDescriptor("codex", "openai", ["codex/gpt-5.5"], resolve)
    )
    BackendRegistry.register("codex", backend)
    return backend


def test_responses_endpoint_rejects_json_schema_for_codex_backend():
    backend = _register_fake_codex_backend()

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "input": "hi",
                "text": {"format": {"type": "json_schema", "schema": PERSON_SCHEMA}},
            },
        )

    assert response.status_code == 400
    assert "json_schema" in response.json()["error"]["message"]
    backend.create_client.assert_not_called()


def test_responses_endpoint_allows_plain_text_format_for_codex_backend():
    """format type 'text' is a no-op and must not be rejected for codex."""
    backend = _register_fake_codex_backend()
    calls = {}

    async def create_client(**kwargs):
        calls["create_client"] = kwargs
        return object()

    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": "Codex response"}]}
        yield {"type": "result", "subtype": "success", "result": "Codex response"}

    backend.create_client = create_client
    backend.run_completion_with_client = run_completion_with_client
    backend.parse_message.return_value = "Codex response"
    backend.estimate_token_usage.return_value = {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": "codex/gpt-5.5",
                "input": "hi",
                "text": {"format": {"type": "text"}},
            },
        )

    assert response.status_code == 200
    assert "output_format" not in calls["create_client"]


# ---------------------------------------------------------------------------
# Option pass-through to the (mocked) SDK
# ---------------------------------------------------------------------------


def _make_cli():
    """Create a ClaudeCodeCLI instance with auth mocked out."""
    with patch("src.auth.validate_claude_code_auth") as mock_validate:
        with patch("src.auth.auth_manager") as mock_auth:
            mock_validate.return_value = (True, {"method": "anthropic"})
            mock_auth.get_claude_code_env_vars.return_value = {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
            }
            from src.backends.claude.client import ClaudeCodeCLI

            return ClaudeCodeCLI(cwd="/tmp")


async def test_create_client_passes_output_format_into_sdk_options():
    cli = _make_cli()
    session = Session(session_id=str(uuid.uuid4()))
    fmt = {"type": "json_schema", "schema": PERSON_SCHEMA}
    captured = {}

    def make_client(options=None, **kwargs):
        captured["options"] = options
        return AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient", side_effect=make_client):
        await cli.create_client(session, output_format=fmt)

    assert captured["options"].output_format == fmt


async def test_create_client_defaults_output_format_to_none():
    cli = _make_cli()
    session = Session(session_id=str(uuid.uuid4()))
    captured = {}

    def make_client(options=None, **kwargs):
        captured["options"] = options
        return AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient", side_effect=make_client):
        await cli.create_client(session)

    assert captured["options"].output_format is None


def test_responses_endpoint_forwards_output_format_to_claude_create_client():
    calls = {}

    async def create_client(**kwargs):
        calls["create_client"] = kwargs
        return object()

    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": '{"name": "Ada", "age": 36}'}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": '{"name": "Ada", "age": 36}',
            "structured_output": {"name": "Ada", "age": 36},
        }

    with client_context() as (client, mock_cli):
        mock_cli.create_client = create_client
        mock_cli.run_completion_with_client = run_completion_with_client
        mock_cli.parse_message.return_value = '{"name": "Ada", "age": 36}'
        mock_cli.estimate_token_usage.return_value = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Extract the person",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "person",
                        "schema": PERSON_SCHEMA,
                        "strict": True,
                    }
                },
            },
        )

    assert response.status_code == 200
    assert calls["create_client"]["output_format"] == {
        "type": "json_schema",
        "schema": PERSON_SCHEMA,
    }


def test_responses_endpoint_omits_output_format_kwarg_when_unset():
    calls = {}

    async def create_client(**kwargs):
        calls["create_client"] = kwargs
        return object()

    with client_context() as (client, mock_cli):
        mock_cli.create_client = create_client
        mock_cli.parse_message.return_value = "Hi"
        mock_cli.estimate_token_usage.return_value = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "hi"},
        )

    assert response.status_code == 200
    assert "output_format" not in calls["create_client"]


# ---------------------------------------------------------------------------
# Structured result surfacing — non-streaming and streaming
# ---------------------------------------------------------------------------


def test_non_streaming_response_surfaces_structured_output():
    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": '{"name": "Ada", "age": 36}'}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": '{"name": "Ada", "age": 36}',
            "structured_output": {"name": "Ada", "age": 36},
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }

    with client_context() as (client, mock_cli):
        mock_cli.run_completion_with_client = run_completion_with_client
        mock_cli.parse_message.return_value = '{"name": "Ada", "age": 36}'
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Extract the person",
                "text": {"format": {"type": "json_schema", "schema": PERSON_SCHEMA}},
            },
        )

    assert response.status_code == 200
    data = response.json()
    # Model JSON output remains the output_text content (OpenAI semantics)
    assert data["output"][-1]["content"][0]["text"] == '{"name": "Ada", "age": 36}'
    # SDK-parsed payload is surfaced alongside
    assert data["structured_output"] == {"name": "Ada", "age": 36}


def test_non_streaming_response_structured_output_null_without_format():
    with client_context() as (client, mock_cli):
        mock_cli.parse_message.return_value = "Hi"
        mock_cli.estimate_token_usage.return_value = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "hi"},
        )

    assert response.status_code == 200
    assert response.json()["structured_output"] is None


def _completed_event_payload(sse_text: str) -> dict:
    """Return the parsed data payload of the response.completed SSE event."""
    for line in sse_text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = json.loads(line[len("data: ") :])
        if payload.get("type") == "response.completed":
            return payload
    raise AssertionError("response.completed event not found in stream")


def test_streaming_response_completed_carries_structured_output():
    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": '{"name": "Ada", "age": 36}'}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": '{"name": "Ada", "age": 36}',
            "structured_output": {"name": "Ada", "age": 36},
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }

    with client_context() as (client, mock_cli):
        mock_cli.run_completion_with_client = run_completion_with_client
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "Extract the person",
                "stream": True,
                "text": {"format": {"type": "json_schema", "schema": PERSON_SCHEMA}},
            },
        )

    assert response.status_code == 200
    assert "response.completed" in response.text
    payload = _completed_event_payload(response.text)
    assert payload["response"]["structured_output"] == {"name": "Ada", "age": 36}


def test_streaming_response_completed_omits_structured_output_when_absent():
    """SSE payloads use exclude_none, so the key is absent without a result payload."""
    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "hi", "stream": True},
        )

    assert response.status_code == 200
    payload = _completed_event_payload(response.text)
    assert "structured_output" not in payload["response"]


# ---------------------------------------------------------------------------
# Continuation guard — output_format is baked at session-create time
# ---------------------------------------------------------------------------


def test_validate_continuation_output_format_noop_cases():
    fmt = {"type": "json_schema", "schema": PERSON_SCHEMA}
    client = SimpleNamespace(options=SimpleNamespace(output_format=fmt))
    # Same schema re-sent → no-op
    _validate_continuation_output_format(client, fmt)
    # No json_schema requested → no-op regardless of existing state
    _validate_continuation_output_format(client, None)
    _validate_continuation_output_format(object(), None)


def test_validate_continuation_output_format_rejects_change():
    client = SimpleNamespace(
        options=SimpleNamespace(output_format={"type": "json_schema", "schema": {}})
    )
    with pytest.raises(HTTPException) as exc_info:
        _validate_continuation_output_format(
            client, {"type": "json_schema", "schema": PERSON_SCHEMA}
        )
    assert exc_info.value.status_code == 400
    assert "continuation" in exc_info.value.detail


def test_responses_endpoint_rejects_schema_change_on_continuation(
    isolated_session_manager,
):
    """A continuation turn asking for a different json_schema fails closed with 400."""
    session_id = str(uuid.uuid4())
    session = isolated_session_manager.get_or_create_session(session_id)
    session.turn_counter = 1
    session.client = SimpleNamespace(
        options=SimpleNamespace(output_format={"type": "json_schema", "schema": {}}),
        disconnect=AsyncMock(),
    )

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "previous_response_id": responses_module._make_response_id(
                    session_id, 1
                ),
                "input": "continue",
                "text": {"format": {"type": "json_schema", "schema": PERSON_SCHEMA}},
            },
        )

    assert response.status_code == 400
    assert "continuation" in response.json()["error"]["message"]
    # Lock must be released so the session stays usable
    assert not session.lock.locked()


def test_responses_endpoint_accepts_same_schema_on_continuation(
    isolated_session_manager,
):
    """Re-sending the format the session was created with is a no-op (OpenAI clients do this)."""
    session_id = str(uuid.uuid4())
    session = isolated_session_manager.get_or_create_session(session_id)
    session.turn_counter = 1
    fmt = {"type": "json_schema", "schema": PERSON_SCHEMA}
    session.client = SimpleNamespace(
        options=SimpleNamespace(output_format=fmt),
        disconnect=AsyncMock(),
    )

    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": '{"name": "Ada", "age": 36}'}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": '{"name": "Ada", "age": 36}',
            "structured_output": {"name": "Ada", "age": 36},
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }

    with client_context() as (client, mock_cli):
        del mock_cli.update_request_policy  # plain continuation, no policy refresh
        mock_cli.run_completion_with_client = run_completion_with_client
        mock_cli.parse_message.return_value = '{"name": "Ada", "age": 36}'
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "previous_response_id": responses_module._make_response_id(
                    session_id, 1
                ),
                "input": "continue",
                "text": {"format": {"type": "json_schema", "schema": PERSON_SCHEMA}},
            },
        )

    assert response.status_code == 200
    assert response.json()["structured_output"] == {"name": "Ada", "age": 36}
