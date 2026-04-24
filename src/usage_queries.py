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
