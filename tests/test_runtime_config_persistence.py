"""Runtime-config overrides survive a restart / reach the other color (#265 §5.5)."""

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import src.runtime_config as rc_module
from src.runtime_config import RuntimeConfig, runtime_config_path

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg_file(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("RUNTIME_CONFIG_FILE", str(path))
    return path


def _write(path: Path, overrides, version=1) -> None:
    path.write_text(json.dumps({"version": version, "overrides": overrides}))


def _fresh() -> RuntimeConfig:
    rc = RuntimeConfig(persist=True)
    rc.load()
    return rc


def test_default_path_is_the_data_dir(monkeypatch):
    monkeypatch.delenv("RUNTIME_CONFIG_FILE", raising=False)
    assert runtime_config_path() == REPO_ROOT / "data" / "runtime_config.json"


def test_conftest_keeps_the_singleton_out_of_the_checkout():
    assert runtime_config_path() != REPO_ROOT / "data" / "runtime_config.json"


def test_set_writes_0600_and_a_fresh_instance_loads_it(cfg_file):
    rc = RuntimeConfig(persist=True)
    rc.set("default_model", "persisted-model")
    rc.set("default_max_turns", "77")
    rc.set("token_streaming", "false")
    assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600
    data = json.loads(cfg_file.read_text())
    assert data == {
        "version": 1,
        "overrides": {
            "default_model": "persisted-model",
            "default_max_turns": 77,
            "token_streaming": False,
        },
    }

    again = _fresh()
    assert again.get("default_model") == "persisted-model"
    assert again.get("default_max_turns") == 77
    assert again.get("token_streaming") is False
    assert again.is_overridden("default_model")


def test_reset_and_reset_all_persist(cfg_file):
    rc = RuntimeConfig(persist=True)
    rc.set("default_model", "m")
    rc.set("default_max_turns", 5)
    rc.reset("default_model")
    assert not _fresh().is_overridden("default_model")
    assert _fresh().get("default_max_turns") == 5
    rc.reset_all()
    assert json.loads(cfg_file.read_text())["overrides"] == {}
    assert not _fresh().is_overridden("default_max_turns")


def test_no_temp_files_left_behind(cfg_file):
    rc = RuntimeConfig(persist=True)
    for i in range(5):
        rc.set("default_max_turns", i + 1)
    assert sorted(p.name for p in cfg_file.parent.iterdir()) == [cfg_file.name]


def test_memory_only_instance_never_writes(cfg_file):
    rc = RuntimeConfig()
    rc.set("default_model", "x")
    rc.load()
    assert not cfg_file.exists()
    assert rc.get("default_model") == "x"


def test_invalid_entries_are_dropped_valid_ones_kept(cfg_file, caplog):
    _write(
        cfg_file,
        {
            "default_model": "kept",
            "no_longer_a_key": 1,
            "default_max_turns": 0,  # below min
            "max_concurrent_turns": 5000,  # above max
            "thinking_mode": "explosive",  # not an option
            "token_streaming": "yes",  # wrong JSON type
            "session_max_age_minutes": True,  # bool for an int key
            "turn_queue_max_waiters": 12,
        },
    )
    with caplog.at_level(logging.WARNING, logger="src.runtime_config"):
        rc = _fresh()
    assert {k for k in rc.get_all() if rc.is_overridden(k)} == {
        "default_model",
        "turn_queue_max_waiters",
    }
    for key in (
        "no_longer_a_key",
        "default_max_turns",
        "max_concurrent_turns",
        "thinking_mode",
        "token_streaming",
        "session_max_age_minutes",
    ):
        assert key in caplog.text
    # Loading does not rewrite the file.
    assert "no_longer_a_key" in cfg_file.read_text()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 1, "overrides": []}),
        json.dumps({"version": 2, "overrides": {"default_model": "x"}}),
        json.dumps({"overrides": {"default_model": "x"}}),
        b"\xff\xfe",
    ],
    ids=["bad-json", "list", "overrides-list", "future-version", "no-version", "binary"],
)
def test_unusable_file_is_ignored_not_fatal(cfg_file, caplog, content):
    if isinstance(content, str):
        content = content.encode()
    cfg_file.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="src.runtime_config"):
        rc = _fresh()
    assert not any(rc.is_overridden(k) for k in rc.get_all())
    assert "ignored" in caplog.text


def test_symlinked_file_is_not_followed(cfg_file, tmp_path, caplog):
    target = tmp_path / "elsewhere.json"
    _write(target, {"default_model": "planted"})
    cfg_file.symlink_to(target)
    with caplog.at_level(logging.WARNING, logger="src.runtime_config"):
        rc = _fresh()
    assert not rc.is_overridden("default_model")


def test_load_replaces_and_notifies(cfg_file):
    _write(cfg_file, {"default_max_turns": 9})
    rc = RuntimeConfig(persist=True)
    rc._overrides["default_model"] = "stale"
    seen = []
    rc.on_change(seen.append)
    rc.load()
    assert seen == [None]
    assert not rc.is_overridden("default_model")
    assert rc.get("default_max_turns") == 9


def test_failed_write_keeps_the_override_live(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("RUNTIME_CONFIG_FILE", str(blocker / "runtime_config.json"))
    rc = RuntimeConfig(persist=True)
    with caplog.at_level(logging.ERROR, logger="src.runtime_config"):
        rc.set("default_model", "still-live")
    assert rc.get("default_model") == "still-live"
    assert "not persisted" in caplog.text


def test_singleton_is_persistent():
    assert rc_module.runtime_config._persist is True


_CHILD = """
import json
from src.runtime_config import runtime_config
from src.concurrency import turn_limiter
print(json.dumps({
    "model": runtime_config.get("default_model"),
    "overridden": runtime_config.is_overridden("default_model"),
    "limiter_total": turn_limiter.snapshot()["limit"],
}))
"""


def test_a_new_process_starts_with_the_persisted_overrides(cfg_file):
    """The green color imports the module fresh and must see blue's overrides,
    including values a long-lived object caches (the turn limiter)."""
    rc = RuntimeConfig(persist=True)
    rc.set("default_model", "from-blue")
    rc.set("max_concurrent_turns", 7)

    env = dict(os.environ)
    env["GATEWAY_SKIP_DOTENV"] = "1"
    env["RUNTIME_CONFIG_FILE"] = str(cfg_file)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"model": "from-blue", "overridden": True, "limiter_total": 7}
