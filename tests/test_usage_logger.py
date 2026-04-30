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
