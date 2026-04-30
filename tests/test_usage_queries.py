"""Tests for admin usage analytics query construction."""

import pytest

from src import usage_queries


class _FakeUsageLogger:
    def __init__(self, *, enabled=True, responses=None):
        self.enabled = enabled
        self.responses = list(responses or [])
        self.calls = []

    async def fetch_rows(self, sql, params=()):
        self.calls.append((sql, params))
        if self.responses:
            return self.responses.pop(0)
        return []


def test_granularity_sql_uses_sql_percent_literals():
    assert usage_queries._GRANULARITY_SQL["week"] == "DATE_FORMAT(ts, '%x-W%v')"
    assert usage_queries._GRANULARITY_SQL["month"] == "DATE_FORMAT(ts, '%Y-%m')"


def test_parse_date_accepts_empty_invalid_and_valid_values():
    assert usage_queries._parse_date(None) is None
    assert usage_queries._parse_date("") is None
    assert usage_queries._parse_date("bad-date") is None
    assert usage_queries._parse_date("2026-04-30").isoformat() == "2026-04-30"


def test_window_clause_uses_inclusive_date_range_and_swaps_reversed_dates():
    where, params = usage_queries._window_clause("2026-04-30", "2026-04-01", 7, column="u.ts")

    assert where == "u.ts >= %s AND u.ts < DATE_ADD(%s, INTERVAL 1 DAY)"
    assert params == ("2026-04-01", "2026-04-30")


def test_window_clause_falls_back_to_minimum_one_day_window():
    where, params = usage_queries._window_clause("bad", "2026-04-01", 0)

    assert where == "ts >= NOW() - INTERVAL %s DAY"
    assert params == (1,)


@pytest.mark.parametrize(
    ("call", "args"),
    [
        (usage_queries.get_summary, ()),
        (usage_queries.get_top_users, ()),
        (usage_queries.get_top_tools, ()),
        (usage_queries.get_time_series, ()),
        (usage_queries.get_tool_breakdown_series, ()),
        (usage_queries.get_recent_turns, ()),
    ],
)
async def test_queries_return_none_when_usage_logging_disabled(monkeypatch, call, args):
    monkeypatch.setattr(usage_queries, "usage_logger", _FakeUsageLogger(enabled=False))

    assert await call(*args) is None


async def test_unknown_granularity_returns_none_before_querying(monkeypatch):
    fake = _FakeUsageLogger(enabled=True)
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    assert await usage_queries.get_time_series("hour") is None
    assert await usage_queries.get_tool_breakdown_series("hour") is None
    assert fake.calls == []


async def test_get_summary_merges_window_and_today_rows(monkeypatch):
    fake = _FakeUsageLogger(
        responses=[
            [{"turns_window": 3, "users_window": 2}],
            [{"turns_today": 1, "tokens_today": 99}],
        ]
    )
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    assert await usage_queries.get_summary(window_days=14) == {
        "turns_window": 3,
        "users_window": 2,
        "turns_today": 1,
        "tokens_today": 99,
    }
    assert fake.calls[0][1] == (14,)
    assert fake.calls[1][1] == ()


async def test_get_summary_returns_none_when_any_query_fails(monkeypatch):
    fake = _FakeUsageLogger(responses=[None, [{"turns_today": 1}]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    assert await usage_queries.get_summary() is None


async def test_get_top_users_uses_date_range_and_limit(monkeypatch):
    fake = _FakeUsageLogger(responses=[[{"user": "alice", "tokens": 100}]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    rows = await usage_queries.get_top_users(
        start_date="2026-04-01",
        end_date="2026-04-30",
        limit=5,
    )

    assert rows == [{"user": "alice", "tokens": 100}]
    sql, params = fake.calls[0]
    assert "FROM usage_turn u" in sql
    assert "LEFT JOIN usage_tool t" in sql
    assert "GROUP BY u.user" in sql
    assert params == ("2026-04-01", "2026-04-30", 5)


async def test_get_top_tools_uses_window_and_limit(monkeypatch):
    fake = _FakeUsageLogger(responses=[[{"tool_name": "Read", "calls": 3}]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    rows = await usage_queries.get_top_tools(window_days=3, limit=7)

    assert rows == [{"tool_name": "Read", "calls": 3}]
    sql, params = fake.calls[0]
    assert "FROM usage_tool t" in sql
    assert "JOIN usage_turn u" in sql
    assert "ORDER BY calls DESC" in sql
    assert params == (3, 7)


async def test_get_time_series_clamps_bucket_count_and_queries(monkeypatch):
    fake = _FakeUsageLogger(responses=[[{"bucket": "2026-04", "turns": 2}]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    rows = await usage_queries.get_time_series(granularity="month", buckets=100)

    assert rows == [{"bucket": "2026-04", "turns": 2}]
    sql, params = fake.calls[0]
    assert "DATE_FORMAT(ts, '%Y-%m')" in sql
    assert params == (60,)


async def test_get_tool_breakdown_series_returns_empty_payload_for_no_rows(monkeypatch):
    fake = _FakeUsageLogger(responses=[[]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    assert await usage_queries.get_tool_breakdown_series() == {"tools": [], "buckets": []}


async def test_get_tool_breakdown_series_pivots_top_tools_by_recent_buckets(monkeypatch):
    fake = _FakeUsageLogger(
        responses=[
            [
                {"bucket": "2026-04-30", "tool_name": "Read", "calls": 5},
                {"bucket": "2026-04-30", "tool_name": "Bash", "calls": 2},
                {"bucket": "2026-04-29", "tool_name": "Read", "calls": 3},
                {"bucket": "2026-04-29", "tool_name": "Edit", "calls": 9},
                {"bucket": "2026-04-28", "tool_name": "Bash", "calls": 20},
            ]
        ]
    )
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    result = await usage_queries.get_tool_breakdown_series(buckets=2, top_n=2)

    assert result == {
        "tools": ["Edit", "Read"],
        "buckets": [
            {"bucket": "2026-04-30", "values": {"Read": 5}},
            {"bucket": "2026-04-29", "values": {"Read": 3, "Edit": 9}},
        ],
    }
    assert fake.calls[0][1] == (5,)


async def test_get_recent_turns_without_user_uses_limit_offset(monkeypatch):
    fake = _FakeUsageLogger(responses=[[{"id": 2}]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    assert await usage_queries.get_recent_turns(limit=10, offset=20) == [{"id": 2}]
    sql, params = fake.calls[0]
    assert "WHERE user = %s" not in sql
    assert params == (10, 20)


async def test_get_recent_turns_with_user_filters_before_limit_offset(monkeypatch):
    fake = _FakeUsageLogger(responses=[[{"id": 3, "user": "alice"}]])
    monkeypatch.setattr(usage_queries, "usage_logger", fake)

    assert await usage_queries.get_recent_turns(user="alice", limit=5, offset=15) == [
        {"id": 3, "user": "alice"}
    ]
    sql, params = fake.calls[0]
    assert "WHERE user = %s" in sql
    assert params == ("alice", 5, 15)
