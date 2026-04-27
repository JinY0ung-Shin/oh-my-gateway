"""Tests for SQLAlchemy-backed usage logging."""

from src.usage_logger import UsageLogger


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
