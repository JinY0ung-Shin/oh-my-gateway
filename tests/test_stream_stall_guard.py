"""Streaming-turn stall guard + active-turn pin-age safety valve.

Why these exist: a streaming turn had no wall-clock bound (non-streaming and
background turns are capped), and the SSE keepalive actively kept a wedged
turn's connection alive — while ``Session.is_expired`` exempted the pinned
session from TTL expiry and LRU eviction. One wedged CLI turn therefore
leaked a live ``claude`` worker plus one session slot and one turn slot until
the container was recreated. These tests pin the two guards that close that:
the in-stream stall detector and the pin-age override in ``is_expired``.
"""

import asyncio
import time

import pytest

from src import streaming_utils
from src.session_manager import Session
from src.streaming_utils import StreamStallError, _SSE_KEEPALIVE, _keepalive_wrapper


async def _silent_source():
    """A source that never yields — a wedged SDK turn."""
    while True:
        await asyncio.sleep(3600)
    yield  # pragma: no cover — makes this an async generator


async def _slow_source(n: int, gap: float):
    for i in range(n):
        await asyncio.sleep(gap)
        yield {"i": i}


class TestKeepaliveStallDetection:
    async def test_silent_source_raises_after_stall_budget(self):
        """Keepalive-only silence past the budget must raise, not keep going."""
        agen = _keepalive_wrapper(_silent_source(), 0.02, stall_after=0.08)
        got_keepalive = False
        with pytest.raises(StreamStallError):
            async for item in agen:
                assert item is _SSE_KEEPALIVE
                got_keepalive = True
        # The guard rides the keepalive timer, so at least one keepalive
        # precedes the raise — proving detection happens mid-silence.
        assert got_keepalive

    async def test_stall_disabled_by_default_keeps_yielding_keepalives(self):
        """stall_after=0 (the default) must preserve the old behavior."""
        agen = _keepalive_wrapper(_silent_source(), 0.01)
        seen = 0
        async for item in agen:
            assert item is _SSE_KEEPALIVE
            seen += 1
            if seen >= 5:
                break
        await agen.aclose()
        assert seen == 5

    async def test_real_items_reset_the_stall_clock(self):
        """A slow-but-alive source must never trip the guard."""
        # Each item arrives after 0.05s — under the 0.09s budget, but the
        # total run (5 * 0.05 = 0.25s) is far over it: only a reset-per-item
        # clock passes this.
        agen = _keepalive_wrapper(_slow_source(5, 0.05), 0.01, stall_after=0.09)
        items = [item async for item in agen if item is not _SSE_KEEPALIVE]
        assert items == [{"i": i} for i in range(5)]

    async def test_stream_response_chunks_fails_the_turn_on_stall(self, monkeypatch):
        """The stall surfaces as response.failed and success=False, so the
        route's teardown disconnects the client and reclaims the worker."""
        monkeypatch.setattr(streaming_utils, "SSE_KEEPALIVE_INTERVAL", 0.02)
        monkeypatch.setattr(streaming_utils, "STREAM_STALL_TIMEOUT_SECONDS", 0.08)

        stream_result = {}
        events = []
        async for sse in streaming_utils.stream_response_chunks(
            chunk_source=_silent_source(),
            model="claude-test",
            response_id="resp_stall_1",
            output_item_id="msg_stall_1",
            chunks_buffer=[],
            logger=streaming_utils.logger,
            stream_result=stream_result,
        ):
            events.append(sse)

        assert stream_result.get("success") is False
        failed = [e for e in events if "response.failed" in e]
        assert failed, f"no response.failed event in {events!r}"
        assert "stalled" in failed[-1].lower()


class TestActiveTurnPinAge:
    def test_fresh_active_turn_still_pins(self, monkeypatch):
        monkeypatch.setattr("src.constants.ACTIVE_TURN_MAX_AGE_SECONDS", 50)
        session = Session(session_id="pin-1", ttl_minutes=0)
        session.active_response_id = "resp_x"
        session.active_response_started_at = time.monotonic()
        assert session.is_expired() is False

    def test_zombie_active_turn_stops_pinning(self, monkeypatch):
        """No progress since begin, past the cap → reclaimed."""
        monkeypatch.setattr("src.constants.ACTIVE_TURN_MAX_AGE_SECONDS", 50)
        session = Session(session_id="pin-2", ttl_minutes=0)
        session.active_response_id = "resp_x"
        session.active_response_started_at = time.monotonic() - 100
        assert session.is_expired() is True

    def test_old_turn_with_recent_progress_keeps_pin(self, monkeypatch):
        """The valve measures NO-PROGRESS time, never absolute turn age.

        A background job deep into its 3600s budget, or an intentionally
        unbounded streaming turn, keeps producing chunks — each stamp resets
        the valve, so a turn far older than the cap must stay pinned while
        it is alive (review: absolute age would reclaim healthy work).
        """
        monkeypatch.setattr("src.constants.ACTIVE_TURN_MAX_AGE_SECONDS", 50)
        session = Session(session_id="pin-progress", ttl_minutes=0)
        session.active_response_id = "resp_x"
        session.active_response_started_at = time.monotonic() - 5000
        session.active_response_last_progress_at = time.monotonic() - 5
        assert session.is_expired() is False

    def test_progress_gone_silent_stops_pinning(self, monkeypatch):
        """Progress that stopped past the cap → reclaimed, however young the turn."""
        monkeypatch.setattr("src.constants.ACTIVE_TURN_MAX_AGE_SECONDS", 50)
        session = Session(session_id="pin-silent", ttl_minutes=0)
        session.active_response_id = "resp_x"
        session.active_response_started_at = time.monotonic() - 200
        session.active_response_last_progress_at = time.monotonic() - 100
        assert session.is_expired() is True

    def test_valve_disabled_preserves_unconditional_pin(self, monkeypatch):
        monkeypatch.setattr("src.constants.ACTIVE_TURN_MAX_AGE_SECONDS", 0)
        session = Session(session_id="pin-3", ttl_minutes=0)
        session.active_response_id = "resp_x"
        session.active_response_started_at = time.monotonic() - 10_000
        assert session.is_expired() is False

    def test_unstamped_active_turn_keeps_pinning(self, monkeypatch):
        """No timestamp (e.g. a turn begun before this field existed, or a
        rehydrated session) must fail safe: keep the pin, never reclaim."""
        monkeypatch.setattr("src.constants.ACTIVE_TURN_MAX_AGE_SECONDS", 50)
        session = Session(session_id="pin-4", ttl_minutes=0)
        session.active_response_id = "resp_x"
        session.active_response_started_at = None
        assert session.is_expired() is False

    def test_idle_session_expiry_unchanged(self):
        session = Session(session_id="pin-5", ttl_minutes=0)
        assert session.active_response_id is None
        assert session.is_expired() is True


class TestBeginFinishStamping:
    async def test_begin_stamps_and_finish_clears(self):
        from src.routes.responses import (
            _begin_active_response,
            _finish_active_response,
        )

        session = Session(session_id="stamp-1")
        await _begin_active_response(session, "resp_1", 1, object())
        assert isinstance(session.active_response_started_at, float)
        # Progress is seeded to the start stamp so a turn that wedges before
        # its first chunk is still measured from begin, not left None.
        assert (
            session.active_response_last_progress_at
            == session.active_response_started_at
        )

        await _finish_active_response(session, "resp_1", "completed")
        assert session.active_response_started_at is None
        assert session.active_response_last_progress_at is None
        assert session.active_response_id is None

    async def test_chunk_wrapper_stamps_progress(self):
        """Every real chunk through the shared turn-path wrapper is progress."""
        from types import SimpleNamespace

        from src.routes.responses import _capture_pending_tool_questions

        session = Session(session_id="stamp-2")
        session.active_response_id = "resp_1"
        resolved = SimpleNamespace(backend="claude")

        async def chunks():
            yield {"type": "stream_event", "event": {}}

        before = time.monotonic()
        out = [
            c
            async for c in _capture_pending_tool_questions(chunks(), resolved, session)
        ]
        assert len(out) == 1
        assert session.active_response_last_progress_at is not None
        assert session.active_response_last_progress_at >= before


class TestOldestActiveTurnSilenceGauge:
    def test_measures_silence_not_absolute_age(self):
        """A long-running turn with recent progress must read SMALL — absolute
        age would flag healthy unbounded turns and produce false alarms; only
        a long-silent turn may dominate the gauge."""
        from src.session_manager import SessionManager

        manager = SessionManager()
        assert manager.oldest_active_turn_silence() == 0.0

        # Very old turn, but progressing 5s ago — must not dominate.
        long_but_alive = Session(session_id="g-alive")
        long_but_alive.active_response_id = "r1"
        long_but_alive.active_response_started_at = time.monotonic() - 9000
        long_but_alive.active_response_last_progress_at = time.monotonic() - 5
        # Young turn that went silent 500s ago — this is the wedge signal.
        silent = Session(session_id="g-silent")
        silent.active_response_id = "r2"
        silent.active_response_started_at = time.monotonic() - 600
        silent.active_response_last_progress_at = time.monotonic() - 500
        # No progress stamp at all (legacy/rehydrated) — falls back to start.
        unstamped = Session(session_id="g-unstamped")
        unstamped.active_response_id = "r3"
        unstamped.active_response_started_at = time.monotonic() - 50
        idle = Session(session_id="g-idle")
        with manager.lock:
            manager.sessions["g-alive"] = long_but_alive
            manager.sessions["g-silent"] = silent
            manager.sessions["g-unstamped"] = unstamped
            manager.sessions["g-idle"] = idle

        silence = manager.oldest_active_turn_silence()
        assert 499 < silence < 510, "the silent turn must set the gauge"
