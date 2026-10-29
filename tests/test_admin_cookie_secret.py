"""The admin cookie secret is shared by every gateway on one data/ dir (#265 §5.5)."""

import logging
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import src.admin_auth as admin_auth

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("ADMIN_COOKIE_SECRET", raising=False)
    monkeypatch.setenv("ADMIN_COOKIE_SECRET_FILE", str(tmp_path / "admin_cookie_secret"))
    admin_auth._reset_cookie_secret_for_tests()
    yield
    admin_auth._reset_cookie_secret_for_tests()


def _secret_file(tmp_path: Path) -> Path:
    return tmp_path / "admin_cookie_secret"


def test_file_is_created_once_0600_and_reused(tmp_path):
    first = admin_auth._get_cookie_secret()
    path = _secret_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert bytes.fromhex(path.read_text()) == first
    assert len(first) == 32

    admin_auth._reset_cookie_secret_for_tests()  # a "restart"
    assert admin_auth._get_cookie_secret() == first


def test_token_survives_a_restart(tmp_path):
    token = admin_auth._make_session_token(int(time.time()))
    admin_auth._reset_cookie_secret_for_tests()
    assert admin_auth._verify_session_token(token)


def test_default_path_is_the_data_dir(monkeypatch):
    monkeypatch.delenv("ADMIN_COOKIE_SECRET_FILE")
    assert admin_auth.cookie_secret_path() == REPO_ROOT / "data" / "admin_cookie_secret"


def test_env_secret_is_deterministic_and_skips_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "  not-hex at all  ")
    a = admin_auth._get_cookie_secret()
    admin_auth._reset_cookie_secret_for_tests()
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "not-hex at all")
    assert admin_auth._get_cookie_secret() == a
    assert len(a) == 32
    assert not _secret_file(tmp_path).exists()

    admin_auth._reset_cookie_secret_for_tests()
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "something else")
    assert admin_auth._get_cookie_secret() != a


def test_blank_env_secret_falls_through_to_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "   ")
    admin_auth._get_cookie_secret()
    assert _secret_file(tmp_path).exists()


@pytest.mark.parametrize(
    "content",
    ["zz-not-hex", "abcd", "\xff\xfe"],
    ids=["not-hex", "too-short", "not-ascii"],
)
def test_corrupt_file_falls_back_to_a_process_secret(tmp_path, caplog, content):
    path = _secret_file(tmp_path)
    path.write_bytes(content.encode("latin-1"))
    with caplog.at_level(logging.WARNING, logger="src.admin_auth"):
        secret = admin_auth._get_cookie_secret()
    assert len(secret) == 32
    assert "per-process random secret" in caplog.text
    assert path.read_bytes() == content.encode("latin-1")  # never overwritten


def test_symlinked_file_is_not_followed(tmp_path, caplog):
    target = tmp_path / "elsewhere"
    target.write_text("ab" * 32)
    _secret_file(tmp_path).symlink_to(target)
    with caplog.at_level(logging.WARNING, logger="src.admin_auth"):
        secret = admin_auth._get_cookie_secret()
    assert secret != bytes.fromhex("ab" * 32)
    assert "unusable" in caplog.text


def test_unwritable_dir_falls_back(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("ADMIN_COOKIE_SECRET_FILE", str(blocker / "secret"))
    with caplog.at_level(logging.WARNING, logger="src.admin_auth"):
        assert len(admin_auth._get_cookie_secret()) == 32
    assert "unusable" in caplog.text


def test_race_loser_waits_for_the_winners_write(tmp_path):
    path = _secret_file(tmp_path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    value = "cd" * 32

    def finish():
        time.sleep(0.1)
        os.write(fd, value.encode())
        os.close(fd)

    t = threading.Thread(target=finish)
    t.start()
    try:
        assert admin_auth._get_cookie_secret() == bytes.fromhex(value)
    finally:
        t.join()


_CHILD = """
import sys, time
start = float(sys.argv[1])
while time.time() < start:
    pass
import src.admin_auth as a
print(a._get_cookie_secret().hex())
"""


def test_concurrent_processes_agree_on_one_secret(tmp_path):
    env = dict(os.environ)
    env["GATEWAY_SKIP_DOTENV"] = "1"
    env["ADMIN_COOKIE_SECRET_FILE"] = str(_secret_file(tmp_path))
    env.pop("ADMIN_COOKIE_SECRET", None)
    start = time.time() + 3.0
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(start)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        outputs.append(out.strip())
    assert len(set(outputs)) == 1
    assert bytes.fromhex(_secret_file(tmp_path).read_text()).hex() == outputs[0]
