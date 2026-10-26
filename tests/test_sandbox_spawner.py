"""The #248 spike spawner frees what a bad or stalled handshake leaves behind.

``docker/sandbox/sandbox_spawner.py`` takes the CLI's stdio over ``SCM_RIGHTS``
before it has read the header. A malformed header (bad length, truncated or
invalid JSON) used to leak those three fds, because ``Spawner.handle`` never got
the list to close, and a client that never finished its header held a thread
forever (#254 review). No Docker needed: only the handshake runs.
"""

from __future__ import annotations

import array
import json
import os
import socket
import struct
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker" / "sandbox"))
import sandbox_spawner as sp  # noqa: E402

HDR = struct.Struct("=I")


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _send_with_fds(sock, data: bytes, nfds: int = 3) -> None:
    fds = [os.open(os.devnull, os.O_RDWR) for _ in range(nfds)]
    try:
        sock.sendmsg(
            [data[:1] or b"\0"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))],
        )
        if len(data) > 1:
            sock.sendall(data[1:])
    finally:
        for fd in fds:
            os.close(fd)


def _frame(body: bytes, length=None) -> bytes:
    return HDR.pack(len(body) if length is None else length) + body


GOOD = json.dumps({"user": "alice", "argv": [], "env": {}, "cwd": "/w"}).encode()

MALFORMED = {
    "zero length": _frame(b"", length=0),
    "oversized length": _frame(b"", length=sp.MAX_HEADER + 1),
    "truncated json": _frame(GOOD[:10], length=len(GOOD)),
    "invalid json": _frame(b"{not json"),
    "short length prefix": b"\x01\x00",
}


@pytest.mark.parametrize("payload", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_header_closes_the_passed_fds(payload):
    before = _open_fds()
    for _ in range(20):
        server, client = socket.socketpair()
        with server, client:
            _send_with_fds(client, payload)
            client.shutdown(socket.SHUT_WR)  # a truncated header ends in EOF
            with pytest.raises(Exception):
                sp._recv_header(server)
    assert _open_fds() == before, "every passed fd was closed"


def test_a_wrong_fd_count_closes_them_too():
    before = _open_fds()
    server, client = socket.socketpair()
    with server, client:
        _send_with_fds(client, _frame(GOOD), nfds=2)
        with pytest.raises(ValueError, match="expected 3 fds"):
            sp._recv_header(server)
    assert _open_fds() == before


def test_a_good_header_hands_the_fds_to_the_caller():
    server, client = socket.socketpair()
    with server, client:
        _send_with_fds(client, _frame(GOOD))
        header, fds = sp._recv_header(server)
    try:
        assert header["user"] == "alice"
        assert len(fds) == 3 and all(os.fstat(fd) for fd in fds)
    finally:
        for fd in fds:
            os.close(fd)


def test_handle_refuses_a_malformed_header_without_leaking():
    spawner = sp.Spawner(types.SimpleNamespace())
    before = _open_fds()
    for _ in range(20):
        server, client = socket.socketpair()
        with client:
            _send_with_fds(client, MALFORMED["invalid json"])
            spawner.handle(server)  # closes server
            assert sp.REC.unpack(client.recv(sp.REC.size)) == (sp.REFUSED, 0)
    assert _open_fds() == before


def test_a_stalled_handshake_times_out_and_frees_its_thread(monkeypatch):
    monkeypatch.setattr(sp, "HANDSHAKE_TIMEOUT_S", 0.2)
    spawner = sp.Spawner(types.SimpleNamespace())
    before = _open_fds()
    server, client = socket.socketpair()
    with client:
        # Sends the fds and half the length prefix, then nothing more.
        _send_with_fds(client, b"\x05")
        worker = threading.Thread(target=spawner.handle, args=(server,))
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "the handshake timed out"
        assert sp.REC.unpack(client.recv(sp.REC.size)) == (sp.REFUSED, 0)
    assert _open_fds() == before
