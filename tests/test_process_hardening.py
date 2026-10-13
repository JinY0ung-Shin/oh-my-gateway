"""Pin the gateway's non-dumpable hardening (src/process_hardening.py).

Every session tool runs as the gateway's uid, so a dumpable gateway lets them
open its /proc/<pid>/environ, mem and fds. The unit tests pin the env parsing
and the never-raise failure paths with a fake prctl, so the test runner itself
is never made non-dumpable. The Linux tests apply the real prctl
in a child process and check, from this same-uid test process, whether the
child's /proc entries can still be opened (nothing is read from them).
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import src.main as main
from src import process_hardening
from src.process_hardening import (
    NON_DUMPABLE_ENV,
    apply_non_dumpable_policy,
    non_dumpable_enabled,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4


@pytest.mark.parametrize("raw", [None, "", "  ", "true", "1", "YES", "on", "bogus"])
def test_enabled_unless_explicitly_false(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(NON_DUMPABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(NON_DUMPABLE_ENV, raw)
    assert non_dumpable_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", " FALSE ", "Off"])
def test_explicit_false_opts_out(monkeypatch, raw):
    monkeypatch.setenv(NON_DUMPABLE_ENV, raw)
    assert non_dumpable_enabled() is False


class _FakePrctl:
    """Stands in for libc.prctl: records each call's args, returns scripted results."""

    def __init__(self, set_result=0, get_result=0, raises=None):
        self.set_result = set_result
        self.get_result = get_result
        self.raises = raises
        self.calls = []

    def __call__(self, option, *args):
        self.calls.append((option.value, *(arg.value for arg in args)))
        if self.raises is not None:
            raise self.raises
        if option.value == PR_SET_DUMPABLE:
            return self.set_result
        return self.get_result


def _install(monkeypatch, prctl=None, cdll_error=None, errno_value=0):
    class _Libc:
        pass

    libc = _Libc()
    if prctl is not None:
        libc.prctl = prctl

    def fake_cdll(*_args, **_kwargs):
        if cdll_error is not None:
            raise cdll_error
        return libc

    monkeypatch.setattr(process_hardening.sys, "platform", "linux")
    monkeypatch.setattr(process_hardening.ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(process_hardening.ctypes, "get_errno", lambda: errno_value)
    monkeypatch.setenv(NON_DUMPABLE_ENV, "true")


SET_CALL = (PR_SET_DUMPABLE, 0, 0, 0, 0)  # prctl(PR_SET_DUMPABLE, 0)
GET_CALL = (PR_GET_DUMPABLE, 0, 0, 0, 0)


def test_sets_then_reads_back_and_logs_on(monkeypatch, caplog):
    prctl = _FakePrctl()
    _install(monkeypatch, prctl)
    with caplog.at_level(logging.INFO, logger="src.process_hardening"):
        assert apply_non_dumpable_policy() == "on"
    assert prctl.calls == [SET_CALL, GET_CALL]
    assert "non-dumpable" in caplog.text


@pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
def test_opt_out_never_calls_prctl_and_echoes_the_value(monkeypatch, caplog, raw):
    prctl = _FakePrctl()
    _install(monkeypatch, prctl)
    monkeypatch.setenv(NON_DUMPABLE_ENV, raw)
    with caplog.at_level(logging.WARNING, logger="src.process_hardening"):
        assert apply_non_dumpable_policy() == "off"
    assert prctl.calls == []
    assert f"{NON_DUMPABLE_ENV}={raw}: " in caplog.text
    assert "stays dumpable" in caplog.text


def _assert_failed(caplog, expected):
    with caplog.at_level(logging.WARNING, logger="src.process_hardening"):
        assert apply_non_dumpable_policy() == "failed"
    assert "Could not make the gateway process non-dumpable" in caplog.text
    assert expected in caplog.text


def test_non_linux_is_unsupported_at_info_without_calling_prctl(monkeypatch, caplog):
    prctl = _FakePrctl()
    _install(monkeypatch, prctl)
    monkeypatch.setattr(process_hardening.sys, "platform", "darwin")
    with caplog.at_level(logging.INFO, logger="src.process_hardening"):
        assert apply_non_dumpable_policy() == "unsupported"
    assert prctl.calls == []
    [record] = caplog.records
    assert record.levelno == logging.INFO
    assert "unsupported on darwin" in record.getMessage()
    assert "/proc" not in record.getMessage()


def test_missing_libc_is_reported_not_raised(monkeypatch, caplog):
    _install(monkeypatch, cdll_error=OSError("no libc"))
    _assert_failed(caplog, "prctl unavailable")


def test_missing_prctl_symbol_is_reported_not_raised(monkeypatch, caplog):
    _install(monkeypatch, prctl=None)
    _assert_failed(caplog, "prctl unavailable")


def test_refused_prctl_is_reported_not_raised(monkeypatch, caplog):
    prctl = _FakePrctl(set_result=-1)
    _install(monkeypatch, prctl, errno_value=errno.EPERM)
    _assert_failed(caplog, os.strerror(errno.EPERM))
    assert prctl.calls == [SET_CALL]


def test_still_dumpable_after_prctl_is_reported(monkeypatch, caplog):
    _install(monkeypatch, _FakePrctl(get_result=1))
    _assert_failed(caplog, "still dumpable")


def test_unexpected_prctl_error_is_reported_not_raised(monkeypatch, caplog):
    _install(monkeypatch, _FakePrctl(raises=ctypes.ArgumentError("bad")))
    _assert_failed(caplog, "unexpected error")


class _Stop(Exception):
    pass


async def test_lifespan_applies_policy_before_any_other_startup_step(monkeypatch):
    calls = []

    def fake_policy():
        calls.append("policy")
        raise _Stop

    def fake_validate_admin_config():
        # Stop here too, so a policy call moved later fails fast at step one
        # instead of running the rest of the real startup first.
        calls.append("admin")
        raise _Stop

    monkeypatch.setattr(process_hardening, "apply_non_dumpable_policy", fake_policy)
    monkeypatch.setattr(
        "src.admin_auth.validate_admin_config", fake_validate_admin_config
    )
    with pytest.raises(_Stop):
        async with main.lifespan(main.app):
            pass
    assert calls == ["policy"]


# --- real prctl, in child processes ---------------------------------------

linux_non_root = pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() == 0,
    reason="needs Linux /proc and a non-root uid (root reads any /proc entry)",
)
_CHILD_TIMEOUT = 30

_CHILD = """
import ctypes, os, sys
from src.process_hardening import apply_non_dumpable_policy
mode = sys.argv[1]
status = apply_non_dumpable_policy() if mode != "skip" else "skipped"
if mode == "exec":
    # Same pid after execve: the kernel resets dumpable for an ordinary binary.
    code = (
        "import ctypes, sys;"
        "print(sys.argv[1], 'exec', ctypes.CDLL(None).prctl(3, 0, 0, 0, 0),"
        " flush=True);"
        "sys.stdin.read()"
    )
    os.execv(sys.executable, [sys.executable, "-c", code, status])
print(status, flush=True)
sys.stdin.read()
"""


@contextmanager
def _child(mode):
    env = {**os.environ, NON_DUMPABLE_ENV: "true"}
    with subprocess.Popen(
        [sys.executable, "-c", _CHILD, mode],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            ready, _, _ = select.select([proc.stdout], [], [], _CHILD_TIMEOUT)
            assert ready, f"child {mode!r} printed nothing in {_CHILD_TIMEOUT}s"
            yield proc.pid, proc.stdout.readline().strip()
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=_CHILD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _can_open(path: str) -> bool:
    """Open and close without reading anything: the access check runs at open()."""
    try:
        os.close(os.open(path, os.O_RDONLY))
    except PermissionError:
        return False
    return True


@linux_non_root
def test_same_uid_process_cannot_open_environ_or_mem_of_non_dumpable_child():
    with _child("apply") as (pid, status):
        assert status == "on"
        assert not _can_open(f"/proc/{pid}/environ")
        assert not _can_open(f"/proc/{pid}/mem")


@linux_non_root
def test_control_child_without_the_call_stays_openable():
    with _child("skip") as (pid, status):
        assert status == "skipped"
        assert _can_open(f"/proc/{pid}/environ")


@linux_non_root
def test_execve_makes_the_process_dumpable_again():
    # Why exec'd CLI children are not covered: the same pid, after execve.
    with _child("exec") as (pid, status):
        assert status == "on exec 1"
        assert _can_open(f"/proc/{pid}/environ")
