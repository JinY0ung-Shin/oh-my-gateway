"""Dispatch ids and the admission ledger (``docs/zero-downtime-deploy.md`` §5.3).

A forwarded request carries ``x-gateway-dispatch-id: <target boot>.<issued ms>.<rand>``.
The owner records it **before** the request reaches the app (admission), so a
sender whose connection broke can ask whether its request was taken
(:meth:`Ledger.settle`) and resend only on a definite "no". Recording at the
start of the request is conservative: a request admitted but cut off while its
body was still arriving answers "received", which costs that request a
``forward_interrupted`` instead of a retry, never a second run.

Lifetime: ``admitted`` stays until the request is finished (however long the
turn runs); finished and ``refused`` entries stay ``DISPATCH_RETENTION``
seconds after they were written. A dispatch issued more than
``DISPATCH_ACCEPT_WINDOW`` seconds ago is never admitted, so a late copy is
refused even after its tombstone is gone. Everything runs on the event loop
with no await between a check and its write, so admission and settle of one id
cannot interleave.
"""

from __future__ import annotations

import secrets
import time
from typing import Dict, Literal, Optional, Tuple

from src import instance_registry
from src.env_utils import parse_int_env

HANDOFF_DEADLINE = parse_int_env("HANDOFF_DEADLINE_SECONDS", 10)
ACCEPT_WINDOW = parse_int_env("DISPATCH_ACCEPT_WINDOW_SECONDS", HANDOFF_DEADLINE)
RETENTION = max(
    parse_int_env("DISPATCH_RETENTION_SECONDS", 600), 3 * ACCEPT_WINDOW + 1
)
# Clocks of two colors on one host agree; this only absorbs rounding.
_FUTURE_SKEW_MS = 5_000

Verdict = Literal["received", "refused", "unknown"]


def new_dispatch_id(target_boot_id: str) -> str:
    return f"{target_boot_id}.{int(time.time() * 1000)}.{secrets.token_hex(16)}"


def parse_dispatch_id(dispatch_id: str) -> Optional[Tuple[str, int]]:
    parts = dispatch_id.split(".")
    if len(parts) != 3 or not parts[0] or len(parts[2]) != 32:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


class Ledger:
    def __init__(self) -> None:
        # id -> (state, written_at); written_at is None while admitted
        self._entries: Dict[str, Tuple[str, Optional[float]]] = {}

    def _gc(self, now: float) -> None:
        stale = [
            k
            for k, (_, t) in self._entries.items()
            if t is not None and now - t > RETENTION
        ]
        for k in stale:
            del self._entries[k]

    def admit(self, dispatch_id: str) -> bool:
        """Take *dispatch_id* in, or refuse it (and remember the refusal)."""
        now = time.time()
        self._gc(now)
        parsed = parse_dispatch_id(dispatch_id)
        if dispatch_id in self._entries:
            return False
        ok = (
            parsed is not None
            and parsed[0] == instance_registry.BOOT_ID
            and now * 1000 - parsed[1] <= ACCEPT_WINDOW * 1000
            and parsed[1] - now * 1000 <= _FUTURE_SKEW_MS
        )
        self._entries[dispatch_id] = ("admitted", None) if ok else ("refused", now)
        return ok

    def finish(self, dispatch_id: str, state: str = "completed") -> None:
        entry = self._entries.get(dispatch_id)
        if entry is not None and entry[0] == "admitted":
            self._entries[dispatch_id] = (state, time.time())

    def settle(self, dispatch_id: str) -> Verdict:
        now = time.time()
        self._gc(now)
        entry = self._entries.get(dispatch_id)
        if entry is not None:
            return "refused" if entry[0] == "refused" else "received"
        parsed = parse_dispatch_id(dispatch_id)
        if (
            parsed is not None
            and parsed[0] == instance_registry.BOOT_ID
            and now * 1000 - parsed[1] < RETENTION * 1000
            and parsed[1] - now * 1000 <= _FUTURE_SKEW_MS
        ):
            # Admitted ids stay until finished and RETENTION after: within it,
            # absent means never admitted, and the tombstone keeps it that way.
            self._entries[dispatch_id] = ("refused", now)
            return "refused"
        return "unknown"

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for state, _ in self._entries.values():
            out[state] = out.get(state, 0) + 1
        return out


ledger = Ledger()
