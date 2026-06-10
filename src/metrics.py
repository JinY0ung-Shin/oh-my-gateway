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

from typing import Dict, Optional, Tuple

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Path grouping — raw URLs must never become label values (session IDs and
# other per-request path segments would explode label cardinality).
# ---------------------------------------------------------------------------

_PATH_GROUP_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("/v1/responses", "responses"),
    ("/v1/sessions", "sessions"),
    ("/v1/models", "models"),
    ("/v1/auth", "auth"),
    ("/v1/mcp", "mcp"),
    ("/v1/debug", "debug"),
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


def render_latest() -> Tuple[bytes, str]:
    """Return the exposition payload and content type for ``GET /metrics``."""
    return generate_latest(), CONTENT_TYPE_LATEST
