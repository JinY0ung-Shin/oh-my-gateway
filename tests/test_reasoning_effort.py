"""Tests for per-request reasoning control (``reasoning.effort``).

Covers:
- request-model validation of the OpenAI-style ``reasoning`` field
- option pass-through to the (mocked) Claude Agent SDK
- 400 rejection for backends that don't support reasoning control
- continuation-turn effort-change guard
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.routes.responses as responses_module
from src.backend_registry import BackendDescriptor, BackendRegistry, ResolvedModel
from src.constants import DEFAULT_MODEL
from src.response_models import ResponseCreateRequest
from src.routes.responses import (
    _response_reasoning_effort,
    _validate_continuation_reasoning,
    _validate_reasoning_backend,
)
from src.session_manager import Session
from tests.test_main_api_unit import client_context

EFFORT_LEVELS = ["none", "low", "medium", "high", "xhigh", "max"]


# ---------------------------------------------------------------------------
# Request model validation
# ---------------------------------------------------------------------------


def test_request_accepts_all_effort_levels():
    for level in EFFORT_LEVELS:
        req = ResponseCreateRequest(input="hi", reasoning={"effort": level})
        assert _response_reasoning_effort(req) == level


def test_request_reasoning_defaults_to_none():
    req = ResponseCreateRequest(input="hi")
    assert req.reasoning is None
    assert _response_reasoning_effort(req) is None

    empty = ResponseCreateRequest(input="hi", reasoning={})
    assert _response_reasoning_effort(empty) is None


def test_request_ignores_openai_extra_reasoning_keys():
    """Stock OpenAI clients send e.g. ``summary`` — must not 422."""
    req = ResponseCreateRequest(
        input="hi", reasoning={"effort": "high", "summary": "auto"}
    )
    assert _response_reasoning_effort(req) == "high"


def test_request_rejects_unknown_effort():
    with pytest.raises(ValidationError):
        ResponseCreateRequest(input="hi", reasoning={"effort": "minimal"})


# ---------------------------------------------------------------------------
# Backend rejection (route/validation layer)
# ---------------------------------------------------------------------------


def test_validate_reasoning_backend_rejects_non_reasoning_backends():
    for backend_name in ("opencode",):
        with pytest.raises(HTTPException) as exc_info:
            _validate_reasoning_backend("high", backend_name)
        assert exc_info.value.status_code == 400
        assert "reasoning.effort" in exc_info.value.detail


def test_validate_reasoning_backend_allows_claude_codex_and_noop():
    _validate_reasoning_backend("max", "claude")
    # Codex forwards reasoning.effort as a per-turn parameter.
    _validate_reasoning_backend("high", "codex")
    _validate_reasoning_backend(None, "codex")
    _validate_reasoning_backend(None, "opencode")


def _register_fake_opencode_backend():
    def resolve(model):
        if model == "opencode/some-model":
            return ResolvedModel(model, "opencode", "some-model")
        return None

    backend = MagicMock()
    backend.name = "opencode"
    BackendRegistry.register_descriptor(
        BackendDescriptor("opencode", "opencode", ["opencode/some-model"], resolve)
    )
    BackendRegistry.register("opencode", backend)
    return backend


def test_responses_endpoint_rejects_reasoning_for_non_reasoning_backend():
    backend = _register_fake_opencode_backend()

    with client_context() as (client, _mock_cli):
        response = client.post(
            "/v1/responses",
            json={
                "model": "opencode/some-model",
                "input": "hi",
                "reasoning": {"effort": "high"},
            },
        )

    assert response.status_code == 400
    assert "reasoning.effort" in response.json()["error"]["message"]
    backend.create_client.assert_not_called()


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


async def test_create_client_passes_effort_into_sdk_options():
    cli = _make_cli()
    session = Session(session_id=str(uuid.uuid4()))
    captured = {}

    def make_client(options=None, **kwargs):
        captured["options"] = options
        return AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient", side_effect=make_client):
        await cli.create_client(session, effort="max")

    assert captured["options"].effort == "max"
    assert captured["options"].thinking == {"type": "adaptive"}


async def test_create_client_effort_none_disables_thinking():
    cli = _make_cli()
    session = Session(session_id=str(uuid.uuid4()))
    captured = {}

    def make_client(options=None, **kwargs):
        captured["options"] = options
        return AsyncMock()

    with patch("src.backends.claude.client.ClaudeSDKClient", side_effect=make_client):
        await cli.create_client(session, effort="none")

    assert captured["options"].thinking == {"type": "disabled"}
    assert captured["options"].effort is None


def test_responses_endpoint_forwards_effort_to_claude_create_client():
    calls = {}

    async def create_client(**kwargs):
        calls["create_client"] = kwargs
        return object()

    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": "Hi"}]}
        yield {"type": "result", "subtype": "success", "result": "Hi"}

    with client_context() as (client, mock_cli):
        mock_cli.create_client = create_client
        mock_cli.run_completion_with_client = run_completion_with_client
        mock_cli.parse_message.return_value = "Hi"
        mock_cli.estimate_token_usage.return_value = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "hi",
                "reasoning": {"effort": "max"},
            },
        )

    assert response.status_code == 200
    assert calls["create_client"]["effort"] == "max"


def test_responses_endpoint_omits_effort_kwarg_when_unset():
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
    assert "effort" not in calls["create_client"]


# ---------------------------------------------------------------------------
# Continuation guard — effort is baked at session-create time
# ---------------------------------------------------------------------------


def test_validate_continuation_reasoning_noop_cases():
    # Same effort level re-sent → no-op
    client = SimpleNamespace(
        options=SimpleNamespace(effort="max", thinking={"type": "adaptive"})
    )
    _validate_continuation_reasoning(client, "max")
    # 'none' re-sent on a thinking-disabled session → no-op
    disabled = SimpleNamespace(
        options=SimpleNamespace(effort=None, thinking={"type": "disabled"})
    )
    _validate_continuation_reasoning(disabled, "none")
    # No effort requested → no-op regardless of existing state
    _validate_continuation_reasoning(client, None)
    _validate_continuation_reasoning(object(), None)


def test_validate_continuation_reasoning_rejects_change():
    client = SimpleNamespace(
        options=SimpleNamespace(effort="high", thinking={"type": "adaptive"})
    )
    for requested in ("max", "none"):
        with pytest.raises(HTTPException) as exc_info:
            _validate_continuation_reasoning(client, requested)
        assert exc_info.value.status_code == 400
        assert "continuation" in exc_info.value.detail

    # Session without any explicit effort (global default) → level request rejected
    default_client = SimpleNamespace(
        options=SimpleNamespace(effort=None, thinking={"type": "adaptive"})
    )
    with pytest.raises(HTTPException):
        _validate_continuation_reasoning(default_client, "high")


def test_responses_endpoint_rejects_effort_change_on_continuation(
    isolated_session_manager,
):
    """A continuation turn asking for a different effort fails closed with 400."""
    session_id = str(uuid.uuid4())
    session = isolated_session_manager.get_or_create_session(session_id)
    session.turn_counter = 1
    session.client = SimpleNamespace(
        options=SimpleNamespace(effort="high", thinking={"type": "adaptive"}),
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
                "reasoning": {"effort": "max"},
            },
        )

    assert response.status_code == 400
    assert "continuation" in response.json()["error"]["message"]
    # Lock must be released so the session stays usable
    assert not session.lock.locked()


def test_responses_endpoint_accepts_same_effort_on_continuation(
    isolated_session_manager,
):
    """Re-sending the effort the session was created with is a no-op."""
    session_id = str(uuid.uuid4())
    session = isolated_session_manager.get_or_create_session(session_id)
    session.turn_counter = 1
    session.client = SimpleNamespace(
        options=SimpleNamespace(effort="max", thinking={"type": "adaptive"}),
        disconnect=AsyncMock(),
    )

    async def run_completion_with_client(client, prompt, session):
        yield {"content": [{"type": "text", "text": "Hi again"}]}
        yield {
            "type": "result",
            "subtype": "success",
            "result": "Hi again",
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }

    with client_context() as (client, mock_cli):
        del mock_cli.update_request_policy  # plain continuation, no policy refresh
        mock_cli.run_completion_with_client = run_completion_with_client
        mock_cli.parse_message.return_value = "Hi again"
        response = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "previous_response_id": responses_module._make_response_id(
                    session_id, 1
                ),
                "input": "continue",
                "reasoning": {"effort": "max"},
            },
        )

    assert response.status_code == 200
