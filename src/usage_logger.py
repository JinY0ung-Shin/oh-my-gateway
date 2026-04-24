"""Async writer that persists per-turn usage records to MySQL.

Logging is **opt-in**: when ``USAGE_LOG_DB_URL`` is unset the logger runs
in no-op mode and :meth:`UsageLogger.log_turn` returns immediately.  When
configured it maintains an ``aiomysql`` connection pool whose lifetime is
bound to the FastAPI lifespan (see :mod:`src.main`).

Writes are fire-and-forget - failures are swallowed after a warning so a
flaky database never impacts user-visible chat behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_url(cls, url: str) -> "_DbConfig":
        """Parse ``mysql://user:pass@host:port/db`` (password may be URL-encoded)."""
        parsed = urlparse(url)
        if parsed.scheme not in ("mysql", "mysql+aiomysql"):
            raise ValueError(f"Unsupported USAGE_LOG_DB_URL scheme: {parsed.scheme!r}")
        database = (parsed.path or "").lstrip("/")
        if not database:
            raise ValueError("USAGE_LOG_DB_URL is missing a database name")
        return cls(
            host=parsed.hostname or "localhost",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=database,
        )


class UsageLogger:
    """Async usage-log writer backed by an aiomysql connection pool."""

    def __init__(self) -> None:
        self._pool: Optional[Any] = None  # aiomysql.Pool when connected
        self._config: Optional[_DbConfig] = None
        self._lock = asyncio.Lock()
        self._disabled_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the connection pool if ``USAGE_LOG_DB_URL`` is configured.

        Safe to call when the env var is unset - logs a single info line and
        leaves the logger in no-op mode.
        """
        url = os.environ.get("USAGE_LOG_DB_URL", "").strip()
        if not url:
            self._disabled_reason = "USAGE_LOG_DB_URL unset"
            logger.info("Usage logging disabled (USAGE_LOG_DB_URL unset)")
            return

        try:
            config = _DbConfig.from_url(url)
        except ValueError as exc:
            self._disabled_reason = f"invalid URL: {exc}"
            logger.warning("Usage logging disabled: %s", exc)
            return

        try:
            import aiomysql  # local import keeps the dep optional at test time
        except ImportError:  # pragma: no cover - surfaced at startup only
            self._disabled_reason = "aiomysql not installed"
            logger.warning("Usage logging disabled: aiomysql not installed")
            return

        try:
            self._pool = await aiomysql.create_pool(
                host=config.host,
                port=config.port,
                user=config.user,
                password=config.password,
                db=config.database,
                charset="utf8mb4",
                autocommit=False,
                minsize=1,
                maxsize=5,
                connect_timeout=5.0,
            )
            self._config = config
            logger.info(
                "Usage logging enabled (mysql://%s@%s:%s/%s)",
                config.user,
                config.host,
                config.port,
                config.database,
            )
        except Exception as exc:
            self._disabled_reason = f"pool init failed: {exc}"
            logger.warning("Usage logging disabled: pool init failed: %s", exc)

    async def close(self) -> None:
        """Close the pool (idempotent)."""
        pool = self._pool
        if pool is None:
            return
        self._pool = None
        try:
            pool.close()
            await pool.wait_closed()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("Usage logger pool shutdown failed")

    @property
    def enabled(self) -> bool:
        return self._pool is not None

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
        if self._pool is None:
            return
        try:
            async with self._lock:  # serialise against concurrent close()
                pool = self._pool
                if pool is None:
                    return
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """INSERT INTO usage_turn
                               (ts, user, session_id, response_id,
                                previous_response_id, turn, model, backend,
                                input_tokens, output_tokens,
                                cache_read_tokens, cache_creation_tokens,
                                duration_ms, status, error_code)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                       %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                turn["ts"],
                                turn["user"],
                                turn["session_id"],
                                turn["response_id"],
                                turn.get("previous_response_id"),
                                turn["turn"],
                                turn.get("model"),
                                turn.get("backend"),
                                turn.get("input_tokens", 0),
                                turn.get("output_tokens", 0),
                                turn.get("cache_read_tokens", 0),
                                turn.get("cache_creation_tokens", 0),
                                turn.get("duration_ms", 0),
                                turn["status"],
                                turn.get("error_code"),
                            ),
                        )
                        turn_id = cur.lastrowid
                        if tool_stats:
                            await cur.executemany(
                                """INSERT INTO usage_tool
                                   (turn_id, tool_name, call_count,
                                    error_count, total_duration_ms)
                                   VALUES (%s, %s, %s, %s, %s)""",
                                [
                                    (
                                        turn_id,
                                        name,
                                        stats.get("count", 0),
                                        stats.get("errors", 0),
                                        stats.get("total_ms", 0),
                                    )
                                    for name, stats in tool_stats.items()
                                ],
                            )
                    await conn.commit()
        except Exception:
            logger.warning("usage-log write failed", exc_info=True)


# Module-level singleton used by the streaming/non-streaming paths.
usage_logger = UsageLogger()
