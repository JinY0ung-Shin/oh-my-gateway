"""Verify /v1/sessions/stats surfaces rehydrate counters."""

from unittest.mock import AsyncMock, patch

import src.routes.sessions as sessions_module
from src.session_manager import session_manager as global_sm
from tests.test_main_api_unit import client_context


def test_stats_exposes_rehydrate_counters():
    """The /v1/sessions/stats GET response includes rehydrate_hits and rehydrate_misses."""
    global_sm._rehydrate_hits = 5
    global_sm._rehydrate_misses = 3

    with (
        client_context() as (client, _mock_cli),
        patch.object(sessions_module, "verify_api_key", new_callable=AsyncMock),
    ):
        resp = client.get("/v1/sessions/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["rehydrate_hits"] == 5
    assert body["rehydrate_misses"] == 3


def test_stats_rehydrate_counters_default_to_zero():
    """Freshly initialized SessionManager starts with zero rehydrate counters."""
    from src.session_manager import SessionManager

    sm = SessionManager()
    assert sm._rehydrate_hits == 0
    assert sm._rehydrate_misses == 0
    result = sm.stats()
    assert result["rehydrate_hits"] == 0
    assert result["rehydrate_misses"] == 0


def test_stats_method_reflects_incremented_counters():
    """stats() returns the current values of _rehydrate_hits and _rehydrate_misses."""
    from src.session_manager import SessionManager

    sm = SessionManager()
    sm._rehydrate_hits = 7
    sm._rehydrate_misses = 2
    result = sm.stats()
    assert result["rehydrate_hits"] == 7
    assert result["rehydrate_misses"] == 2
    assert "active_sessions" in result
