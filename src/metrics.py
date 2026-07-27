"""Prometheus metrics for gateway observability.

The gateway runs as a single uvicorn process (see ``run_server`` in
:mod:`src.main`), so the default ``prometheus_client`` registry is used —
no multiprocess mode is required.

Request metrics are recorded from ``RequestLoggingMiddleware`` in
:mod:`src.main` (the same chokepoint as the in-memory request logger);
token metrics are recorded from the usage-accounting path in
:mod:`src.usage_logger` via :func:`record_token_usage`.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Path grouping — raw URLs must never become label values (session IDs and
# other per-request path segments would explode label cardinality).
# ---------------------------------------------------------------------------

# Longest-prefix wins is NOT applied — entries are matched in order, so more
# specific prefixes must come first (``/v1/agents/messages`` before any
# hypothetical ``/v1/agents``).
_PATH_GROUP_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("/v1/responses", "responses"),
    ("/v1/agents/messages", "agent_messages"),
    ("/v1/sessions", "sessions"),
    ("/v1/models", "models"),
    ("/v1/auth", "auth"),
    ("/v1/mcp", "mcp"),
    ("/v1/debug", "debug"),
    ("/v1/messages", "sanitizer"),
    ("/files", "workspace_files"),
    ("/admin", "admin"),
    ("/health", "health"),
    ("/version", "version"),
    ("/metrics", "metrics"),
)


def path_group(path: str) -> str:
    """Collapse a request path into a bounded-cardinality group label."""
    if path == "/":
        return "root"
    for prefix, group in _PATH_GROUP_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return group
    return "other"


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

REQUESTS_TOTAL = Counter(
    "gateway_requests_total",
    "Total HTTP requests handled by the gateway.",
    ["path_group", "method", "status"],
)

REQUEST_LATENCY = Histogram(
    "gateway_request_latency_seconds",
    "HTTP request handler latency in seconds. For streaming responses this "
    "measures handler creation time only, not stream completion.",
    ["path_group", "method"],
)

RESPONSES_MODE_TOTAL = Counter(
    "gateway_responses_requests_total",
    "Responses API requests by streaming mode.",
    ["mode"],
)

TOKENS_TOTAL = Counter(
    "gateway_tokens_total",
    "Token usage per backend, model, and token kind.",
    ["backend", "model", "kind"],
)

TURNS_IN_FLIGHT = Gauge(
    "gateway_turns_in_flight",
    "Agent turns currently executing (admission-controlled by src.concurrency).",
)

TURNS_REJECTED_TOTAL = Counter(
    "gateway_turns_rejected_total",
    "Agent runs rejected with 503 because a concurrency limit was reached.",
    ["scope"],
)

LIVE_SESSIONS = Gauge(
    "gateway_live_sessions",
    "Sessions held in memory. Each pins a Claude CLI subprocess (~400 MB) for "
    "its whole TTL, so this — not turns_in_flight — tracks the memory ceiling.",
)

SESSIONS_REJECTED_TOTAL = Counter(
    "gateway_sessions_rejected_total",
    "Session creations refused because MAX_LIVE_SESSIONS was reached.",
)

STREAM_DURATION = Histogram(
    "gateway_stream_duration_seconds",
    "Wall-clock duration of a streaming agent response, first byte to last. "
    "gateway_request_latency_seconds only covers handler creation for these.",
    ["path_group"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800),
)

# Maps the metric ``kind`` label to the key used in usage dicts produced by
# ``src.usage_logger.extract_sdk_usage_detail``.
_TOKEN_KIND_KEYS: Tuple[Tuple[str, str], ...] = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_tokens"),
    ("cache_creation", "cache_creation_tokens"),
)


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------


def record_request(
    *,
    path: str,
    method: str,
    status_code: int,
    duration_seconds: float,
    streaming: Optional[bool] = None,
) -> None:
    """Record one handled HTTP request.

    ``streaming`` should be passed only for Responses API requests where the
    mode is known; ``None`` skips the streaming/non-streaming counter.
    """
    group = path_group(path)
    REQUESTS_TOTAL.labels(
        path_group=group, method=method, status=str(status_code)
    ).inc()
    REQUEST_LATENCY.labels(path_group=group, method=method).observe(duration_seconds)
    if streaming is not None:
        mode = "streaming" if streaming else "non_streaming"
        RESPONSES_MODE_TOTAL.labels(mode=mode).inc()


def record_token_usage(
    *,
    backend: Optional[str],
    model: Optional[str],
    usage: Dict[str, int],
) -> None:
    """Record per-turn token usage.

    ``usage`` uses the ``extract_sdk_usage_detail`` key shape:
    ``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` /
    ``cache_creation_tokens``.
    """
    backend_label = backend or "unknown"
    model_label = model or "unknown"
    for kind, key in _TOKEN_KIND_KEYS:
        amount = int(usage.get(key, 0) or 0)
        if amount > 0:
            TOKENS_TOTAL.labels(
                backend=backend_label, model=model_label, kind=kind
            ).inc(amount)


def set_turns_in_flight(value: int) -> None:
    """Publish the current in-flight turn count."""
    TURNS_IN_FLIGHT.set(value)


def record_turn_rejected(scope: str) -> None:
    """Record one agent run refused by admission control.

    *scope* is ``"global"`` or ``"per_user"`` — a bounded label set.
    """
    TURNS_REJECTED_TOTAL.labels(scope=scope).inc()


def bind_live_sessions_source(source: Callable[[], float]) -> None:
    """Have ``gateway_live_sessions`` read *source* at scrape time.

    A pull-based gauge cannot drift: sessions are added and removed from
    several paths (create, delete, expiry sweep, shutdown) and any missed
    call site would leave a wrong value published indefinitely.
    """
    LIVE_SESSIONS.set_function(source)


def record_session_rejected() -> None:
    """Record one session creation refused by ``MAX_LIVE_SESSIONS``."""
    SESSIONS_REJECTED_TOTAL.inc()


def record_stream_duration(*, path: str, duration_seconds: float) -> None:
    """Record end-to-end duration of a streaming response."""
    STREAM_DURATION.labels(path_group=path_group(path)).observe(duration_seconds)


def render_latest() -> Tuple[bytes, str]:
    """Return the exposition payload and content type for ``GET /metrics``."""
    return generate_latest(), CONTENT_TYPE_LATEST
