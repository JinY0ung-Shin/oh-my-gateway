# Coverage Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise total `src` coverage from the current `89%` to at least `91%` by adding focused tests for the lowest-risk uncovered usage logging and usage analytics paths.

**Architecture:** This is a test-only plan. Cover pure helpers, async database wrapper behavior, and SQL query construction with fakes instead of real database connections. Do not change production behavior unless a test reveals a real defect.

**Tech Stack:** Python 3.13, pytest, pytest-asyncio auto mode, pytest-cov, SQLAlchemy already available through project dependencies.

---

## Baseline

Run:

```bash
uv run pytest --cov=src --cov-report=term-missing
```

Current result:

```text
1208 passed, 2 skipped, 4 deselected
TOTAL 5792 statements, 656 missed, 89% coverage
```

Primary low-coverage targets:

```text
src/usage_queries.py   90 statements, 77 missed, 14%
src/usage_logger.py   134 statements, 79 missed, 41%
```

Covering these two modules should remove roughly 130-150 misses and move total coverage to about `91%`.

## File Structure

- Modify `tests/test_usage_logger.py`
  - Owns unit tests for URL normalization, positional bind conversion, SDK usage extraction, logger lifecycle, read failure handling, write behavior, and context-to-turn conversion.
- Modify `tests/test_usage_queries.py`
  - Owns query-construction tests for the admin usage dashboard read model.
- No production files should be modified in this plan.

---

### Task 1: UsageLogger Pure Helper Coverage

**Files:**
- Modify: `tests/test_usage_logger.py`
- Test: `tests/test_usage_logger.py`

- [ ] **Step 1: Add helper imports and pure helper tests**

Add these imports at the top:

```python
import pytest

from src.usage_logger import (
    UsageLogger,
    _bind_positional_params,
    _normalize_db_url,
    _safe_url,
    extract_sdk_usage_detail,
)
```

Add these tests after the fake engine classes:

```python
def test_bind_positional_params_rejects_too_many_values():
    with pytest.raises(ValueError, match="Too many SQL parameters supplied"):
        _bind_positional_params("SELECT 1", ("unused",))


def test_bind_positional_params_rejects_too_few_values():
    with pytest.raises(ValueError, match="Not enough SQL parameters supplied"):
        _bind_positional_params("SELECT * FROM usage_turn WHERE user = %s", ())


def test_normalize_db_url_adds_async_drivers_only_for_plain_aliases():
    assert _normalize_db_url("mysql://u:p@db/app") == "mysql+aiomysql://u:p@db/app"
    assert _normalize_db_url("mariadb://u:p@db/app") == "mariadb+aiomysql://u:p@db/app"
    assert _normalize_db_url("sqlite:///usage.db") == "sqlite+aiosqlite:///usage.db"
    assert _normalize_db_url("mysql+aiomysql://u:p@db/app") == "mysql+aiomysql://u:p@db/app"


def test_safe_url_masks_password_and_falls_back_for_invalid_url():
    assert _safe_url("mysql+aiomysql://user:secret@example.com/db") == (
        "mysql+aiomysql://user:***@example.com/db"
    )
    assert _safe_url("not a database url") == "not a database url"


def test_extract_sdk_usage_detail_prefers_final_result_usage():
    chunks = [
        {
            "type": "assistant",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 4,
            },
        },
        {
            "type": "result",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    ]

    assert extract_sdk_usage_detail(chunks) == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_creation_tokens": 40,
    }


def test_extract_sdk_usage_detail_sums_assistant_usage_when_result_missing():
    chunks = [
        {"type": "assistant", "usage": {"input_tokens": 1, "output_tokens": 2}},
        {
            "type": "assistant",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 4,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 6,
            },
        },
        {"type": "user", "usage": {"input_tokens": 100}},
    ]

    assert extract_sdk_usage_detail(chunks) == {
        "input_tokens": 4,
        "output_tokens": 6,
        "cache_read_tokens": 5,
        "cache_creation_tokens": 6,
    }


def test_extract_sdk_usage_detail_returns_zeroes_for_no_usage():
    assert extract_sdk_usage_detail([{"type": "assistant"}]) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
```

- [ ] **Step 2: Run the focused tests**

Run:

```bash
uv run pytest tests/test_usage_logger.py -q
```

Expected:

```text
9 passed
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_usage_logger.py
git commit -m "test: cover usage logger helpers"
```

---

### Task 2: UsageLogger Lifecycle and Write Path Coverage

**Files:**
- Modify: `tests/test_usage_logger.py`
- Test: `tests/test_usage_logger.py`

- [ ] **Step 1: Extend fake classes for lifecycle and write-path tests**

Add these fakes below `_FakeEngine`:

```python
class _FakeInsertResult:
    def __init__(self, lastrowid):
        self.lastrowid = lastrowid


class _FakeBeginConnection:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.executed = []

    async def execute(self, statement, params=None):
        if self.fail:
            raise RuntimeError("write failed")
        self.executed.append((str(statement), params))
        return _FakeInsertResult(42)


class _FakeBeginContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _FakeLifecycleEngine:
    def __init__(self, *, connect_conn=None, begin_conn=None, connect_fails=False):
        self.connect_conn = connect_conn or _FakeConnection([])
        self.begin_conn = begin_conn or _FakeBeginConnection()
        self.connect_fails = connect_fails
        self.disposed = False

    def connect(self):
        if self.connect_fails:
            raise RuntimeError("connect failed")
        return _FakeConnectContext(self.connect_conn)

    def begin(self):
        return _FakeBeginContext(self.begin_conn)

    async def dispose(self):
        self.disposed = True
```

Add this sample-turn helper:

```python
def _sample_turn():
    return {
        "ts": "2026-04-30 12:00:00.000",
        "user": "alice",
        "session_id": "sess-1",
        "response_id": "resp-1",
        "previous_response_id": "resp-0",
        "turn": 3,
        "model": "claude-sonnet",
        "backend": "claude",
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_creation_tokens": 40,
        "duration_ms": 123,
        "status": "completed",
        "error_code": None,
    }
```

- [ ] **Step 2: Add lifecycle tests**

Add:

```python
async def test_start_disables_logger_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("USAGE_LOG_DB_URL", raising=False)
    logger = UsageLogger()

    await logger.start()

    assert logger.enabled is False
    assert logger._disabled_reason == "USAGE_LOG_DB_URL unset"


async def test_start_creates_engine_with_normalized_url_and_probe(monkeypatch):
    from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

    engine = _FakeLifecycleEngine()
    calls = []

    def fake_create_async_engine(url, **kwargs):
        calls.append((url, kwargs))
        return engine

    monkeypatch.setenv("USAGE_LOG_DB_URL", "mysql://user:pw@example.com/app")
    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", fake_create_async_engine)

    logger = UsageLogger()
    await logger.start()

    assert logger.enabled is True
    assert logger._engine is engine
    assert calls == [
        (
            "mysql+aiomysql://user:pw@example.com/app",
            {
                "pool_size": 5,
                "max_overflow": 0,
                "pool_pre_ping": True,
                "connect_args": {"connect_timeout": 5},
            },
        )
    ]
    assert engine.connect_conn.executed[0][0] == "SELECT 1"


async def test_start_disables_logger_when_engine_creation_fails(monkeypatch):
    from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

    def fake_create_async_engine(url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setenv("USAGE_LOG_DB_URL", "sqlite:///usage.db")
    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", fake_create_async_engine)

    logger = UsageLogger()
    await logger.start()

    assert logger.enabled is False
    assert logger._disabled_reason == "engine init failed: boom"


async def test_start_disposes_engine_when_probe_fails(monkeypatch):
    from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

    engine = _FakeLifecycleEngine(connect_fails=True)

    monkeypatch.setenv("USAGE_LOG_DB_URL", "sqlite:///usage.db")
    monkeypatch.setattr(sqlalchemy_asyncio, "create_async_engine", lambda url, **kwargs: engine)

    logger = UsageLogger()
    await logger.start()

    assert logger.enabled is False
    assert logger._disabled_reason == "connection probe failed: connect failed"
    assert engine.disposed is True


async def test_close_disposes_engine_and_is_idempotent():
    logger = UsageLogger()
    engine = _FakeLifecycleEngine()
    logger._engine = engine

    await logger.close()
    await logger.close()

    assert logger.enabled is False
    assert engine.disposed is True
```

- [ ] **Step 3: Add read and write error path tests**

Add:

```python
async def test_fetch_rows_returns_none_when_query_fails():
    class FailingConnection(_FakeConnection):
        async def execute(self, statement, params=None):
            raise RuntimeError("read failed")

    logger = UsageLogger()
    logger._engine = _FakeEngine(FailingConnection([]))

    assert await logger.fetch_rows("SELECT * FROM usage_turn") is None


async def test_log_turn_inserts_turn_and_tool_rows():
    conn = _FakeBeginConnection()
    logger = UsageLogger()
    logger._engine = _FakeLifecycleEngine(begin_conn=conn)

    await logger.log_turn(
        turn=_sample_turn(),
        tool_stats={
            "Read": {"count": 2, "errors": 0, "total_ms": 12},
            "Bash": {"count": 1, "errors": 1, "total_ms": 34},
        },
    )

    assert len(conn.executed) == 2
    turn_sql, turn_params = conn.executed[0]
    tool_sql, tool_params = conn.executed[1]
    assert "INSERT INTO usage_turn" in turn_sql
    assert turn_params["user"] == "alice"
    assert turn_params["previous_response_id"] == "resp-0"
    assert "INSERT INTO usage_tool" in tool_sql
    assert tool_params == [
        {"turn_id": 42, "tool_name": "Read", "call_count": 2, "error_count": 0, "total_duration_ms": 12},
        {"turn_id": 42, "tool_name": "Bash", "call_count": 1, "error_count": 1, "total_duration_ms": 34},
    ]


async def test_log_turn_swallows_write_failures():
    logger = UsageLogger()
    logger._engine = _FakeLifecycleEngine(begin_conn=_FakeBeginConnection(fail=True))

    await logger.log_turn(turn=_sample_turn(), tool_stats={"Read": {"count": 1}})
```

- [ ] **Step 4: Add context conversion tests**

Add:

```python
async def test_log_turn_from_context_returns_when_metadata_missing():
    logger = UsageLogger()
    logger._engine = object()
    calls = []

    async def fake_log_turn(**kwargs):
        calls.append(kwargs)

    logger.log_turn = fake_log_turn

    await logger.log_turn_from_context(
        request_context={"user": "", "session_id": "sess", "turn": 1},
        response_id="resp",
        model="model",
        chunks=[],
        tool_stats=None,
        started_monotonic=0,
        status="completed",
    )
    await logger.log_turn_from_context(
        request_context={"user": "alice", "session_id": "", "turn": 1},
        response_id="resp",
        model="model",
        chunks=[],
        tool_stats=None,
        started_monotonic=0,
        status="completed",
    )
    await logger.log_turn_from_context(
        request_context={"user": "alice", "session_id": "sess", "turn": None},
        response_id="resp",
        model="model",
        chunks=[],
        tool_stats=None,
        started_monotonic=0,
        status="completed",
    )

    assert calls == []


async def test_log_turn_from_context_builds_turn_record(monkeypatch):
    logger = UsageLogger()
    logger._engine = object()
    calls = []

    async def fake_log_turn(**kwargs):
        calls.append(kwargs)

    logger.log_turn = fake_log_turn
    monkeypatch.setattr("src.usage_logger.time.monotonic", lambda: 105.0)

    await logger.log_turn_from_context(
        request_context={
            "user": "alice",
            "session_id": "sess-1",
            "previous_response_id": "resp-0",
            "turn": "7",
            "provider_model": "fallback-model",
            "backend": "claude",
        },
        response_id="resp-1",
        model="",
        chunks=[{"type": "result", "usage": {"input_tokens": 10, "output_tokens": 20}}],
        tool_stats={"Read": {"count": 1}},
        started_monotonic=100.0,
        status="errored",
        error_code="rate_limit",
    )

    assert len(calls) == 1
    assert calls[0]["tool_stats"] == {"Read": {"count": 1}}
    turn = calls[0]["turn"]
    assert turn["user"] == "alice"
    assert turn["session_id"] == "sess-1"
    assert turn["response_id"] == "resp-1"
    assert turn["previous_response_id"] == "resp-0"
    assert turn["turn"] == 7
    assert turn["model"] == "fallback-model"
    assert turn["backend"] == "claude"
    assert turn["input_tokens"] == 10
    assert turn["output_tokens"] == 20
    assert turn["cache_read_tokens"] == 0
    assert turn["cache_creation_tokens"] == 0
    assert turn["duration_ms"] == 5000
    assert turn["status"] == "errored"
    assert turn["error_code"] == "rate_limit"
```

- [ ] **Step 5: Run the focused tests**

Run:

```bash
uv run pytest tests/test_usage_logger.py --cov=src.usage_logger --cov-report=term-missing
```

Expected:

```text
all tests pass
src/usage_logger.py coverage is at least 90%
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_usage_logger.py
git commit -m "test: cover usage logger lifecycle"
```

---

### Task 3: UsageQueries Read Model Coverage

**Files:**
- Modify: `tests/test_usage_queries.py`
- Test: `tests/test_usage_queries.py`

- [ ] **Step 1: Add fake usage logger and date/window tests**

Replace `tests/test_usage_queries.py` with this structure:

```python
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
```

- [ ] **Step 2: Add disabled-state tests for every public query**

Add:

```python
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
```

- [ ] **Step 3: Add summary/top-list query tests**

Add:

```python
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
```

- [ ] **Step 4: Add series and recent-turn query tests**

Add:

```python
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
```

- [ ] **Step 5: Run focused usage query coverage**

Run:

```bash
uv run pytest tests/test_usage_queries.py --cov=src.usage_queries --cov-report=term-missing
```

Expected:

```text
all tests pass
src/usage_queries.py coverage is at least 90%
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_usage_queries.py
git commit -m "test: cover usage query construction"
```

---

### Task 4: Full Coverage Verification

**Files:**
- No file changes expected.

- [ ] **Step 1: Run full coverage**

Run:

```bash
uv run pytest --cov=src --cov-report=term-missing
```

Expected:

```text
all non-e2e tests pass
TOTAL coverage is at least 91%
```

- [ ] **Step 2: Inspect remaining low files**

If total coverage is below `91%`, inspect the new report and continue with the next best candidates in this order:

```text
src/routes/admin.py
src/backends/opencode/client.py
src/backends/opencode/__init__.py
src/streaming_utils.py
```

Use the same rule: add tests around public behavior and existing branch logic, avoid production changes unless a test exposes a bug.

- [ ] **Step 3: Commit only if verification artifacts changed**

If no files changed, do not commit. If a generated coverage artifact was intentionally added later, commit it with:

```bash
git add <intentional-artifact>
git commit -m "test: record coverage baseline"
```

---

## Self-Review

- Spec coverage: The plan covers the two lowest-coverage modules from the latest report and includes full-suite verification.
- Placeholder scan: Implementation steps contain concrete commands and test code rather than deferred work markers.
- Type consistency: Fake objects match the methods production code calls: `connect()`, `begin()`, async context manager methods, `execute()`, `mappings().all()`, `lastrowid`, and `dispose()`.
