"""Regression tests proving the admin usage dashboard works on SQLite.

config_check advertises ``sqlite`` as a supported ``USAGE_LOG_DB_URL`` dialect.
The write path is dialect-agnostic, but the read-side analytics queries in
``src.usage_queries`` use MySQL-only date/time builtins; without dialect
branching they raise ``OperationalError`` on SQLite, ``fetch_rows`` swallows it
and returns ``None``, and the dashboard silently shows "usage logging off".

These tests run every query function against a *real* in-memory SQLite engine
and assert non-None, correctly-shaped results.
"""

import datetime as _dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import src.usage_queries as uq
from src.usage_logger import usage_logger
from src.usage_time import KST

# Portable SQLite mirror of docker/mysql_init/01_schema.sql.
_DDL = [
    """
    CREATE TABLE usage_turn (
      id                    INTEGER PRIMARY KEY AUTOINCREMENT,
      ts                    TEXT NOT NULL,
      user                  TEXT NOT NULL,
      session_id            TEXT NOT NULL,
      response_id           TEXT NOT NULL,
      previous_response_id  TEXT,
      turn                  INTEGER NOT NULL,
      model                 TEXT,
      backend               TEXT,
      input_tokens          INTEGER NOT NULL DEFAULT 0,
      output_tokens         INTEGER NOT NULL DEFAULT 0,
      cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
      cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
      duration_ms           INTEGER NOT NULL DEFAULT 0,
      status                TEXT NOT NULL,
      error_code            TEXT
    )
    """,
    """
    CREATE TABLE usage_tool (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      turn_id           INTEGER NOT NULL,
      tool_name         TEXT NOT NULL,
      call_count        INTEGER NOT NULL,
      error_count       INTEGER NOT NULL DEFAULT 0,
      total_duration_ms INTEGER NOT NULL DEFAULT 0
    )
    """,
]


def _ts(days_ago: int) -> str:
    """A UTC DB timestamp ``days_ago`` days in the past."""
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)
    return when.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


@pytest.fixture
async def sqlite_usage_logger(monkeypatch):
    """Point the module-level ``usage_logger`` at a seeded in-memory SQLite DB.

    A single shared connection keeps the ``:memory:`` database alive across the
    query calls (each ``engine.connect()`` would otherwise get a fresh, empty DB).
    """
    engine = create_async_engine("sqlite+aiosqlite://")

    async with engine.begin() as conn:
        for ddl in _DDL:
            await conn.execute(text(ddl))
        # Two users, three turns across recent days + today, mixed statuses and
        # tools so every aggregate has something non-trivial to compute.
        rows = [
            # id 1: alice, today, completed
            (
                _ts(0),
                "alice",
                "s1",
                "r1",
                None,
                1,
                "claude-opus",
                "claude",
                100,
                50,
                10,
                5,
                200,
                "completed",
                None,
            ),
            # id 2: alice, 2 days ago, errored (exercises the error CASE)
            (
                _ts(2),
                "alice",
                "s1",
                "r2",
                "r1",
                2,
                "claude-opus",
                "claude",
                80,
                40,
                0,
                0,
                150,
                "errored",
                "rate_limit",
            ),
            # id 3: bob, 1 day ago, completed
            (
                _ts(1),
                "bob",
                "s2",
                "r3",
                None,
                1,
                "claude-sonnet",
                "claude",
                60,
                30,
                5,
                2,
                120,
                "completed",
                None,
            ),
        ]
        for r in rows:
            await conn.execute(
                text("""
                    INSERT INTO usage_turn
                      (ts, user, session_id, response_id, previous_response_id,
                       turn, model, backend, input_tokens, output_tokens,
                       cache_read_tokens, cache_creation_tokens, duration_ms,
                       status, error_code)
                    VALUES (:ts,:user,:sid,:rid,:prid,:turn,:model,:backend,
                            :it,:ot,:crt,:cct,:dur,:status,:err)
                    """),
                {
                    "ts": r[0],
                    "user": r[1],
                    "sid": r[2],
                    "rid": r[3],
                    "prid": r[4],
                    "turn": r[5],
                    "model": r[6],
                    "backend": r[7],
                    "it": r[8],
                    "ot": r[9],
                    "crt": r[10],
                    "cct": r[11],
                    "dur": r[12],
                    "status": r[13],
                    "err": r[14],
                },
            )
        tools = [
            (1, "Read", 3, 0, 30),
            (1, "Bash", 1, 1, 50),
            (2, "Read", 2, 0, 20),
            (3, "Edit", 4, 0, 40),
        ]
        for t in tools:
            await conn.execute(
                text("""
                    INSERT INTO usage_tool
                      (turn_id, tool_name, call_count, error_count, total_duration_ms)
                    VALUES (:tid,:name,:calls,:errs,:ms)
                    """),
                {"tid": t[0], "name": t[1], "calls": t[2], "errs": t[3], "ms": t[4]},
            )

    monkeypatch.setattr(usage_logger, "_engine", engine)
    assert usage_logger.enabled is True
    assert usage_logger.dialect == "sqlite"
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_get_summary_on_sqlite(sqlite_usage_logger):
    summary = await uq.get_summary(window_days=7)
    assert summary is not None
    # All three turns fall inside the 7-day window.
    assert summary["turns_window"] == 3
    assert summary["users_window"] == 2
    assert summary["chats_window"] == 2
    assert summary["input_tokens_window"] == 240
    assert summary["output_tokens_window"] == 120
    # One non-completed turn -> errors_window == 1 (the portable CASE form).
    assert summary["errors_window"] == 1
    # Today's row only (id 1): turns_today == 1, tokens_today == 150.
    assert summary["turns_today"] == 1
    assert summary["tokens_today"] == 150


async def test_get_summary_explicit_date_range_on_sqlite(sqlite_usage_logger):
    today = _dt.datetime.now(KST).date()
    start = (today - _dt.timedelta(days=3)).isoformat()
    end = today.isoformat()
    summary = await uq.get_summary(start_date=start, end_date=end)
    assert summary is not None
    # The inclusive KST range is converted to UTC bounds and covers all rows.
    assert summary["turns_window"] == 3


async def test_kst_boundary_is_used_for_filters_buckets_and_turn_timestamps(
    sqlite_usage_logger,
):
    async with sqlite_usage_logger.begin() as conn:
        await conn.execute(
            text("""
                INSERT INTO usage_turn
                  (ts, user, session_id, response_id, turn, status)
                VALUES
                  (:ts, :user, :session_id, :response_id, :turn, :status)
                """),
            [
                {
                    # Still 2026-12-31 in KST, one millisecond before midnight.
                    "ts": "2026-12-31 14:59:59.999",
                    "user": "boundary-before",
                    "session_id": "s-boundary-before",
                    "response_id": "r-boundary-before",
                    "turn": 1,
                    "status": "completed",
                },
                {
                    # Exact KST midnight: 2027-01-01 00:00:00+09:00.
                    "ts": "2026-12-31 15:00:00.000",
                    "user": "boundary",
                    "session_id": "s-boundary",
                    "response_id": "r-boundary",
                    "turn": 1,
                    "status": "completed",
                },
            ],
        )

    summary = await uq.get_summary(start_date="2027-01-01", end_date="2027-01-01")
    assert summary is not None
    assert summary["turns_window"] == 1

    boundary_day = _dt.date(2027, 1, 1)
    iso = boundary_day.isocalendar()
    assert (iso.year, iso.week) == (2026, 53)
    expected_buckets = {
        "day": "2027-01-01",
        "week": f"{iso.year}-W{iso.week:02d}",
        "month": "2027-01",
    }
    for granularity, expected in expected_buckets.items():
        series = await uq.get_time_series(granularity=granularity, buckets=60)
        assert series is not None
        assert any(row["bucket"] == expected for row in series)

    turns = await uq.get_recent_turns(user="boundary", limit=1)
    assert turns is not None
    assert turns[0]["ts"] == "2027-01-01T00:00:00.000+09:00"


async def test_get_top_users_on_sqlite(sqlite_usage_logger):
    users = await uq.get_top_users(window_days=7, limit=10)
    assert users is not None and len(users) == 2
    by_user = {row["user"]: row for row in users}
    assert set(by_user) == {"alice", "bob"}
    # The LEFT JOIN with usage_tool fans turns out per tool row, so turn-level
    # counters are inflated by the tool count - this is identical to the MySQL
    # behaviour; we only assert the SQLite path computes the same numbers.
    # alice: turn1 (Read,Bash) + turn2 (Read) = 3 joined rows.
    assert by_user["alice"]["turns"] == 3
    assert by_user["alice"]["tokens"] == 420  # turn1 (150)*2 + turn2 (120)
    assert by_user["alice"]["turn_errors"] == 1
    assert by_user["alice"]["tool_calls"] == 6  # 3 + 1 + 2
    assert by_user["alice"]["tool_errors"] == 1
    # Ordered by tokens DESC -> alice before bob.
    assert users[0]["user"] == "alice"


async def test_get_top_tools_on_sqlite(sqlite_usage_logger):
    tools = await uq.get_top_tools(window_days=7, limit=10)
    assert tools is not None and len(tools) == 3
    by_tool = {row["tool_name"]: row for row in tools}
    assert by_tool["Read"]["calls"] == 5  # 3 + 2
    assert by_tool["Edit"]["calls"] == 4
    assert by_tool["Bash"]["errors"] == 1
    # Ordered by calls DESC -> Read (5) first.
    assert tools[0]["tool_name"] == "Read"


@pytest.mark.parametrize("granularity", ["day", "week", "month"])
async def test_get_time_series_on_sqlite(sqlite_usage_logger, granularity):
    series = await uq.get_time_series(granularity=granularity, buckets=10)
    assert series is not None and len(series) >= 1
    total_turns = sum(int(r["turns"]) for r in series)
    total_tool_calls = sum(int(r["tool_calls"]) for r in series)
    assert total_turns == 3
    assert total_tool_calls == 10  # 3+1+2+4
    for row in series:
        assert "bucket" in row and row["bucket"]
        assert {"turns", "users", "input_tokens", "output_tokens", "tool_calls"} <= set(
            row
        )


@pytest.mark.parametrize("granularity", ["day", "week", "month"])
async def test_get_tool_breakdown_series_on_sqlite(sqlite_usage_logger, granularity):
    result = await uq.get_tool_breakdown_series(
        granularity=granularity, buckets=10, top_n=5
    )
    assert result is not None
    assert "tools" in result and "buckets" in result
    # Read is the highest-volume tool across the window.
    assert "Read" in result["tools"]
    total_calls = sum(sum(b["values"].values()) for b in result["buckets"])
    assert total_calls > 0


async def test_get_recent_turns_on_sqlite(sqlite_usage_logger):
    turns = await uq.get_recent_turns(limit=10)
    assert turns is not None and len(turns) == 3
    # Ordered by id DESC -> most recent insert first.
    assert turns[0]["id"] == 3
    expected_cols = {
        "id",
        "ts",
        "user",
        "session_id",
        "response_id",
        "previous_response_id",
        "turn",
        "model",
        "backend",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "duration_ms",
        "status",
        "error_code",
    }
    assert expected_cols <= set(turns[0])

    # Filtered by user.
    alice_turns = await uq.get_recent_turns(user="alice", limit=10)
    assert alice_turns is not None and len(alice_turns) == 2
    assert all(t["user"] == "alice" for t in alice_turns)
