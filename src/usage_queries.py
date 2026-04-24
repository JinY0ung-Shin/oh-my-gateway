"""Read-side analytics queries for the admin usage dashboard.

All functions return plain Python values (lists of dicts / dicts) or ``None``
when the usage-log pool is disabled / unavailable.  The calling endpoints in
``src.routes.admin`` translate ``None`` into an empty response so the UI can
render a "usage logging off" hint without treating it as an error.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.usage_logger import usage_logger


async def get_summary(window_days: int = 7) -> Optional[Dict[str, Any]]:
    """Overview counters for the given rolling window and today (00:00 KST-ish)."""
    if not usage_logger.enabled:
        return None
    rows = await usage_logger.fetch_rows(
        """
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
        WHERE ts >= NOW() - INTERVAL %s DAY
        """,
        (int(window_days),),
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


async def get_top_users(window_days: int = 7, limit: int = 20) -> Optional[List[Dict[str, Any]]]:
    if not usage_logger.enabled:
        return None
    rows = await usage_logger.fetch_rows(
        """
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
        WHERE u.ts >= NOW() - INTERVAL %s DAY
        GROUP BY u.user
        ORDER BY tokens DESC
        LIMIT %s
        """,
        (int(window_days), int(limit)),
    )
    return rows


async def get_top_tools(window_days: int = 7, limit: int = 30) -> Optional[List[Dict[str, Any]]]:
    if not usage_logger.enabled:
        return None
    rows = await usage_logger.fetch_rows(
        """
        SELECT
          t.tool_name,
          SUM(t.call_count) AS calls,
          SUM(t.error_count) AS errors,
          SUM(t.total_duration_ms) AS total_ms,
          COUNT(DISTINCT u.user) AS users
        FROM usage_tool t
        JOIN usage_turn u ON u.id = t.turn_id
        WHERE u.ts >= NOW() - INTERVAL %s DAY
        GROUP BY t.tool_name
        ORDER BY calls DESC
        LIMIT %s
        """,
        (int(window_days), int(limit)),
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
