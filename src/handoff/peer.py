"""Signed instance-to-instance HTTP (``/internal/…``) and request forwarding headers.

Every call one color makes to another carries ``x-gateway-forwarded`` (the
sender's name), ``x-gateway-dispatch-id`` and ``x-gateway-signature`` — an HMAC
over sender, id, method and path with the secret both colors read from
``data/instance_secret``. The HMAC authenticates the caller; it is not an
idempotency key (the dispatch id and the ledger are).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from src import instance_registry
from src.handoff.ledger import new_dispatch_id, parse_dispatch_id

FORWARDED = "x-gateway-forwarded"
DISPATCH_ID = "x-gateway-dispatch-id"
SIGNATURE = "x-gateway-signature"
REROUTE = "x-gateway-reroute"
_OURS = {FORWARDED.encode(), DISPATCH_ID.encode(), SIGNATURE.encode()}
# Internal calls are fresh by construction; a signature older than this is refused.
_INTERNAL_MAX_AGE_MS = 60_000


def sign(sender: str, dispatch_id: str, method: str, target: str) -> str:
    message = "\n".join((sender, dispatch_id, method.upper(), target)).encode()
    return hmac.new(instance_registry.shared_secret(), message, hashlib.sha256).hexdigest()


def signed_headers(target_boot_id: str, method: str, target: str) -> Dict[str, str]:
    sender = instance_registry.instance_name()
    dispatch_id = new_dispatch_id(target_boot_id)
    return {
        FORWARDED: sender,
        DISPATCH_ID: dispatch_id,
        SIGNATURE: sign(sender, dispatch_id, method, target),
    }


def request_target(scope) -> str:
    """What a signature covers: the decoded path (the query is left out — an HTTP
    client may re-encode it, and nothing it carries grants access)."""
    return scope["path"]


def verify(scope, headers: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """``(sender, dispatch_id)`` when the request is a validly signed call from a
    live peer instance, else None."""
    sender = headers.get(FORWARDED, "")
    dispatch_id = headers.get(DISPATCH_ID, "")
    signature = headers.get(SIGNATURE, "")
    if not (sender and dispatch_id and signature):
        return None
    if sender == instance_registry.instance_name():
        return None
    expected = sign(sender, dispatch_id, scope["method"], request_target(scope))
    if not hmac.compare_digest(expected, signature):
        return None
    return sender, dispatch_id


def fresh_internal(dispatch_id: str) -> bool:
    parsed = parse_dispatch_id(dispatch_id)
    if parsed is None or parsed[0] != instance_registry.BOOT_ID:
        return False
    return abs(time.time() * 1000 - parsed[1]) <= _INTERNAL_MAX_AGE_MS


def strip_ours(raw_headers: Iterable[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    """Drop forwarding headers a client sent itself (only a peer may set them)."""
    return [(k, v) for k, v in raw_headers if k.lower() not in _OURS]


async def call(
    instance: instance_registry.Instance,
    method: str,
    path: str,
    *,
    json: Optional[dict] = None,
    timeout: float = 5.0,
) -> httpx.Response:
    """One signed internal call to *instance* (``/internal/…``)."""
    headers = signed_headers(instance.boot_id, method, path)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        return await client.request(method, instance.url + path, json=json, headers=headers)
