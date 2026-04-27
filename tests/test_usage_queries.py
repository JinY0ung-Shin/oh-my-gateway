"""Tests for admin usage analytics query construction."""

from src import usage_queries


def test_granularity_sql_uses_sql_percent_literals():
    assert usage_queries._GRANULARITY_SQL["week"] == "DATE_FORMAT(ts, '%x-W%v')"
    assert usage_queries._GRANULARITY_SQL["month"] == "DATE_FORMAT(ts, '%Y-%m')"
