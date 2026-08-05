"""Admission control for concurrent agent runs.

Two independent limits are covered here: the in-flight turn cap enforced by
``ConcurrencyLimitMiddleware`` and the live-session cap enforced inside
``SessionManager``. The second is the one that actually bounds memory —
sessions pin a Claude CLI subprocess for their whole TTL — so its tests
assert that continuations are *never* refused.
"""

import asyncio
import contextlib
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

import src.main as main
from src.concurrency import (
    BYTES_PER_LIVE_SESSION,
    SessionLimitExceeded,
    TurnLimiter,
    check_session_limit_fits_memory,
    detect_memory_limit_bytes,
    peak_subprocess_count,
)
from src.concurrency_middleware import logger as concurrency_middleware_logger
from src.constants import DEFAULT_MODEL
from src.session_manager import SessionManager, _utcnow
from tests.test_main_api_unit import client_context


def _drain(limiter, timeout=5.0):
    """Block until every detached background run has released its slot."""
    deadline = time.monotonic() + timeout
    while limiter.in_flight and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _admin_key_present():
    """``client_context`` boots the real lifespan, which fails fast without an
    admin key. Set it here so this module runs standalone rather than relying
    on another test file leaking the env var."""
    with patch("src.admin_auth.ADMIN_API_KEY", "test-admin-key"):
        yield


@pytest.fixture(autouse=True)
def _reset_turn_limiter():
    from src import concurrency
    from src.concurrency_middleware import reset_rejection_log_throttle

    # The middleware is built once with the app, so its rejection-warning
    # throttle would otherwise carry across tests.
    reset_rejection_log_throttle()

    original_total = concurrency.turn_limiter.total
    original_per_user = concurrency.turn_limiter.per_user
    concurrency.turn_limiter.reset()
    yield
    concurrency.turn_limiter.total = original_total
    concurrency.turn_limiter.per_user = original_per_user
    concurrency.turn_limiter.reset()


class TestTurnLimiter:
    def test_grants_up_to_the_global_limit(self):
        limiter = TurnLimiter(total=2, per_user=0)
        first, _ = limiter.try_acquire("a")
        second, _ = limiter.try_acquire("b")
        third, reason = limiter.try_acquire("c")
        assert first is not None and second is not None
        assert third is None
        assert reason == "global"

    def test_release_frees_a_slot(self):
        limiter = TurnLimiter(total=1, per_user=0)
        slot, _ = limiter.try_acquire("a")
        assert limiter.try_acquire("b")[0] is None
        slot.release()
        assert limiter.try_acquire("b")[0] is not None

    def test_release_is_idempotent(self):
        """A double release must not inflate capacity beyond the limit."""
        limiter = TurnLimiter(total=1, per_user=0)
        slot, _ = limiter.try_acquire("a")
        slot.release()
        slot.release()
        assert limiter.in_flight == 0
        assert limiter.try_acquire("b")[0] is not None
        assert limiter.try_acquire("c")[0] is None

    def test_per_user_limit_is_independent_of_the_global_one(self):
        limiter = TurnLimiter(total=10, per_user=2)
        assert limiter.try_acquire("ada")[0] is not None
        assert limiter.try_acquire("ada")[0] is not None
        blocked, reason = limiter.try_acquire("ada")
        assert blocked is None
        assert reason == "per_user"
        # A different user still gets in — the global pool is not exhausted.
        assert limiter.try_acquire("grace")[0] is not None

    def test_unidentified_callers_are_not_per_user_capped(self):
        """All anonymous requests hash to one bucket, so enforcing per_user
        there would cap a whole endpoint at that number rather than share it.

        /v1/agents/messages forbids a ``user`` field outright, and chunked
        requests have no content-length to peek — both would silently collapse
        to ``per_user`` concurrent turns for every caller combined. They are
        bounded by the global limit instead.
        """
        limiter = TurnLimiter(total=10, per_user=1)
        slots = [limiter.try_acquire(None)[0] for _ in range(10)]
        assert all(s is not None for s in slots)
        blocked, reason = limiter.try_acquire(None)
        assert blocked is None
        assert reason == "global"

    def test_identified_callers_are_still_per_user_capped(self):
        limiter = TurnLimiter(total=10, per_user=1)
        assert limiter.try_acquire("ada")[0] is not None
        assert limiter.try_acquire("ada")[1] == "per_user"

    def test_zero_disables_a_limit(self):
        limiter = TurnLimiter(total=0, per_user=0)
        slots = [limiter.try_acquire("a")[0] for _ in range(50)]
        assert all(s is not None for s in slots)

    def test_user_keys_are_dropped_when_idle(self):
        """Otherwise the gateway accumulates one dict entry per user seen."""
        limiter = TurnLimiter(total=10, per_user=2)
        slot, _ = limiter.try_acquire("transient")
        slot.release()
        assert limiter.snapshot()["distinct_users"] == 0

    def test_accounting_holds_under_contention(self):
        """The limiter is a threading.Lock counter reached from an async app.

        Slots are held (not acquired-and-freed) so threads genuinely compete:
        the count must reach the cap, never exceed it, return to zero, and
        leave no per-user keys behind even with double releases in flight.
        """
        import threading

        limiter = TurnLimiter(total=8, per_user=0)
        peak = 0
        violations = []
        guard = threading.Lock()
        start = threading.Barrier(16)

        def worker(n):
            nonlocal peak
            start.wait()
            for _ in range(25):
                slot, _ = limiter.try_acquire(f"u{n % 5}")
                if slot is None:
                    continue
                with guard:
                    current = limiter.in_flight
                    peak = max(peak, current)
                    if current > 8:
                        violations.append(current)
                time.sleep(0.001)
                slot.release()
                slot.release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not violations, f"cap exceeded: {violations[:3]}"
        assert peak > 1, "threads never overlapped — test proved nothing"
        assert limiter.in_flight == 0
        assert limiter.snapshot()["distinct_users"] == 0

    def test_context_manager_releases(self):
        limiter = TurnLimiter(total=1, per_user=0)
        slot, _ = limiter.try_acquire("a")
        with slot:
            assert limiter.in_flight == 1
        assert limiter.in_flight == 0


class TestConcurrencyMiddleware:
    def test_rejects_with_503_and_retry_after_when_full(self):
        from src import concurrency

        with client_context() as (client, _mock_cli):
            concurrency.turn_limiter.total = 1
            # Occupy the only slot so the request cannot be admitted.
            concurrency.turn_limiter.try_acquire("someone-else")
            resp = client.post("/v1/responses", json={"model": DEFAULT_MODEL, "input": "hi"})

        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "30"
        body = resp.json()
        assert body["error"]["code"] == "concurrency_limit_exceeded"
        assert body["error"]["type"] == "server_overloaded"

    def test_slot_is_released_after_a_successful_turn(self):
        from src import concurrency

        concurrency.turn_limiter.reset()
        with client_context() as (client, _mock_cli):
            resp = client.post("/v1/responses", json={"model": DEFAULT_MODEL, "input": "hi"})
            assert resp.status_code == 200
        assert concurrency.turn_limiter.in_flight == 0

    def test_slot_is_released_after_a_streaming_turn(self):
        """The streaming path is why this middleware is pure ASGI: the slot
        must survive until the last body chunk, then be freed."""
        from src import concurrency

        concurrency.turn_limiter.reset()
        with client_context() as (client, _mock_cli):
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hi", "stream": True},
            )
            assert resp.status_code == 200
            assert "response.completed" in resp.text
        assert concurrency.turn_limiter.in_flight == 0

    def test_body_survives_the_user_peek(self):
        """The middleware consumes the request body to read ``user``; the
        route must still receive it intact."""
        from src import concurrency

        concurrency.turn_limiter.reset()
        with client_context() as (client, _mock_cli):
            resp = client.post(
                "/v1/responses",
                json={"model": DEFAULT_MODEL, "input": "hello there", "user": "ada"},
            )
        assert resp.status_code == 200
        assert concurrency.turn_limiter.in_flight == 0

    def test_background_turn_holds_its_slot_past_the_queued_reply(self):
        """``background: true`` answers immediately and keeps working in a
        detached task. Without a handoff the middleware would free the slot at
        the queued reply and background turns would escape the cap entirely.
        """
        import asyncio

        from src import concurrency

        async def slow(client, prompt, session):
            await asyncio.sleep(0.3)
            yield {"content": [{"type": "text", "text": "done"}]}
            yield {"subtype": "success", "result": "done"}

        with client_context() as (client, mock_cli):
            mock_cli.run_completion_with_client = slow
            resp = client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hi",
                    "background": True,
                    "store": True,
                },
            )
            assert resp.json()["status"] == "queued"
            assert concurrency.turn_limiter.in_flight == 1
            _drain(concurrency.turn_limiter)
            assert concurrency.turn_limiter.in_flight == 0

    def test_background_slot_is_released_when_the_backend_fails(self):
        """A leaked slot permanently shrinks capacity — worse than no cap."""
        import asyncio

        from src import concurrency

        async def boom(client, prompt, session):
            await asyncio.sleep(0.05)
            raise RuntimeError("backend exploded")
            yield  # pragma: no cover

        with client_context() as (client, mock_cli):
            mock_cli.run_completion_with_client = boom
            client.post(
                "/v1/responses",
                json={
                    "model": DEFAULT_MODEL,
                    "input": "hi",
                    "background": True,
                    "store": True,
                },
            )
            _drain(concurrency.turn_limiter)
            assert concurrency.turn_limiter.in_flight == 0

    def test_cancel_stays_reachable_when_the_pool_is_full(self):
        """Guarding by prefix would deadlock the gateway: background turns can
        pin every slot for BACKGROUND_RESPONSE_TIMEOUT_S, and cancel is the
        only request able to drain them. It must never need a slot itself.
        """
        from src.concurrency_middleware import _is_guarded

        assert _is_guarded({"type": "http", "method": "POST", "path": "/v1/responses"})
        assert not _is_guarded(
            {"type": "http", "method": "POST", "path": "/v1/responses/resp_abc_1/cancel"}
        )

    def test_agents_messages_is_not_capped_at_per_user(self):
        """AgentMessagesRequest forbids a ``user`` field, so every call is
        unidentified. Bucketing those together capped the whole endpoint at 3.
        """
        from src.agent_message_models import AgentMessagesRequest

        assert "user" not in AgentMessagesRequest.model_fields

        limiter = TurnLimiter(total=8, per_user=3)
        slots = [limiter.try_acquire(None)[0] for _ in range(8)]
        assert all(s is not None for s in slots), "endpoint collapsed below the global cap"

    def test_oversized_body_is_413_even_at_capacity(self):
        """Admission control must sit inside the size limit, or a saturated
        gateway masks every oversized request as a transient 503."""
        from src import concurrency
        from src.constants import MAX_REQUEST_SIZE

        concurrency.turn_limiter.total = 1
        with client_context() as (client, _mock_cli):
            concurrency.turn_limiter.try_acquire("hog")
            resp = client.post(
                "/v1/responses",
                content=b"x" * 16,
                headers={
                    "content-type": "application/json",
                    "content-length": str(MAX_REQUEST_SIZE + 1),
                },
            )
        assert resp.status_code == 413

    def test_rejection_logging_is_throttled(self):
        """Rejections short-circuit before the per-IP rate limiter, so without
        throttling a retrying client turns an overload into a log flood."""
        import io
        import logging

        from src import concurrency

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.WARNING)
        log = logging.getLogger("src.concurrency_middleware")
        log.addHandler(handler)

        concurrency.turn_limiter.total = 1
        try:
            with client_context() as (client, _mock_cli):
                concurrency.turn_limiter.try_acquire("hog")
                for _ in range(40):
                    client.post("/v1/responses", json={"model": DEFAULT_MODEL, "input": "x"})
        finally:
            log.removeHandler(handler)

        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected one throttled warning, got {len(lines)}"
        assert "global concurrency limit" in lines[0]
        # The first warning fires with nothing suppressed yet, so the count
        # only appears once a later rejection reopens the window.
        assert "suppressed" not in lines[0]

    def test_suppressed_rejections_are_counted_in_the_next_warning(self):
        import src.concurrency_middleware as cm

        mw = cm.ConcurrencyLimitMiddleware(app=None)
        mw._log_rejection("/v1/responses", "global")  # opens the window
        for _ in range(3):
            mw._log_rejection("/v1/responses", "global")  # suppressed

        with patch.object(cm, "_REJECTION_LOG_WINDOW_S", 0.0):
            with patch.object(concurrency_middleware_logger, "warning") as warn:
                mw._log_rejection("/v1/responses", "global")

        assert "3 more suppressed" in warn.call_args.args[4]

    async def test_client_disconnect_is_not_replayed_as_an_empty_body(self):
        """http.disconnect carries no body and no more_body. Treating it as a
        clean end-of-body handed the route an empty payload, turning a dropped
        request into a fabricated 422 in the logs and request metrics.
        """
        from src import concurrency
        from src.concurrency_middleware import ConcurrencyLimitMiddleware

        seen = []

        async def app(scope, receive, send):
            seen.append((await receive())["type"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        frames = iter(
            [
                {"type": "http.request", "body": b'{"mo', "more_body": True},
                {"type": "http.disconnect"},
            ]
        )

        async def receive():
            return next(frames)

        async def send(_message):
            pass

        await ConcurrencyLimitMiddleware(app)(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/responses",
                "headers": [(b"content-length", b"40")],
                "state": {},
            },
            receive,
            send,
        )

        assert seen == ["http.disconnect"]
        assert concurrency.turn_limiter.in_flight == 0

    def test_unguarded_paths_are_never_blocked(self):
        """A saturated gateway must stay diagnosable."""
        from src import concurrency

        concurrency.turn_limiter.total = 1
        concurrency.turn_limiter.try_acquire("hog")
        with client_context() as (client, _mock_cli):
            assert client.get("/health").status_code == 200
            assert client.get("/v1/models").status_code == 200


class TestLiveSessionLimit:
    def test_refuses_a_new_session_at_the_cap(self):
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 2):
            manager.get_or_create_session("s1")
            manager.get_or_create_session("s2")
            with pytest.raises(SessionLimitExceeded) as exc:
                manager.get_or_create_session("s3")
        assert exc.value.limit == 2

    def test_existing_sessions_are_always_reachable_at_the_cap(self):
        """An established conversation already owns its subprocess, so
        refusing it would free nothing and break the session mid-flight."""
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            created = manager.get_or_create_session("s1")
            again = manager.get_or_create_session("s1")
        assert again is created

    def test_zero_disables_the_cap(self):
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 0):
            for i in range(30):
                manager.get_or_create_session(f"s{i}")
        assert len(manager.sessions) == 30

    def test_expired_client_less_sessions_are_reclaimed_inline(self):
        """The sweep task runs every few minutes; without an inline reclaim
        the gateway would reject while holding dead sessions."""
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            stale = manager.get_or_create_session("stale")
            stale.expires_at = stale.created_at  # force expiry, no live client
            fresh = manager.get_or_create_session("fresh")
        assert fresh.session_id == "fresh"
        assert "stale" not in manager.sessions

    def test_expired_sessions_holding_a_client_still_refuse(self):
        """The inline sweep cannot drop a session with a live SDK client —
        disconnecting one has to be awaited — and *every* session that has
        served a turn holds one. The earlier version of this test used a
        client-less session and so never exercised the real case.
        """
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            stale = manager.get_or_create_session("stale")
            stale.client = object()  # as if it had served a turn
            stale.expires_at = stale.created_at
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("fresh")
        assert "stale" in manager.sessions

    async def test_capacity_pressure_kicks_an_async_sweep(self):
        """Rather than wait up to SESSION_CLEANUP_INTERVAL_MINUTES, hitting the
        cap on expired-but-client-holding sessions schedules the async cleanup
        so the *next* caller succeeds in about a second."""
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            stale = manager.get_or_create_session("stale")
            stale.client = object()
            stale.expires_at = stale.created_at
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("fresh")

            sweep = manager._sweep_task
            assert sweep is not None
            await sweep

            # The awaited sweep disconnected and dropped it, so a retry fits.
            assert manager.get_or_create_session("fresh").session_id == "fresh"

    def test_rehydrate_respects_the_cap(self):
        """Rehydration materializes a session that pins its own subprocess, so
        replaying stored previous_response_ids must not walk past the cap.
        """
        from src.session_manager import Session

        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 2):
            with patch(
                "src.session_manager._try_rehydrate_from_jsonl",
                lambda sid, *, user, cwd: Session(session_id=sid, user=user),
            ):
                assert manager.get_session("s1", user="u", cwd="/w") is not None
                assert manager.get_session("s2", user="u", cwd="/w") is not None
                with pytest.raises(SessionLimitExceeded):
                    manager.get_session("s3", user="u", cwd="/w")

        assert len(manager.sessions) == 2

    def test_maps_to_503_with_retry_after(self):
        with client_context() as (client, _mock_cli):
            with patch("src.constants.MAX_LIVE_SESSIONS", 1):
                main.session_manager.get_or_create_session("occupied")
                try:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": DEFAULT_MODEL, "input": "hi"},
                    )
                finally:
                    main.session_manager.sessions.pop("occupied", None)
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "30"
        assert resp.json()["error"]["code"] == "session_limit_exceeded"

    def test_error_body_does_not_leak_the_configured_capacity(self):
        """Capacity is operator information, not caller information."""
        with client_context() as (client, _mock_cli):
            with patch("src.constants.MAX_LIVE_SESSIONS", 7):
                main.session_manager.sessions.clear()
                for i in range(7):
                    main.session_manager.get_or_create_session(f"occupied-{i}")
                try:
                    resp = client.post(
                        "/v1/responses",
                        json={"model": DEFAULT_MODEL, "input": "hi"},
                    )
                finally:
                    main.session_manager.sessions.clear()
        assert resp.status_code == 503
        assert "7" not in resp.json()["error"]["message"]


@contextlib.contextmanager
def _lru_policy(cap, min_idle=60):
    """MAX_LIVE_SESSIONS at *cap* with SESSION_EVICTION_POLICY=lru opted in."""
    with patch("src.constants.MAX_LIVE_SESSIONS", cap):
        with patch("src.constants.SESSION_EVICTION_POLICY", "lru"):
            with patch("src.constants.SESSION_EVICTION_MIN_IDLE_SECONDS", min_idle):
                yield


def _age(session, seconds):
    """Make *session* look idle for *seconds* without expiring it."""
    session.last_accessed = _utcnow() - timedelta(seconds=seconds)


class TestLruEviction:
    """Opt-in ``SESSION_EVICTION_POLICY=lru`` at the MAX_LIVE_SESSIONS cap.

    The default policy stays ``reject``: every test in TestLiveSessionLimit
    above runs unmodified as the regression guard for that.
    """

    def test_default_policy_still_rejects(self):
        """Without the opt-in, a cap full of unexpired sessions refuses even
        when a perfectly evictable idle session is available."""
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            victim = manager.get_or_create_session("idle")
            _age(victim, seconds=600)
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("fresh")
        assert "idle" in manager.sessions

    def test_lru_evicts_the_least_recently_accessed_session(self):
        manager = SessionManager()
        with _lru_policy(cap=2):
            older = manager.get_or_create_session("older")
            newer = manager.get_or_create_session("newer")
            _age(older, seconds=300)
            _age(newer, seconds=120)
            admitted = manager.get_or_create_session("fresh")
        assert admitted.session_id == "fresh"
        assert "older" not in manager.sessions
        assert "newer" in manager.sessions

    def test_never_evicts_a_session_with_an_active_response(self):
        """An in-flight turn pins the session; when it is the only candidate
        the gateway must 503 rather than orphan the running client."""
        manager = SessionManager()
        with _lru_policy(cap=1):
            busy = manager.get_or_create_session("busy")
            _age(busy, seconds=600)
            busy.active_response_id = "resp_1"
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("fresh")
        assert "busy" in manager.sessions

    def test_recently_touched_sessions_are_never_evicted(self):
        """The min-idle floor prevents cap+1 newcomers from cascade-evicting
        each other: a cap full of just-created sessions still refuses."""
        manager = SessionManager()
        with _lru_policy(cap=2, min_idle=60):
            manager.get_or_create_session("s1")
            manager.get_or_create_session("s2")
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("s3")
        assert set(manager.sessions) == {"s1", "s2"}

    async def test_evicting_a_client_holding_session_disconnects_it(self):
        """The slot frees synchronously; the disconnect — and with it the
        actual memory return — happens in the scheduled teardown task."""
        manager = SessionManager()
        client = AsyncMock()
        with _lru_policy(cap=1):
            victim = manager.get_or_create_session("victim")
            victim.client = client
            _age(victim, seconds=600)

            admitted = manager.get_or_create_session("fresh")
            assert admitted.session_id == "fresh"
            assert "victim" not in manager.sessions

            await asyncio.gather(*list(manager._evict_tasks))
        client.disconnect.assert_awaited_once()

    def test_evicted_session_workspace_is_cleaned(self, tmp_path):
        manager = SessionManager()
        workspace = tmp_path / "_tmp_victim"
        workspace.mkdir()
        with _lru_policy(cap=1):
            victim = manager.get_or_create_session("victim")
            victim.workspace = str(workspace)
            _age(victim, seconds=600)
            manager.get_or_create_session("fresh")
        assert not workspace.exists()

    def test_unlimited_cap_never_evicts(self):
        manager = SessionManager()
        with _lru_policy(cap=0):
            for i in range(30):
                _age(manager.get_or_create_session(f"s{i}"), seconds=600)
        assert len(manager.sessions) == 30

    def test_eviction_increments_the_metric(self):
        from prometheus_client import REGISTRY

        def evicted_total():
            return REGISTRY.get_sample_value("gateway_sessions_evicted_total") or 0

        manager = SessionManager()
        before = evicted_total()
        with _lru_policy(cap=1):
            _age(manager.get_or_create_session("victim"), seconds=600)
            manager.get_or_create_session("fresh")
        assert evicted_total() == before + 1

    def test_touching_a_session_moves_it_off_the_eviction_frontline(self):
        manager = SessionManager()
        with _lru_policy(cap=2):
            frontline = manager.get_or_create_session("frontline")
            second = manager.get_or_create_session("second")
            _age(frontline, seconds=300)
            _age(second, seconds=120)
            # Reaching an existing session touches it: no longer the LRU.
            manager.get_or_create_session("frontline")
            manager.get_or_create_session("fresh")
        assert "frontline" in manager.sessions
        assert "second" not in manager.sessions

    def test_without_a_loop_client_holding_candidates_are_not_orphaned(self):
        """Sync context (no running loop): the disconnect cannot be scheduled,
        so a client-holding candidate is skipped — rejecting beats orphaning
        a live subprocess."""
        manager = SessionManager()
        with _lru_policy(cap=1):
            victim = manager.get_or_create_session("victim")
            victim.client = object()
            _age(victim, seconds=600)
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("fresh")
        assert "victim" in manager.sessions

    def test_without_a_loop_falls_past_client_holders_to_a_clientless_one(self):
        """A skipped client-holder does not end the scan: a younger
        client-less session behind it is still evicted inline."""
        manager = SessionManager()
        with _lru_policy(cap=2):
            holder = manager.get_or_create_session("holder")
            holder.client = object()
            _age(holder, seconds=600)  # oldest: visited first, then skipped
            clientless = manager.get_or_create_session("clientless")
            _age(clientless, seconds=300)
            admitted = manager.get_or_create_session("fresh")
        assert admitted.session_id == "fresh"
        assert "clientless" not in manager.sessions
        assert "holder" in manager.sessions

    def test_admin_runtime_toggle_enables_eviction_without_restart(self):
        """The env default stays reject; flipping the runtime key opts in."""
        from src.runtime_config import runtime_config

        manager = SessionManager()
        try:
            with patch("src.constants.MAX_LIVE_SESSIONS", 1):
                _age(manager.get_or_create_session("victim"), seconds=600)
                with pytest.raises(SessionLimitExceeded):
                    manager.get_or_create_session("fresh")

                runtime_config.set("session_eviction_policy", "lru")
                admitted = manager.get_or_create_session("fresh")
                assert admitted.session_id == "fresh"
                assert "victim" not in manager.sessions
        finally:
            runtime_config.reset("session_eviction_policy")

    def test_never_evicts_a_session_paused_on_ask_user_question(self):
        """A paused AskUserQuestion turn clears active_response_id but leaves
        pending_tool_call / input_event set — its continuation is coming back
        for this exact session, so it is pinned like an in-flight turn."""
        manager = SessionManager()
        with _lru_policy(cap=2):
            paused = manager.get_or_create_session("paused")
            _age(paused, seconds=600)
            paused.pending_tool_call = {"call_id": "call_1"}
            waiting = manager.get_or_create_session("waiting")
            _age(waiting, seconds=600)
            waiting.input_event = asyncio.Event()
            with pytest.raises(SessionLimitExceeded):
                manager.get_or_create_session("fresh")
        assert set(manager.sessions) == {"paused", "waiting"}

    async def test_prefers_an_expired_client_holder_over_a_live_session(self):
        """Per-session TTLs can diverge (admin edits), making an expired
        session the *younger*-accessed one; it must still be chosen over
        someone's live conversation."""
        manager = SessionManager()
        dead_client = AsyncMock()
        with _lru_policy(cap=2):
            live = manager.get_or_create_session("live")
            _age(live, seconds=300)
            dead = manager.get_or_create_session("dead")
            dead.client = dead_client
            _age(dead, seconds=120)  # younger access than "live"...
            dead.expires_at = _utcnow() - timedelta(seconds=1)  # ...but expired

            admitted = manager.get_or_create_session("fresh")
            assert admitted.session_id == "fresh"
            assert "dead" not in manager.sessions
            assert "live" in manager.sessions
            await asyncio.gather(*list(manager._evict_tasks))
        dead_client.disconnect.assert_awaited_once()

    async def test_eviction_still_schedules_the_sweep_for_leftover_expired(self):
        """Admission via eviction must not starve the out-of-band sweep that
        reclaims expired-but-client-holding sessions."""
        manager = SessionManager()
        with _lru_policy(cap=2):
            first = manager.get_or_create_session("expired-evicted")
            first.client = AsyncMock()
            _age(first, seconds=600)
            first.expires_at = _utcnow() - timedelta(seconds=1)
            leftover = manager.get_or_create_session("expired-leftover")
            leftover.client = AsyncMock()
            _age(leftover, seconds=300)
            leftover.expires_at = _utcnow() - timedelta(seconds=1)

            admitted = manager.get_or_create_session("fresh")
            assert admitted.session_id == "fresh"

            sweep = manager._sweep_task
            assert sweep is not None
            await asyncio.gather(*list(manager._evict_tasks))
            await sweep
        assert "expired-leftover" not in manager.sessions

    async def test_shutdown_settles_inflight_eviction_teardowns(self):
        """async_shutdown snapshots self.sessions, which no longer contains
        an evicted session — its teardown must be awaited explicitly or the
        subprocess outlives the gateway. The victim's disconnect genuinely
        suspends: an AsyncMock would resolve incidentally at shutdown's next
        await point and pass even without the explicit settle."""

        class SlowClient:
            def __init__(self):
                self.disconnected = False

            async def disconnect(self):
                await asyncio.sleep(0.05)
                self.disconnected = True

        manager = SessionManager()
        client = SlowClient()
        with _lru_policy(cap=1):
            victim = manager.get_or_create_session("victim")
            victim.client = client
            _age(victim, seconds=600)
            manager.get_or_create_session("fresh")
            await manager.async_shutdown()
        assert client.disconnected is True

    async def test_expired_candidates_ignore_the_min_idle_floor(self):
        """The floor protects fresh *live* newcomers from cascade eviction; a
        dead-but-client-holding session must not shield itself with it while
        someone's live conversation gets destroyed instead."""
        manager = SessionManager()
        dead_client = AsyncMock()
        with _lru_policy(cap=2, min_idle=200):
            dead = manager.get_or_create_session("dead")
            dead.client = dead_client
            _age(dead, seconds=90)  # under the 200s floor...
            dead.expires_at = _utcnow() - timedelta(seconds=1)  # ...but expired
            live = manager.get_or_create_session("live")
            _age(live, seconds=400)

            admitted = manager.get_or_create_session("fresh")
            assert admitted.session_id == "fresh"
            assert "dead" not in manager.sessions
            assert "live" in manager.sessions
            await asyncio.gather(*list(manager._evict_tasks))
        dead_client.disconnect.assert_awaited_once()

    async def test_expiry_sweep_survives_a_hung_disconnect(self):
        """A dead anyio channel must not wedge the sweep: a wedged sweep
        blocks every future out-of-band sweep and the periodic cleanup loop
        awaits the same coroutine, so session expiry would stop for the
        process lifetime."""

        class WedgedClient:
            async def disconnect(self):
                await asyncio.Event().wait()  # never resolves

        manager = SessionManager()
        stale = manager.get_or_create_session("stale")
        stale.client = WedgedClient()
        stale.expires_at = stale.created_at
        removed = await asyncio.wait_for(
            manager.cleanup_expired_sessions(), timeout=5.0
        )
        assert removed == 1
        assert "stale" not in manager.sessions

    async def test_shutdown_cancels_an_inflight_sweep(self):
        """async_shutdown must not return with a live sweep task parked in a
        hung disconnect — the loop then closes over a pending task and the
        session it was tearing down never finishes."""

        class WedgedClient:
            async def disconnect(self):
                await asyncio.Event().wait()

        manager = SessionManager()
        stale = manager.get_or_create_session("stale")
        stale.client = WedgedClient()
        stale.expires_at = stale.created_at
        manager._schedule_expired_sweep()
        await asyncio.sleep(0)  # let the sweep reach the hung disconnect
        await manager.async_shutdown()
        assert manager._sweep_task is not None
        assert manager._sweep_task.done()

    async def test_finishing_a_turn_touches_the_session_whatever_the_outcome(self):
        """Only the success paths touch via add_messages; the failure path
        must refresh last_accessed too, or a long failed turn reads as idle
        for its whole duration and is instantly evictable under a queued
        follow-up."""
        from src.routes.responses import _finish_active_response

        manager = SessionManager()
        session = manager.get_or_create_session("s")
        session.active_response_id = "resp_1"
        _age(session, seconds=600)

        await _finish_active_response(session, "resp_1", "failed")
        assert (_utcnow() - session.last_accessed).total_seconds() < 5

    def test_rehydration_for_a_future_turn_does_not_evict(self):
        """A previous_response_id pointing past the transcript's turn count
        would be 404ed by the route right after admission — it must be
        discarded before it can evict anyone (or burn a slot)."""
        from src.session_manager import Session

        manager = SessionManager()
        with _lru_policy(cap=1):
            with patch(
                "src.session_manager._try_rehydrate_from_jsonl",
                lambda sid, *, user, cwd: Session(
                    session_id=sid, user=user, turn_counter=3
                ),
            ):
                victim = manager.get_or_create_session("victim")
                _age(victim, seconds=600)

                miss = manager.get_session("other", user="u", cwd="/w", max_turn=9)
                assert miss is None
                assert "victim" in manager.sessions

                # A servable turn still admits (and may evict) as before.
                hit = manager.get_session("other", user="u", cwd="/w", max_turn=3)
                assert hit is not None
                assert "victim" not in manager.sessions


class TestMemorySizingCheck:
    def test_detects_a_positive_memory_ceiling(self):
        assert (detect_memory_limit_bytes() or 0) > 0

    def test_prefers_the_cgroup_limit_over_host_memory(self):
        from unittest.mock import mock_open

        with patch("builtins.open", mock_open(read_data="2147483648")):
            assert detect_memory_limit_bytes() == 2147483648

    def test_treats_cgroup_max_as_unlimited_and_falls_through(self):
        """``memory.max`` reads ``max`` when the container is uncapped."""
        real_open = open

        def fake_open(path, *a, **kw):
            if "cgroup" in str(path):
                from io import StringIO

                return StringIO("max")
            return real_open(path, *a, **kw)

        with patch("builtins.open", fake_open):
            # Falls through to /proc/meminfo, which is a real host value.
            assert (detect_memory_limit_bytes() or 0) > 0

    def test_peak_counts_the_stateless_path_too(self):
        """/v1/agents/messages builds a fresh SDK client per call and never
        enters the session manager, so it is invisible to MAX_LIVE_SESSIONS.
        Budgeting sessions alone understated the ceiling."""
        with patch("src.concurrency.MAX_LIVE_SESSIONS", 12):
            with patch("src.concurrency.MAX_CONCURRENT_TURNS", 8):
                assert peak_subprocess_count() == 20

    def test_warns_when_the_stateless_path_pushes_past_memory(self):
        """8 GB fits ~9 subprocesses; the defaults can reach 20."""
        with patch("src.concurrency.MAX_LIVE_SESSIONS", 12):
            with patch("src.concurrency.MAX_CONCURRENT_TURNS", 8):
                with patch(
                    "src.concurrency.detect_memory_limit_bytes", return_value=8 * 1024**3
                ):
                    message = check_session_limit_fits_memory()
        assert message is not None
        assert "agents/messages" in message

    def test_warns_when_the_cap_cannot_fit_in_memory(self):
        with patch("src.concurrency.MAX_LIVE_SESSIONS", 10_000):
            with patch("src.concurrency.detect_memory_limit_bytes", return_value=2 * 1024**3):
                message = check_session_limit_fits_memory()
        assert message is not None
        assert "MAX_LIVE_SESSIONS" in message

    def test_silent_when_the_cap_fits(self):
        roomy = BYTES_PER_LIVE_SESSION * 100
        with patch("src.concurrency.MAX_LIVE_SESSIONS", 2):
            with patch("src.concurrency.detect_memory_limit_bytes", return_value=roomy):
                assert check_session_limit_fits_memory() is None

    def test_silent_when_memory_cannot_be_detected(self):
        with patch("src.concurrency.MAX_LIVE_SESSIONS", 10_000):
            with patch("src.concurrency.detect_memory_limit_bytes", return_value=None):
                assert check_session_limit_fits_memory() is None


class TestCapacityMetrics:
    def test_rejection_and_in_flight_are_published(self):
        from src import concurrency
        from src.metrics import render_latest

        concurrency.turn_limiter.total = 1
        with client_context() as (client, _mock_cli):
            concurrency.turn_limiter.try_acquire("hog")
            assert (
                client.post(
                    "/v1/responses", json={"model": DEFAULT_MODEL, "input": "hi"}
                ).status_code
                == 503
            )

        exposition = render_latest()[0].decode()
        assert 'gateway_turns_rejected_total{scope="global"}' in exposition
        assert "gateway_turns_in_flight" in exposition
        assert "gateway_live_sessions" in exposition

    def test_rejection_is_still_counted_as_a_request(self):
        """The middleware must sit *inside* the logging/metrics layers, or a
        saturated gateway would look idle in its own dashboards."""
        from src import concurrency
        from src.metrics import render_latest

        concurrency.turn_limiter.total = 1
        with client_context() as (client, _mock_cli):
            concurrency.turn_limiter.try_acquire("hog")
            client.post("/v1/responses", json={"model": DEFAULT_MODEL, "input": "hi"})

        exposition = render_latest()[0].decode()
        assert any(
            line.startswith("gateway_requests_total{")
            and 'path_group="responses"' in line
            and 'status="503"' in line
            for line in exposition.splitlines()
        )
