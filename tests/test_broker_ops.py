"""The gateway's client for the broker's ``cleanup`` / ``transcript`` ops.

A fake broker on a temp AF_UNIX socket checks what ``broker_ops`` sends — one
``=I`` length + JSON header ``{"ticket", "argv": [], "env": {}}``, no file
descriptors, a ticket that ``uid_isolation.verify_ticket`` accepts — and answers
with whatever records a test scripts. The real broker side is
``tests/test_isolation_broker.py``; this pins the client against the contract
(``docs/isolation-uid-ownership-design.md`` §3.3, §3.6).
"""

from __future__ import annotations

import array
import importlib.util
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from src.backends.claude import broker_ops, landlock
from src.backends.claude import uid_isolation as ui
from src.backends.claude.transcript_turns import (
    TRANSCRIPT_CORRUPT,
    TRANSCRIPT_MISMATCH,
    TRANSCRIPT_MISSING,
    TRANSCRIPT_UNREADABLE,
)

_RULES = Path(__file__).resolve().parents[1] / "docker" / "_landlock_rules.py"
_REC = struct.Struct("=Ii")
_HANG = object()
_SECRET = "broker-ops-test-secret"


def _record(kind: int, value: int) -> bytes:
    return _REC.pack(kind, value)


class FakeBroker:
    """Accepts op connections and answers each with ``responder(payload)``.

    The responder returns the bytes to send (then the link closes), ``b""`` to
    close without answering, or ``_HANG`` to keep the link open until the
    client gives up.
    """

    def __init__(self, path: Path, responder) -> None:
        self.path = path
        self.responder = responder
        self.requests: list = []
        self.errors: list = []
        self.seen: set = set()
        self._closing = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing.set()
        self._server.close()

    def _serve(self) -> None:
        while not self._closing.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _read(self, conn, need: int, buf: bytearray, fds: list) -> bool:
        while len(buf) < need:
            data, ancdata, _flags, _addr = conn.recvmsg(
                65536, socket.CMSG_SPACE(16 * array.array("i").itemsize)
            )
            for level, kind, cdata in ancdata:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    got = array.array("i")
                    got.frombytes(cdata[: len(cdata) - len(cdata) % got.itemsize])
                    fds.extend(got)
            if not data:
                return False
            buf.extend(data)
        return True

    def _handle(self, conn) -> None:
        with conn:
            conn.settimeout(10)
            buf: bytearray = bytearray()
            fds: list = []
            try:
                if not self._read(conn, 4, buf, fds):
                    return
                (hlen,) = struct.unpack("=I", bytes(buf[:4]))
                if not self._read(conn, 4 + hlen, buf, fds):
                    return
                header = json.loads(bytes(buf[4 : 4 + hlen]).decode("utf-8"))
                payload = ui.verify_ticket(header["ticket"], seen_nonces=self.seen)
            except Exception as exc:  # recorded for the test to assert on
                self.errors.append(exc)
                return
            for fd in fds:
                os.close(fd)
            self.requests.append(
                {
                    "header": header,
                    "payload": payload,
                    "fds": len(fds),
                    "trailing": bytes(buf[4 + hlen :]),
                }
            )
            reply = self.responder(payload)
            if reply is _HANG:
                self._closing.wait(10)
                return
            if reply:
                conn.sendall(reply)


@pytest.fixture
def uid_env(monkeypatch, tmp_path):
    """uid mode with a reachable broker faked, a ticket secret and a socket."""
    monkeypatch.setenv(landlock.MODE_ENV, "uid")
    monkeypatch.setenv(ui.TICKET_SECRET_ENV, _SECRET)
    monkeypatch.setattr(ui, "_captured_secret", None)
    monkeypatch.setattr(landlock, "uid_mode_available", lambda: True)
    monkeypatch.setattr(landlock, "_effective_mode_cache", None)
    sock = tmp_path / "b.sock"
    monkeypatch.setenv(landlock.BROKER_SOCKET_ENV, str(sock))
    return sock


@pytest.fixture
def broker(uid_env):
    """A fake broker; ``broker.responder`` defaults to ``(ST_EXITED, 0)``."""
    fake = FakeBroker(uid_env, lambda _payload: _record(broker_ops.ST_EXITED, 0))
    yield fake
    fake.close()


# ---------------------------------------------------------------------------
# Record kinds mirror the broker's
# ---------------------------------------------------------------------------


class TestRecordKindParity:
    """The broker never imports the gateway tree; its record kinds live in
    docker/_landlock_rules.py and broker_ops mirrors them."""

    @pytest.fixture(scope="class")
    def rules(self):
        spec = importlib.util.spec_from_file_location("landlock_rules_ops", _RULES)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_status_kinds_match(self, rules):
        assert broker_ops.ST_EXITED == rules.ST_EXITED
        assert broker_ops.ST_SIGNALED == rules.ST_SIGNALED
        assert broker_ops.ST_REFUSED == rules.ST_REFUSED

    def test_the_transcript_kind_matches_once_the_broker_has_it(self, rules):
        if not hasattr(rules, "ST_TRANSCRIPT"):
            pytest.skip("docker/_landlock_rules.py has no ST_TRANSCRIPT yet")
        assert broker_ops.ST_TRANSCRIPT == rules.ST_TRANSCRIPT

    def test_the_record_layout_matches(self, rules):
        assert rules.REC.format == "=Ii"
        assert broker_ops._RECORD.format == rules.REC.format


# ---------------------------------------------------------------------------
# available()
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_uid_mode_with_a_socket(self, uid_env):
        assert broker_ops.available() is True

    def test_uid_mode_without_a_socket(self, uid_env, monkeypatch):
        monkeypatch.delenv(landlock.BROKER_SOCKET_ENV)
        assert broker_ops.available() is False

    @pytest.mark.parametrize("mode", ["off", "landlock"])
    def test_other_modes(self, uid_env, monkeypatch, mode):
        monkeypatch.setenv(landlock.MODE_ENV, mode)
        monkeypatch.setattr(landlock, "_effective_mode_cache", None)
        assert broker_ops.available() is False


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanupWire:
    def test_one_header_no_fds_and_a_verified_single_use_ticket(self, broker, tmp_path):
        ws = tmp_path / "_tmp_ws"
        assert broker_ops.cleanup(
            20001,
            workspace=ws,
            clear=[ws, tmp_path / "cfg" / "projects"],
            remove=[str(tmp_path / "x")],
            shm=True,
            wait=10,
        )
        (request,) = broker.requests
        assert request["fds"] == 0
        assert request["trailing"] == b""
        assert request["header"]["argv"] == [] and request["header"]["env"] == {}
        assert set(request["header"]) == {"ticket", "argv", "env"}
        payload = request["payload"]
        assert payload["op"] == ui.OP_CLEANUP
        assert payload["uid"] == 20001
        assert "gid" not in payload  # the broker takes the uid
        assert payload["workspace"] == str(ws)
        assert payload["wait"] == 10
        assert payload["clear"] == [str(ws), str(tmp_path / "cfg" / "projects")]
        assert payload["remove"] == [str(tmp_path / "x")]
        assert payload["shm"] is True
        assert "expire" not in payload
        assert set(payload) == {
            "op",
            "uid",
            "workspace",
            "wait",
            "clear",
            "remove",
            "shm",
            "nonce",
            "exp",
        }
        assert broker.errors == []

    def test_defaults(self, broker):
        assert broker_ops.cleanup(20002) is True
        payload = broker.requests[0]["payload"]
        assert payload["workspace"] == ""
        assert payload["wait"] == 0
        assert payload["clear"] == [] and payload["remove"] == []
        assert payload["shm"] is False

    def test_paths_leave_absolute_and_normalized(self, broker, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        broker_ops.cleanup(20003, workspace="ws/./a/../b", clear=["c//d/"])
        payload = broker.requests[0]["payload"]
        assert payload["workspace"] == str(tmp_path / "ws" / "b")
        assert payload["clear"] == [str(tmp_path / "c" / "d")]

    def test_expire_is_passed_when_given(self, broker, tmp_path):
        expire = {
            "older_than": 30 * 86400,
            "units": [tmp_path / "session-env"],
            "nested": [str(tmp_path / "projects")],
        }
        broker_ops.cleanup(20004, expire=expire)
        assert broker.requests[0]["payload"]["expire"] == {
            "older_than": 30 * 86400,
            "units": [str(tmp_path / "session-env")],
            "nested": [str(tmp_path / "projects")],
        }

    def test_each_call_is_a_fresh_ticket(self, broker):
        broker_ops.cleanup(20005)
        broker_ops.cleanup(20005)
        nonces = {r["payload"]["nonce"] for r in broker.requests}
        assert len(nonces) == 2 and broker.errors == []


class TestCleanupAnswers:
    def test_a_failed_step_is_false(self, broker):
        broker.responder = lambda _p: _record(broker_ops.ST_EXITED, 1)
        assert broker_ops.cleanup(20010) is False

    @pytest.mark.parametrize(
        "reply",
        [
            _record(broker_ops.ST_REFUSED, 0),
            _record(broker_ops.ST_SIGNALED, 9),
            _record(broker_ops.ST_TRANSCRIPT, 3),
            _record(99, 0),
        ],
        ids=["refused", "signaled", "transcript", "unknown"],
    )
    def test_any_other_record_is_an_error(self, broker, reply):
        broker.responder = lambda _p: reply
        with pytest.raises(broker_ops.BrokerOpError):
            broker_ops.cleanup(20011)

    @pytest.mark.parametrize("reply", [b"", _record(1, 0)[:5]], ids=["none", "partial"])
    def test_eof_before_a_full_record_is_an_error(self, broker, reply):
        broker.responder = lambda _p: reply
        with pytest.raises(broker_ops.BrokerOpError, match="closed the link"):
            broker_ops.cleanup(20012)

    def test_a_silent_broker_times_out(self, broker):
        broker.responder = lambda _p: _HANG
        started = time.monotonic()
        with pytest.raises(broker_ops.BrokerOpError, match="timed out"):
            broker_ops.cleanup(20013, timeout=0.3)
        assert time.monotonic() - started < 5

    def test_the_timeout_includes_the_wait(self, broker):
        # A parked request answers only after the broker's wait.
        def late(_payload):
            time.sleep(0.6)
            return _record(broker_ops.ST_EXITED, 0)

        broker.responder = late
        assert broker_ops.cleanup(20014, wait=1, timeout=0.2) is True

    def test_no_broker_listening(self, uid_env):
        with pytest.raises(broker_ops.BrokerOpError, match="failed"):
            broker_ops.cleanup(20015)

    def test_no_socket_configured(self, uid_env, monkeypatch):
        monkeypatch.delenv(landlock.BROKER_SOCKET_ENV)
        with pytest.raises(broker_ops.BrokerOpError, match="no broker socket"):
            broker_ops.cleanup(20016)

    def test_no_ticket_secret(self, broker, monkeypatch):
        monkeypatch.delenv(ui.TICKET_SECRET_ENV)
        with pytest.raises(broker_ops.BrokerOpError, match="cannot mint"):
            broker_ops.cleanup(20017)
        assert broker.requests == []

    def test_errors_never_carry_the_ticket(self, broker):
        broker.responder = lambda _p: _record(broker_ops.ST_REFUSED, 0)
        with pytest.raises(broker_ops.BrokerOpError) as excinfo:
            broker_ops.cleanup(20018, clear=["/srv/x"])
        message = str(excinfo.value)
        ticket = broker.requests[0]["header"]["ticket"]
        signature = ticket.partition(".")[0]
        assert broker.requests[0]["payload"]["nonce"] not in message
        assert signature not in message and ticket not in message


class TestCleanupArguments:
    """Requests the broker would refuse fail here, before any round trip."""

    @pytest.mark.parametrize("wait", [-1, 31, 1.5, True, "1"])
    def test_wait_out_of_range(self, broker, wait):
        with pytest.raises(ValueError):
            broker_ops.cleanup(20020, wait=wait)
        assert broker.requests == []

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"clear": ["/"]},
            {"remove": ["/x\0y"]},
            {"workspace": "/"},
            {"clear": "/one/path"},
            {"remove": [b"/bytes"]},
        ],
        ids=["root", "nul", "root-ws", "str-list", "bytes"],
    )
    def test_bad_paths(self, broker, kwargs):
        with pytest.raises(ValueError):
            broker_ops.cleanup(20021, **kwargs)
        assert broker.requests == []

    @pytest.mark.parametrize(
        "expire",
        [
            {"older_than": 3599},
            {"older_than": 3600.0},
            {"older_than": True},
            {"units": ["/x"]},
            {"older_than": 7200, "other": []},
            [("older_than", 7200)],
        ],
        ids=["short", "float", "bool", "missing", "unknown-key", "not-a-mapping"],
    )
    def test_bad_expire(self, broker, expire):
        with pytest.raises(ValueError):
            broker_ops.cleanup(20022, expire=expire)
        assert broker.requests == []


# ---------------------------------------------------------------------------
# read_transcript
# ---------------------------------------------------------------------------


class TestReadTranscript:
    @pytest.mark.parametrize(
        "value",
        [
            0,
            7,
            TRANSCRIPT_MISSING,
            TRANSCRIPT_MISMATCH,
            TRANSCRIPT_CORRUPT,
            TRANSCRIPT_UNREADABLE,
        ],
    )
    def test_the_count_or_code_is_returned(self, broker, tmp_path, value):
        broker.responder = lambda _p: _record(broker_ops.ST_TRANSCRIPT, value)
        path = tmp_path / "cfg" / "projects" / "-ws" / "sid.jsonl"
        assert broker_ops.read_transcript(20030, path, tmp_path / "ws") == value
        (request,) = broker.requests
        payload = request["payload"]
        assert request["fds"] == 0
        assert payload["op"] == ui.OP_TRANSCRIPT
        assert payload["uid"] == 20030
        assert payload["path"] == str(path)
        assert payload["cwd"] == str(tmp_path / "ws")
        assert set(payload) == {"op", "uid", "path", "cwd", "nonce", "exp"}

    @pytest.mark.parametrize(
        "reply",
        [
            _record(broker_ops.ST_EXITED, 0),
            _record(broker_ops.ST_REFUSED, 0),
            _record(broker_ops.ST_TRANSCRIPT, -5),
        ],
        ids=["exited", "refused", "bad-code"],
    )
    def test_anything_else_is_an_error(self, broker, tmp_path, reply):
        broker.responder = lambda _p: reply
        with pytest.raises(broker_ops.BrokerOpError):
            broker_ops.read_transcript(20031, tmp_path / "s.jsonl", tmp_path)

    def test_only_a_jsonl_path(self, broker, tmp_path):
        with pytest.raises(ValueError):
            broker_ops.read_transcript(20032, tmp_path / "s.json", tmp_path)
        assert broker.requests == []

    def test_a_silent_broker_times_out(self, broker, tmp_path):
        broker.responder = lambda _p: _HANG
        with pytest.raises(broker_ops.BrokerOpError, match="timed out"):
            broker_ops.read_transcript(
                20033, tmp_path / "s.jsonl", tmp_path, timeout=0.3
            )


class TestTransientFailures:
    """``BrokerOpError.transient``: a later attempt may succeed (unreachable,
    link failed or closed early, no answer in time) versus the same request
    getting the same answer (refused, wrong record, no socket, no secret)."""

    @pytest.mark.parametrize(
        "reply",
        [b"", _record(1, 0)[:3], _HANG],
        ids=["closed", "partial", "silent"],
    )
    def test_no_full_answer_is_transient(self, broker, reply):
        broker.responder = lambda _p: reply
        with pytest.raises(broker_ops.BrokerOpError) as excinfo:
            broker_ops.cleanup(20040, timeout=0.3)
        assert excinfo.value.transient is True

    def test_no_broker_listening_is_transient(self, uid_env):
        with pytest.raises(broker_ops.BrokerOpError) as excinfo:
            broker_ops.read_transcript(20041, "/cfg/s.jsonl", "/ws")
        assert excinfo.value.transient is True

    @pytest.mark.parametrize(
        "reply",
        [_record(broker_ops.ST_REFUSED, 0), _record(broker_ops.ST_SIGNALED, 9)],
        ids=["refused", "wrong-kind"],
    )
    def test_an_answer_is_not_transient(self, broker, reply):
        broker.responder = lambda _p: reply
        with pytest.raises(broker_ops.BrokerOpError) as excinfo:
            broker_ops.read_transcript(20042, "/cfg/s.jsonl", "/ws")
        assert excinfo.value.transient is False

    def test_a_missing_socket_or_secret_is_not_transient(self, broker, monkeypatch):
        monkeypatch.delenv(ui.TICKET_SECRET_ENV)
        with pytest.raises(broker_ops.BrokerOpError) as excinfo:
            broker_ops.cleanup(20043)
        assert excinfo.value.transient is False
        monkeypatch.setenv(ui.TICKET_SECRET_ENV, _SECRET)
        monkeypatch.delenv(landlock.BROKER_SOCKET_ENV)
        with pytest.raises(broker_ops.BrokerOpError) as excinfo:
            broker_ops.cleanup(20043)
        assert excinfo.value.transient is False


def test_the_transcript_read_outwaits_the_brokers_alarm():
    # The broker's transcript reader gets 60 s; the client waits longer.
    import inspect

    timeout = inspect.signature(broker_ops.read_transcript).parameters["timeout"]
    assert timeout.default == 75.0
