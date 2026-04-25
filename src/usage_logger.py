"""Async writer that persists per-turn usage records via SQLAlchemy.

Logging is **opt-in**: when ``USAGE_LOG_DB_URL`` is unset the logger runs
in no-op mode and :meth:`UsageLogger.log_turn` returns immediately.  When
configured it owns a SQLAlchemy ``AsyncEngine`` whose lifetime is bound
to the FastAPI lifespan (see :mod:`src.main`).

The URL determines the dialect/driver - swap drivers without changing
this code.  Convenience aliases are normalised to async drivers:

- ``mysql://``    -> ``mysql+aiomysql://``    (driver: aiomysql)
- ``mariadb://``  -> ``mariadb+aiomysql://``  (driver: aiomysql)
- ``sqlite://``   -> ``sqlite+aiosqlite://``  (driver: aiosqlite, install separately)

The insert path relies on ``result.lastrowid``, which is supported by
MySQL/MariaDB/SQLite but not PostgreSQL.  Adding PostgreSQL would
require switching to ``insert(...).returning(id)``.

Writes are fire-and-forget - failures are swallowed after a warning so a
flaky database never impacts user-visible chat behaviour.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


_INSERT_TURN_SQL = """
INSERT INTO usage_turn
    (ts, user, session_id, response_id, previous_response_id, turn,
     model, backend, input_tokens, output_tokens,
     cache_read_tokens, cache_creation_tokens,
     duration_ms, status, error_code)
VALUES
    (:ts, :user, :session_id, :response_id, :previous_response_id, :turn,
     :model, :backend, :input_tokens, :output_tokens,
     :cache_read_tokens, :cache_creation_tokens,
     :duration_ms, :status, :error_code)
"""

_INSERT_TOOL_SQL = """
INSERT INTO usage_tool
    (turn_id, tool_name, call_count, error_count, total_duration_ms)
VALUES
    (:turn_id, :tool_name, :call_count, :error_count, :total_duration_ms)
"""


def _normalize_db_url(url: str) -> str:
    """Map shorthand schemes to their async-driver counterparts."""
    aliases = {
        "mysql://": "mysql+aiomysql://",
        "mariadb://": "mariadb+aiomysql://",
        "sqlite://": "sqlite+aiosqlite://",
    }
    for prefix, replacement in aliases.items():
        if url.startswith(prefix) and "+" not in url.split("://", 1)[0]:
            return replacement + url[len(prefix):]
    return url


def _safe_url(url: str) -> str:
    """Render a DB URL with the password masked, for logs."""
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return url


def extract_sdk_usage_detail(chunks: list) -> Dict[str, int]:
    """Return the per-token breakdown used by the usage-log schema.

    Prefers the final ``ResultMessage.usage`` totals.  Falls back to
    summing per-turn ``AssistantMessage.usage`` entries.
    """
    for msg in reversed(chunks):
        if isinstance(msg, dict) and msg.get("type") == "result" and msg.get("usage"):
            u = msg["usage"]
            return {
                "input_tokens": int(u.get("input_tokens", 0) or 0),
                "output_tokens": int(u.get("output_tokens", 0) or 0),
                "cache_read_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
            }

    total = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    for msg in chunks:
        if isinstance(msg, dict) and msg.get("type") == "assistant" and msg.get("usage"):
            u = msg["usage"]
            total["input_tokens"] += int(u.get("input_tokens", 0) or 0)
            total["output_tokens"] += int(u.get("output_tokens", 0) or 0)
            total["cache_read_tokens"] += int(u.get("cache_read_input_tokens", 0) or 0)
            total["cache_creation_tokens"] += int(u.get("cache_creation_input_tokens", 0) or 0)
    return total


class UsageLogger:
    """Async usage-log writer backed by a SQLAlchemy ``AsyncEngine``."""

    def __init__(self) -> None:
        self._engine: Optional[Any] = None  # AsyncEngine when connected
        self._lock = asyncio.Lock()
        self._disabled_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the engine if ``USAGE_LOG_DB_URL`` is configured.

        Safe to call when the env var is unset - logs a single info line and
        leaves the logger in no-op mode.
        """
        raw_url = os.environ.get("USAGE_LOG_DB_URL", "").strip()
        if not raw_url:
            self._disabled_reason = "USAGE_LOG_DB_URL unset"
            logger.info("Usage logging disabled (USAGE_LOG_DB_URL unset)")
            return

        try:
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import create_async_engine
        except ImportError:  # pragma: no cover - surfaced at startup only
            self._disabled_reason = "sqlalchemy[asyncio] not installed"
            logger.warning("Usage logging disabled: sqlalchemy[asyncio] not installed")
            return

        url = _normalize_db_url(raw_url)
        try:
            engine = create_async_engine(
                url,
                pool_size=5,
                max_overflow=0,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 5} if url.startswith("mysql") else {},
            )
        except Exception as exc:
            self._disabled_reason = f"engine init failed: {exc}"
            logger.warning("Usage logging disabled: engine init failed: %s", exc)
            return

        # Eager connectivity probe so a dead DB fails fast at startup
        # instead of stalling each request behind connect_timeout under _lock.
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            self._disabled_reason = f"connection probe failed: {exc}"
            logger.warning("Usage logging disabled: connection probe failed: %s", exc)
            await engine.dispose()
            return

        self._engine = engine
        logger.info("Usage logging enabled (%s)", _safe_url(url))

    async def close(self) -> None:
        """Dispose the engine (idempotent)."""
        engine = self._engine
        if engine is None:
            return
        self._engine = None
        try:
            await engine.dispose()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("Usage logger engine dispose failed")

    @property
    def enabled(self) -> bool:
        return self._engine is not None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def log_turn(
        self,
        *,
        turn: Dict[str, Any],
        tool_stats: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Persist one turn record plus its per-tool aggregates.

        Never raises - DB errors are logged at WARNING and swallowed so the
        request flow is unaffected.
        """
        if self._engine is None:
            return

        from sqlalchemy import text

        try:
            async with self._lock:  # serialise against concurrent close()
                engine = self._engine
                if engine is None:
                    return
                async with engine.begin() as conn:
                    result = await conn.execute(
                        text(_INSERT_TURN_SQL),
                        {
                            "ts": turn["ts"],
                            "user": turn["user"],
                            "session_id": turn["session_id"],
                            "response_id": turn["response_id"],
                            "previous_response_id": turn.get("previous_response_id"),
                            "turn": turn["turn"],
                            "model": turn.get("model"),
                            "backend": turn.get("backend"),
                            "input_tokens": turn.get("input_tokens", 0),
                            "output_tokens": turn.get("output_tokens", 0),
                            "cache_read_tokens": turn.get("cache_read_tokens", 0),
                            "cache_creation_tokens": turn.get("cache_creation_tokens", 0),
                            "duration_ms": turn.get("duration_ms", 0),
                            "status": turn["status"],
                            "error_code": turn.get("error_code"),
                        },
                    )
                    turn_id = result.lastrowid
                    if tool_stats and turn_id is not None:
                        await conn.execute(
                            text(_INSERT_TOOL_SQL),
                            [
                                {
                                    "turn_id": turn_id,
                                    "tool_name": name,
                                    "call_count": stats.get("count", 0),
                                    "error_count": stats.get("errors", 0),
                                    "total_duration_ms": stats.get("total_ms", 0),
                                }
                                for name, stats in tool_stats.items()
                            ],
                        )
        except Exception:
            logger.warning("usage-log write failed", exc_info=True)


    async def log_turn_from_context(
        self,
        *,
        request_context: Optional[Dict[str, Any]],
        response_id: str,
        model: str,
        chunks: list,
        tool_stats: Optional[Dict[str, Dict[str, int]]],
        started_monotonic: float,
        status: str,
        error_code: Optional[str] = None,
    ) -> None:
        """Build and write a usage_turn record from streaming-loop context.

        Returns silently when the logger is disabled, when the request has
        no ``user`` identifier, or when the turn metadata is incomplete -
        the caller doesn't need to pre-check.
        """
        if self._engine is None:
            return
        ctx = request_context or {}
        user = ctx.get("user") or ""
        if not user:
            return
        session_id = ctx.get("session_id") or ""
        turn = ctx.get("turn")
        if not session_id or turn is None or not response_id:
            return

        usage = extract_sdk_usage_detail(chunks)
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        await self.log_turn(
            turn={
                "ts": ts,
                "user": user,
                "session_id": session_id,
                "response_id": response_id,
                "previous_response_id": ctx.get("previous_response_id"),
                "turn": int(turn),
                "model": model or ctx.get("provider_model"),
                "backend": ctx.get("backend"),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_read_tokens": usage["cache_read_tokens"],
                "cache_creation_tokens": usage["cache_creation_tokens"],
                "duration_ms": duration_ms,
                "status": status,
                "error_code": error_code,
            },
            tool_stats=tool_stats,
        )


# Module-level singleton used by the streaming/non-streaming paths.
usage_logger = UsageLogger()
