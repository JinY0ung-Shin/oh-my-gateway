"""uid mode's ticket secret reaches the gateway through a pipe, not the env.

PR #244 follow-up (remaining-problems comment, §2): the entrypoint used to exec
the image CMD with ``GATEWAY_ISOLATION_TICKET_SECRET`` in the environment, so
every startup step before uvicorn (the ``sh -c`` wrapper,
``GATEWAY_STARTUP_SCRIPT``, ``install_plugins.py`` and its children) got the
secret while the broker already accepted tickets signed with it, and uvicorn's
exec-time environment kept a copy. These pin both halves of the handoff
(``docker/entrypoint.py`` ``hand_off_ticket_secret`` and
``uid_isolation.capture_ticket_secret``) and run the real entrypoint
unprivileged: a non-root start skips the root block.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.backends.claude import uid_isolation as ui

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.py"
SECRET_ENV = ui.TICKET_SECRET_ENV
FD_ENV = ui.TICKET_SECRET_FD_ENV


@pytest.fixture(scope="module")
def entrypoint():
    spec = importlib.util.spec_from_file_location(
        "docker_entrypoint_secret_handoff", ENTRYPOINT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {name: os.environ.pop(name, None) for name in (SECRET_ENV, FD_ENV)}
    ui.reset_captured_secret()
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    ui.reset_captured_secret()


@pytest.fixture
def no_hang():
    """Fail instead of hanging the suite if a read blocks on a pipe."""

    def _blocked(*_args):
        raise TimeoutError("blocked reading the ticket secret pipe")

    previous = signal.signal(signal.SIGALRM, _blocked)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def _pipe_holding(data: bytes) -> int:
    """A pipe's read end holding *data*, its write end already closed."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, data)
    finally:
        os.close(write_fd)
    return read_fd


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _fd_value(fd: int) -> str:
    """``<fd>:<dev>:<ino>`` naming *fd*, as the entrypoint writes it."""
    st = os.fstat(fd)
    return f"{fd}:{st.st_dev}:{st.st_ino}"


def _fd_of(value: str) -> int:
    return int(value.split(":")[0])


def _child_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in (SECRET_ENV, FD_ENV)}
    env["GATEWAY_SKIP_DOTENV"] = "1"
    env.update(extra)
    return env


class TestEntrypointHandoff:
    def test_names_and_limit_match_the_gateway(self, entrypoint):
        assert entrypoint.TICKET_SECRET_ENV == ui.TICKET_SECRET_ENV
        assert entrypoint.TICKET_SECRET_FD_ENV == ui.TICKET_SECRET_FD_ENV
        assert entrypoint.MAX_TICKET_SECRET_BYTES == ui._SECRET_FD_MAX_BYTES

    def test_moves_the_secret_into_an_inheritable_pipe(self, entrypoint):
        os.environ[SECRET_ENV] = "s3cret"
        entrypoint.hand_off_ticket_secret()

        assert SECRET_ENV not in os.environ
        fd = _fd_of(os.environ[FD_ENV])
        try:
            assert stat.S_ISFIFO(os.fstat(fd).st_mode)
            # The value names the pipe itself, not just a number.
            assert os.environ[FD_ENV] == _fd_value(fd)
            assert os.get_inheritable(fd)
            os.set_blocking(fd, False)
            assert os.read(fd, 4096) == b"s3cret"
            # EOF, not EAGAIN: no write end is left open in this process.
            assert os.read(fd, 4096) == b""
        finally:
            os.close(fd)

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_no_secret_means_no_pipe(self, entrypoint, value):
        if value is not None:
            os.environ[SECRET_ENV] = value
        entrypoint.hand_off_ticket_secret()
        assert FD_ENV not in os.environ
        assert os.environ.get(SECRET_ENV) == value

    def test_a_secret_at_the_limit_is_handed_over(self, entrypoint):
        limit = entrypoint.MAX_TICKET_SECRET_BYTES
        os.environ[SECRET_ENV] = "x" * limit
        entrypoint.hand_off_ticket_secret()
        fd = _fd_of(os.environ[FD_ENV])
        try:
            assert os.read(fd, limit + 1) == b"x" * limit
        finally:
            os.close(fd)

    def test_an_oversized_secret_stops_instead_of_blocking(self, entrypoint):
        limit = entrypoint.MAX_TICKET_SECRET_BYTES
        os.environ[SECRET_ENV] = "x" * (limit + 1)
        with pytest.raises(SystemExit) as excinfo:
            entrypoint.hand_off_ticket_secret()
        assert str(limit) in str(excinfo.value.code)
        assert FD_ENV not in os.environ

    def test_a_closed_stdin_never_becomes_the_pipe(self):
        # With fd 0 closed, os.pipe() hands out 0: the command would read the
        # secret as its stdin. The read end must move above the stdio range.
        code = textwrap.dedent(
            f"""
            import importlib.util, json, os
            os.close(0)
            spec = importlib.util.spec_from_file_location("ep", {str(ENTRYPOINT)!r})
            ep = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ep)
            ep.hand_off_ticket_secret()
            fd = int(os.environ[{FD_ENV!r}].split(":")[0])
            try:
                os.fstat(0)
                stdin_open = True
            except OSError:
                stdin_open = False
            print(json.dumps({{
                "fd": fd,
                "inheritable": os.get_inheritable(fd),
                "data": os.read(fd, 100).decode(),
                "stdin_open": stdin_open,
            }}))
            """
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code],
            env=_child_env(**{SECRET_ENV: "s3cret"}),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode == 0, out.stderr
        facts = json.loads(out.stdout)
        assert facts["fd"] > 2
        assert facts["inheritable"] is True
        assert facts["data"] == "s3cret"
        assert facts["stdin_open"] is False

    def test_main_hands_off_before_the_exec(self, entrypoint, monkeypatch):
        seen = {}

        class _Exec(Exception):
            pass

        def execvp(*_args):
            seen.update(os.environ)
            raise _Exec

        class _Os:
            def __getattr__(self, name):
                return getattr(os, name)

        fake_os = _Os()
        fake_os.geteuid = lambda: 1000  # a non-root start: no root block
        fake_os.execvp = execvp
        monkeypatch.setattr(entrypoint, "os", fake_os)
        os.environ[SECRET_ENV] = "s3cret"
        try:
            with pytest.raises(_Exec):
                entrypoint.main(["true"])
            assert SECRET_ENV not in seen
            fd = _fd_of(seen[FD_ENV])
            assert os.read(fd, 100) == b"s3cret"
        finally:
            fd_value = os.environ.get(FD_ENV)
            if fd_value is not None:
                os.close(_fd_of(fd_value))


class TestGatewayCapture:
    def test_reads_and_closes_the_pipe(self):
        fd = _pipe_holding(b"s3cret\n")
        os.environ[FD_ENV] = _fd_value(fd)
        ui.capture_ticket_secret()

        assert ui.ticket_secret() == b"s3cret"
        assert FD_ENV not in os.environ
        assert not _is_open(fd)
        # minting and verifying work from the captured copy
        t = ui.mint_ticket(uid=20001, workspace="/ws", ro=[], rw=[], env_allow=[])
        assert ui.verify_ticket(t)["uid"] == 20001

    def test_capture_is_idempotent(self):
        os.environ[FD_ENV] = _fd_value(_pipe_holding(b"s3cret"))
        ui.capture_ticket_secret()
        ui.capture_ticket_secret()  # pipe gone, variable gone: keeps the secret
        assert ui.ticket_secret() == b"s3cret"

    def test_the_variable_wins_and_the_pipe_is_still_closed(self):
        fd = _pipe_holding(b"from-pipe")
        os.environ[SECRET_ENV] = "from-env"
        os.environ[FD_ENV] = _fd_value(fd)
        ui.capture_ticket_secret()

        assert ui.ticket_secret() == b"from-env"
        assert SECRET_ENV not in os.environ
        assert FD_ENV not in os.environ
        assert not _is_open(fd)

    def test_a_blank_variable_falls_back_to_the_pipe(self):
        os.environ[SECRET_ENV] = "   "
        os.environ[FD_ENV] = _fd_value(_pipe_holding(b"from-pipe"))
        ui.capture_ticket_secret()
        assert ui.ticket_secret() == b"from-pipe"

    @pytest.mark.parametrize("kind", ["file", "socket"])
    def test_a_descriptor_that_is_not_a_pipe_is_left_alone(
        self, kind, tmp_path, caplog
    ):
        if kind == "file":
            path = tmp_path / "not-a-secret"
            path.write_text("not-a-secret")
            fd = os.open(path, os.O_RDONLY)
            keep = []
        else:
            left, right = socket.socketpair()
            right.sendall(b"not-a-secret")
            fd = left.fileno()
            keep = [left, right]
        os.environ[FD_ENV] = _fd_value(fd)
        try:
            with caplog.at_level(logging.WARNING, logger=ui.__name__):
                ui.capture_ticket_secret()
            assert ui.ticket_secret() is None
            assert _is_open(fd)  # never closed: it is this process's own
            assert "not the entrypoint's pipe" in caplog.text
            assert FD_ENV not in os.environ
        finally:
            if kind == "file":
                os.close(fd)
            for sock in keep:
                sock.close()

    @pytest.mark.parametrize("data", [b"", b"  \n"])
    def test_an_empty_pipe_warns_and_yields_no_secret(self, data, caplog):
        fd = _pipe_holding(data)
        os.environ[FD_ENV] = _fd_value(fd)
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            ui.capture_ticket_secret()
        assert ui.ticket_secret() is None
        assert "was empty" in caplog.text
        assert not _is_open(fd)

    def test_an_open_writer_never_blocks_the_read(self, no_hang, caplog):
        # Another process still holding the write end (a CMD step's background
        # child, say) must not wedge startup: read what is there and stop.
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"s3cret")
            os.environ[FD_ENV] = _fd_value(read_fd)
            ui.capture_ticket_secret()
            assert ui.ticket_secret() == b"s3cret"
            assert not _is_open(read_fd)

            ui.reset_captured_secret()
            read_fd2, write_fd2 = os.pipe()
            try:
                os.environ[FD_ENV] = _fd_value(read_fd2)
                with caplog.at_level(logging.WARNING, logger=ui.__name__):
                    ui.capture_ticket_secret()
                assert ui.ticket_secret() is None
                assert "was empty" in caplog.text
            finally:
                os.close(write_fd2)
        finally:
            os.close(write_fd)

    def test_a_secret_at_the_limit_is_read_and_one_byte_more_is_refused(self, caplog):
        limit = ui._SECRET_FD_MAX_BYTES
        os.environ[FD_ENV] = _fd_value(_pipe_holding(b"x" * limit))
        ui.capture_ticket_secret()
        assert ui.ticket_secret() == b"x" * limit

        ui.reset_captured_secret()
        fd = _pipe_holding(b"x" * (limit + 1))
        os.environ[FD_ENV] = _fd_value(fd)
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            ui.capture_ticket_secret()
        assert ui.ticket_secret() is None
        assert f"more than {limit} bytes" in caplog.text
        assert not _is_open(fd)

    def test_a_non_utf8_pipe_is_refused(self, caplog):
        os.environ[FD_ENV] = _fd_value(_pipe_holding(b"\xff\xfe"))
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            ui.capture_ticket_secret()
        assert ui.ticket_secret() is None
        assert "not UTF-8" in caplog.text

    def test_another_pipe_under_that_number_is_left_alone(self, caplog):
        # A uvicorn --workers/--reload worker inherits the variable but not the
        # descriptor; under that number it may hold a pipe of its own
        # (multiprocessing's parent sentinel). Never read or close it.
        theirs = _pipe_holding(b"s3cret")
        ours = _pipe_holding(b"worker-private")
        try:
            th = os.fstat(theirs)
            os.environ[FD_ENV] = f"{ours}:{th.st_dev}:{th.st_ino}"
            with caplog.at_level(logging.WARNING, logger=ui.__name__):
                ui.capture_ticket_secret()
            assert ui.ticket_secret() is None
            assert _is_open(ours)
            assert os.read(ours, 100) == b"worker-private"  # unread
            assert "not the entrypoint's pipe" in caplog.text
            assert "--workers" in caplog.text
        finally:
            os.close(theirs)
            os.close(ours)

    @pytest.mark.parametrize(
        "value",
        ["abc", "-1", "3.5", "999999", "7", "7:1", "0:1:2", "2:1:2", "999999:1:2"],
        ids=[
            "text",
            "negative",
            "float",
            "bare-closed",
            "bare-number",
            "two-parts",
            "stdin",
            "stderr",
            "closed",
        ],
    )
    def test_a_bad_descriptor_value_is_ignored(self, value, caplog):
        os.environ[FD_ENV] = value
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            ui.capture_ticket_secret()
        assert ui.ticket_secret() is None
        assert FD_ENV not in os.environ
        assert f"Ignoring {FD_ENV}" in caplog.text

    def test_a_blank_descriptor_value_is_dropped_quietly(self, caplog):
        os.environ[FD_ENV] = "  "
        with caplog.at_level(logging.WARNING, logger=ui.__name__):
            ui.capture_ticket_secret()
        assert ui.ticket_secret() is None
        assert FD_ENV not in os.environ
        assert caplog.text == ""


_GATEWAY_STAND_IN = """
import json, os, stat, sys
sys.path.insert(0, sys.argv[1])
fd_value = os.environ.get({fd_env!r})
fd = int(fd_value.split(":")[0]) if fd_value is not None else None
facts = {{
    "secret_in_env": {secret_env!r} in os.environ,
    "fd_is_fifo": fd is not None and stat.S_ISFIFO(os.fstat(fd).st_mode),
}}
from src.backends.claude import uid_isolation as ui
ui.capture_ticket_secret()
secret = ui.ticket_secret()
facts["captured"] = secret.decode() if secret else None
facts["fd_env_left"] = {fd_env!r} in os.environ
facts["fd_still_open"] = False
if fd is not None:
    try:
        os.fstat(fd)
        facts["fd_still_open"] = True
    except OSError:
        pass
print(json.dumps(facts))
"""

# Stands in for the CMD steps before uvicorn (install_plugins.py and the
# startup script): it must not see the secret, and its Python subprocess
# children (git, ``claude plugin``) must not even get the descriptor.
_STARTUP_STEP_STAND_IN = """
import json, os, subprocess, sys
fd = int(os.environ[{fd_env!r}].split(":")[0])
child = subprocess.run(
    [sys.executable, "-c", "import os, sys; os.fstat(int(sys.argv[1]))", str(fd)],
    capture_output=True,
)
print(json.dumps({{
    "secret_in_env": {secret_env!r} in os.environ,
    "fd_open_here": os.get_inheritable(fd),
    "subprocess_child_has_fd": child.returncode == 0,
}}), file=sys.stderr)
"""


@pytest.mark.skipif(
    os.geteuid() == 0, reason="a root start would run the entrypoint's root block"
)
class TestEntrypointEndToEnd:
    def _scripts(self, tmp_path):
        gateway = tmp_path / "gateway_stand_in.py"
        gateway.write_text(
            _GATEWAY_STAND_IN.format(fd_env=FD_ENV, secret_env=SECRET_ENV)
        )
        step = tmp_path / "startup_step_stand_in.py"
        step.write_text(
            _STARTUP_STEP_STAND_IN.format(fd_env=FD_ENV, secret_env=SECRET_ENV)
        )
        return gateway, step

    def _run(self, argv, tmp_path):
        return subprocess.run(
            [sys.executable, "-I", str(ENTRYPOINT), *argv],
            env=_child_env(**{SECRET_ENV: "s3cret-e2e"}),
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_the_command_gets_a_pipe_not_the_secret(self, tmp_path):
        gateway, _ = self._scripts(tmp_path)
        out = self._run([sys.executable, str(gateway), str(ROOT)], tmp_path)
        assert out.returncode == 0, out.stderr
        facts = json.loads(out.stdout.strip().splitlines()[-1])
        assert facts == {
            "secret_in_env": False,
            "fd_is_fifo": True,
            "captured": "s3cret-e2e",
            "fd_env_left": False,
            "fd_still_open": False,
        }

    def test_the_image_cmd_shape(self, tmp_path):
        # Dockerfile CMD: sh -c '<startup steps> && exec python -m uvicorn ...'
        gateway, step = self._scripts(tmp_path)
        python = shlex.quote(sys.executable)
        script = (
            f"{python} {shlex.quote(str(step))} && "
            f"exec {python} {shlex.quote(str(gateway))} {shlex.quote(str(ROOT))}"
        )
        out = self._run(["sh", "-c", script], tmp_path)
        assert out.returncode == 0, out.stderr
        step_facts = json.loads(out.stderr.strip().splitlines()[-1])
        assert step_facts == {
            "secret_in_env": False,
            "fd_open_here": True,
            "subprocess_child_has_fd": False,
        }
        facts = json.loads(out.stdout.strip().splitlines()[-1])
        assert facts["secret_in_env"] is False
        assert facts["captured"] == "s3cret-e2e"
