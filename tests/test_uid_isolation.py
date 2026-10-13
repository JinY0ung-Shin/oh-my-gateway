"""Gateway-side uid mapping + spawn ticket (issue #241, PR-C).

Pure logic, no privilege: uid allocation/persistence and the HMAC ticket. The
privileged broker that consumes a ticket and switches uid is container-only
(docker/isolation_broker.py); its protocol is exercised same-uid in
test_isolation_broker.py.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
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

    def test_ephemeral_pool_recycles_and_never_exhausts(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
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
        # A registry that cannot be written must refuse to allocate rather than
        # hand out a uid that will be lost on restart.
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)

        def _boom(*_a, **_k):
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as m:
            m.setattr(ui.os, "replace", _boom)
            with pytest.raises(ui.UidAllocationError):
                reg.resolve("alice")
        # the in-memory grant was rolled back, so a retry may try again
        assert reg.resolve("alice") == 20000

    def test_unusable_registry_path_fails_closed(self, tmp_path):
        # A registry path that cannot be read (here: its parent is a file) is
        # refused at load, not treated as an empty registry.
        target = tmp_path / "blocker"
        target.write_text("x")  # a file where a directory is needed
        with pytest.raises(ui.UidAllocationError):
            ui.UidRegistry(target / "uids.json", 20000, 20005)


class TestRegistryFailClosed:
    """PR #244 review 2 (#8): a damaged registry must never load as empty."""

    def test_missing_file_is_an_empty_registry(self, tmp_path):
        reg = ui.UidRegistry(tmp_path / "uids.json", 20000, 20005)
        assert reg.resolve("alice") == 20000

    @pytest.mark.parametrize("content", ['{"alice": 20000', "[1, 2]", "\udcff"])
    def test_corrupt_or_non_object_registry_refuses(self, tmp_path, content):
        path = tmp_path / "uids.json"
        path.write_bytes(content.encode("utf-8", "surrogateescape"))
        with pytest.raises(ui.UidAllocationError):
            ui.UidRegistry(path, 20000, 20005)
        # the file is left untouched for the operator to restore
        assert path.read_bytes() == content.encode("utf-8", "surrogateescape")

    def test_unreadable_registry_refuses(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root reads a 000 file")
        path = tmp_path / "uids.json"
        path.write_text('{"alice": 20000}')
        path.chmod(0)
        try:
            with pytest.raises(ui.UidAllocationError):
                ui.UidRegistry(path, 20000, 20005)
        finally:
            path.chmod(0o600)

    def test_registry_errors_reports_a_corrupt_file(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text("{truncated")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        errs = ui.registry_errors()
        assert len(errs) == 1 and "corrupt" in errs[0]
        # and a named user's spawn refuses instead of getting a fresh uid
        with pytest.raises(ui.UidAllocationError):
            ui.resolve_uid("alice")

    def test_registry_errors_ok_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        assert ui.registry_errors() == []

    def test_out_of_band_entry_is_held_not_reassigned(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 31000, "bob": 20001}))
        reg = ui.UidRegistry(path, 20000, 20005)
        assert reg.resolve("bob") == 20001  # honored
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("alice")  # held: no silently different uid
        # a new user allocates, and the held entry survives the rewrite
        assert reg.resolve("carol") == 20000
        on_disk = json.loads(path.read_text())
        assert on_disk == {"alice": 31000, "bob": 20001, "carol": 20000}

    def test_shared_uid_is_held_for_both_keys_and_never_reused(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 20000, "mallory": 20000}))
        reg = ui.UidRegistry(path, 20000, 20005)
        for key in ("alice", "mallory"):
            with pytest.raises(ui.UidAllocationError):
                reg.resolve(key)
        assert reg.resolve("carol") == 20001  # 20000 is never handed out again

    def test_reserved_and_malformed_entries_are_held(self, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"r": 20002, "b": True, "s": "20003"}))
        reg = ui.UidRegistry(path, 20000, 20005, reserved={20002})
        for key in ("r", "b", "s"):
            with pytest.raises(ui.UidAllocationError):
                reg.resolve(key)

    def test_persist_writes_0600(self, tmp_path):
        path = tmp_path / "uids.json"
        reg = ui.UidRegistry(path, 20000, 20005)
        reg.resolve("alice")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {"alice": 20000}
        assert not (tmp_path / "uids.json.tmp").exists()

    def test_dir_fsync_failure_after_replace_keeps_the_allocation(
        self, tmp_path, monkeypatch
    ):
        # Once the new map replaced the file the grant is on disk; rolling it
        # back in memory would free a uid the file still assigns (review 2).
        path = tmp_path / "uids.json"
        reg = ui.UidRegistry(path, 20000, 20005)
        real_fsync = ui.os.fsync

        def _fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        with monkeypatch.context() as m:
            m.setattr(ui.os, "fsync", _fsync)
            assert reg.resolve("alice") == 20000
        assert reg.resolve("bob") == 20001  # 20000 was not handed out again
        assert json.loads(path.read_text()) == {"alice": 20000, "bob": 20001}

    def test_held_entries_fail_startup(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"alice": 31000}))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        errs = ui.registry_errors()
        assert len(errs) == 1 and "cannot honor" in errs[0]

    def test_ephemeral_pool_never_hands_out_a_held_uid(self, monkeypatch, tmp_path):
        # A benign band change (a larger ephemeral slice) moves a named user's
        # uid into the ephemeral band: that user is held, and the uid must not
        # go to an anonymous run (cross-user file access in uid mode).
        path = tmp_path / "uids.json"
        path.write_text(json.dumps({"bob": 20009}))
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20009")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "3")  # ephemeral [20007, 20009]
        assert ui.registry_errors()  # bob is held: startup refuses
        handed = {ui.resolve_uid(f"/ws/anon-{i}", persist=False) for i in range(20)}
        assert 20009 not in handed
        assert handed <= {20007, 20008}

    def test_corrupt_registry_fails_the_anonymous_path_closed(
        self, monkeypatch, tmp_path
    ):
        path = tmp_path / "uids.json"
        path.write_text("{nope")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        with pytest.raises(ui.UidAllocationError):
            ui.resolve_uid("/ws/anon", persist=False)

    def test_persist_refuses_a_planted_tmp_symlink(self, tmp_path):
        path = tmp_path / "uids.json"
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (tmp_path / "uids.json.tmp").symlink_to(victim)
        reg = ui.UidRegistry(path, 20000, 20005)
        with pytest.raises(ui.UidAllocationError):
            reg.resolve("alice")
        assert victim.read_text() == "keep"


class TestBandsReview2:
    def test_default_broker_uid_is_outside_the_default_band(self):
        lo, hi = ui.uid_range()
        assert not (lo <= ui._DEFAULT_BROKER_UID <= hi)
        assert ui._DEFAULT_BROKER_UID in ui.reserved_uids()

    def test_zero_ephemeral_uids_refuses(self, monkeypatch):
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "0")
        assert any("ephemeral" in e for e in ui.validate_bands())

    def test_fully_reserved_ephemeral_band_refuses(self, monkeypatch):
        monkeypatch.setenv(ui.UID_MIN_ENV, "20000")
        monkeypatch.setenv(ui.UID_MAX_ENV, "20003")
        monkeypatch.setenv(ui.EPHEMERAL_UIDS_ENV, "1")
        monkeypatch.setenv(ui.BROKER_UID_ENV, "20003")  # the only ephemeral uid
        assert any("ephemeral" in e for e in ui.validate_bands())


class TestTicketSecretCapture:
    """PR #244 review 2 (#5): the secret must not stay in os.environ."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        ui.reset_captured_secret()
        yield
        ui.reset_captured_secret()

    def test_capture_moves_the_secret_out_of_the_environment(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        ui.capture_ticket_secret()
        assert ui.TICKET_SECRET_ENV not in os.environ
        assert ui.ticket_secret() == b"s3cret"
        # minting and verifying keep working from the captured copy
        t = ui.mint_ticket(uid=20001, workspace="/ws", ro=[], rw=[], env_allow=[])
        assert ui.verify_ticket(t)["uid"] == 20001

    def test_child_copying_os_environ_does_not_inherit_it(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        ui.capture_ticket_secret()
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os; print(os.environ.get({ui.TICKET_SECRET_ENV!r}, ''))",
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.stdout.strip() == ""

    def test_capture_is_idempotent_and_blank_is_ignored(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "s3cret")
        ui.capture_ticket_secret()
        ui.capture_ticket_secret()  # env already gone: keeps the captured one
        assert ui.ticket_secret() == b"s3cret"
        ui.reset_captured_secret()
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "   ")
        ui.capture_ticket_secret()
        assert ui.ticket_secret() is None

    def test_env_value_wins_over_a_captured_one(self, monkeypatch):
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "old")
        ui.capture_ticket_secret()
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, "new")
        assert ui.ticket_secret() == b"new"


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
