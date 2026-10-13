"""Entrypoint side of the uid-isolation broker (PR #244 review 2).

The root-only launch itself is container-verify-only; these pin the parts that
run unprivileged: the broker uid default, the socket-directory preparation the
root step performs, and the bounded wait for the broker to accept.
"""

from __future__ import annotations

import importlib.util
import os
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
