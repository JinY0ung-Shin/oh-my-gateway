"""In-memory request logger for admin observability.

Captures API request metadata in a bounded circular buffer.  Provides
query/filter, summary statistics, and approximate rate-limit snapshot
derived from logged requests (does **not** touch the ``slowapi`` layer).

All public methods are thread-safe via a ``threading.Lock``.
"""

import math
import time
from collections import deque
from dataclasses import dataclass, asdict
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.constants import RATE_LIMITS

# ---------------------------------------------------------------------------
# Path  rate-limit bucket mapping
# ---------------------------------------------------------------------------
# ``RATE_LIMITS`` uses logical bucket names (chat, responses, ...) while
# request paths are raw URLs.  This mapping collapses URLs into the correct
# bucket so approximate rate-limit monitoring can compare against configured
# limits.

# Maps request paths to rate-limit bucket names.  Only paths that are
# **actually decorated** with ``@rate_limit_endpoint(...)`` should appear
# here.  Undecorated endpoints (e.g. ``/v1/models``, ``/v1/compatibility``)
# are intentionally absent so the monitoring view doesn't overreport.
_PATH_BUCKET_MAP: Dict[str, str] = {
    "/v1/chat/completions": "chat",
    "/v1/messages": "chat",
    "/v1/responses": "responses",
    "/health": "health",
    "/version": "health",
    "/v1/mcp/servers": "general",
    "/v1/auth/status": "auth",
    "/v1/sessions": "session",
    "/v1/debug/request": "debug",
}


def _bucket_for_path(path: str) -> Optional[str]:
    """Return the rate-limit bucket name for *path*.

    Returns ``None`` for paths that are **not** rate-limited, so the
    monitoring view does not overreport usage on unthrottled endpoints.
    """
    # Exact match first
    if path in _PATH_BUCKET_MAP:
        return _PATH_BUCKET_MAP[path]
    # Prefix match for parameterised paths (e.g. /v1/sessions/{id})
    for prefix, bucket in _PATH_BUCKET_MAP.items():
        if path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return bucket
    return None


# ---------------------------------------------------------------------------
# Log entry dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestLogEntry:
    """Single request log record."""

    timestamp: float  # time.time()
    method: str
    path: str
    status_code: int
    response_time_ms: float
    client_ip: str
    model: Optional[str] = None
    backend: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["bucket"] = _bucket_for_path(self.path) or "untracked"
        return d


# ---------------------------------------------------------------------------
# Default exclusion prefixes (admin polling should not pollute logs)
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDE_PREFIXES: Tuple[str, ...] = (
    "/admin/api/",
    "/admin",
    "/docs",
    "/openapi.json",
    "/favicon.ico",
)


# ---------------------------------------------------------------------------
# RequestLogger
# ---------------------------------------------------------------------------


class RequestLogger:
    """Thread-safe bounded in-memory request log."""

    def __init__(
        self,
        maxlen: int = 1000,
        exclude_prefixes: Optional[Sequence[str]] = None,
    ) -> None:
        self._buffer: deque[RequestLogEntry] = deque(maxlen=maxlen)
        self._lock = Lock()
        self._exclude_prefixes = tuple(exclude_prefixes or DEFAULT_EXCLUDE_PREFIXES)
        self._total_logged: int = 0

    # ----- write -----

    def should_log(self, path: str) -> bool:
        """Return ``True`` when *path* should be recorded."""
        for prefix in self._exclude_prefixes:
            if path == prefix or path.startswith(prefix):
                return False  # excluded
        return True

    def log(self, entry: RequestLogEntry) -> None:
        """Append an entry to the buffer."""
        with self._lock:
            self._buffer.append(entry)
            self._total_logged += 1

    # ----- read -----

    def query(
        self,
        *,
        endpoint: Optional[str] = None,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        backend: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return filtered log entries plus summary stats.

        *status* accepts an exact code (``"200"``) or a class prefix
        (``"4xx"``, ``"5xx"``) for range filtering.

        Returns ``{items, total, stats}``.
        """
        with self._lock:
            snapshot = list(self._buffer)

        # Filter
        filtered = snapshot
        if endpoint:
            filtered = [e for e in filtered if endpoint in e.path]
        if session_id is not None:
            filtered = [e for e in filtered if e.session_id == session_id]
        if backend is not None:
            filtered = [e for e in filtered if e.backend == backend]
        if status is not None:
            status_str = str(status)
            if status_str.endswith("xx"):
                # Range filter: "4xx" matches 400-499, "5xx" matches 500-599
                try:
                    prefix = int(status_str[0])
                    filtered = [e for e in filtered if e.status_code // 100 == prefix]
                except (ValueError, IndexError):
                    pass  # Malformed range like "axx" — ignore filter
            else:
                try:
                    exact = int(status_str)
                    filtered = [e for e in filtered if e.status_code == exact]
                except ValueError:
                    pass  # Invalid filter — return unfiltered

        total = len(filtered)

        # Summary stats over the *filtered* set
        stats = self._compute_stats(filtered)

        # Paginate (newest first)
        filtered.reverse()
        page = filtered[offset : offset + limit]

        return {
            "items": [e.to_dict() for e in page],
            "total": total,
            "stats": stats,
            "total_logged": self._total_logged,
        }

    def _compute_stats(self, entries: List[RequestLogEntry]) -> Dict[str, Any]:
        """Compute summary statistics over *entries*."""
        if not entries:
            return {
                "total_requests": 0,
                "error_count": 0,
                "error_rate": 0.0,
                "avg_latency_ms": 0.0,
                "p50_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
                "p99_latency_ms": 0.0,
            }

        latencies = sorted(e.response_time_ms for e in entries)
        error_count = sum(1 for e in entries if e.status_code >= 400)

        # For p95: use ceiling to pick from the upper tail, not the lower
        p50_idx = min(len(latencies) - 1, math.ceil(len(latencies) * 0.50) - 1)
        p95_idx = min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1)
        p99_idx = min(len(latencies) - 1, math.ceil(len(latencies) * 0.99) - 1)
        return {
            "total_requests": len(entries),
            "error_count": error_count,
            "error_rate": round(error_count / len(entries), 4),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2),
            "p50_latency_ms": round(latencies[p50_idx], 2),
            "p95_latency_ms": round(latencies[p95_idx], 2),
            "p99_latency_ms": round(latencies[p99_idx], 2),
        }

    # ----- rate-limit snapshot (Feature 2) -----

    def get_rate_limit_snapshot(self, window_seconds: int = 60) -> Dict[str, Any]:
        """Approximate rate-limit usage derived from logged requests.

        Groups requests in the last *window_seconds* by bucket and client IP,
        then compares counts against configured limits in ``RATE_LIMITS``.

        This is **approximate** monitoring; actual enforcement stays in slowapi.
        """
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [e for e in self._buffer if e.timestamp >= cutoff]

        # Group by bucket -> IP -> count (skip untracked endpoints)
        buckets: Dict[str, Dict[str, int]] = {}
        for entry in recent:
            bucket = _bucket_for_path(entry.path)
            if bucket is None:
                continue  # Not rate-limited — don't count
            ip = entry.client_ip
            buckets.setdefault(bucket, {})
            buckets[bucket][ip] = buckets[bucket].get(ip, 0) + 1

        result: Dict[str, Any] = {}
        for bucket_name, configured_limit in RATE_LIMITS.items():
            ip_counts = buckets.get(bucket_name, {})
            total_usage = sum(ip_counts.values())
            # Top 5 consumers
            top = sorted(ip_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            clients = [
                {
                    "ip": ip,
                    "count": count,
                    "pct_used": round(count / configured_limit * 100, 1) if configured_limit else 0,
                }
                for ip, count in top
            ]
            result[bucket_name] = {
                "limit": configured_limit,
                "window": "1m",
                "total_usage": total_usage,
                "clients": clients,
            }

        return result


# ---------------------------------------------------------------------------
# Global instance
# ---------------------------------------------------------------------------

request_logger = RequestLogger()
