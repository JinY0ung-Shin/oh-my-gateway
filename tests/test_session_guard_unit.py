"""Unit tests for src/session_guard.py — shared session validation logic."""

import pytest
from fastapi import HTTPException

from src.backends.base import ResolvedModel
from src.models import Message
from src.session_manager import Session
from src.session_guard import (
    acquire_session_preflight,
    session_preflight_scope,
)


def _make_session(session_id: str = "test-sid", **overrides) -> Session:
    s = Session(session_id=session_id)
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _claude_resolved(model: str = "opus") -> ResolvedModel:
    return ResolvedModel(public_model=model, backend="claude", provider_model=model)


def _codex_resolved(model: str = "codex") -> ResolvedModel:
    return ResolvedModel(public_model=model, backend="codex", provider_model="o3")


# ---------------------------------------------------------------------------
# New session (first turn)
# ---------------------------------------------------------------------------


class TestNewSession:
    async def test_first_turn_tags_backend_and_sets_is_new(self):
        session = _make_session()
        pf = await acquire_session_preflight(session, _claude_resolved(), "sid-1")
        try:
            assert pf.is_new is True
            assert pf.resume_id is None
            assert session.backend == "claude"
            assert pf.lock_acquired is True
        finally:
            session.lock.release()

    async def test_first_turn_snapshots_base_system_prompt(self):
        session = _make_session()
        result = await acquire_session_preflight(session, _claude_resolved(), "sid-1")
        try:
            assert result.is_new is True
            # base_system_prompt is set (could be None for preset mode)
            assert hasattr(session, "base_system_prompt")
        finally:
            session.lock.release()

    async def test_first_turn_commits_messages(self):
        session = _make_session()
        msgs = [Message(role="user", content="hello")]
        result = await acquire_session_preflight(
            session, _claude_resolved(), "sid-1", messages=msgs
        )
        try:
            assert result.is_new is True
            assert len(session.messages) == 1
            assert session.messages[0].content == "hello"
        finally:
            session.lock.release()

    async def test_next_turn_is_1_for_new_session(self):
        session = _make_session()
        pf = await acquire_session_preflight(session, _claude_resolved(), "sid-1")
        try:
            assert pf.next_turn == 1
        finally:
            session.lock.release()


# ---------------------------------------------------------------------------
# Existing session (resume)
# ---------------------------------------------------------------------------


class TestExistingSession:
    async def test_resume_populates_resume_id(self):
        session = _make_session(backend="claude", provider_session_id="sdk-123")
        session.messages.append(Message(role="user", content="prev"))

        pf = await acquire_session_preflight(session, _claude_resolved(), "sid-1")
        try:
            assert pf.is_new is False
            assert pf.resume_id == "sdk-123"
        finally:
            session.lock.release()

    async def test_resume_falls_back_to_session_id(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))

        pf = await acquire_session_preflight(session, _claude_resolved(), "sid-1")
        try:
            assert pf.resume_id == "sid-1"
        finally:
            session.lock.release()

    async def test_is_new_override(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))

        pf = await acquire_session_preflight(
            session, _claude_resolved(), "sid-1", is_new=True
        )
        try:
            assert pf.is_new is True
        finally:
            session.lock.release()


# ---------------------------------------------------------------------------
# Backend mismatch guard
# ---------------------------------------------------------------------------


class TestBackendMismatch:
    async def test_raises_400_on_mismatch(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))

        with pytest.raises(HTTPException) as exc_info:
            await acquire_session_preflight(session, _codex_resolved(), "sid-1")

        assert exc_info.value.status_code == 400
        assert "Cannot mix backends" in exc_info.value.detail
        # Lock must be released
        assert not session.lock.locked()

    async def test_no_error_on_new_session(self):
        session = _make_session()
        pf = await acquire_session_preflight(session, _codex_resolved(), "sid-1")
        try:
            assert pf.is_new is True
            assert session.backend == "codex"
        finally:
            session.lock.release()


# ---------------------------------------------------------------------------
# Codex resume guard
# ---------------------------------------------------------------------------


class TestCodexResumeGuard:
    async def test_raises_409_without_thread_id(self):
        session = _make_session(backend="codex", provider_session_id=None)
        session.messages.append(Message(role="user", content="prev"))

        with pytest.raises(HTTPException) as exc_info:
            await acquire_session_preflight(session, _codex_resolved(), "sid-1")

        assert exc_info.value.status_code == 409
        assert "thread_id" in exc_info.value.detail
        assert not session.lock.locked()

    async def test_no_error_with_thread_id(self):
        session = _make_session(backend="codex", provider_session_id="thread-abc")
        session.messages.append(Message(role="user", content="prev"))

        pf = await acquire_session_preflight(session, _codex_resolved(), "sid-1")
        try:
            assert pf.resume_id == "thread-abc"
        finally:
            session.lock.release()


# ---------------------------------------------------------------------------
# Turn counter validation (responses flow)
# ---------------------------------------------------------------------------


class TestTurnValidation:
    async def test_stale_turn_raises_409(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))
        session.turn_counter = 5

        with pytest.raises(HTTPException) as exc_info:
            await acquire_session_preflight(
                session, _claude_resolved(), "sid-1", is_new=False, turn=3
            )

        assert exc_info.value.status_code == 409
        assert "Stale" in exc_info.value.detail
        assert not session.lock.locked()

    async def test_future_turn_raises_404(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))
        session.turn_counter = 2

        with pytest.raises(HTTPException) as exc_info:
            await acquire_session_preflight(
                session, _claude_resolved(), "sid-1", is_new=False, turn=5
            )

        assert exc_info.value.status_code == 404
        assert "future turn" in exc_info.value.detail
        assert not session.lock.locked()

    async def test_matching_turn_succeeds(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))
        session.turn_counter = 3

        pf = await acquire_session_preflight(
            session, _claude_resolved(), "sid-1", is_new=False, turn=3
        )
        try:
            assert pf.next_turn == 4
        finally:
            session.lock.release()


# ---------------------------------------------------------------------------
# session_preflight_scope context manager
# ---------------------------------------------------------------------------


class TestSessionPreflightScope:
    async def test_releases_lock_on_success(self):
        session = _make_session()
        async with session_preflight_scope(session, _claude_resolved(), "sid-1") as pf:
            assert pf.lock_acquired is True
            assert session.lock.locked()
        assert not session.lock.locked()

    async def test_releases_lock_on_exception(self):
        session = _make_session()
        with pytest.raises(RuntimeError):
            async with session_preflight_scope(
                session, _claude_resolved(), "sid-1"
            ):
                raise RuntimeError("boom")
        assert not session.lock.locked()

    async def test_validation_error_releases_lock(self):
        session = _make_session(backend="claude")
        session.messages.append(Message(role="user", content="prev"))

        with pytest.raises(HTTPException):
            async with session_preflight_scope(
                session, _codex_resolved(), "sid-1"
            ):
                pass  # should not reach here
        assert not session.lock.locked()
