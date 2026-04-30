"""Tests for SQLAlchemy-backed usage logging."""

import pytest

from src.usage_logger import (
    UsageLogger,
    _bind_positional_params,
    _normalize_db_url,
    _safe_url,
    extract_sdk_usage_detail,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return _FakeResult(self.rows)


class _FakeConnectContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def connect(self):
        return _FakeConnectContext(self.conn)


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
        self.dispose_count = 0

    def connect(self):
        if self.connect_fails:
            raise RuntimeError("connect failed")
        return _FakeConnectContext(self.connect_conn)

    def begin(self):
        return _FakeBeginContext(self.begin_conn)

    async def dispose(self):
        self.dispose_count += 1
        self.disposed = True


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


def test_bind_positional_params_rejects_too_many_values():
    with pytest.raises(ValueError, match="Too many SQL parameters supplied"):
        _bind_positional_params("SELECT 1", ("unused",))


def test_bind_positional_params_rejects_too_few_values():
    with pytest.raises(ValueError, match="Not enough SQL parameters supplied"):
        _bind_positional_params("SELECT * FROM usage_turn WHERE user = %s", ())


def test_bind_positional_params_converts_placeholders_to_named_binds():
    sql = "SELECT * FROM usage_turn WHERE user = %s AND turn > %s"
    converted, bound = _bind_positional_params(sql, ("alice", 1))
    assert converted == "SELECT * FROM usage_turn WHERE user = :p0 AND turn > :p1"
    assert bound == {"p0": "alice", "p1": 1}


def test_normalize_db_url_adds_async_drivers_only_for_plain_aliases():
    assert _normalize_db_url("mysql://u:p@db/app") == "mysql+aiomysql://u:p@db/app"
    assert _normalize_db_url("mariadb://u:p@db/app") == "mariadb+aiomysql://u:p@db/app"
    assert _normalize_db_url("sqlite:///usage.db") == "sqlite+aiosqlite:///usage.db"
    assert _normalize_db_url("mysql+aiomysql://u:p@db/app") == "mysql+aiomysql://u:p@db/app"
    assert _normalize_db_url("postgresql://u:p@db/app") == "postgresql://u:p@db/app"


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


async def test_fetch_rows_uses_sqlalchemy_engine_and_binds_positional_params():
    logger = UsageLogger()
    conn = _FakeConnection([{"turns": 2, "user": "alice"}])
    logger._engine = _FakeEngine(conn)

    rows = await logger.fetch_rows(
        "SELECT COUNT(*) AS turns, user FROM usage_turn WHERE user = %s AND turn > %s",
        ("alice", 1),
    )

    assert rows == [{"turns": 2, "user": "alice"}]
    assert conn.executed == [
        (
            "SELECT COUNT(*) AS turns, user FROM usage_turn WHERE user = :p0 AND turn > :p1",
            {"p0": "alice", "p1": 1},
        )
    ]


async def test_fetch_rows_returns_none_when_usage_logger_disabled():
    logger = UsageLogger()

    assert await logger.fetch_rows("SELECT 1") is None


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
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "mysql+aiomysql://user:pw@example.com/app"
    assert kwargs["pool_size"] == 5
    assert kwargs["max_overflow"] == 0
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["connect_args"] == {"connect_timeout": 5}
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
    assert engine.dispose_count == 1


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
