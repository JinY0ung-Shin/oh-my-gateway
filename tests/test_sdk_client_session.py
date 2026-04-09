"""Tests for ClaudeSDKClient fields and disconnect lifecycle in Session/SessionManager."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock


from src.session_manager import Session, SessionManager


# ---------------------------------------------------------------------------
# Session dataclass field defaults
# ---------------------------------------------------------------------------


def test_session_new_fields_default_to_none():
    """Session has client, input_event, input_response, pending_tool_call — all None by default."""
    session = Session(session_id="test-defaults")
    assert session.client is None
    assert session.input_event is None
    assert session.input_response is None
    assert session.pending_tool_call is None


def test_session_new_fields_can_be_set():
    """New fields accept arbitrary values for assignment."""
    event = asyncio.Event()
    session = Session(
        session_id="test-set",
        client=object(),
        input_event=event,
        input_response="yes",
        pending_tool_call={"tool": "ask_user", "id": "tc_1"},
    )
    assert session.client is not None
    assert session.input_event is event
    assert session.input_response == "yes"
    assert session.pending_tool_call == {"tool": "ask_user", "id": "tc_1"}


def test_session_input_event_excluded_from_repr():
    """input_event must not appear in Session repr (repr=False)."""
    event = asyncio.Event()
    session = Session(session_id="repr-test", input_event=event)
    assert "input_event" not in repr(session)


def test_session_input_event_excluded_from_compare():
    """Sessions with different input_event values compare equal when other fields match."""
    s1 = Session(session_id="cmp", input_event=asyncio.Event())
    s2 = Session(
        session_id=s1.session_id,
        backend=s1.backend,
        provider_session_id=s1.provider_session_id,
        ttl_minutes=s1.ttl_minutes,
        messages=s1.messages,
        created_at=s1.created_at,
        last_accessed=s1.last_accessed,
        expires_at=s1.expires_at,
        turn_counter=s1.turn_counter,
        base_system_prompt=s1.base_system_prompt,
        user=s1.user,
        workspace=s1.workspace,
        client=s1.client,
        input_event=asyncio.Event(),  # different object
        input_response=s1.input_response,
        pending_tool_call=s1.pending_tool_call,
    )
    assert s1 == s2


# ---------------------------------------------------------------------------
# Expired session cleanup disconnects client
# ---------------------------------------------------------------------------


async def test_cleanup_expired_sessions_disconnects_client(fresh_session_manager: SessionManager):
    """When an expired session has a client, cleanup calls client.disconnect()."""
    mock_client = AsyncMock()

    session = fresh_session_manager.get_or_create_session("session-with-client")
    session.client = mock_client
    # Force expiry
    session.expires_at = session.expires_at - timedelta(hours=2)

    removed = await fresh_session_manager.cleanup_expired_sessions()

    assert removed == 1
    mock_client.disconnect.assert_awaited_once()
    # Client reference is cleared
    assert session.client is None
    # Session is removed from the manager
    assert "session-with-client" not in fresh_session_manager.sessions


async def test_cleanup_expired_sessions_tolerates_disconnect_error(
    fresh_session_manager: SessionManager,
):
    """Disconnect errors are swallowed so other sessions still get cleaned up."""
    failing_client = AsyncMock()
    failing_client.disconnect.side_effect = RuntimeError("connection lost")

    ok_client = AsyncMock()

    s1 = fresh_session_manager.get_or_create_session("failing")
    s1.client = failing_client
    s1.expires_at = s1.expires_at - timedelta(hours=2)

    s2 = fresh_session_manager.get_or_create_session("ok")
    s2.client = ok_client
    s2.expires_at = s2.expires_at - timedelta(hours=2)

    removed = await fresh_session_manager.cleanup_expired_sessions()

    assert removed == 2
    failing_client.disconnect.assert_awaited_once()
    ok_client.disconnect.assert_awaited_once()


async def test_cleanup_leaves_non_expired_sessions_intact(fresh_session_manager: SessionManager):
    """cleanup_expired_sessions only removes expired sessions, not live ones."""
    mock_client = AsyncMock()

    live = fresh_session_manager.get_or_create_session("live")
    live.client = mock_client

    expired = fresh_session_manager.get_or_create_session("dead")
    expired.expires_at = expired.expires_at - timedelta(hours=2)

    removed = await fresh_session_manager.cleanup_expired_sessions()

    assert removed == 1
    assert "live" in fresh_session_manager.sessions
    assert "dead" not in fresh_session_manager.sessions
    mock_client.disconnect.assert_not_awaited()


# ---------------------------------------------------------------------------
# async_shutdown disconnects all clients
# ---------------------------------------------------------------------------


async def test_async_shutdown_disconnects_all_clients(fresh_session_manager: SessionManager):
    """async_shutdown calls disconnect on every session that has a client."""
    client_a = AsyncMock()
    client_b = AsyncMock()

    sa = fresh_session_manager.get_or_create_session("a")
    sa.client = client_a

    sb = fresh_session_manager.get_or_create_session("b")
    sb.client = client_b

    fresh_session_manager.get_or_create_session("c")
    # session "c" has no client — should not raise

    await fresh_session_manager.async_shutdown()

    client_a.disconnect.assert_awaited_once()
    client_b.disconnect.assert_awaited_once()
    assert len(fresh_session_manager.sessions) == 0


async def test_async_shutdown_tolerates_disconnect_error(fresh_session_manager: SessionManager):
    """async_shutdown swallows disconnect errors and still clears all sessions."""
    bad_client = AsyncMock()
    bad_client.disconnect.side_effect = Exception("boom")

    good_client = AsyncMock()

    s1 = fresh_session_manager.get_or_create_session("bad")
    s1.client = bad_client

    s2 = fresh_session_manager.get_or_create_session("good")
    s2.client = good_client

    await fresh_session_manager.async_shutdown()

    bad_client.disconnect.assert_awaited_once()
    good_client.disconnect.assert_awaited_once()
    assert len(fresh_session_manager.sessions) == 0


async def test_async_shutdown_clears_client_reference(fresh_session_manager: SessionManager):
    """Client reference is set to None after disconnect during shutdown."""
    mock_client = AsyncMock()
    session = fresh_session_manager.get_or_create_session("ref-clear")
    session.client = mock_client

    await fresh_session_manager.async_shutdown()

    # The session object itself has client=None after shutdown
    assert session.client is None


# ---------------------------------------------------------------------------
# _purge_all_expired TOCTOU re-check
# ---------------------------------------------------------------------------


async def test_purge_skips_session_refreshed_between_snapshot_and_delete(
    fresh_session_manager: SessionManager,
):
    """A session refreshed (TTL extended) after the expired snapshot is NOT deleted."""
    session = fresh_session_manager.get_or_create_session("refreshable")
    # Force expiry so it appears in the snapshot
    session.expires_at = session.expires_at - timedelta(hours=2)

    # Take the snapshot (the internal method does this under lock)
    with fresh_session_manager.lock:
        expired_snapshot = [
            (sid, fresh_session_manager.sessions[sid])
            for sid, s in fresh_session_manager.sessions.items()
            if s.is_expired()
        ]
    assert len(expired_snapshot) == 1

    # Simulate a concurrent refresh: touch() extends the TTL
    session.touch()
    assert not session.is_expired()

    # Now run the full purge — the re-check should skip this session
    removed = await fresh_session_manager.cleanup_expired_sessions()

    assert removed == 0
    assert "refreshable" in fresh_session_manager.sessions
