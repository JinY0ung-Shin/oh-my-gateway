"""Tests for background mode (``background=true``) on /v1/responses."""

import asyncio
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import src.main as main
import src.routes.general as general_module
import src.routes.responses as responses_module
from src.backend_registry import BackendRegistry
from src.constants import DEFAULT_MODEL
from src.session_manager import Session, _utcnow


@contextmanager
def client_context(run_with_client=None, interrupt_client=None):
    """TestClient with a mocked claude backend (background-mode variants)."""
    mock_wm = MagicMock()
    mock_wm.resolve.return_value = Path("/tmp/ws/alice")

    mock_cli = MagicMock()
    mock_cli.verify_cli = AsyncMock(return_value=True)
    mock_cli.verify = AsyncMock(return_value=True)

    async def _default_create_client(**kwargs):
        return object()

    async def _default_run_with_client(client, prompt, session):
        yield {"subtype": "success", "result": "Hello"}

    mock_cli.create_client = _default_create_client
    mock_cli.run_completion_with_client = run_with_client or _default_run_with_client
    mock_cli.parse_message = MagicMock(return_value="Hello")
    mock_cli.estimate_token_usage = MagicMock(
        return_value={"prompt_tokens": 3, "completion_tokens": 5}
    )
    if interrupt_client is not None:
        mock_cli.interrupt_client = interrupt_client
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()

    def _mock_discover():
        from tests.conftest import register_all_descriptors

        register_all_descriptors()
        BackendRegistry.register("claude", mock_cli)

    patches = [
        patch.object(main, "discover_backends", _mock_discover),
        patch.object(general_module, "verify_api_key", new=AsyncMock(return_value=True)),
        patch.object(
            main, "validate_claude_code_auth", return_value=(True, {"method": "test"})
        ),
        patch.object(responses_module, "validate_backend_auth_or_raise"),
        patch.object(main.session_manager, "start_cleanup_task"),
        patch.object(main.session_manager, "async_shutdown", new=AsyncMock()),
        patch.object(responses_module, "workspace_manager", mock_wm),
        patch.object(responses_module, "verify_api_key", new=AsyncMock(return_value=True)),
    ]

    responses_module._BACKGROUND_RUNS.clear()
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with TestClient(main.app) as client:
            yield client, mock_cli

    responses_module._BACKGROUND_RUNS.clear()
    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


def _poll_until(client, resp_id, target_statuses, timeout_s=5.0, user=None):
    """Poll GET /v1/responses/{id} until its status lands in *target_statuses*."""
    params = {"user": user} if user is not None else None
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/responses/{resp_id}", params=params)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["status"] in target_statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"response never reached {target_statuses}; last={last}")


class TestBackgroundValidation:
    def test_rejects_stream(self, isolated_session_manager):
        with client_context() as (client, _):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "x",
                    "background": True,
                    "stream": True,
                },
            )
        assert resp.status_code == 400
        assert "stream" in resp.json()["error"]["message"]

    def test_rejects_store_false(self, isolated_session_manager):
        with client_context() as (client, _):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "x",
                    "background": True,
                    "store": False,
                },
            )
        assert resp.status_code == 400
        assert "store" in resp.json()["error"]["message"]

    def test_rejects_function_call_output_continuation(self, isolated_session_manager):
        with client_context() as (client, _):
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "background": True,
                    "input": [
                        {
                            "type": "function_call_output",
                            "call_id": "call_1",
                            "output": "answer",
                        }
                    ],
                },
            )
        assert resp.status_code == 400
        assert "function_call_output" in resp.json()["error"]["message"]


class TestBackgroundLifecycle:
    def test_queued_then_in_progress_then_completed_and_chains(
        self, isolated_session_manager
    ):
        release = threading.Event()

        async def gated_run(client, prompt, session):
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield {"subtype": "success", "result": "Hello"}

        with client_context(run_with_client=gated_run) as (client, _):
            created = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hi", "background": True},
            )
            assert created.status_code == 200, created.text
            body = created.json()
            assert body["status"] == "queued"
            assert body["background"] is True
            assert body["output"] == []
            resp_id = body["id"]

            # The runner flips the registry payload to in_progress; the run
            # cannot finish until the gate opens, so this is deterministic.
            in_flight = _poll_until(client, resp_id, {"in_progress"})
            assert in_flight["background"] is True

            release.set()
            final = _poll_until(client, resp_id, {"completed"})
            assert final["output"][-1]["content"][0]["text"] == "Hello"
            assert final["background"] is True
            assert final["usage"]["input_tokens"] >= 0
            assert resp_id not in responses_module._BACKGROUND_RUNS

            # The committed turn chains like any other turn.
            release.set()
            follow_up = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "next",
                    "previous_response_id": resp_id,
                },
            )
            assert follow_up.status_code == 200, follow_up.text
            assert follow_up.json()["status"] == "completed"

    def test_failure_is_retrievable_and_turn_is_retryable(
        self, isolated_session_manager
    ):
        calls = {"n": 0}

        async def flaky_run(client, prompt, session):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            yield {"subtype": "success", "result": "Hello"}

        with client_context(run_with_client=flaky_run) as (client, _):
            first = client.post(
                "/v1/responses", json={"model": DEFAULT_MODEL, "input": "hi"}
            )
            assert first.status_code == 200, first.text
            prev_id = first.json()["id"]

            background = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "do it",
                    "background": True,
                    "previous_response_id": prev_id,
                },
            ).json()
            failed = _poll_until(client, background["id"], {"failed"})
            assert failed["error"]["code"] == "server_error"
            # Redaction policy: raw exception text must not leak.
            assert "boom" not in failed["error"]["message"]

            # The failed turn never advanced the counter — retrying from the
            # same previous id reuses the same response id and overwrites the
            # failed registry entry.
            retry = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "do it",
                    "background": True,
                    "previous_response_id": prev_id,
                },
            ).json()
            assert retry["id"] == background["id"]
            final = _poll_until(client, retry["id"], {"completed"})
            assert final["output"][-1]["content"][0]["text"] == "Hello"

    def test_cancel_commits_continuable_incomplete_turn(self, isolated_session_manager):
        cancel = {"requested": False}

        async def gated_run(client, prompt, session):
            if cancel["requested"]:
                # Continuation turns after the cancel finish immediately.
                yield {"subtype": "success", "result": "Hello"}
                return
            while not cancel["requested"]:
                await asyncio.sleep(0.01)
            yield {
                "type": "result",
                "subtype": "error_during_execution",
                "gateway_interrupted": True,
            }

        async def interrupt_client(client):
            cancel["requested"] = True

        with client_context(
            run_with_client=gated_run, interrupt_client=interrupt_client
        ) as (client, _):
            background = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "long task", "background": True},
            ).json()
            resp_id = background["id"]
            _poll_until(client, resp_id, {"in_progress"})

            cancelled = client.post(f"/v1/responses/{resp_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelling"

            final = _poll_until(client, resp_id, {"incomplete"})
            assert final["incomplete_details"]["reason"] == "user_cancelled"
            assert final["background"] is True
            assert resp_id not in responses_module._BACKGROUND_RUNS

            # Interrupt committed the turn — it chains like a streamed interrupt.
            follow_up = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "continue",
                    "previous_response_id": resp_id,
                },
            )
            assert follow_up.status_code == 200, follow_up.text

    def test_get_scopes_background_run_to_owner(self, isolated_session_manager):
        release = threading.Event()

        async def gated_run(client, prompt, session):
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield {"subtype": "success", "result": "Hello"}

        with client_context(run_with_client=gated_run) as (client, _):
            background = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hi",
                    "background": True,
                    "user": "alice",
                },
            ).json()
            resp_id = background["id"]

            wrong_user = client.get(f"/v1/responses/{resp_id}", params={"user": "bob"})
            assert wrong_user.status_code == 404
            owner = client.get(f"/v1/responses/{resp_id}", params={"user": "alice"})
            assert owner.status_code == 200

            release.set()
            _poll_until(client, resp_id, {"completed"}, user="alice")


class TestBackgroundSessionPinning:
    def test_active_response_pins_expired_session(self):
        session = Session(session_id=str(uuid.uuid4()))
        session.expires_at = _utcnow() - timedelta(seconds=5)
        assert session.is_expired() is True

        session.active_response_id = "resp_x_1"
        assert session.is_expired() is False

        session.active_response_id = None
        assert session.is_expired() is True
