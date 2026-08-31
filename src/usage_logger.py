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

Writes are fire-and-forget: ``log_turn_from_context`` records Prometheus
metrics synchronously, then schedules the DB INSERT on a detached background
task (``_schedule_log_turn``). Detaching matters - awaiting the write inline in
the request task let a client disconnect raise ``CancelledError`` through the
in-flight commit, poisoning the pooled connection. DB errors are swallowed
after a warning so a flaky database never impacts user-visible chat behaviour;
in-flight writes are drained on ``close()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from src.usage_time import current_db_timestamp

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


def _bind_positional_params(sql: str, params: tuple) -> tuple[str, Dict[str, Any]]:
    """Convert ``%s`` placeholders to SQLAlchemy named bind parameters."""
    bound: Dict[str, Any] = {}
    converted = sql
    for idx, value in enumerate(params):
        marker = f"p{idx}"
        if "%s" not in converted:
            raise ValueError("Too many SQL parameters supplied")
        converted = converted.replace("%s", f":{marker}", 1)
        bound[marker] = value
    if "%s" in converted:
        raise ValueError("Not enough SQL parameters supplied")
    return converted, bound


def _normalize_db_url(url: str) -> str:
    """Map shorthand schemes to their async-driver counterparts."""
    aliases = {
        "mysql://": "mysql+aiomysql://",
        "mariadb://": "mariadb+aiomysql://",
        "sqlite://": "sqlite+aiosqlite://",
    }
    for prefix, replacement in aliases.items():
        if url.startswith(prefix) and "+" not in url.split("://", 1)[0]:
            return replacement + url[len(prefix) :]
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


def _prompt_size(usage: Any) -> Optional[int]:
    """Prompt tokens of one model request: ``input_tokens`` + both cache counters.

    A cache read still occupies the context window — it is only cheaper — so all
    three counters belong in the size. Returns ``None`` for a usage-less or
    zero-prompt record (streaming ``message_delta`` usage reports output only).
    """
    if not isinstance(usage, dict):
        return None
    tokens = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    return tokens if tokens > 0 else None


def context_snapshot_tokens(chunk: Any) -> Optional[int]:
    """Window-occupancy snapshot carried by ONE chunk, or ``None``.

    Two chunk shapes carry it, because which one appears depends on
    ``TOKEN_STREAMING``:

    * ``assistant`` — prompt counts on the message's own ``usage``. This is the
      shape in non-streaming mode.
    * ``stream_event`` / ``message_start`` — the raw Anthropic stream event. With
      ``include_partial_messages`` the prompt counts ride here, while the
      assembled ``assistant`` message may report only the ``message_delta``
      counts (output, ``input_tokens`` null). Reading assistant chunks alone
      would therefore go blind in the gateway's DEFAULT configuration.

    Subagent chunks (``parent_tool_use_id`` set) return ``None`` for the same
    reason :func:`extract_model_id` skips them: a subagent runs its own separate
    context, so its prompt size says nothing about the main conversation.

    This predicate is the single place that knows those shapes — the streaming
    loop uses it to decide which chunks are worth buffering, and
    :func:`extract_context_tokens` uses it to read them back. Splitting that
    knowledge is how one side starts dropping what the other needs.
    """
    if not isinstance(chunk, dict):
        return None
    if chunk.get("parent_tool_use_id"):
        return None
    kind = chunk.get("type")
    if kind == "assistant":
        return _prompt_size(chunk.get("usage"))
    if kind == "stream_event":
        event = chunk.get("event")
        if not isinstance(event, dict) or event.get("type") != "message_start":
            return None
        message = event.get("message")
        return _prompt_size(message.get("usage") if isinstance(message, dict) else None)
    return None


def extract_context_tokens(chunks: list) -> Optional[int]:
    """Return the window-occupancy snapshot for this turn, or ``None``.

    This is deliberately NOT the turn's cumulative prompt total that
    :func:`extract_sdk_usage_detail` reports. An agentic turn re-sends the whole
    transcript on every tool round, so the cumulative ``input_tokens`` sums many
    overlapping prompts and sails past the context window on tool-heavy turns
    (a client dividing it by the window drew 263k/250k). What a "how full is
    this conversation" indicator needs is a *snapshot*: the prompt size of the
    LAST main-agent request, which is exactly what occupies the window going
    forward.

    Walks forward and keeps the latest snapshot rather than scanning backwards,
    because both shapes :func:`context_snapshot_tokens` accepts can appear in one
    turn and only chunk order says which request came last.

    ``ResultMessage.usage`` is useless here (it carries the same cumulative
    totals). Returns ``None`` for turns with no main-agent prompt at all — error
    -only turns, non-Claude backends, or the character-estimation fallback — so
    callers can report "unmeasured" instead of publishing a guess.
    """
    snapshot: Optional[int] = None
    for chunk in chunks:
        tokens = context_snapshot_tokens(chunk)
        if tokens is not None:
            snapshot = tokens
    return snapshot


def extract_model_id(chunks: list) -> Optional[str]:
    """Return the concrete model id the backend actually used, if reported.

    The public request model is an alias (``opus`` / ``sonnet`` / ``haiku``);
    the Claude CLI resolves it — honoring ``ANTHROPIC_DEFAULT_*_MODEL`` — and
    echoes the concrete id (e.g. ``claude-opus-4-5-20250929``) back on each
    ``AssistantMessage.model``. Logging that id is ground-truth and needs no
    alias-to-id mapping in the gateway.

    Only the primary turn's model is considered: assistant chunks carrying a
    ``parent_tool_use_id`` come from subagents (which may run a different
    model) and are skipped. Returns ``None`` when no chunk reports a model
    (non-Claude backends, error-only turns), so callers fall back to the alias.
    """
    for msg in chunks:
        if not isinstance(msg, dict) or msg.get("type") != "assistant":
            continue
        if msg.get("parent_tool_use_id"):
            continue
        model = msg.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None


class UsageLogger:
    """Async usage-log writer backed by a SQLAlchemy ``AsyncEngine``."""

    def __init__(self) -> None:
        self._engine: Optional[Any] = None  # AsyncEngine when connected
        self._lock = asyncio.Lock()
        self._disabled_reason: Optional[str] = None
        # In-flight detached write tasks (see _schedule_log_turn). Tracked so
        # they survive GC and can be drained on close().
        self._pending: "set[asyncio.Task[Any]]" = set()

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
        """Drain in-flight writes, then dispose the engine (idempotent)."""
        # Let detached writes finish so a graceful shutdown doesn't drop the
        # last turns. return_exceptions=True: a failed/cancelled write must not
        # abort the drain.
        pending = list(self._pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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

    @property
    def dialect(self) -> Optional[str]:
        """SQLAlchemy dialect name (e.g. ``"mysql"``, ``"sqlite"``) or ``None``.

        ``None`` when logging is disabled. Read-side queries branch on this to
        emit dialect-correct date/time SQL (see :mod:`src.usage_queries`).
        """
        engine = self._engine
        if engine is None:
            return None
        return engine.dialect.name

    async def fetch_rows(self, sql: str, params: tuple = ()) -> Optional[list]:
        """Execute a read-only SELECT and return ``list[dict]``.

        Returns ``None`` when the engine is not configured or the query fails -
        callers should treat that as "usage logging is not available".
        Intended only for admin-side analytics queries; callers must supply
        the full parameterised SQL (no automatic quoting).
        """
        engine = self._engine
        if engine is None:
            return None
        try:
            from sqlalchemy import text

            query, bound_params = _bind_positional_params(sql, params)
            async with engine.connect() as conn:
                result = await conn.execute(text(query), bound_params)
                return [dict(row) for row in result.mappings().all()]
        except Exception:
            logger.warning("usage-log read failed: %s", sql[:120], exc_info=True)
            return None

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
        except asyncio.CancelledError:
            # Cancellation (e.g. at shutdown drain) must propagate, not be
            # swallowed as a generic failure — cooperative cancellation relies
            # on it. Request cancellation never reaches here: the write runs on
            # a detached task (see _schedule_log_turn), not the request task.
            raise
        except Exception:
            logger.warning("usage-log write failed", exc_info=True)

    def _schedule_log_turn(
        self,
        *,
        turn: Dict[str, Any],
        tool_stats: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Run :meth:`log_turn` on a detached background task.

        The INSERT in :meth:`log_turn` runs under ``await``. Awaiting it inline
        in the request task means a client disconnect raises
        ``asyncio.CancelledError`` — a ``BaseException`` that the
        ``except Exception`` guard does **not** catch — straight through the
        in-flight commit, which can poison the pooled connection (and surface
        later as ``pymysql`` errors). Detaching the write onto its own task
        keeps request cancellation from reaching it, so logging is genuinely
        fire-and-forget as the module docstring promises.

        The task is tracked in ``self._pending`` (and removed on completion) so
        it is not garbage-collected mid-flight and can be drained by
        :meth:`close` on shutdown.
        """
        try:
            task = asyncio.create_task(self.log_turn(turn=turn, tool_stats=tool_stats))
        except RuntimeError:  # pragma: no cover - no running loop (non-async caller)
            logger.warning("usage-log skipped: no running event loop")
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

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

        Prometheus token counters are recorded unconditionally (before the
        early returns) so metrics work even when DB logging is disabled.
        """
        ctx = request_context or {}
        usage = extract_sdk_usage_detail(chunks)
        # Prefer the concrete model id the backend reported (resolves opus/
        # sonnet/haiku aliases via ANTHROPIC_DEFAULT_*_MODEL); fall back to the
        # public alias when no chunk carries one.
        logged_model = extract_model_id(chunks) or model or ctx.get("provider_model")

        try:
            from src.metrics import record_token_usage

            record_token_usage(
                backend=ctx.get("backend"),
                model=logged_model,
                usage=usage,
            )
        except Exception:  # pragma: no cover - metrics must never break the turn
            logger.debug("Prometheus token metrics recording failed", exc_info=True)

        if self._engine is None:
            return
        user = ctx.get("user") or ""
        if not user:
            return
        session_id = ctx.get("session_id") or ""
        turn = ctx.get("turn")
        if not session_id or turn is None or not response_id:
            return

        ts = current_db_timestamp()
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)

        # Detach the DB write so request cancellation can't interrupt the
        # in-flight commit (see ``_schedule_log_turn`` / module docstring).
        self._schedule_log_turn(
            turn={
                "ts": ts,
                "user": user,
                "session_id": session_id,
                "response_id": response_id,
                "previous_response_id": ctx.get("previous_response_id"),
                "turn": int(turn),
                "model": logged_model,
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
