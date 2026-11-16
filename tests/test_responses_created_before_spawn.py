"""The stream opens before a fresh backend client is spawned (#277).

Starting a CLI process tree takes seconds (production p50 4.2 s), and the stream's first byte used to
wait for it: the user saw nothing at all. Now a streamed turn that must start a client sends
``response.created`` + ``response.in_progress`` first and spawns inside the stream. Pinned here:

- the opening frames reach the client **while** the spawn is still running (raw ASGI, the bytes);
- the turn then streams on with continuous sequence numbers (2, 3, …) and commits as before;
- a turn whose client is already up keeps the old order;
- every check before the spawn still answers with its HTTP status;
- ``RESPONSES_CREATED_BEFORE_SPAWN=false`` restores the old order.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import src.main as main
from src.constants import DEFAULT_MODEL
from src.session_manager import session_manager
from tests.test_main_coverage_unit import client_context


def _events(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


async def _post_raw(body: dict, sent: list[dict], created_seen: asyncio.Event) -> None:
    """POST /v1/responses straight through the ASGI app, recording every message it sends."""
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()  # no disconnect

    async def send(message):
        sent.append(message)
        if message[
            "type"
        ] == "http.response.body" and b"response.created" in message.get("body", b""):
            created_seen.set()

    await main.app(scope, receive, send)


def test_response_created_reaches_the_client_while_the_cli_is_still_starting():
    state: dict = {}

    with client_context() as (client, mock_cli):

        async def slow_create_client(**kwargs):
            # The spawn: it finishes only after the client already holds response.created.
            try:
                await asyncio.wait_for(state["created_seen"].wait(), timeout=2.0)
                state["created_before_spawn_returned"] = True
            except asyncio.TimeoutError:
                state["created_before_spawn_returned"] = False
            return object()

        mock_cli.create_client = slow_create_client

        async def run():
            state["created_seen"] = asyncio.Event()
            sent: list[dict] = []
            await _post_raw(
                {"model": DEFAULT_MODEL, "input": "hello", "stream": True},
                sent,
                state["created_seen"],
            )
            return sent

        sent = client.portal.call(run)

    assert (
        state["created_before_spawn_returned"] is True
    ), "response.created waited for the spawn"
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    text = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    ).decode()
    events = _events(text)
    assert [e["type"] for e in events[:2]] == [
        "response.created",
        "response.in_progress",
    ]
    assert events[-1]["type"] == "response.completed"
    # one response, numbered continuously across the hand-off
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert len({e["response"]["id"] for e in events if "response" in e}) == 1


def test_the_turn_commits_and_continues_as_before():
    with client_context() as (client, mock_cli):
        first = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "hello", "stream": True},
        )
        events = _events(first.text)
        assert events[-1]["type"] == "response.completed"
        resp_id = events[-1]["response"]["id"]
        assert events[0]["response"]["id"] == resp_id
        session_id = resp_id[len("resp_") :].rsplit("_", 1)[0]
        session = session_manager.get_session(session_id)
        assert session is not None and session.turn_counter == 1
        assert not session.lock.locked() and session.active_response_id is None

        second = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "again",
                "stream": True,
                "previous_response_id": resp_id,
            },
        )
        assert second.status_code == 200
        assert _events(second.text)[-1]["type"] == "response.completed"
        assert session.turn_counter == 2


def test_a_turn_whose_client_is_already_up_keeps_the_old_order(monkeypatch):
    """Nothing to wait for: the continuation's client is live, so no spawn moves into the stream."""
    from src.routes import responses as responses_module

    with client_context() as (client, mock_cli):
        first = client.post(
            "/v1/responses",
            json={"model": DEFAULT_MODEL, "input": "hello", "stream": True},
        )
        resp_id = _events(first.text)[-1]["response"]["id"]
        calls: list[bool] = []
        real = responses_module._created_before_spawn

        def spy():
            calls.append(True)
            return real()

        monkeypatch.setattr(responses_module, "_created_before_spawn", spy)
        second = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "again",
                "stream": True,
                "previous_response_id": resp_id,
            },
        )
    assert second.status_code == 200
    assert (
        calls == []
    ), "a live client must not be routed through the spawn-in-stream path"


def test_checks_before_the_spawn_still_answer_with_their_http_status():
    """An unknown previous_response_id is refused before any stream opens."""
    with client_context() as (client, mock_cli):
        resp = client.post(
            "/v1/responses",
            json={
                "model": DEFAULT_MODEL,
                "input": "hello",
                "stream": True,
                "previous_response_id": "resp_" + "0" * 32 + "_1",
            },
        )
    assert resp.status_code in (400, 404)
    assert "response.created" not in resp.text


@pytest.mark.parametrize(
    "detail, status, expected",
    [
        (
            {
                "error": {
                    "code": "session_ended",
                    "message": "The session ended.",
                    "type": "x",
                }
            },
            409,
            ("session_ended", "The session ended."),
        ),
        (
            "claude backend unavailable; retry shortly",
            503,
            ("backend_unavailable", "claude backend unavailable; retry shortly"),
        ),
        ("boom", 500, ("server_error", "boom")),
    ],
    ids=["structured", "retryable-503", "plain"],
)
def test_a_spawn_failure_keeps_its_code_and_message(detail, status, expected):
    from fastapi import HTTPException

    from src.routes.responses import _stream_setup_failure

    assert (
        _stream_setup_failure(HTTPException(status_code=status, detail=detail))
        == expected
    )
    assert _stream_setup_failure(RuntimeError("x"))[0] == "server_error"


def test_the_old_order_is_one_switch_away(monkeypatch):
    monkeypatch.setenv("RESPONSES_CREATED_BEFORE_SPAWN", "false")
    state: dict = {}

    with client_context() as (client, mock_cli):

        async def slow_create_client(**kwargs):
            try:
                await asyncio.wait_for(state["created_seen"].wait(), timeout=0.3)
                state["created_before_spawn_returned"] = True
            except asyncio.TimeoutError:
                state["created_before_spawn_returned"] = False
            return object()

        mock_cli.create_client = slow_create_client

        async def run():
            state["created_seen"] = asyncio.Event()
            await _post_raw(
                {"model": DEFAULT_MODEL, "input": "hello", "stream": True},
                [],
                state["created_seen"],
            )

        client.portal.call(run)

    assert state["created_before_spawn_returned"] is False


def test_a_client_that_leaves_while_the_cli_starts_does_not_strand_the_session():
    """Stop in ChatDRAGON closes the stream. If that happens during the spawn, the session lock and the
    client-start bookkeeping are released — the next turn on the session is not blocked.
    """
    state: dict = {}

    with client_context() as (client, mock_cli):

        async def never_ready(**kwargs):
            state["session"] = kwargs["session"]
            await asyncio.Event().wait()  # the CLI never finishes starting
            return object()

        mock_cli.create_client = never_ready

        async def run():
            created = asyncio.Event()
            payload = json.dumps(
                {"model": DEFAULT_MODEL, "input": "hello", "stream": True}
            ).encode()
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/responses",
                "raw_path": b"/v1/responses",
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"host", b"testserver"),
                ],
                "client": ("127.0.0.1", 50001),
                "server": ("testserver", 80),
            }
            calls = 0

            async def receive():
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {"type": "http.request", "body": payload, "more_body": False}
                await created.wait()  # the user presses Stop once the stream is open
                return {"type": "http.disconnect"}

            async def send(message):
                if message[
                    "type"
                ] == "http.response.body" and b"response.created" in message.get(
                    "body", b""
                ):
                    created.set()

            await asyncio.wait_for(main.app(scope, receive, send), timeout=5.0)
            for _ in range(50):  # the teardown may finish detached
                if not state["session"].lock.locked():
                    break
                await asyncio.sleep(0.02)

        client.portal.call(run)

    session = state["session"]
    assert not session.lock.locked()
    assert session.client is None
    assert session.active_response_id is None
