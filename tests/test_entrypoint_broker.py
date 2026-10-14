"""Entrypoint side of the uid-isolation broker (PR #244 review 2).

The root-only launch itself is container-verify-only; these pin the parts that
run unprivileged: the broker uid default, the socket-directory preparation the
root step performs, the bounded wait for the broker to accept, and (review 4)
the root step's ownership repair leaving uid mode's session-owned state alone.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def entrypoint():
    spec = importlib.util.spec_from_file_location(
        "docker_entrypoint_broker", ROOT / "docker" / "entrypoint.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_broker_uid_is_outside_the_session_band(entrypoint):
    from src.backends.claude import uid_isolation as ui

    assert entrypoint.DEFAULT_BROKER_UID == ui._DEFAULT_BROKER_UID
    lo, hi = ui._DEFAULT_UID_MIN, ui._DEFAULT_UID_MAX
    assert not lo <= entrypoint.DEFAULT_BROKER_UID <= hi


_WAIT_KNOB = [
    ("", 10.0),
    ("2.5", 2.5),
    ("0", 0.0),
    ("-3", 0.0),
    ("x", 10.0),
    ("inf", 10.0),  # would wait forever
    ("-inf", 10.0),
    ("nan", 10.0),  # used to read as 0
    ("1e400", 10.0),  # parses to inf
    ("299.5", 299.5),
    ("300", 300.0),
    ("301", 300.0),  # clamped to the cap
    ("1000", 300.0),
    ("1e6", 300.0),
]


@pytest.mark.parametrize("raw, seconds", _WAIT_KNOB)
def test_wait_knob_values(entrypoint, monkeypatch, raw, seconds):
    monkeypatch.setenv(entrypoint.BROKER_WAIT_ENV, raw)
    assert entrypoint._broker_wait_seconds() == seconds


@pytest.mark.parametrize("raw", [raw for raw, _ in _WAIT_KNOB])
def test_wait_knob_parses_like_the_gateway(entrypoint, monkeypatch, raw):
    # One operator knob bounds both broker waits; the two parses must agree.
    from src.backends.claude import landlock

    assert entrypoint.BROKER_WAIT_ENV == landlock.BROKER_WAIT_ENV
    assert entrypoint.DEFAULT_BROKER_WAIT_SECONDS == (
        landlock._DEFAULT_BROKER_WAIT_SECONDS
    )
    assert entrypoint.MAX_BROKER_WAIT_SECONDS == landlock._MAX_BROKER_WAIT_SECONDS
    monkeypatch.setenv(landlock.BROKER_WAIT_ENV, raw)
    assert entrypoint._broker_wait_seconds() == landlock._broker_wait_seconds()


def test_entrypoint_never_passes_the_test_only_broker_flag():
    # --test-same-uid lets a ticket name the broker's own uid; production
    # must never start the broker with it.
    assert "--test-same-uid" not in (ROOT / "docker" / "entrypoint.py").read_text()


class TestSocketDir:
    """B11: never re-own a shared directory or follow a symlink as root."""

    def _prepare(self, entrypoint, path: Path, uid=None):
        return entrypoint._prepare_broker_socket_dir(
            path,
            os.getuid() if uid is None else uid,
            os.getgid(),
            trusted_owners=frozenset({0, os.getuid()}),
        )

    def test_creates_a_private_dir(self, entrypoint, tmp_path):
        target = tmp_path / "broker"
        assert self._prepare(entrypoint, target)
        info = target.stat()
        assert stat.S_IMODE(info.st_mode) == 0o750
        assert info.st_uid == os.getuid()

    def test_adopts_a_dir_already_owned_as_expected(self, entrypoint, tmp_path):
        target = tmp_path / "broker"
        target.mkdir(mode=0o700)
        assert self._prepare(entrypoint, target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o750

    def test_refuses_a_symlinked_dir(self, entrypoint, tmp_path, capsys):
        real = tmp_path / "real"
        real.mkdir(mode=0o755)
        link = tmp_path / "broker"
        link.symlink_to(real)
        assert not self._prepare(entrypoint, link)
        assert stat.S_IMODE(real.stat().st_mode) == 0o755  # untouched
        assert "symlink" in capsys.readouterr().err

    def test_refuses_a_shared_dir(self, entrypoint, tmp_path, capsys):
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o1777)
        assert not self._prepare(entrypoint, shared)
        assert stat.S_IMODE(shared.stat().st_mode) == 0o1777  # untouched
        assert "not a private directory" in capsys.readouterr().err

    def test_refuses_a_dir_owned_by_someone_else(self, entrypoint, tmp_path):
        target = tmp_path / "broker"
        target.mkdir(mode=0o700)
        assert not self._prepare(entrypoint, target, uid=os.getuid() + 1)

    def test_refuses_tmp_itself(self, entrypoint):
        # The original bug: GATEWAY_ISOLATION_BROKER_SOCKET=/tmp/broker.sock
        # made the root step chown /tmp and chmod it 0750.
        before = os.stat("/tmp")
        assert not entrypoint._prepare_broker_socket_dir(
            Path("/tmp"), os.getuid(), os.getgid()
        )
        after = os.stat("/tmp")
        assert (after.st_uid, after.st_mode) == (before.st_uid, before.st_mode)

    def test_refuses_an_untrusted_parent(self, entrypoint, tmp_path, capsys):
        parent = tmp_path / "open"
        parent.mkdir()
        parent.chmod(0o777)  # anyone could rename our dir away
        assert not self._prepare(entrypoint, parent / "broker")
        assert not (parent / "broker").exists()
        assert "writable by other users" in capsys.readouterr().err

        linked = tmp_path / "linked"
        linked.symlink_to(tmp_path)
        assert not self._prepare(entrypoint, linked / "broker")
        assert "is a symlink" in capsys.readouterr().err

    def test_refuses_a_relative_path(self, entrypoint):
        assert not entrypoint._prepare_broker_socket_dir(
            Path("run/broker"), os.getuid(), os.getgid()
        )


_LISTENER = (
    "import socket, sys, time\n"
    "time.sleep(float(sys.argv[2]))\n"
    "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "s.bind(sys.argv[1]); s.listen(8)\n"
    "time.sleep(30)\n"
)


class TestWaitForBroker:
    """B9: the gateway is exec'd only once the broker accepts (bounded)."""

    def _child(self, *args):
        return subprocess.Popen([sys.executable, "-c", *args])

    def test_returns_once_the_broker_listens(self, entrypoint, tmp_path):
        sock = tmp_path / "b.sock"
        child = self._child(_LISTENER, str(sock), "0.3")
        try:
            start = time.monotonic()
            assert entrypoint._wait_for_broker(str(sock), child.pid, timeout=10)
            assert time.monotonic() - start < 5
        finally:
            child.kill()
            child.wait()

    def test_stops_at_once_when_the_broker_dies(self, entrypoint, tmp_path, capsys):
        child = self._child("import sys; sys.exit(78)")
        start = time.monotonic()
        assert not entrypoint._wait_for_broker(
            str(tmp_path / "b.sock"), child.pid, timeout=10
        )
        assert time.monotonic() - start < 5
        assert "exit code 78" in capsys.readouterr().err

    def test_times_out_and_warns(self, entrypoint, tmp_path, capsys):
        child = self._child("import time; time.sleep(30)")
        try:
            assert not entrypoint._wait_for_broker(
                str(tmp_path / "b.sock"), child.pid, timeout=0.5
            )
            assert "not accepting" in capsys.readouterr().err
        finally:
            child.kill()
            child.wait()


# --- uid mode: the root step leaves session-owned state alone (review 4) -----

_ISOLATION_ENVS = (
    "CLAUDE_ISOLATION_MODE",
    "CLAUDE_ISOLATION_FALLBACK",
    "CLAUDE_ISOLATION_UID_MIN",
    "CLAUDE_ISOLATION_UID_MAX",
    "CLAUDE_ISOLATION_EPHEMERAL_UIDS",
    "CLAUDE_ISOLATION_BROKER_UID",
    "CLAUDE_ISOLATION_UID_REGISTRY",
    "APP_UID",
)


@pytest.fixture
def isolation_env(monkeypatch, tmp_path):
    for name in _ISOLATION_ENVS:
        monkeypatch.delenv(name, raising=False)
    # Never this host's /app/data registry: an absent one unless a test writes it.
    registry = tmp_path / "isolation_uids.json"
    monkeypatch.setenv("CLAUDE_ISOLATION_UID_REGISTRY", str(registry))

    def apply(**env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)

    return apply


_BAND_VALUES = [
    "",
    " ",
    "20000",
    " 21000 ",
    "+20000",
    "2_0000",  # int() accepts underscores, so both sides must
    chr(0x0662) + chr(0x0660) * 4,  # Arabic-Indic digits: int() reads 20000
    "-5",
    "0",
    "x",
    "20000.0",
    "0x4e20",
    "1e4",
    "99999999999999999999",
]


@pytest.mark.parametrize("raw", _BAND_VALUES)
@pytest.mark.parametrize(
    "name", ["CLAUDE_ISOLATION_UID_MIN", "CLAUDE_ISOLATION_UID_MAX"]
)
def test_session_band_parses_like_the_gateway(entrypoint, isolation_env, name, raw):
    # The entrypoint cannot import src, so it duplicates uid_range(): a band
    # read differently here would re-own (or spare) the wrong entries.
    from src.backends.claude import uid_isolation as ui

    assert entrypoint.SESSION_UID_MIN_ENV == ui.UID_MIN_ENV
    assert entrypoint.SESSION_UID_MAX_ENV == ui.UID_MAX_ENV
    assert entrypoint.DEFAULT_SESSION_UID_MIN == ui._DEFAULT_UID_MIN
    assert entrypoint.DEFAULT_SESSION_UID_MAX == ui._DEFAULT_UID_MAX
    isolation_env(**{name: raw})
    assert entrypoint._session_uid_band() == ui.uid_range()


# Every kind of value a registry can hold; the gateway's loader takes the ints.
_MIXED_REGISTRY = {
    "alice": 20000,
    "bob": 31000,  # outside the default band: held, never handed out again
    "root": 0,
    "app": 1000,
    "broker": 19999,
    "flag": True,
    "float": 20001.0,
    "text": "20002",
    "none": None,
    "list": [20003],
    "neg": -5,
}


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"CLAUDE_ISOLATION_UID_MIN": "0", "CLAUDE_ISOLATION_UID_MAX": "60000"},
        {
            "CLAUDE_ISOLATION_UID_MIN": "500",
            "CLAUDE_ISOLATION_UID_MAX": "1500",
            "APP_UID": "1200",
        },
        {"CLAUDE_ISOLATION_BROKER_UID": "20005"},
        {"CLAUDE_ISOLATION_BROKER_UID": "x"},
        {"CLAUDE_ISOLATION_UID_MIN": "30000", "CLAUDE_ISOLATION_UID_MAX": "20000"},
    ],
    ids=[
        "defaults",
        "overlaps-reserved",
        "around-app",
        "broker-in-band",
        "bad",
        "empty",
    ],
)
@pytest.mark.parametrize("registered", [False, True], ids=["no-registry", "registry"])
def test_session_owned_uids_match_the_gateway_allocator(
    entrypoint, isolation_env, env, registered
):
    # Exactly the uids the gateway can hand a session (the band) or has handed
    # one (every uid its registry records), minus reserved_uids(): root, the
    # app uid and the broker uid.
    from src.backends.claude import uid_isolation as ui

    assert entrypoint.BROKER_UID_ENV == ui.BROKER_UID_ENV
    isolation_env(CLAUDE_ISOLATION_MODE="uid", **env)
    registry = Path(os.environ["CLAUDE_ISOLATION_UID_REGISTRY"])
    if registered:
        registry.write_text(json.dumps(_MIXED_REGISTRY))
    owned = entrypoint._session_owned_uids(int(env.get("APP_UID", "1000")))
    lo, hi = ui.uid_range()
    reserved = ui.reserved_uids()
    claimed = ui.UidRegistry(registry, *ui._stable_band(), reserved).claimed_uids()
    assert claimed == ({20000, 31000, 0, 1000, 19999, -5} if registered else set())
    probes = {0, 1, 499, 500, 1000, 1200, 1500, 1501, 19999, 20000, 20005}
    probes |= {20001, 20002, 29999, 30000, 31000, 60000, 60001, -5}
    probes |= {lo - 1, lo, hi, hi + 1, *reserved}
    for uid in sorted(probes):
        expected = (lo <= uid <= hi or uid in claimed) and uid not in reserved
        assert (uid in owned) == expected, uid


@pytest.mark.parametrize(
    "raw", [None, "uid", "UID", " uid\n", "landlock", "off", "", " ", "uidx", "u id"]
)
def test_uid_mode_reads_like_the_gateway(entrypoint, isolation_env, raw):
    from src.backends.claude import landlock

    assert entrypoint.ISOLATION_MODE_ENV == landlock.MODE_ENV
    if raw is not None:
        isolation_env(CLAUDE_ISOLATION_MODE=raw)
    assert entrypoint._uid_mode() == (landlock.isolation_mode() == "uid")


class TestUidRegistryRead:
    """N4 (review 4 follow-up): the root step also spares every uid the uid
    registry records. Read as root, without importing src, like the gateway's
    loader takes values; anything it cannot use is ignored, never fatal."""

    def _gateway_claimed(self, path):
        from src.backends.claude import uid_isolation as ui

        registry = ui.UidRegistry(path, *ui._stable_band(), ui.reserved_uids())
        return registry.claimed_uids()

    @pytest.mark.parametrize("raw", ["/srv/reg.json", " /srv/reg.json ", "rel/r.json"])
    def test_path_reads_like_the_gateway(self, entrypoint, isolation_env, raw):
        from src.backends.claude import uid_isolation as ui

        assert entrypoint.UID_REGISTRY_ENV == ui.UID_REGISTRY_ENV
        isolation_env(CLAUDE_ISOLATION_UID_REGISTRY=raw)
        assert entrypoint._uid_registry_path() == ui._registry_path()

    @pytest.mark.parametrize("raw", ["", "  "])
    def test_default_path_is_the_gateways_in_the_image(
        self, entrypoint, isolation_env, raw
    ):
        # The gateway derives <app root>/data/isolation_uids.json from its own
        # source location; the image puts that source at /app/src.
        from src.backends.claude import uid_isolation as ui

        isolation_env(CLAUDE_ISOLATION_UID_REGISTRY=raw)
        assert ui._registry_path() == ROOT / "data" / "isolation_uids.json"
        dockerfile = (ROOT / "Dockerfile").read_text()
        assert "WORKDIR /app\n" in dockerfile and "COPY src ./src\n" in dockerfile
        assert entrypoint._uid_registry_path() == Path("/app/data/isolation_uids.json")

    @pytest.mark.parametrize(
        "content",
        [
            {},
            {"alice": 20000, "bob": 20001},
            {"alice": 20005, "carol": 20005},  # a shared uid: held by the gateway
            _MIXED_REGISTRY,
        ],
        ids=["empty", "honored", "shared", "mixed"],
    )
    def test_uids_match_the_gateway_loader(
        self, entrypoint, isolation_env, tmp_path, content
    ):
        isolation_env(CLAUDE_ISOLATION_UID_MIN="21000")  # alice's uid now held
        registry = tmp_path / "isolation_uids.json"
        registry.write_text(json.dumps(content))
        assert entrypoint._registry_uids() == self._gateway_claimed(registry)

    def test_a_missing_registry_is_quietly_empty(
        self, entrypoint, isolation_env, tmp_path, capsys
    ):
        assert entrypoint._registry_uids() == frozenset()
        assert self._gateway_claimed(tmp_path / "isolation_uids.json") == set()
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize(
        "data",
        [
            b"{",
            b"[20000]",
            b'"20000"',
            b"\xff\xfe{}",
            b'\xef\xbb\xbf{"alice": 20000}',  # the gateway reads it as UTF-8 text
            b"",
        ],
        ids=["corrupt", "list", "string", "not-utf8", "bom", "empty-file"],
    )
    def test_a_registry_the_gateway_refuses_is_ignored(
        self, entrypoint, isolation_env, tmp_path, capsys, data
    ):
        from src.backends.claude import uid_isolation as ui

        registry = tmp_path / "isolation_uids.json"
        registry.write_bytes(data)
        with pytest.raises(ui.UidAllocationError):
            self._gateway_claimed(registry)
        assert entrypoint._registry_uids() == frozenset()
        assert "warning: ignoring the uid registry" in capsys.readouterr().err

    def test_a_directory_is_ignored(self, entrypoint, isolation_env, tmp_path, capsys):
        (tmp_path / "isolation_uids.json").mkdir()
        assert entrypoint._registry_uids() == frozenset()
        assert "not a regular file" in capsys.readouterr().err

    def test_a_deeply_nested_registry_is_ignored(
        self, entrypoint, isolation_env, tmp_path, capsys
    ):
        (tmp_path / "isolation_uids.json").write_text("[" * 100000 + "]" * 100000)
        assert entrypoint._registry_uids() == frozenset()
        assert "warning: ignoring the uid registry" in capsys.readouterr().err

    def test_a_symlinked_registry_is_never_followed(
        self, entrypoint, isolation_env, tmp_path, capsys
    ):
        real = tmp_path / "real.json"
        real.write_text(json.dumps({"alice": 20000}))
        (tmp_path / "isolation_uids.json").symlink_to(real)
        assert entrypoint._registry_uids() == frozenset()
        assert "is a symlink" in capsys.readouterr().err

    def test_a_symlinked_parent_is_never_followed(
        self, entrypoint, isolation_env, tmp_path, capsys
    ):
        real = tmp_path / "real"
        real.mkdir()
        (real / "isolation_uids.json").write_text(json.dumps({"alice": 20000}))
        (tmp_path / "data").symlink_to(real, target_is_directory=True)
        registry = tmp_path / "data" / "isolation_uids.json"
        isolation_env(CLAUDE_ISOLATION_UID_REGISTRY=str(registry))
        assert entrypoint._registry_uids() == frozenset()
        assert f"{tmp_path / 'data'} is a symlink" in capsys.readouterr().err

    def test_a_fifo_registry_does_not_block(
        self, entrypoint, isolation_env, tmp_path, capsys
    ):
        os.mkfifo(tmp_path / "isolation_uids.json")

        def stuck(*_):
            raise TimeoutError("the registry read blocked on a FIFO")

        previous = signal.signal(signal.SIGALRM, stuck)
        signal.alarm(5)
        try:
            assert entrypoint._registry_uids() == frozenset()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        assert "not a regular file" in capsys.readouterr().err

    def test_an_oversized_registry_is_ignored(
        self, entrypoint, isolation_env, tmp_path, monkeypatch, capsys
    ):
        registry = tmp_path / "isolation_uids.json"
        registry.write_text(json.dumps({"alice": 20000}))
        size = registry.stat().st_size
        monkeypatch.setattr(entrypoint, "MAX_UID_REGISTRY_BYTES", size)
        assert entrypoint._registry_uids() == frozenset({20000})
        monkeypatch.setattr(entrypoint, "MAX_UID_REGISTRY_BYTES", size - 1)
        assert entrypoint._registry_uids() == frozenset()
        assert f"larger than {size - 1} bytes" in capsys.readouterr().err


class _Proxy:
    """A module stand-in: the given overrides, everything else delegated."""

    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeOwners(_Proxy):
    """``entrypoint.os`` with lstat reporting chosen owners (*default* for
    every other path) and chown recorded instead of applied (no root here)."""

    def __init__(self, owners, default):
        super().__init__(os)
        self.owners = {Path(path): uid for path, uid in owners.items()}
        self.default = default
        self.lstats: list[Path] = []
        self.chowned: list[Path] = []

    def lstat(self, path, *args, **kwargs):
        path = Path(path)
        self.lstats.append(path)
        fields = list(os.lstat(path, *args, **kwargs))
        fields[4] = self.owners.get(path, self.default)  # st_uid
        return os.stat_result(fields)

    def chown(self, path, uid, gid, *, follow_symlinks=True):
        assert follow_symlinks is False
        self.chowned.append(Path(path))


_APP = 1000


class TestUidModeOwnership:
    """#10 (review 4): every root start re-owned the persisted ``~/.claude``
    to the app uid. In uid mode that handed a session uid's ``projects/<name>``
    (0755), transcripts (0600) and ``memory/`` (0700) to 1000:1000 with their
    modes kept, so after a restart the session uid could no longer resume,
    record turns or keep auto-memory. uid mode now leaves entries a session uid
    owns alone and does not walk them; other modes still re-own everything,
    which also hands uid mode's leftovers back after a mode change."""

    def _tree(self, tmp_path):
        home = tmp_path / "home"
        paths = {
            "data_dir": tmp_path / "data",
            "claude_home": home / ".claude",
            "codex_home": home / ".codex",
            "opencode_home": home / ".local" / "share" / "opencode",
            "opencode_config": home / ".config" / "opencode",
            "uv_cache_dir": home / ".cache" / "uv",
        }
        claude = paths["claude_home"]
        alice = claude / "projects" / "-data-workspaces-alice"
        (alice / "s1" / "subagents").mkdir(parents=True)
        (alice / "s1.jsonl").write_text("{}\n")
        (alice / "s1" / "subagents" / "agent-a.jsonl").write_text("{}\n")
        (alice / "memory").mkdir()
        (alice / "memory" / "MEMORY.md").write_text("x")
        legacy = claude / "projects" / "-data-workspaces-bob"  # app-owned
        legacy.mkdir()
        (legacy / "s2.jsonl").write_text("{}\n")
        (legacy / "s3.jsonl").write_text("{}\n")
        (claude / "skills" / "demo").mkdir(parents=True)
        (claude / "skills" / "demo" / "SKILL.md").write_text("x")
        session = {
            alice: 20000,
            alice / "s1.jsonl": 20000,
            alice / "s1": 20000,
            alice / "s1" / "subagents": 20000,
            alice / "s1" / "subagents" / "agent-a.jsonl": 20000,
            alice / "memory": 20000,
            alice / "memory" / "MEMORY.md": 20000,
            legacy / "s3.jsonl": 20001,  # a session's file in an app-owned dir
        }
        others = {claude / "skills" / "demo" / "SKILL.md": 0}  # root-owned
        return paths, alice, legacy, session, others

    def _repair(self, entrypoint, monkeypatch, paths, owners):
        fake = _FakeOwners(owners, default=_APP)
        monkeypatch.setattr(entrypoint, "os", fake)
        entrypoint.prepare_writable_paths(
            uid=_APP,
            gid=_APP,
            skip_owners=entrypoint._session_owned_uids(_APP),
            **paths,
        )
        return fake

    def test_uid_mode_leaves_session_state_alone(
        self, entrypoint, monkeypatch, isolation_env, tmp_path
    ):
        isolation_env(CLAUDE_ISOLATION_MODE="uid")
        paths, alice, legacy, session, others = self._tree(tmp_path)
        fake = self._repair(entrypoint, monkeypatch, paths, {**session, **others})

        assert not set(session) & set(fake.chowned)
        # Not walked either: nothing below the session's own dir is looked at.
        assert [p for p in fake.lstats if alice in p.parents] == []
        claude = paths["claude_home"]
        for repaired in (
            claude,
            claude / "projects",
            legacy,
            legacy / "s2.jsonl",
            claude / "skills" / "demo" / "SKILL.md",  # root-owned: repaired
        ):
            assert repaired in fake.chowned, repaired

    @pytest.mark.parametrize("mode", [None, "off", "landlock", "uidx"])
    def test_other_modes_reown_everything(
        self, entrypoint, monkeypatch, isolation_env, tmp_path, mode
    ):
        # Sessions run as the app uid there; this also hands uid mode's
        # leftovers back to it after a mode change.
        if mode is not None:
            isolation_env(CLAUDE_ISOLATION_MODE=mode)
        paths, _alice, _legacy, session, others = self._tree(tmp_path)
        fake = self._repair(entrypoint, monkeypatch, paths, {**session, **others})

        assert set(session) <= set(fake.chowned)

    def test_reserved_uids_inside_the_band_are_still_repaired(
        self, entrypoint, monkeypatch, isolation_env, tmp_path
    ):
        # A band overlapping root, the app uid and the broker uid only warns
        # in the gateway; their entries are never taken for a session's.
        isolation_env(
            CLAUDE_ISOLATION_MODE="uid",
            CLAUDE_ISOLATION_UID_MIN="0",
            CLAUDE_ISOLATION_UID_MAX="60000",
        )
        paths, _alice, legacy, session, others = self._tree(tmp_path)
        claude = paths["claude_home"]
        broker_owned = claude / "skills" / "demo"
        owners = {**session, **others, broker_owned: 19999}
        fake = self._repair(entrypoint, monkeypatch, paths, owners)

        assert not set(session) & set(fake.chowned)
        for repaired in (claude, legacy, broker_owned, broker_owned / "SKILL.md"):
            assert repaired in fake.chowned, repaired

    def test_a_narrowed_band_still_spares_registered_users(
        self, entrypoint, monkeypatch, isolation_env, tmp_path
    ):
        # N4: the registry holds alice=20000 and the band was narrowed past
        # her. The gateway refuses to start over that entry, but only after
        # this step ran: her state must still be hers once the band is back.
        isolation_env(CLAUDE_ISOLATION_MODE="uid", CLAUDE_ISOLATION_UID_MIN="21000")
        registry = Path(os.environ["CLAUDE_ISOLATION_UID_REGISTRY"])
        registry.write_text(json.dumps({"alice": 20000, "app": _APP, "root": 0}))
        paths, _alice, legacy, session, others = self._tree(tmp_path)
        alice_state = {path for path, uid in session.items() if uid == 20000}
        fake = self._repair(entrypoint, monkeypatch, paths, {**session, **others})

        assert not alice_state & set(fake.chowned)
        claude = paths["claude_home"]
        for repaired in (
            claude,  # the app uid, registered or not, is reserved
            claude / "skills" / "demo" / "SKILL.md",  # so is root
            legacy / "s3.jsonl",  # 20001: neither in the band nor registered
        ):
            assert repaired in fake.chowned, repaired

    def test_fallback_still_keys_on_the_requested_mode(
        self, entrypoint, monkeypatch, isolation_env, tmp_path
    ):
        # Whether the gateway falls back to landlock is decided later, by its
        # startup probe, so uid mode's state is kept either way.
        isolation_env(CLAUDE_ISOLATION_MODE="uid", CLAUDE_ISOLATION_FALLBACK="landlock")
        paths, _alice, _legacy, session, others = self._tree(tmp_path)
        fake = self._repair(entrypoint, monkeypatch, paths, {**session, **others})

        assert not set(session) & set(fake.chowned)

    def test_other_modes_never_read_the_registry(
        self, entrypoint, isolation_env, capsys
    ):
        isolation_env(CLAUDE_ISOLATION_MODE="landlock")
        os.mkfifo(os.environ["CLAUDE_ISOLATION_UID_REGISTRY"])
        assert entrypoint._session_owned_uids(_APP) == ()
        assert capsys.readouterr().err == ""

    def test_pr233_protections_hold_in_uid_mode(
        self, entrypoint, monkeypatch, isolation_env, tmp_path, capsys
    ):
        # Links are still never followed and foreign hard links still skipped.
        isolation_env(CLAUDE_ISOLATION_MODE="uid")
        paths, _alice, _legacy, session, others = self._tree(tmp_path)
        claude = paths["claude_home"]
        outside = tmp_path / "outside"
        (outside / "nested").mkdir(parents=True)
        (outside / "file.txt").write_text("x")
        (claude / "dir-link").symlink_to(outside, target_is_directory=True)
        os.link(outside / "file.txt", claude / "linked.txt")
        owners = {**session, **others, claude / "linked.txt": 4242}
        fake = self._repair(entrypoint, monkeypatch, paths, owners)

        link = claude / "dir-link"
        assert link in fake.chowned  # the link itself (lchown)
        assert [p for p in fake.lstats if link in p.parents] == []  # not walked
        assert claude / "linked.txt" not in fake.chowned
        assert "2 hard links, owned by uid 4242" in capsys.readouterr().err

    @pytest.mark.parametrize("mode", ["uid", "landlock"])
    def test_main_passes_the_session_band_only_in_uid_mode(
        self, entrypoint, monkeypatch, isolation_env, mode
    ):
        isolation_env(CLAUDE_ISOLATION_MODE=mode, APP_UID=str(_APP))
        seen = {}

        class _Exec(Exception):
            pass

        def execvp(*args):
            raise _Exec

        fake_os = _Proxy(os, geteuid=lambda: 0, execvp=execvp)
        monkeypatch.setattr(entrypoint, "os", fake_os)
        monkeypatch.setattr(entrypoint, "prepare_writable_paths", seen.update)
        monkeypatch.setattr(entrypoint, "launch_isolation_broker", lambda *a: None)
        monkeypatch.setattr(entrypoint, "drop_privileges", lambda *a: None)
        with pytest.raises(_Exec):
            entrypoint.main(["true"])

        assert seen["uid"] == _APP
        assert (20000 in seen["skip_owners"]) is (mode == "uid")
        assert _APP not in seen["skip_owners"]
