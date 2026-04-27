"""Read-side analytics queries for the admin usage dashboard.

All functions return plain Python values (lists of dicts / dicts) or ``None``
when the usage-log pool is disabled / unavailable.  The calling endpoints in
``src.routes.admin`` translate ``None`` into an empty response so the UI can
render a "usage logging off" hint without treating it as an error.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Tuple

from src.usage_logger import usage_logger


def _parse_date(value: Optional[str]) -> Optional[_dt.date]:
    """Return ``date`` from an ``YYYY-MM-DD`` string, or ``None``."""
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def _window_clause(
    start_date: Optional[str],
    end_date: Optional[str],
    window_days: int,
    *,
    column: str = "ts",
) -> Tuple[str, tuple]:
    """Build the WHERE clause fragment for the requested range.

    When both ``start_date`` and ``end_date`` are valid ``YYYY-MM-DD``
    strings, filters the inclusive calendar range.  Otherwise falls back
    to the rolling ``window_days`` window ending now.
    """
    s, e = _parse_date(start_date), _parse_date(end_date)
    if s and e:
        if e < s:
            s, e = e, s
        return (
            f"{column} >= %s AND {column} < DATE_ADD(%s, INTERVAL 1 DAY)",
            (s.isoformat(), e.isoformat()),
        )
    return (
        f"{column} >= NOW() - INTERVAL %s DAY",
        (max(1, int(window_days)),),
    )


async def get_summary(
    window_days: int = 7,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Overview counters for the given range plus today's totals."""
    if not usage_logger.enabled:
        return None
    where, params = _window_clause(start_date, end_date, window_days)
    rows = await usage_logger.fetch_rows(
        f"""
        SELECT
          COUNT(*) AS turns_window,
          COUNT(DISTINCT user) AS users_window,
          COUNT(DISTINCT session_id) AS chats_window,
          COALESCE(SUM(input_tokens), 0) AS input_tokens_window,
          COALESCE(SUM(output_tokens), 0) AS output_tokens_window,
          COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens_window,
          COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens_window,
          SUM(status <> 'completed') AS errors_window
        FROM usage_turn
        WHERE {where}
        """,
        params,
    )
    today_rows = await usage_logger.fetch_rows(
        """
        SELECT
          COUNT(*) AS turns_today,
          COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens_today
        FROM usage_turn
        WHERE ts >= CURDATE()
        """,
    )
    if rows is None or today_rows is None:
        return None
    out = dict(rows[0]) if rows else {}
    out.update(today_rows[0] if today_rows else {})
    return out


async def get_top_users(
    window_days: int = 7,
    limit: int = 20,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    if not usage_logger.enabled:
        return None
    where, params = _window_clause(start_date, end_date, window_days, column="u.ts")
    rows = await usage_logger.fetch_rows(
        f"""
        SELECT
          u.user,
          COUNT(DISTINCT u.session_id) AS chats,
          COUNT(*) AS turns,
          COALESCE(SUM(u.input_tokens + u.output_tokens), 0) AS tokens,
          COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read_tokens,
          COALESCE(SUM(t.call_count), 0) AS tool_calls,
          COALESCE(SUM(t.error_count), 0) AS tool_errors,
          SUM(u.status <> 'completed') AS turn_errors
        FROM usage_turn u
        LEFT JOIN usage_tool t ON t.turn_id = u.id
        WHERE {where}
        GROUP BY u.user
        ORDER BY tokens DESC
        LIMIT %s
        """,
        params + (int(limit),),
    )
    return rows


async def get_top_tools(
    window_days: int = 7,
    limit: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    if not usage_logger.enabled:
        return None
    where, params = _window_clause(start_date, end_date, window_days, column="u.ts")
    rows = await usage_logger.fetch_rows(
        f"""
        SELECT
          t.tool_name,
          SUM(t.call_count) AS calls,
          SUM(t.error_count) AS errors,
          SUM(t.total_duration_ms) AS total_ms,
          COUNT(DISTINCT u.user) AS users
        FROM usage_tool t
        JOIN usage_turn u ON u.id = t.turn_id
        WHERE {where}
        GROUP BY t.tool_name
        ORDER BY calls DESC
        LIMIT %s
        """,
        params + (int(limit),),
    )
    return rows


_GRANULARITY_SQL: Dict[str, str] = {
    # MySQL expressions that bucket ``ts`` for the requested granularity.
    # ``%v`` (ISO week) paired with ``%x`` (ISO week year) so boundaries
    # line up with the Monday-first ISO week definition.
    #
    # ``%`` is doubled because aiomysql.Cursor.execute uses printf-style
    # parameter interpolation on the SQL string; any bare ``%`` would be
    # eaten as a format directive and raise a binding error.
    "day": "DATE(ts)",
    "week": "DATE_FORMAT(ts, '%%x-W%%v')",
    "month": "DATE_FORMAT(ts, '%%Y-%%m')",
}


async def get_time_series(
    granularity: str = "day",
    buckets: int = 5,
) -> Optional[List[Dict[str, Any]]]:
    """Recent bucket totals for the USAGE time-series charts.

    Returns up to ``buckets`` rows ordered newest-first with fields:
      - ``bucket`` (string label)
      - ``turns``, ``users``, ``tool_calls``, ``input_tokens``,
        ``output_tokens``

    Returns ``None`` when usage logging is disabled or the granularity is
    unknown (caller surfaces that as "logging off").
    """
    bucket_expr = _GRANULARITY_SQL.get(granularity)
    if bucket_expr is None or not usage_logger.enabled:
        return None
    n = max(1, min(int(buckets), 60))

    # Turn aggregates - SUM(input_tokens) etc. must be computed BEFORE the
    # join with usage_tool so tool rows don't duplicate turn tokens.
    rows = await usage_logger.fetch_rows(
        f"""
        SELECT
          turn_agg.bucket AS bucket,
          turn_agg.turns AS turns,
          turn_agg.users AS users,
          turn_agg.input_tokens AS input_tokens,
          turn_agg.output_tokens AS output_tokens,
          COALESCE(tool_agg.tool_calls, 0) AS tool_calls
        FROM (
          SELECT
            {bucket_expr} AS bucket,
            COUNT(*) AS turns,
            COUNT(DISTINCT user) AS users,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens
          FROM usage_turn
          GROUP BY {bucket_expr}
        ) turn_agg
        LEFT JOIN (
          SELECT
            {bucket_expr.replace('ts', 'u.ts')} AS bucket,
            SUM(t.call_count) AS tool_calls
          FROM usage_tool t
          JOIN usage_turn u ON u.id = t.turn_id
          GROUP BY {bucket_expr.replace('ts', 'u.ts')}
        ) tool_agg ON tool_agg.bucket = turn_agg.bucket
        ORDER BY turn_agg.bucket DESC
        LIMIT %s
        """,
        (n,),
    )
    return rows


async def get_tool_breakdown_series(
    granularity: str = "day",
    buckets: int = 5,
    top_n: int = 5,
) -> Optional[Dict[str, Any]]:
    """Per-bucket tool-call breakdown for the grouped TOOL CALLS bar chart.

    Returns ``{tools: [...], buckets: [{bucket, values: {tool: calls}}]}``
    with ``tools`` ordered by total calls (descending) so the frontend
    can map each tool to a stable colour.

    Approximates the time window per granularity as 5d / 35d / 150d for
    day / week / month so the same backend can serve all three columns.
    """
    bucket_expr = _GRANULARITY_SQL.get(granularity)
    if bucket_expr is None or not usage_logger.enabled:
        return None
    bucket_expr_uts = bucket_expr.replace("ts", "u.ts")
    days_window = {"day": 5, "week": 35, "month": 150}.get(granularity, 30)

    rows = await usage_logger.fetch_rows(
        f"""
        SELECT
          {bucket_expr_uts} AS bucket,
          t.tool_name AS tool_name,
          SUM(t.call_count) AS calls
        FROM usage_tool t
        JOIN usage_turn u ON u.id = t.turn_id
        WHERE u.ts >= NOW() - INTERVAL %s DAY
        GROUP BY {bucket_expr_uts}, t.tool_name
        ORDER BY {bucket_expr_uts} DESC, calls DESC
        """,
        (days_window,),
    )
    if not rows:
        return {"tools": [], "buckets": []}

    # Distinct buckets in DESC order, take the most recent N
    seen: List[str] = []
    for r in rows:
        b = str(r["bucket"])
        if b not in seen:
            seen.append(b)
        if len(seen) >= int(buckets):
            break
    keep_buckets = seen[: int(buckets)]
    keep_set = set(keep_buckets)

    # Sum tool totals over the kept buckets to pick the top N
    tool_totals: Dict[str, int] = {}
    for r in rows:
        if str(r["bucket"]) in keep_set:
            tool_totals[r["tool_name"]] = tool_totals.get(r["tool_name"], 0) + int(r["calls"])
    top_tools = [t for t, _ in sorted(tool_totals.items(), key=lambda x: -x[1])[: int(top_n)]]
    top_set = set(top_tools)

    # Pivot: bucket -> {tool: calls}
    by_bucket: Dict[str, Dict[str, int]] = {b: {} for b in keep_buckets}
    for r in rows:
        b = str(r["bucket"])
        if b in keep_set and r["tool_name"] in top_set:
            by_bucket[b][r["tool_name"]] = int(r["calls"])
    return {
        "tools": top_tools,
        "buckets": [{"bucket": b, "values": by_bucket[b]} for b in keep_buckets],
    }


async def get_recent_turns(
    user: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Optional[List[Dict[str, Any]]]:
    if not usage_logger.enabled:
        return None
    params: list = []
    where = ""
    if user:
        where = "WHERE user = %s"
        params.append(user)
    params.extend([int(limit), int(offset)])
    rows = await usage_logger.fetch_rows(
        f"""
        SELECT
          id, ts, user, session_id, response_id, previous_response_id,
          turn, model, backend,
          input_tokens, output_tokens,
          cache_read_tokens, cache_creation_tokens,
          duration_ms, status, error_code
        FROM usage_turn
        {where}
        ORDER BY id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params),
    )
    return rows
