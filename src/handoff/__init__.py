"""Blue/green session hand-off between gateway instances (``docs/zero-downtime-deploy.md``).

Inactive unless ``GATEWAY_INSTANCE`` is set; a single-container deploy runs
exactly as before.
"""

from src.handoff.owners import SessionLock, SessionOwnedElsewhere, owner_locks

__all__ = ["SessionLock", "SessionOwnedElsewhere", "owner_locks", "admission_closed"]


def admission_closed() -> bool:
    """True while this instance drains: it opens no session (new or rehydrated)."""
    from src.handoff.drain import drain_state

    return drain_state.draining
