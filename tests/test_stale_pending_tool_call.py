"""Stale AskUserQuestion state must not survive its owning client.

The pause lives inside the client's ``can_use_tool`` callback; when that
client dies (stream-failure disconnect, consumer disconnect, replacement),
a leftover ``pending_tool_call`` makes every later turn re-emit the same
requires_action card at stream end and keeps the idle reader gated off.
"""

import asyncio

import pytest

from src.routes.responses import (
    _clear_stale_pending_tool_call,
    _disconnect_session_client,
)
from src.session_manager import Session


class _DummyClient:
    def __init__(self):
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


@pytest.mark.asyncio
async def test_disconnect_clears_pending_ask_state():
    client = _DummyClient()
    session = Session(session_id="s-stale", client=client)
    session.pending_tool_call = {
        "call_id": "c1",
        "name": "AskUserQuestion",
        "arguments": {},
    }
    session.input_event = asyncio.Event()
    session.input_response = "ignored"

    await _disconnect_session_client(session, "test teardown")

    assert session.pending_tool_call is None
    assert session.input_event is None
    assert session.input_response is None
    assert session.client is None
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_clear_unblocks_live_hook_waiter():
    session = Session(session_id="s-hook")
    session.pending_tool_call = {
        "call_id": "c2",
        "name": "AskUserQuestion",
        "arguments": {},
    }
    event = asyncio.Event()
    session.input_event = event

    _clear_stale_pending_tool_call(session, "test")

    # A still-alive hook coroutine resumes immediately (reads a null response
    # and denies gracefully) instead of waiting out ASK_USER_TIMEOUT_SECONDS.
    assert event.is_set()
    assert session.pending_tool_call is None
    assert session.input_event is None


def test_clear_is_noop_without_pending_state():
    session = Session(session_id="s-clean")
    _clear_stale_pending_tool_call(session, "test")
    assert session.pending_tool_call is None
    assert session.input_event is None
