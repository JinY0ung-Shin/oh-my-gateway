"""Gateway-side uid mapping + spawn ticket (issue #241, PR-C).

Pure logic, no privilege: uid allocation/persistence and the HMAC ticket. The
privileged broker that consumes a ticket and switches uid is container-only
(docker/isolation_broker.py); its protocol is exercised same-uid in
test_isolation_broker.py.
"""

from __future__ import annotations

import time

import pytest

from src.backends.claude import uid_isolation as ui


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (
        ui.UID_MIN_ENV,
        ui.UID_MAX_ENV,
        ui.UID_REGISTRY_ENV,
        ui.EPHEMERAL_UIDS_ENV,
        ui.BROKER_UID_ENV,
        ui.TICKET_SECRET_ENV,
        "APP_UID",
    ):
        monkeypatch.delenv(var, raising=False)
    # Reset the module-level registry + ephemeral-pool caches between tests.
    monkeypatch.setattr(ui, "_registry", None)
    monkeypatch.setattr(ui, "_ephemeral_pool", None)


class TestUidRange:
    def test_defaults(self):
        assert ui.uid_range() == (ui._DEFAULT_UID_MIN, ui._DEFAULT_UID_MAX)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "30000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "30010")
        assert ui.uid_range() == (30000, 30010)

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "notanint")
        assert ui.uid_range()[0] == ui._DEFAULT_UID_MIN


class TestUidRegistry:
    def test_stable_and_distinct(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)
        a1 = reg.resolve("alice")
        b1 = reg.resolve("bob")
        a2 = reg.resolve("alice")
        assert a1 == a2  # stable per key
        assert a1 != b1  # distinct keys, distinct uids
        assert 20000 <= a1 <= 20005

    def test_lowest_free_allocation(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)
        assert reg.resolve("a") == 20000
        assert reg.resolve("b") == 20001

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "uids.json"
        reg = ui.UidRegistry(path, 20000, 20005)
        uid = reg.resolve("carol")
        reg2 = ui.UidRegistry(path, 20000, 20005)
        assert reg2.resolve("carol") == uid  # survived a "restart"
        # and a new key does not collide with the persisted one
        assert reg2.resolve("dave") != uid

    def test_exhaustion_refuses(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20001)
        reg.resolve("a")
        reg.resolve("b")
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("c")

    def test_resolve_uid_uses_env_range(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        monkeypatch.setenv(ui.UID_MIN_ENV, "25000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "25100")
        uid = ui.resolve_uid("someone")
        assert 25000 <= uid <= 25100


class TestReviewFixes:
    """Fixes from the PR #244 review: ephemeral recycling (#9), reserved-uid
    exclusion + band validation (#15), registry path, persist fail-closed."""

    def test_default_registry_path_is_data_volume(self):
        # /app/data (durable volume), never /app/src/data (code tree).
        p = ui._registry_path()
        assert p.name == "isolation_uids.json"
        assert p.parent.name == "data"
        assert p.parent.parent.name != "src"

    def test_ephemeral_pool_recycles_and_never_exhausts(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20010")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "3")
        pool = ui.get_ephemeral_pool()
        first = [pool.resolve(f"ws{i}") for i in range(3)]
        assert len(set(first)) == 3  # distinct while the ring has room
        # A 4th distinct key recycles the LRU rather than raising.
        fourth = pool.resolve("ws3")
        assert fourth == first[0]
        # A repeated key is stable while still in the ring.
        assert pool.resolve("ws3") == fourth

    def test_anonymous_runs_do_not_touch_the_registry(self, monkeypatch, tmp_path):
        reg_path = tmp_path / "uids.json"
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(reg_path))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20100")
        for i in range(50):
            ui.resolve_uid(f"anon{i}", persist=False)
        # Nothing persisted: the ephemeral path never writes the registry file.
        assert not reg_path.exists()

    def test_reserved_uids_excluded_from_allocation(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_UID", "20000")
        monkeypatch.setenv(ui.BROKER_UID_ENV, "20001")
        reg = ui.UidRegistry(tmp_path / "u.json", 20000, 20010, ui.reserved_uids())
        got = {reg.resolve(f"u{i}") for i in range(5)}
        assert 20000 not in got and 20001 not in got and 0 not in got

    def test_validate_bands_flags_root_and_empty(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "0")
        monkeypatch.setenv(ui.UID_MAX_ENV, "10")
        errs = ui.validate_bands()
        assert any("uid 0" in e or ">= 1" in e for e in errs)

        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "19999")
        assert any("empty" in e for e in ui.validate_bands())

    def test_validate_bands_ok_for_default(self):
        assert ui.validate_bands() == []

    def test_persist_failure_fails_closed(self, tmp_path, monkeypatch):
        # A registry whose parent cannot be created must refuse to allocate
        # rather than hand out a uid that will be lost on restart.
        target = tmp_path / "blocker"
        target.write_text("x")  # a file where a directory is needed
        reg = ui.UidRegistry(target / "uids.json", 20000, 20005)
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("alice")


class TestTicket:
    def test_mint_requires_secret(self):
        with pytest.raises(RuntimeError):
            ui.mint_ticket(uid=20000, workspace="/ws", ro=[], rw=[], env_allow=[])

    def test_roundtrip(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(
            uid=20001, workspace="/ws", ro=["/usr"], rw=["/ws"], env_allow=["PATH"]
        )
        payload = ui.verify_ticket(t)
        assert payload["uid"] == 20001
        assert payload["workspace"] == "/ws"
        assert payload["rw"] == ["/ws"]
        assert payload["env_allow"] == ["PATH"]

    def test_bad_signature_rejected(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[])
        sig, _, body = t.partition(".")
        tampered = sig + "." + body.replace('"uid":1', '"uid":0')
        with pytest.raises(ValueError):
            ui.verify_ticket(tampered)

    def test_wrong_secret_rejected(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[])
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "different")
        with pytest.raises(ValueError):
            ui.verify_ticket(t)

    def test_expiry_rejected(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[], ttl=1)
        real = time.time()
        monkeypatch.setattr(ui.time, "time", lambda: real + 100)
        with pytest.raises(ValueError):
            ui.verify_ticket(t)

    def test_single_use(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        t = ui.mint_ticket(uid=1, workspace="/ws", ro=[], rw=[], env_allow=[])
        seen: set = set()
        ui.verify_ticket(t, seen_nonces=seen)  # first use OK
        with pytest.raises(ValueError):
            ui.verify_ticket(t, seen_nonces=seen)  # replay rejected
