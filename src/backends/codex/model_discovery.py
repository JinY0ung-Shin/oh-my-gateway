"""Best-effort model discovery for the Codex backend.

Mirrors the Claude backend's discovery contract: opt-in, TTL-cached, and
never able to make ``/v1/models`` fail. The upstream here is the Codex
app-server's own ``model/list`` (served through the openai-codex SDK), so a
refresh spawns a short-lived SDK client; the TTL keeps that rare.

Discovery is opt-in via ``CODEX_MODEL_DISCOVERY_ENABLED=true`` so merely
enabling the codex backend never changes which models a deployment
advertises beyond the static ``CODEX_MODELS`` list.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import List, Optional

from src.env_utils import parse_bool_env, parse_float_env

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 300.0
# A failed refresh must not make every concurrent /v1/models reader pay the
# app-server spawn cost. Retry quickly, but cap the failure backoff so
# recovery is noticed well before a long successful-cache TTL would expire.
_FAILURE_BACKOFF_MAX_SECONDS = 30.0


@dataclass
class _DiscoveryCache:
    model_ids: tuple[str, ...] = ()
    expires_at: float = 0.0
    populated: bool = False


_cache = _DiscoveryCache()
_cache_lock: Optional[asyncio.Lock] = None


def discovery_enabled() -> bool:
    """Whether Codex model discovery may run at all (read per call)."""
    return parse_bool_env("CODEX_MODEL_DISCOVERY_ENABLED", "false")


def _positive_float_env(name: str, default: float) -> float:
    value = parse_float_env(name, default)
    if not math.isfinite(value) or value <= 0:
        logger.warning("Invalid %s=%r; using %.1f", name, os.getenv(name), default)
        return default
    return value


def _ttl_seconds() -> float:
    return _positive_float_env(
        "CODEX_MODEL_DISCOVERY_TTL_SECONDS", _DEFAULT_TTL_SECONDS
    )


def _failure_backoff_seconds() -> float:
    return min(_ttl_seconds(), _FAILURE_BACKOFF_MAX_SECONDS)


def _lock() -> asyncio.Lock:
    global _cache_lock
    if _cache_lock is None:
        _cache_lock = asyncio.Lock()
    return _cache_lock


async def _fetch_model_ids() -> List[str]:
    """List model IDs from a short-lived SDK client, prefixed for the gateway."""
    from src.backends.codex.client import CodexClient

    client = CodexClient()
    codex = client._new_codex()
    try:
        await client._start_codex(codex)
        payload = await codex._client.model_list()
        out: List[str] = []
        seen: set[str] = set()
        for row in payload.data or []:
            model_id = getattr(row, "id", None)
            if not isinstance(model_id, str):
                continue
            model_id = model_id.strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            out.append(f"codex/{model_id}")
        return out
    finally:
        try:
            await codex.close()
        except Exception:
            logger.debug("failed to close codex discovery client", exc_info=True)


def discovered_model_ids() -> frozenset[str]:
    """Return the cached public IDs (``codex/<id>``) for sync resolution."""
    if not discovery_enabled():
        return frozenset()
    return frozenset(_cache.model_ids)


async def discover_models() -> List[str]:
    """Return cached/live Codex model IDs without propagating fetch errors."""
    global _cache

    if not discovery_enabled():
        return []

    now = time.monotonic()
    if _cache.populated and now < _cache.expires_at:
        return list(_cache.model_ids)

    async with _lock():
        now = time.monotonic()
        if _cache.populated and now < _cache.expires_at:
            return list(_cache.model_ids)

        stale = list(_cache.model_ids)
        try:
            model_ids = await _fetch_model_ids()
        except Exception as exc:
            # Negative-cache the failure and keep the stale snapshot so
            # concurrent readers don't serialize on repeated spawn failures.
            _cache = _DiscoveryCache(
                model_ids=tuple(stale),
                expires_at=time.monotonic() + _failure_backoff_seconds(),
                populated=True,
            )
            logger.warning(
                "Codex model discovery failed; using %s model snapshot: %s",
                "stale" if stale else "static",
                exc,
            )
            return stale

        _cache = _DiscoveryCache(
            model_ids=tuple(model_ids),
            expires_at=time.monotonic() + _ttl_seconds(),
            populated=True,
        )
        return model_ids


def _reset_cache_for_tests() -> None:
    global _cache, _cache_lock
    _cache = _DiscoveryCache()
    _cache_lock = None
