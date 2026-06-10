"""Tests for GET /v1/responses/{response_id} and DELETE /v1/responses/{response_id}."""

import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import src.main as main
import src.routes.general as general_module
import src.routes.responses as responses_module
from src.backend_registry import BackendRegistry
from src.constants import DEFAULT_MODEL
from src.session_manager import Session


@contextmanager
def client_context(patch_responses_auth=True):
    """TestClient with a mocked claude backend and workspace manager.

    ``patch_responses_auth=False`` leaves the real ``verify_api_key`` active
    on the responses routes so API-key enforcement can be tested.
    """
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
    mock_cli.run_completion_with_client = _default_run_with_client
    mock_cli.parse_message = MagicMock(return_value="Hello")
    mock_cli.estimate_token_usage = MagicMock(
        return_value={"prompt_tokens": 3, "completion_tokens": 5}
    )
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
    ]
    if patch_responses_auth:
        patches.append(
            patch.object(
                responses_module, "verify_api_key", new=AsyncMock(return_value=True)
            )
        )

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with TestClient(main.app) as client:
            yield client, mock_cli

    if main.limiter and hasattr(main.limiter, "_storage"):
        main.limiter._storage.reset()


def _post_turn(client, **overrides):
    payload = {"model": DEFAULT_MODEL, "input": "hello"}
    payload.update(overrides)
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestRetrieveResponse:
    def test_retrieve_happy_path_returns_stored_payload(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            created = _post_turn(client)
            resp = client.get(f"/v1/responses/{created['id']}")

        assert resp.status_code == 200
        body = resp.json()
        # Retrieval returns exactly what POST returned (ids, usage, created_at).
        assert body == created
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output"][-1]["content"][0]["text"] == "Hello"

    def test_retrieve_streaming_turn(self, isolated_session_manager):
        async def fake_stream_response_chunks(**kwargs):
            kwargs["chunks_buffer"].append(
                {"content": [{"type": "text", "text": "streamed answer"}]}
            )
            kwargs["stream_result"]["success"] = True
            kwargs["stream_result"]["assistant_text"] = "streamed answer"
            yield (
                'event: response.created\n'
                'data: {"type":"response.created","sequence_number":0}\n\n'
            )

        with (
            client_context() as (client, _mock_cli),
            patch.object(
                main.streaming_utils,
                "stream_response_chunks",
                new=fake_stream_response_chunks,
            ),
        ):
            with client.stream(
                "POST",
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "Stream this", "stream": True},
            ) as response:
                "".join(response.iter_text())
            assert response.status_code == 200

            session = next(iter(isolated_session_manager.sessions.values()))
            resp_id = f"resp_{session.session_id}_1"
            resp = client.get(f"/v1/responses/{resp_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == resp_id
        assert body["status"] == "completed"
        assert body["output"][-1]["content"][0]["text"] == "streamed answer"

    def test_retrieve_bad_id_format_returns_404(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            for bad_id in ("bogus", "resp_not-a-uuid_1", f"resp_{uuid.uuid4()}_0"):
                resp = client.get(f"/v1/responses/{bad_id}")
                assert resp.status_code == 404, bad_id
                error = resp.json()["error"]
                assert error["type"] == "invalid_request_error"
                assert error["code"] == "response_not_found"
                assert bad_id in error["message"]

    def test_retrieve_unknown_session_returns_404(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            resp = client.get(f"/v1/responses/resp_{uuid.uuid4()}_1")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "response_not_found"

    def test_retrieve_unknown_turn_returns_404(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            created = _post_turn(client)
            session_id = created["id"].split("_")[1]
            resp = client.get(f"/v1/responses/resp_{session_id}_2")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "response_not_found"

    def test_retrieve_store_false_returns_404(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            created = _post_turn(client, store=False)
            resp = client.get(f"/v1/responses/{created['id']}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "response_not_found"

    def test_retrieve_user_scoping(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            created = _post_turn(client, user="alice")
            resp_id = created["id"]

            matching = client.get(f"/v1/responses/{resp_id}", params={"user": "alice"})
            mismatching = client.get(f"/v1/responses/{resp_id}", params={"user": "eve"})
            omitted = client.get(f"/v1/responses/{resp_id}")

        assert matching.status_code == 200
        # Mismatch is indistinguishable from a missing response (no probing).
        assert mismatching.status_code == 404
        assert mismatching.json()["error"]["code"] == "response_not_found"
        # API-key auth alone suffices when user is omitted (sessions-route model).
        assert omitted.status_code == 200


class TestDeleteResponse:
    def test_delete_latest_turn_deletes_session(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            created = _post_turn(client)
            resp_id = created["id"]
            assert len(isolated_session_manager.sessions) == 1

            deleted = client.delete(f"/v1/responses/{resp_id}")
            retrieved_after = client.get(f"/v1/responses/{resp_id}")

        assert deleted.status_code == 200
        assert deleted.json() == {"id": resp_id, "object": "response", "deleted": True}
        assert isolated_session_manager.sessions == {}
        assert retrieved_after.status_code == 404

    def test_delete_non_latest_turn_returns_409(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            first = _post_turn(client)
            second = _post_turn(client, previous_response_id=first["id"])

            resp = client.delete(f"/v1/responses/{first['id']}")
            latest_still_there = client.get(f"/v1/responses/{second['id']}")

        assert resp.status_code == 409
        error = resp.json()["error"]
        assert error["code"] == "response_delete_not_latest"
        assert second["id"] in error["message"]
        # Session and its latest turn survive a rejected turn-level delete.
        assert len(isolated_session_manager.sessions) == 1
        assert latest_still_there.status_code == 200

    def test_delete_unknown_response_returns_404(self, isolated_session_manager):
        with client_context() as (client, _mock_cli):
            resp = client.delete(f"/v1/responses/resp_{uuid.uuid4()}_1")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "response_not_found"

    def test_delete_user_mismatch_returns_404_and_preserves_session(
        self, isolated_session_manager
    ):
        with client_context() as (client, _mock_cli):
            created = _post_turn(client, user="alice")
            resp = client.delete(
                f"/v1/responses/{created['id']}", params={"user": "eve"}
            )
        assert resp.status_code == 404
        assert len(isolated_session_manager.sessions) == 1


class TestRetrieveDeleteAuth:
    def test_endpoints_require_api_key(self, isolated_session_manager, monkeypatch):
        # Other tests importlib.reload(src.auth), replacing the module-level
        # auth_manager; verify_api_key reads the live instance via its module
        # globals, so set the key on the current attribute, not the import-time
        # binding.
        import src.auth as auth_module

        monkeypatch.setattr(auth_module.auth_manager, "runtime_api_key", "sk-test-key")
        resp_id = f"resp_{uuid.uuid4()}_1"
        with client_context(patch_responses_auth=False) as (client, _mock_cli):
            unauth_get = client.get(f"/v1/responses/{resp_id}")
            unauth_delete = client.delete(f"/v1/responses/{resp_id}")
            headers = {"Authorization": "Bearer sk-test-key"}
            auth_get = client.get(f"/v1/responses/{resp_id}", headers=headers)
            auth_delete = client.delete(f"/v1/responses/{resp_id}", headers=headers)

        assert unauth_get.status_code == 401
        assert unauth_delete.status_code == 401
        # With a valid key, auth passes and the unknown id 404s instead.
        assert auth_get.status_code == 404
        assert auth_delete.status_code == 404


class TestSessionTurnRecords:
    def test_record_and_get_turn_response(self):
        session = Session(session_id="s1")
        assert session.get_turn_response(1) is None
        session.record_turn_response(1, {"id": "resp_s1_1"})
        assert session.get_turn_response(1) == {"id": "resp_s1_1"}
        assert session.get_turn_response(2) is None
