"""HTTP coverage for the admin usage analytics endpoints."""

from fastapi.testclient import TestClient

from src import usage_queries
from src.admin_auth import require_admin
from src.main import app


def _bypass_admin_auth():
    """Override the admin auth dependency for the duration of a TestClient."""
    return None


def _client():
    app.dependency_overrides[require_admin] = _bypass_admin_auth
    return TestClient(app)


def _restore():
    app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# /admin/api/usage/summary
# ---------------------------------------------------------------------------


def test_usage_summary_returns_disabled_when_query_returns_none(monkeypatch):
    async def fake_get_summary(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_summary", fake_get_summary)
    try:
        resp = _client().get("/admin/api/usage/summary")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_usage_summary_returns_payload_when_query_succeeds(monkeypatch):
    captured = {}

    async def fake_get_summary(**kwargs):
        captured.update(kwargs)
        return {"turns_window": 5}

    monkeypatch.setattr(usage_queries, "get_summary", fake_get_summary)
    try:
        resp = _client().get(
            "/admin/api/usage/summary",
            params={"window_days": 14, "start_date": "2026-04-01", "end_date": "2026-04-30"},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "window_days": 14,
        "summary": {"turns_window": 5},
    }
    assert captured["window_days"] == 14
    assert captured["start_date"] == "2026-04-01"
    assert captured["end_date"] == "2026-04-30"


def test_usage_summary_clamps_window_days(monkeypatch):
    captured = {}

    async def fake_get_summary(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(usage_queries, "get_summary", fake_get_summary)
    try:
        resp = _client().get("/admin/api/usage/summary", params={"window_days": 9999})
        # Query received the clamped value (max 365)...
        assert captured["window_days"] == 365
        # ...but the response body echoes the raw query param.
        assert resp.json()["window_days"] == 9999

        resp = _client().get("/admin/api/usage/summary", params={"window_days": 0})
        assert captured["window_days"] == 1
        assert resp.json()["window_days"] == 0
    finally:
        _restore()


# ---------------------------------------------------------------------------
# /admin/api/usage/users
# ---------------------------------------------------------------------------


def test_usage_users_returns_empty_items_when_disabled(monkeypatch):
    async def fake_get_top_users(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_top_users", fake_get_top_users)
    try:
        resp = _client().get("/admin/api/usage/users")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "items": []}


def test_usage_users_returns_rows_with_clamped_limit(monkeypatch):
    captured = {}

    async def fake_get_top_users(**kwargs):
        captured.update(kwargs)
        return [{"user": "alice", "tokens": 100}]

    monkeypatch.setattr(usage_queries, "get_top_users", fake_get_top_users)
    try:
        resp = _client().get("/admin/api/usage/users", params={"limit": 9999})
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "items": [{"user": "alice", "tokens": 100}]}
    assert captured["limit"] == 500


# ---------------------------------------------------------------------------
# /admin/api/usage/tools
# ---------------------------------------------------------------------------


def test_usage_tools_returns_empty_items_when_disabled(monkeypatch):
    async def fake_get_top_tools(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_top_tools", fake_get_top_tools)
    try:
        resp = _client().get("/admin/api/usage/tools")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "items": []}


def test_usage_tools_returns_rows(monkeypatch):
    async def fake_get_top_tools(**kwargs):
        return [{"tool_name": "Read", "calls": 3}]

    monkeypatch.setattr(usage_queries, "get_top_tools", fake_get_top_tools)
    try:
        resp = _client().get("/admin/api/usage/tools", params={"window_days": 3, "limit": 7})
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "items": [{"tool_name": "Read", "calls": 3}],
    }


# ---------------------------------------------------------------------------
# /admin/api/usage/series
# ---------------------------------------------------------------------------


def test_usage_series_returns_disabled_payload_when_logger_off(monkeypatch):
    async def fake_get_time_series(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_time_series", fake_get_time_series)
    try:
        resp = _client().get("/admin/api/usage/series", params={"granularity": "week"})
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "granularity": "week", "buckets": []}


def test_usage_series_falls_back_to_day_for_unknown_granularity(monkeypatch):
    captured = {}

    async def fake_get_time_series(**kwargs):
        captured.update(kwargs)
        return [{"bucket": "2026-04-30", "turns": 1}]

    monkeypatch.setattr(usage_queries, "get_time_series", fake_get_time_series)
    try:
        resp = _client().get(
            "/admin/api/usage/series",
            params={"granularity": "minute", "buckets": 200},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["granularity"] == "day"
    assert body["buckets"] == [{"bucket": "2026-04-30", "turns": 1}]
    assert captured["granularity"] == "day"
    assert captured["buckets"] == 60


# ---------------------------------------------------------------------------
# /admin/api/usage/tools-series
# ---------------------------------------------------------------------------


def test_usage_tools_series_returns_disabled_payload(monkeypatch):
    async def fake_get_tool_breakdown_series(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_tool_breakdown_series", fake_get_tool_breakdown_series)
    try:
        resp = _client().get("/admin/api/usage/tools-series")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "granularity": "day", "tools": [], "buckets": []}


def test_usage_tools_series_returns_payload_with_top_clamp(monkeypatch):
    captured = {}

    async def fake_get_tool_breakdown_series(**kwargs):
        captured.update(kwargs)
        return {"tools": ["Read"], "buckets": [{"bucket": "2026-04-30", "values": {"Read": 3}}]}

    monkeypatch.setattr(usage_queries, "get_tool_breakdown_series", fake_get_tool_breakdown_series)
    try:
        resp = _client().get(
            "/admin/api/usage/tools-series",
            params={"granularity": "month", "top": 100},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["granularity"] == "month"
    assert body["tools"] == ["Read"]
    assert captured["top_n"] == 20


# ---------------------------------------------------------------------------
# /admin/api/usage/turns
# ---------------------------------------------------------------------------


def test_usage_turns_returns_disabled_payload(monkeypatch):
    async def fake_get_recent_turns(**kwargs):
        return None

    monkeypatch.setattr(usage_queries, "get_recent_turns", fake_get_recent_turns)
    try:
        resp = _client().get("/admin/api/usage/turns")
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "items": []}


def test_usage_turns_returns_rows_with_user_filter(monkeypatch):
    captured = {}

    async def fake_get_recent_turns(**kwargs):
        captured.update(kwargs)
        return [{"id": 7, "user": "alice"}]

    monkeypatch.setattr(usage_queries, "get_recent_turns", fake_get_recent_turns)
    try:
        resp = _client().get(
            "/admin/api/usage/turns",
            params={"user": "alice", "limit": 9999, "offset": -1},
        )
    finally:
        _restore()

    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "items": [{"id": 7, "user": "alice"}]}
    assert captured["user"] == "alice"
    assert captured["limit"] == 500
    assert captured["offset"] == 0
