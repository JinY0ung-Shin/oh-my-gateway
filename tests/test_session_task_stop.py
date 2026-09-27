"""Tests for POST /v1/sessions/{id}/tasks/{task_id}/stop and the Claude stop path."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import CLIConnectionError, ProcessError
from claude_agent_sdk._internal.query import Query
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.routes.sessions as sessions_module
from src.backends import BackendRegistry
from src.backends.claude.client import (
    ClaudeCodeCLI,
    TaskStopClientUnavailable,
    TaskStopRejected,
)
from src.backends.claude.sdk_client import GatewayClaudeSDKClient
from src.constants import RATE_LIMITS
from src.rate_limiter import limiter, rate_limit_exceeded_handler
from src.session_manager import Session, session_manager
from src.session_outbox import get_outbox

STOP_URL = "/v1/sessions/sess-stop/tasks/t1/stop"


def _reset_limiter() -> None:
    if limiter is not None:
        limiter.reset()


@pytest.fixture()
def stop_client():
    app = FastAPI()
    app.include_router(sessions_module.router)
    _reset_limiter()
    with patch.object(
        sessions_module, "verify_api_key", new=AsyncMock(return_value=True)
    ):
        with TestClient(app) as client:
            yield client
    _reset_limiter()


@pytest.fixture()
def stored_session():
    session = Session(session_id="sess-stop", user="alice")
    session.client = SimpleNamespace(name="live-sdk-client")
    outbox = get_outbox(session)
    outbox.apply_task_event(
        outbox.append({"type": "task_started", "task_id": "t1", "description": "d"})
    )
    with session_manager.lock:
        session_manager.sessions["sess-stop"] = session
    yield session
    with session_manager.lock:
        session_manager.sessions.pop("sess-stop", None)


@pytest.fixture()
def backend():
    fake = SimpleNamespace(stop_task_client=AsyncMock(return_value=None))
    with patch.object(BackendRegistry, "get", return_value=fake) as get:
        fake.registry_get = get
        yield fake


class TestStopEndpoint:
    def test_success_202_forwards_client_and_task(
        self, stop_client, stored_session, backend
    ):
        response = stop_client.post(STOP_URL, params={"user": "alice"})

        assert response.status_code == 202
        assert response.json() == {
            "session_id": "sess-stop",
            "task_id": "t1",
            "status": "stop_requested",
        }
        backend.registry_get.assert_called_once_with("claude")
        backend.stop_task_client.assert_awaited_once_with(stored_session.client, "t1")
        # The terminal task_updated drops the entry later; the route must not.
        assert "t1" in get_outbox(stored_session).active_tasks

    def test_unknown_session_404(self, stop_client, backend):
        response = stop_client.post("/v1/sessions/nope/tasks/t1/stop")

        assert response.status_code == 404
        assert response.json()["detail"] == "Session not found"
        backend.stop_task_client.assert_not_awaited()

    def test_legacy_user_mismatch_404_without_touching_ttl(
        self, stop_client, stored_session, backend
    ):
        expires_before = stored_session.expires_at

        response = stop_client.post(STOP_URL, params={"user": "mallory"})

        assert response.status_code == 404
        assert response.json()["detail"] == "Session not found"
        assert stored_session.expires_at == expires_before
        backend.stop_task_client.assert_not_awaited()

    def test_credential_scoped_foreign_user_404(
        self, stop_client, stored_session, backend
    ):
        # The authenticated principal wins over a caller-supplied ``user``.
        with patch.object(
            sessions_module, "get_authenticated_user", return_value="mallory"
        ):
            response = stop_client.post(STOP_URL, params={"user": "alice"})

        assert response.status_code == 404
        backend.stop_task_client.assert_not_awaited()

    def test_credential_scoped_owner_202(self, stop_client, stored_session, backend):
        with patch.object(
            sessions_module, "get_authenticated_user", return_value="alice"
        ):
            response = stop_client.post(STOP_URL, params={"user": "mallory"})

        assert response.status_code == 202
        backend.stop_task_client.assert_awaited_once()

    def test_unknown_task_404_never_reaches_backend(
        self, stop_client, stored_session, backend
    ):
        response = stop_client.post("/v1/sessions/sess-stop/tasks/nope/stop")

        assert response.status_code == 404
        assert response.json()["detail"] == "Task not found"
        backend.stop_task_client.assert_not_awaited()

    def test_finished_task_404(self, stop_client, stored_session, backend):
        outbox = get_outbox(stored_session)
        outbox.apply_task_event(
            {"type": "task_updated", "task_id": "t1", "status": "killed"}
        )

        response = stop_client.post(STOP_URL)

        assert response.status_code == 404
        backend.stop_task_client.assert_not_awaited()

    def test_no_live_client_409(self, stop_client, stored_session, backend):
        stored_session.client = None

        response = stop_client.post(STOP_URL)

        assert response.status_code == 409
        assert response.json()["detail"] == "Session has no live client"
        backend.stop_task_client.assert_not_awaited()

    def test_backend_without_stop_support_400(self, stop_client, stored_session):
        with patch.object(BackendRegistry, "get", return_value=SimpleNamespace()):
            response = stop_client.post(STOP_URL)

        assert response.status_code == 400
        assert "does not support task stop" in response.json()["detail"]

    def test_backend_unavailable_503(self, stop_client, stored_session):
        with patch.object(BackendRegistry, "get", side_effect=ValueError("gone")):
            response = stop_client.post(STOP_URL)

        assert response.status_code == 503

    def test_cli_rejection_409_carries_message(
        self, stop_client, stored_session, backend
    ):
        backend.stop_task_client.side_effect = TaskStopRejected(
            "stop_task: task_id must be a string"
        )

        response = stop_client.post(STOP_URL)

        assert response.status_code == 409
        assert "stop_task: task_id must be a string" in response.json()["detail"]

    def test_client_not_connected_409(self, stop_client, stored_session, backend):
        backend.stop_task_client.side_effect = TaskStopClientUnavailable(
            "Not connected. Call connect() first."
        )

        response = stop_client.post(STOP_URL)

        assert response.status_code == 409
        assert response.json()["detail"] == "Session has no live client"

    def test_gateway_timeout_504(self, stop_client, stored_session, backend):
        async def _hang(client, task_id):
            await asyncio.sleep(30)

        backend.stop_task_client.side_effect = _hang
        with patch.object(sessions_module, "TASK_STOP_TIMEOUT_S", 0.05):
            response = stop_client.post(STOP_URL)

        assert response.status_code == 504

    def test_sdk_timeout_504(self, stop_client, stored_session, backend):
        backend.stop_task_client.side_effect = TimeoutError(
            "Control request timeout: stop_task"
        )

        response = stop_client.post(STOP_URL)

        assert response.status_code == 504

    def test_unexpected_failure_502(self, stop_client, stored_session, backend):
        backend.stop_task_client.side_effect = RuntimeError("boom")

        response = stop_client.post(STOP_URL)

        assert response.status_code == 502
        assert response.json()["detail"] == "Failed to stop task"

    def test_idle_reader_starts_before_the_stop_is_sent(
        self, stop_client, stored_session, backend
    ):
        # The SDK delivers the control reply through the read loop that feeds
        # its bounded message stream: between turns the reader must already
        # be draining, or a full buffer holds the reply back until timeout.
        calls = []
        backend.stop_task_client.side_effect = lambda client, task_id: calls.append(
            "stop"
        )
        with patch.object(
            sessions_module,
            "resume_idle_reader_between_turns",
            side_effect=lambda session: calls.append("resume"),
        ):
            response = stop_client.post(STOP_URL)

        assert response.status_code == 202
        assert calls == ["resume", "stop"]

    def test_idle_reader_left_running_when_the_stop_times_out(
        self, stop_client, stored_session, backend
    ):
        backend.stop_task_client.side_effect = TimeoutError(
            "Control request timeout: stop_task"
        )
        with patch.object(
            sessions_module, "resume_idle_reader_between_turns"
        ) as resume:
            response = stop_client.post(STOP_URL)

        assert response.status_code == 504
        resume.assert_called_once_with(stored_session)

    def test_stop_during_a_locked_turn_leaves_the_idle_reader_off(
        self, stop_client, stored_session, backend
    ):
        # A non-streaming turn holds session.lock and reads the client itself;
        # a reader started now would split that turn's message stream.
        from src.session_outbox import idle_reader_running

        class _StreamingClient:
            async def receive_messages(self):
                await asyncio.sleep(30)
                yield None

        stored_session.client = _StreamingClient()
        with patch.object(stored_session.lock, "locked", return_value=True):
            response = stop_client.post(STOP_URL)

        assert response.status_code == 202
        backend.stop_task_client.assert_awaited_once_with(stored_session.client, "t1")
        assert not idle_reader_running(stored_session)

    def test_task_of_a_dropped_client_404(self, stop_client, stored_session, backend):
        # The CLI acknowledges ids it does not know, so a registry entry left
        # behind by a dead client would turn into a silent 202.
        from src.session_outbox import reset_active_tasks

        reset_active_tasks(stored_session, "client replacement")

        response = stop_client.post(STOP_URL)

        assert response.status_code == 404
        backend.stop_task_client.assert_not_awaited()

    @pytest.mark.skipif(limiter is None, reason="rate limiting disabled")
    def test_rate_limited_like_cancel(self, stored_session, backend):
        app = FastAPI()
        app.include_router(sessions_module.router)
        app.state.limiter = limiter
        app.add_exception_handler(429, rate_limit_exceeded_handler)
        _reset_limiter()
        try:
            with patch.object(
                sessions_module, "verify_api_key", new=AsyncMock(return_value=True)
            ):
                with TestClient(app) as client:
                    statuses = [
                        client.post(STOP_URL).status_code
                        for _ in range(RATE_LIMITS["responses"] + 1)
                    ]
        finally:
            _reset_limiter()

        assert statuses[:-1] == [202] * RATE_LIMITS["responses"]
        assert statuses[-1] == 429


# ---------------------------------------------------------------------------
# ClaudeCodeCLI.stop_task_client — exception classification
# ---------------------------------------------------------------------------


def _backend() -> ClaudeCodeCLI:
    return ClaudeCodeCLI.__new__(ClaudeCodeCLI)


def _timeout_flavored() -> Exception:
    # The SDK's own control timeout: a bare Exception chained from TimeoutError.
    try:
        try:
            raise TimeoutError()
        except TimeoutError as exc:
            raise Exception("Control request timeout: stop_task") from exc
    except Exception as exc:  # noqa: BLE001 - building the SDK's exact shape
        return exc


class TestStopTaskClient:
    async def test_awaits_sdk_stop_task(self):
        client = MagicMock()
        client.stop_task = AsyncMock(return_value=None)

        await _backend().stop_task_client(client, "t1")

        client.stop_task.assert_awaited_once_with("t1")

    @pytest.mark.parametrize(
        "raised, expected",
        [
            (CLIConnectionError("Not connected"), TaskStopClientUnavailable),
            (Exception("stop_task is not supported"), TaskStopRejected),
            (_timeout_flavored(), TimeoutError),
            (ProcessError("CLI died", exit_code=1), ProcessError),
            (RuntimeError("unexpected"), RuntimeError),
        ],
    )
    async def test_classifies_sdk_exceptions_by_type(self, raised, expected):
        client = MagicMock()
        client.stop_task = AsyncMock(side_effect=raised)

        with pytest.raises(expected) as info:
            await _backend().stop_task_client(client, "t1")

        if expected in (TaskStopClientUnavailable, TaskStopRejected, TimeoutError):
            assert info.value.__cause__ is raised


# ---------------------------------------------------------------------------
# Pin: the real SDK Query's error shapes (an SDK bump that types them fails here)
# ---------------------------------------------------------------------------


class _ControlReplyTransport:
    """Transport stub answering each control_request via ``reply(request)``.

    ``reply`` returns the control_response body, or None to never answer.
    """

    def __init__(self, reply: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]):
        self._reply = reply
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.requests: List[Dict[str, Any]] = []

    async def write(self, data: str) -> None:
        message = json.loads(data)
        if message.get("type") != "control_request":
            return
        self.requests.append(message["request"])
        body = self._reply(message["request"])
        if body is not None:
            await self._inbox.put(
                {
                    "type": "control_response",
                    "response": {"request_id": message["request_id"], **body},
                }
            )

    async def read_messages(self):
        while True:
            message = await self._inbox.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        await self._inbox.put(None)


async def _sdk_client(transport: _ControlReplyTransport):
    query = Query(transport=transport, is_streaming_mode=True)
    await query.start()
    client = GatewayClaudeSDKClient.__new__(GatewayClaudeSDKClient)
    client._query = query
    return client, query


class TestStopTaskAgainstRealSdkQuery:
    async def test_success_reply_returns_and_sends_stop_task(self):
        transport = _ControlReplyTransport(lambda req: {"subtype": "success"})
        client, query = await _sdk_client(transport)
        try:
            await _backend().stop_task_client(client, "t1")
        finally:
            await query.close()

        assert transport.requests == [{"subtype": "stop_task", "task_id": "t1"}]

    async def test_cli_error_reply_becomes_task_stop_rejected(self):
        transport = _ControlReplyTransport(
            lambda req: {"subtype": "error", "error": "stop_task: nope"}
        )
        client, query = await _sdk_client(transport)
        try:
            with pytest.raises(TaskStopRejected, match="stop_task: nope"):
                await _backend().stop_task_client(client, "t1")
        finally:
            await query.close()

    async def test_sdk_control_timeout_shape_becomes_timeout_error(self):
        # stop_task() uses the SDK's fixed 60 s budget, so drive the same
        # control path with a short one to capture the real timeout shape.
        transport = _ControlReplyTransport(lambda req: None)
        client, query = await _sdk_client(transport)
        try:
            with pytest.raises(Exception) as info:
                await query._send_control_request(
                    {"subtype": "stop_task", "task_id": "t1"}, timeout=0.05
                )
        finally:
            await query.close()
        sdk_timeout = info.value
        assert type(sdk_timeout) is Exception
        assert isinstance(sdk_timeout.__cause__, TimeoutError)

        stub = MagicMock()
        stub.stop_task = AsyncMock(side_effect=sdk_timeout)
        with pytest.raises(TimeoutError):
            await _backend().stop_task_client(stub, "t1")

    async def test_disconnected_client_becomes_client_unavailable(self):
        client = GatewayClaudeSDKClient.__new__(GatewayClaudeSDKClient)
        client._query = None

        with pytest.raises(TaskStopClientUnavailable):
            await _backend().stop_task_client(client, "t1")
