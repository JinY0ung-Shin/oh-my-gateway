"""Best-effort upstream model discovery for the Claude gateway backend.

The gateway's public ``/v1/models`` surface should reflect models exposed by an
Anthropic-compatible upstream such as LiteLLM without making model listing depend
on upstream availability. A short TTL cache limits traffic; refresh failures use
the last successful snapshot and otherwise fall back to the descriptor's static
aliases.

Discovery is opt-in: it stays off until an operator sets
``MODEL_DISCOVERY_ENABLED=true``, so merely configuring an upstream never
changes which models a deployment advertises.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from src.env_utils import parse_bool_env, parse_float_env

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 60.0
_DEFAULT_TIMEOUT_SECONDS = 3.0
# A failed refresh must not make every concurrent /v1/models reader pay the
# upstream timeout. Retry quickly, but cap the failure backoff so recovery is
# noticed well before a long successful-cache TTL would expire.
_FAILURE_BACKOFF_MAX_SECONDS = 10.0


@dataclass
class _DiscoveryCache:
    source: str = ""
    model_ids: tuple[str, ...] = ()
    expires_at: float = 0.0


_cache = _DiscoveryCache()
_cache_lock: Optional[asyncio.Lock] = None


def _upstream_base_url() -> str:
    return (os.getenv("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")


def discovery_enabled() -> bool:
    """Whether upstream model discovery may run at all.

    Off by default: configuring ``ANTHROPIC_BASE_URL`` alone must not widen
    the advertised model list, so an operator opts in explicitly. Read per
    call, never cached, so flipping the switch takes effect on the next
    request rather than requiring the module to be re-imported. Both public
    entry points below check it, so turning discovery off stops the upstream
    fetch *and* revokes already-discovered IDs from resolution — a snapshot
    cached before the switch flipped must not keep routing traffic.
    """
    return parse_bool_env("MODEL_DISCOVERY_ENABLED", "false")


def _positive_float_env(name: str, default: float) -> float:
    """Read a positive float using the repository-wide env parser."""
    value = parse_float_env(name, default)
    if value <= 0:
        logger.warning("Invalid %s=%r; using %.1f", name, os.getenv(name), default)
        return default
    return value


def _request_headers() -> Dict[str, str]:
    """Build auth/custom headers using Claude Code discovery precedence."""
    headers = {"anthropic-version": "2023-06-01"}

    # Claude Code's gateway discovery sends exactly one credential header:
    # ANTHROPIC_AUTH_TOKEN wins, otherwise the API key is sent as x-api-key.
    token = (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip()
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    elif api_key:
        headers["x-api-key"] = api_key

    # Claude Code accepts custom headers as newline-separated ``Name: value``
    # entries. Reuse the same operator configuration for discovery, ignoring
    # malformed lines rather than making /v1/models fail.
    custom = os.getenv("ANTHROPIC_CUSTOM_HEADERS") or ""
    for line in custom.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip() and value.strip():
            headers[name.strip()] = value.strip()

    return headers


def _make_client() -> httpx.AsyncClient:
    timeout = _positive_float_env(
        "MODEL_DISCOVERY_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS
    )
    # Keep the sanitizer import local: src.backends.claude.__init__ is loaded
    # very early and deliberately avoids eager imports that can form cycles.
    # Discovery and the sanitizer target the same upstream, so they share its
    # TLS trust policy even when the sanitizer route itself is disabled.
    from src.sanitizer.config import get_tls_verify

    # httpx does not follow redirects by default. Keep it that way so a bearer
    # credential is never forwarded to a redirect target.
    return httpx.AsyncClient(timeout=timeout, verify=get_tls_verify())


def _parse_model_ids(payload: object) -> List[str]:
    if not isinstance(payload, dict):
        raise ValueError("model discovery response must be a JSON object")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("model discovery response is missing a data list")

    out: List[str] = []
    seen: set[str] = set()
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else row
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
    return out


def discovered_model_ids() -> frozenset[str]:
    """Return the cached IDs for the currently configured upstream only."""
    if not discovery_enabled():
        return frozenset()
    source = _upstream_base_url()
    if not source or _cache.source != source:
        return frozenset()
    return frozenset(_cache.model_ids)


def _lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


def _failure_backoff_seconds() -> float:
    ttl = _positive_float_env("MODEL_DISCOVERY_TTL_SECONDS", _DEFAULT_TTL_SECONDS)
    return min(ttl, _FAILURE_BACKOFF_MAX_SECONDS)


async def discover_models() -> List[str]:
    """Return cached/live upstream model IDs without propagating fetch errors."""
    global _cache

    if not discovery_enabled():
        return []

    source = _upstream_base_url()
    if not source:
        return []

    now = time.monotonic()
    if _cache.source == source and now < _cache.expires_at:
        return list(_cache.model_ids)

    async with _lock():
        now = time.monotonic()
        if _cache.source == source and now < _cache.expires_at:
            return list(_cache.model_ids)

        stale = list(_cache.model_ids) if _cache.source == source else []
        client: Optional[httpx.AsyncClient] = None
        try:
            client = _make_client()
            # Claude Code's gateway discovery uses the Anthropic endpoint's
            # maximum page size. We intentionally do not paginate beyond 1000:
            # that is already far above realistic coding-gateway deployments.
            response = await client.get(
                f"{source}/v1/models?limit=1000",
                headers=_request_headers(),
            )
            response.raise_for_status()
            model_ids = _parse_model_ids(response.json())
        except Exception as exc:
            # Negative-cache the failure as well as retaining the same-source
            # stale snapshot. Without this, N concurrent readers serialize on
            # the lock and each pay the full upstream timeout while it is down.
            _cache = _DiscoveryCache(
                source=source,
                model_ids=tuple(stale),
                expires_at=time.monotonic() + _failure_backoff_seconds(),
            )
            logger.warning(
                "upstream model discovery failed; using %s model snapshot: %s",
                "stale" if stale else "static",
                exc,
            )
            return stale
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.debug("failed to close model discovery client", exc_info=True)

        ttl = _positive_float_env("MODEL_DISCOVERY_TTL_SECONDS", _DEFAULT_TTL_SECONDS)
        _cache = _DiscoveryCache(
            source=source,
            model_ids=tuple(model_ids),
            expires_at=time.monotonic() + ttl,
        )
        return model_ids


def _reset_cache_for_tests() -> None:
    global _cache, _cache_lock
    _cache = _DiscoveryCache()
    _cache_lock = None
