#!/usr/bin/env python3
"""
Unit tests for src/session_manager.py

Tests the Session and SessionManager classes.
These are pure unit tests that don't require a running server.
"""

import pytest
from datetime import datetime, timedelta, timezone
import asyncio

from src.session_manager import Session, SessionManager
from src.models import Message


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TestSession:
    """Test the Session dataclass."""

    def test_session_creation_with_id(self):
        """Session can be created with just an ID."""
        session = Session(session_id="test-123")
        assert session.session_id == "test-123"
        assert session.messages == []
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.last_accessed, datetime)
        assert isinstance(session.expires_at, datetime)

    def test_session_expiry_in_future(self):
        """Newly created session expires in the future."""
        session = Session(session_id="test-123")
        assert session.expires_at > utc_now()

    def test_touch_updates_last_accessed(self):
        """touch() updates last_accessed time."""
        session = Session(session_id="test-123")
        original_accessed = session.last_accessed

        import time

        time.sleep(0.01)
        session.touch()

        assert session.last_accessed >= original_accessed

    def test_touch_extends_expiration(self):
        """touch() extends the expiration time."""
        session = Session(session_id="test-123")
        original_expires = session.expires_at

        import time

        time.sleep(0.01)
        session.touch()

        assert session.expires_at >= original_expires

    def test_add_messages_appends_to_list(self):
        """add_messages() appends new messages to the session."""
        session = Session(session_id="test-123")
        msg1 = Message(role="user", content="Hello")
        msg2 = Message(role="assistant", content="Hi there")

        session.add_messages([msg1])
        assert len(session.messages) == 1

        session.add_messages([msg2])
        assert len(session.messages) == 2

    def test_add_messages_touches_session(self):
        """add_messages() also touches the session."""
        session = Session(session_id="test-123")
        original_accessed = session.last_accessed

        import time

        time.sleep(0.01)
        session.add_messages([Message(role="user", content="Test")])

        assert session.last_accessed >= original_accessed

    def test_get_all_messages_returns_all(self):
        """get_all_messages() returns all messages."""
        session = Session(session_id="test-123")
        msg1 = Message(role="user", content="Hello")
        msg2 = Message(role="assistant", content="Hi")

        session.add_messages([msg1, msg2])
        messages = session.get_all_messages()

        assert len(messages) == 2
        assert messages[0].content == "Hello"
        assert messages[1].content == "Hi"

    def test_get_all_messages_returns_copy(self):
        """get_all_messages() returns a shallow copy — mutating it doesn't affect the session."""
        session = Session(session_id="test-123")
        session.add_messages([Message(role="user", content="Hello")])

        returned = session.get_all_messages()
        returned.append(Message(role="user", content="Ghost"))

        assert len(session.messages) == 1, "Internal list should not be modified"

    def test_is_expired_false_for_new_session(self):
        """Newly created session is not expired."""
        session = Session(session_id="test-123")
        assert session.is_expired() is False

    def test_is_expired_true_for_past_expiry(self):
        """Session with past expiry is expired."""
        session = Session(session_id="test-123", expires_at=utc_now() - timedelta(hours=1))
        assert session.is_expired() is True

    def test_to_session_info_returns_correct_model(self):
        """to_session_info() returns properly populated SessionInfo."""
        session = Session(session_id="test-123")
        session.add_messages([Message(role="user", content="Test")])

        info = session.to_session_info()

        assert info.session_id == "test-123"
        assert info.message_count == 1
        assert isinstance(info.created_at, datetime)
        assert isinstance(info.last_accessed, datetime)
        assert isinstance(info.expires_at, datetime)


class TestSessionManager:
    """Test the SessionManager class."""

    @pytest.fixture
    def manager(self, fresh_session_manager):
        return fresh_session_manager

    def test_manager_initialization(self, manager):
        """SessionManager initializes with empty sessions."""
        assert len(manager.sessions) == 0
        assert manager.default_ttl_minutes == 60
        assert manager.cleanup_interval_minutes == 5

    def test_get_or_create_session_creates_new(self, manager):
        """get_or_create_session() creates new session if not exists."""
        session = manager.get_or_create_session("new-session")

        assert session is not None
        assert session.session_id == "new-session"
        assert "new-session" in manager.sessions

    def test_get_or_create_session_returns_existing(self, manager):
        """get_or_create_session() returns existing session."""
        session1 = manager.get_or_create_session("existing")
        session1.add_messages([Message(role="user", content="Test")])

        session2 = manager.get_or_create_session("existing")

        assert session2 is session1
        assert len(session2.messages) == 1

    def test_get_or_create_replaces_expired_session(self, manager):
        """get_or_create_session() replaces expired session with new one."""
        # Create session and add messages first
        session1 = manager.get_or_create_session("expiring")
        session1.add_messages([Message(role="user", content="Old")])
        # Expire AFTER adding messages (add_messages calls touch() which extends expiry)
        session1.expires_at = utc_now() - timedelta(hours=1)

        # Should get a new session since the old one is expired
        session2 = manager.get_or_create_session("expiring")

        assert len(session2.messages) == 0  # New session has no messages

    def test_get_session_returns_none_for_nonexistent(self, manager):
        """get_session() returns None for non-existent session."""
        result = manager.get_session("nonexistent")
        assert result is None

    def test_get_session_returns_existing(self, manager):
        """get_session() returns existing active session."""
        manager.get_or_create_session("existing")
        result = manager.get_session("existing")

        assert result is not None
        assert result.session_id == "existing"

    def test_get_session_returns_none_for_expired(self, manager):
        """get_session() returns None and cleans up expired session."""
        session = manager.get_or_create_session("expiring")
        session.expires_at = utc_now() - timedelta(hours=1)

        result = manager.get_session("expiring")

        assert result is None
        assert "expiring" not in manager.sessions

    def test_delete_session_removes_session(self, manager):
        """delete_session() removes existing session."""
        manager.get_or_create_session("to-delete")
        assert "to-delete" in manager.sessions

        result = manager.delete_session("to-delete")

        assert result is True
        assert "to-delete" not in manager.sessions

    def test_delete_session_returns_false_for_nonexistent(self, manager):
        """delete_session() returns False for non-existent session."""
        result = manager.delete_session("nonexistent")
        assert result is False

    def test_list_sessions_returns_active_sessions(self, manager):
        """list_sessions() returns list of active sessions."""
        manager.get_or_create_session("session-1")
        manager.get_or_create_session("session-2")

        sessions = manager.list_sessions()

        assert len(sessions) == 2
        session_ids = [s.session_id for s in sessions]
        assert "session-1" in session_ids
        assert "session-2" in session_ids

    def test_list_sessions_excludes_expired(self, manager):
        """list_sessions() excludes and cleans up expired sessions."""
        manager.get_or_create_session("active")
        expired = manager.get_or_create_session("expired")
        expired.expires_at = utc_now() - timedelta(hours=1)

        sessions = manager.list_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "active"

    def test_process_messages_stateless_mode(self, manager):
        """process_messages() in stateless mode returns messages as-is."""
        messages = [Message(role="user", content="Hello")]

        result_msgs, session_id = manager.process_messages(messages, session_id=None)

        assert result_msgs == messages
        assert session_id is None

    def test_process_messages_session_mode(self, manager):
        """process_messages() in session mode accumulates messages."""
        msg1 = Message(role="user", content="First")
        msg2 = Message(role="user", content="Second")

        # First call
        result1, sid1 = manager.process_messages([msg1], session_id="my-session")
        assert len(result1) == 1
        assert sid1 == "my-session"

        # Second call - should have both messages
        result2, sid2 = manager.process_messages([msg2], session_id="my-session")
        assert len(result2) == 2
        assert sid2 == "my-session"

    def test_add_assistant_response_in_session_mode(self, manager):
        """add_assistant_response() adds response to session."""
        manager.get_or_create_session("my-session")
        assistant_msg = Message(role="assistant", content="Hello!")

        manager.add_assistant_response("my-session", assistant_msg)

        session = manager.get_session("my-session")
        assert len(session.messages) == 1
        assert session.messages[0].role == "assistant"

    def test_add_assistant_response_stateless_mode_noop(self, manager):
        """add_assistant_response() does nothing in stateless mode."""
        assistant_msg = Message(role="assistant", content="Hello!")

        # Should not raise, just do nothing
        manager.add_assistant_response(None, assistant_msg)

    def test_get_stats_returns_correct_counts(self, manager):
        """get_stats() returns correct statistics."""
        manager.get_or_create_session("session-1")
        session2 = manager.get_or_create_session("session-2")
        session2.add_messages([Message(role="user", content="Test")])

        # Create expired session
        expired = manager.get_or_create_session("expired")
        expired.expires_at = utc_now() - timedelta(hours=1)

        stats = manager.get_stats()

        assert stats["active_sessions"] == 2
        assert stats["expired_sessions"] == 1
        assert stats["total_messages"] == 1

    def test_shutdown_clears_sessions(self, manager):
        """shutdown() clears all sessions."""
        manager.get_or_create_session("session-1")
        manager.get_or_create_session("session-2")
        assert len(manager.sessions) == 2

        manager.shutdown()

        assert len(manager.sessions) == 0

    async def test_cleanup_expired_sessions(self, manager):
        """cleanup_expired_sessions() removes only expired sessions."""
        manager.get_or_create_session("active")
        expired = manager.get_or_create_session("expired")
        expired.expires_at = utc_now() - timedelta(hours=1)

        removed = await manager.cleanup_expired_sessions()

        assert "active" in manager.sessions
        assert "expired" not in manager.sessions
        assert removed == 1


class TestSessionManagerAsync:
    """Test async functionality of SessionManager."""

    @pytest.fixture
    def manager(self, fresh_session_manager):
        return fresh_session_manager

    async def test_start_cleanup_task_creates_task(self, manager):
        """start_cleanup_task() creates an async task when loop is running."""
        # Start the cleanup task
        manager.start_cleanup_task()

        # Task should be created
        assert manager._cleanup_task is not None

        # Clean up
        manager.shutdown()

    async def test_start_cleanup_task_idempotent(self, manager):
        """start_cleanup_task() only creates one task."""
        manager.start_cleanup_task()
        first_task = manager._cleanup_task

        manager.start_cleanup_task()
        second_task = manager._cleanup_task

        assert first_task is second_task

        # Clean up
        manager.shutdown()


class TestSessionManagerThreadSafety:
    """Test thread safety of SessionManager operations."""

    @pytest.fixture
    def manager(self):
        """Create a fresh SessionManager for each test."""
        return SessionManager()

    def test_concurrent_session_creation(self, manager):
        """Multiple threads can create sessions concurrently."""
        import threading

        results = []
        errors = []

        def create_session(session_id):
            try:
                session = manager.get_or_create_session(session_id)
                results.append(session.session_id)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for i in range(10):
            t = threading.Thread(target=create_session, args=(f"session-{i}",))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        assert len(manager.sessions) == 10


class TestSessionTurnCounterAndLock:
    """Test Session.turn_counter field and lock."""

    def test_session_has_turn_counter_default_zero(self):
        session = Session(session_id="test-123")
        assert session.turn_counter == 0

    def test_session_turn_counter_can_be_incremented(self):
        session = Session(session_id="test-123")
        session.turn_counter += 1
        assert session.turn_counter == 1

    def test_session_has_lock(self):
        session = Session(session_id="test-123")
        assert hasattr(session, "lock")
        assert isinstance(session.lock, asyncio.Lock)

    def test_session_lock_excluded_from_repr(self):
        """Per-session lock should not appear in repr."""
        session = Session(session_id="test-123")
        assert "lock=" not in repr(session)


class TestEnsureUtc:
    """Test the _ensure_utc() helper function."""

    def test_naive_datetime_gets_utc_attached(self):
        """Naive datetime (no tzinfo) gets UTC timezone attached."""
        from src.session_manager import _ensure_utc

        naive = datetime(2025, 6, 15, 12, 0, 0)
        assert naive.tzinfo is None

        result = _ensure_utc(naive)
        assert result.tzinfo == timezone.utc
        # Value should be identical (just tagged, not converted)
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 15
        assert result.hour == 12

    def test_utc_datetime_passes_through_unchanged(self):
        """Already-UTC datetime passes through unchanged."""
        from src.session_manager import _ensure_utc

        aware = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = _ensure_utc(aware)

        assert result.tzinfo == timezone.utc
        assert result == aware

    def test_non_utc_aware_datetime_converted_to_utc(self):
        """Non-UTC aware datetime gets converted to UTC."""
        from src.session_manager import _ensure_utc

        # Create a datetime at UTC+9 (e.g., KST)
        kst = timezone(timedelta(hours=9))
        dt_kst = datetime(2025, 6, 15, 21, 0, 0, tzinfo=kst)  # 21:00 KST = 12:00 UTC

        result = _ensure_utc(dt_kst)

        assert result.tzinfo == timezone.utc
        assert result.hour == 12
        assert result.day == 15


class TestSessionCustomTtl:
    """Test Session with custom ttl_minutes."""

    def test_custom_ttl_affects_expiration(self):
        """Session with custom TTL expires at the correct time."""
        session = Session(session_id="short-lived", ttl_minutes=5)

        # expires_at should be roughly 5 minutes from now, not 60
        expected_lower = utc_now() + timedelta(minutes=4, seconds=50)
        expected_upper = utc_now() + timedelta(minutes=5, seconds=10)

        assert expected_lower < session.expires_at < expected_upper

    def test_touch_extends_by_custom_ttl(self):
        """touch() extends expiration by the session's own TTL, not default."""
        session = Session(session_id="short-lived", ttl_minutes=10)

        import time

        time.sleep(0.01)
        session.touch()

        expected_lower = utc_now() + timedelta(minutes=9, seconds=50)
        expected_upper = utc_now() + timedelta(minutes=10, seconds=10)

        assert expected_lower < session.expires_at < expected_upper


class TestAsyncShutdown:
    """Test async_shutdown() method."""

    @pytest.fixture
    def manager(self, fresh_session_manager):
        return fresh_session_manager

    async def test_async_shutdown_cancels_cleanup_task(self, manager):
        """async_shutdown() cancels the cleanup task if one exists."""
        manager.start_cleanup_task()
        assert manager._cleanup_task is not None

        task = manager._cleanup_task
        await manager.async_shutdown()

        # Allow cancellation to propagate
        await asyncio.sleep(0)

        assert task.cancelled()
        assert len(manager.sessions) == 0

    async def test_async_shutdown_clears_all_sessions(self, manager):
        """async_shutdown() clears all sessions."""
        manager.get_or_create_session("s1")
        manager.get_or_create_session("s2")
        manager.get_or_create_session("s3")
        assert len(manager.sessions) == 3

        await manager.async_shutdown()

        assert len(manager.sessions) == 0

    async def test_async_shutdown_works_without_cleanup_task(self, manager):
        """async_shutdown() works even when _cleanup_task is None."""
        assert manager._cleanup_task is None

        manager.get_or_create_session("s1")

        # Should not raise
        await manager.async_shutdown()

        assert len(manager.sessions) == 0


class TestProcessMessagesEdgeCases:
    """Test edge cases of process_messages()."""

    @pytest.fixture
    def manager(self, fresh_session_manager):
        return fresh_session_manager

    def test_session_mode_with_empty_messages(self, manager):
        """Session mode with empty messages list creates session but adds nothing."""
        result_msgs, sid = manager.process_messages([], session_id="empty-session")

        assert sid == "empty-session"
        assert result_msgs == []
        assert "empty-session" in manager.sessions

    def test_multiple_rounds_accumulate_correctly(self, manager):
        """Three or more rounds of messages accumulate correctly."""
        sid = "multi-round"

        # Round 1
        r1, _ = manager.process_messages([Message(role="user", content="Round 1")], session_id=sid)
        assert len(r1) == 1

        # Add assistant response
        manager.add_assistant_response(sid, Message(role="assistant", content="Reply 1"))

        # Round 2
        r2, _ = manager.process_messages([Message(role="user", content="Round 2")], session_id=sid)
        assert len(r2) == 3  # user1 + assistant1 + user2

        # Add assistant response
        manager.add_assistant_response(sid, Message(role="assistant", content="Reply 2"))

        # Round 3
        r3, _ = manager.process_messages([Message(role="user", content="Round 3")], session_id=sid)
        assert len(r3) == 5  # user1 + assistant1 + user2 + assistant2 + user3

        # Verify order
        assert r3[0].content == "Round 1"
        assert r3[1].content == "Reply 1"
        assert r3[2].content == "Round 2"
        assert r3[3].content == "Reply 2"
        assert r3[4].content == "Round 3"


class TestGetStatsAllExpired:
    """Test get_stats() when all sessions are expired."""

    def test_all_sessions_expired_returns_zero_active(self):
        """get_stats() returns active=0 when all sessions are expired."""
        manager = SessionManager(default_ttl_minutes=60)

        s1 = manager.get_or_create_session("expired-1")
        s2 = manager.get_or_create_session("expired-2")
        s1.expires_at = utc_now() - timedelta(hours=1)
        s2.expires_at = utc_now() - timedelta(hours=1)

        stats = manager.get_stats()

        assert stats["active_sessions"] == 0
        assert stats["expired_sessions"] == 2


class TestCleanupExpiredSessionsMixed:
    """Test cleanup_expired_sessions() with a mix of expired and active."""

    async def test_cleanup_removes_only_expired_keeps_active(self):
        """cleanup_expired_sessions() removes expired, keeps active sessions intact."""
        manager = SessionManager(default_ttl_minutes=60)

        # Create 3 active, 2 expired
        manager.get_or_create_session("active-1")
        active2 = manager.get_or_create_session("active-2")
        active2.add_messages([Message(role="user", content="Keep me")])
        manager.get_or_create_session("active-3")

        exp1 = manager.get_or_create_session("expired-1")
        exp1.expires_at = utc_now() - timedelta(hours=1)
        exp2 = manager.get_or_create_session("expired-2")
        exp2.expires_at = utc_now() - timedelta(minutes=5)

        assert len(manager.sessions) == 5

        removed = await manager.cleanup_expired_sessions()

        assert removed == 2
        assert len(manager.sessions) == 3
        assert "active-1" in manager.sessions
        assert "active-2" in manager.sessions
        assert "active-3" in manager.sessions
        assert "expired-1" not in manager.sessions
        assert "expired-2" not in manager.sessions

        # Verify active session data is preserved
        assert len(manager.sessions["active-2"].messages) == 1

    async def test_cleanup_returns_zero_when_nothing_expired(self):
        """cleanup_expired_sessions() returns 0 when no sessions are expired."""
        manager = SessionManager(default_ttl_minutes=60)
        manager.get_or_create_session("active-1")
        manager.get_or_create_session("active-2")

        removed = await manager.cleanup_expired_sessions()

        assert removed == 0
        assert len(manager.sessions) == 2


class TestPerSessionLock:
    """Test that per-session asyncio.Lock can be used for concurrent safety."""

    async def test_per_session_lock_serializes_access(self):
        """Concurrent coroutines using session.lock don't interleave."""
        session = Session(session_id="lock-test")
        order = []

        async def writer(label: str):
            async with session.lock:
                order.append(f"{label}-start")
                await asyncio.sleep(0.01)
                session.add_messages([Message(role="user", content=label)])
                order.append(f"{label}-end")

        await asyncio.gather(writer("A"), writer("B"))

        # One writer should fully complete before the other starts
        assert order[0].endswith("-start")
        assert order[1].endswith("-end")
        assert order[0][0] == order[1][0]  # Same label for start/end pair
        assert len(session.messages) == 2
