"""Admission control for concurrent agent runs.

Two independent limits are covered here: the in-flight turn cap enforced by
``ConcurrencyLimitMiddleware`` and the live-session cap enforced inside
``SessionManager``. The second is the one that actually bounds memory —
sessions pin a Claude CLI subprocess for their whole TTL — so its tests
assert that continuations are *never* refused.
"""

from unittest.mock import patch

import pytest

import src.main as main
from src.concurrency import (
    BYTES_PER_LIVE_SESSION,
    SessionLimitExceeded,
    TurnLimiter,
    check_session_limit_fits_memory,
    detect_memory_limit_bytes,
)
from src.constants import DEFAULT_MODEL
from src.session_manager import SessionManager
from tests.test_main_api_unit import client_context


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

    def test_anonymous_callers_share_one_bucket(self):
        limiter = TurnLimiter(total=10, per_user=1)
        assert limiter.try_acquire(None)[0] is not None
        assert limiter.try_acquire(None)[0] is None

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

    def test_expired_sessions_are_reclaimed_before_refusing(self):
        """The sweep task runs every few minutes; without an inline reclaim
        the gateway would reject while holding dead sessions."""
        manager = SessionManager()
        with patch("src.constants.MAX_LIVE_SESSIONS", 1):
            stale = manager.get_or_create_session("stale")
            stale.expires_at = stale.created_at  # force expiry, no live client
            fresh = manager.get_or_create_session("fresh")
        assert fresh.session_id == "fresh"
        assert "stale" not in manager.sessions

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
