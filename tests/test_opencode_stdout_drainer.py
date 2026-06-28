"""Regression test for the managed OpenCode server stdout drainer.

Without a drainer, the startup loop stops reading ``proc.stdout`` after it
sees the "listening" line, so the OS pipe buffer eventually fills from the
server's ongoing logging and the server's next ``write()`` blocks, hanging
the whole process. ``_start_managed_server`` must keep draining stdout on a
daemon thread, and ``close()`` must tear that thread down with the process.
"""

import threading
import time

import pytest

from src.backends.opencode.client import OpenCodeClient


class _FakeStdout:
    """A pipe-like stdout that blocks the reader after the listening line.

    ``readline`` feeds the startup loop until the listening line, then the
    iterator (used by the drainer) emits lines until the process is marked
    terminated, mimicking a real pipe that reaches EOF on shutdown.
    """

    def __init__(self, alive_flag):
        self._alive = alive_flag
        self._startup_lines = iter(
            [
                "booting...\n",
                "opencode server listening on http://127.0.0.1:4096\n",
            ]
        )

    def readline(self):
        try:
            return next(self._startup_lines)
        except StopIteration:
            return ""

    def __iter__(self):
        # Drainer consumes this; yield log lines until the process is gone.
        while self._alive[0]:
            time.sleep(0.005)
            yield "background log line\n"


class _FakeProc:
    def __init__(self):
        self._alive = [True]
        self.stdout = _FakeStdout(self._alive)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive[0] else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive[0] = False
        self.returncode = 0

    def kill(self):
        self.killed = True
        self._alive[0] = False
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_managed_server_starts_stdout_drainer(monkeypatch):
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    fake = _FakeProc()
    monkeypatch.setattr(
        "src.backends.opencode.client.subprocess.Popen",
        lambda *a, **k: fake,
    )
    # select.select must report stdout readable so the startup loop reads.
    monkeypatch.setattr(
        "src.backends.opencode.client.select.select",
        lambda r, w, x, timeout: (r, [], []),
    )

    # Managed mode (no OPENCODE_BASE_URL): __init__ spawns the server.
    client = OpenCodeClient()
    assert client.base_url == "http://127.0.0.1:4096"

    drainer = client._drain_thread
    assert drainer is not None
    assert drainer.daemon is True
    assert drainer.is_alive()

    # Drainer keeps consuming after startup, so the pipe never fills.
    time.sleep(0.02)
    assert drainer.is_alive()

    # close() tears the process down and the drainer exits with it.
    client.close()
    assert fake.terminated is True
    drainer.join(timeout=1)
    assert not drainer.is_alive()
