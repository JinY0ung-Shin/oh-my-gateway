"""Units of the blue/green hand-off (``src/handoff``): ledger lifecycle, poisoned
session locks, cross-process owner locks, ``release_for_handoff`` and the
drain admission fence."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import pytest

from src import instance_registry
from src.handoff import ledger as ledger_mod
from src.handoff.ledger import Ledger, new_dispatch_id
from src.handoff.owners import OwnerLocks, SessionLock, SessionOwnedElsewhere


@pytest.fixture
def blue(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_INSTANCE", "blue")
    monkeypatch.setenv("GATEWAY_INSTANCE_DIR", str(tmp_path / "data" / "instances"))
    return tmp_path


class FakeClock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(ledger_mod.time, "time", c)
    return c


# --------------------------------------------------------------------- ledger


def test_admitted_dispatch_is_never_forgotten_while_running(clock):
    """#265 4th review: a long turn outlives every window; settle must still say received."""
    led = Ledger()
    k = new_dispatch_id(instance_registry.BOOT_ID)
    assert led.admit(k)
    clock.now += ledger_mod.RETENTION + ledger_mod.ACCEPT_WINDOW + 3600
    assert led.settle(k) == "received"
    led.finish(k)
    clock.now += ledger_mod.RETENTION - 1
    assert led.settle(k) == "received"
    clock.now += 2
    assert led.settle(k) == "unknown"  # no basis left: never "refused"


def test_settle_refuses_only_with_proof_and_fences_the_late_copy(clock):
    led = Ledger()
    k = new_dispatch_id(instance_registry.BOOT_ID)
    assert led.settle(k) == "refused"
    assert led.admit(k) is False  # tombstone
    led2 = Ledger()
    k2 = new_dispatch_id(instance_registry.BOOT_ID)
    clock.now += ledger_mod.ACCEPT_WINDOW + 1
    assert led2.admit(k2) is False  # late even without a tombstone


def test_settle_unknown_for_another_boot_or_beyond_retention(clock):
    led = Ledger()
    other_boot = new_dispatch_id("f" * 32)
    assert led.settle(other_boot) == "unknown"
    k = new_dispatch_id(instance_registry.BOOT_ID)
    clock.now += ledger_mod.RETENTION + 1
    assert led.settle(k) == "unknown"


def test_admit_refuses_replays_and_foreign_boots(clock):
    led = Ledger()
    k = new_dispatch_id(instance_registry.BOOT_ID)
    assert led.admit(k)
    assert led.admit(k) is False
    assert led.admit(new_dispatch_id("e" * 32)) is False
    assert led.admit("garbage") is False


def test_retention_exceeds_accept_window():
    assert ledger_mod.RETENTION > ledger_mod.ACCEPT_WINDOW


# ---------------------------------------------------------------- session lock


async def test_poisoned_lock_reroutes_current_waiters():
    lock = SessionLock()
    await lock.acquire()
    waiter = asyncio.ensure_future(lock.acquire())
    await asyncio.sleep(0)
    lock.poison()
    lock.release()
    with pytest.raises(SessionOwnedElsewhere) as info:
        await waiter
    assert info.value.status_code == 421
    assert info.value.headers["x-gateway-reroute"] == "1"
    assert not lock.locked()
    with pytest.raises(SessionOwnedElsewhere):
        async with lock:
            pass


# ----------------------------------------------------------------- owner locks


def test_owner_lock_spans_processes(blue):
    locks = OwnerLocks()
    sid = "11111111-2222-3333-4444-555555555555"
    token = object()
    assert locks.acquire(sid, token)
    assert locks.owner(sid) == "blue"
    code = (
        "import os,sys;sys.path.insert(0,'.');"
        "from src.handoff.owners import OwnerLocks;l=OwnerLocks();"
        f"print(l.acquire({sid!r}, object()), l.owner({sid!r}))"
    )
    env = {**__import__("os").environ, "GATEWAY_INSTANCE": "green", "GATEWAY_SKIP_DOTENV": "1"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["False", "blue"], out.stderr
    locks.release(sid, token)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.stdout.split() == ["True", "green"], out.stderr


def test_two_incarnations_share_one_lock(blue):
    locks = OwnerLocks()
    sid = "aaaaaaaa-2222-3333-4444-555555555555"
    old, new = object(), object()
    assert locks.acquire(sid, old)
    assert locks.acquire(sid, new)
    locks.release(sid, old)
    assert locks.holds(sid)
    locks.release(sid, new)
    assert not locks.holds(sid)
    assert locks.owner(sid) is None


def test_owner_locks_are_noops_without_blue_green(monkeypatch):
    monkeypatch.delenv("GATEWAY_INSTANCE", raising=False)
    locks = OwnerLocks()
    assert locks.acquire("x", object()) is True
    assert locks.owner("x") is None


# -------------------------------------------------------- session manager hooks


async def test_release_for_handoff_states(blue):
    from src.session_manager import SessionManager

    sm = SessionManager()
    s = sm.get_or_create_session("bbbbbbbb-2222-3333-4444-555555555555")
    assert await sm.release_for_handoff("nope") == "absent"
    s.active_response_id = "resp"
    assert await sm.release_for_handoff(s.session_id) == "busy"
    s.active_response_id = None
    await s.lock.acquire()
    assert await sm.release_for_handoff(s.session_id) == "busy"
    s.lock.release()
    s.pending_tool_call = {"call_id": "c"}
    assert await sm.release_for_handoff(s.session_id) == "busy"
    s.pending_tool_call = None
    assert await sm.release_for_handoff(s.session_id) == "released"
    assert s.session_id not in sm.sessions
    assert s.lock.poisoned
    from src.handoff.owners import owner_locks

    assert owner_locks.owner(s.session_id) is None


async def test_draining_instance_admits_no_session(blue, monkeypatch):
    from src.handoff.drain import drain_state
    from src.session_manager import SessionManager

    monkeypatch.setattr(drain_state, "target", instance_registry.Instance("green", "b" * 32, "http://x"))
    sm = SessionManager()
    with pytest.raises(SessionOwnedElsewhere):
        sm.get_or_create_session("cccccccc-2222-3333-4444-555555555555")
    assert sm.sessions == {}
