"""A remote marketplace refresh never removes the clone readers use (#262).

The admin refresh (``plugin_admin_service.prepare_repo``) and the startup
replay (``docker/install_plugins.py``) used to ``git clone`` into a fixed
``<dest>.new``, ``rmtree`` the registered clone and then rename: a reader of
the clone saw a half-deleted tree, a failure between the two lost it, and two
refreshes of one repo removed each other's staging dir. Both now publish
through ``plugin_local_copy.replace_tree`` (the #259 helper): a per-clone lock,
a fresh staging dir and one ``RENAME_EXCHANGE``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import plugin_admin_service as pas
from src import plugin_local_copy as lc

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://example.com/acme/mkt.git"


def _fake_clone(version: str, *, delay: float = 0.0, files: int = 20):
    """A ``run`` stand-in whose ``git clone`` writes a marketplace tree."""

    def run(argv, token=""):
        assert argv[:2] == ["git", "clone"]
        dest = Path(argv[-1])
        meta = dest / ".claude-plugin"
        meta.mkdir(parents=True, exist_ok=True)
        for i in range(files):
            (dest / f"f{i}.md").write_text(version)
            if delay:
                time.sleep(delay / files)
        (meta / "marketplace.json").write_text(json.dumps({"name": "mkt"}))
        (dest / "VERSION").write_text(version)

    return run


def _siblings(dest: Path, marker: str) -> list:
    return sorted(dest.parent.glob(dest.name + marker + "*"))


@pytest.fixture
def clones(tmp_path):
    return tmp_path / "clones"


def _refresh(clones: Path) -> Path:
    return pas.prepare_repo(REPO, branch="main", token="", root=clones)


def test_a_refresh_exchanges_the_clone(clones, monkeypatch):
    monkeypatch.setattr(pas, "run", _fake_clone("v1"))
    dest = _refresh(clones)
    assert (dest / "VERSION").read_text() == "v1"
    monkeypatch.setattr(pas, "run", _fake_clone("v2"))
    assert _refresh(clones) == dest
    assert (dest / "VERSION").read_text() == "v2"
    (old,) = _siblings(dest, lc._OLD)
    assert (old / "VERSION").read_text() == "v1", "parked, not rmtree'd first"
    assert not _siblings(dest, lc._NEW)


def test_a_failed_clone_keeps_the_previous_clone(clones, monkeypatch):
    monkeypatch.setattr(pas, "run", _fake_clone("v1"))
    dest = _refresh(clones)

    def failing(argv, token=""):
        Path(argv[-1], "partial").write_text("x")
        raise subprocess.CalledProcessError(128, argv)

    monkeypatch.setattr(pas, "run", failing)
    with pytest.raises(pas.PluginAdminError, match="git clone failed"):
        _refresh(clones)
    assert (dest / "VERSION").read_text() == "v1"
    assert not (dest / "partial").exists()
    assert not _siblings(dest, lc._NEW)


def test_a_failed_swap_keeps_the_previous_clone(clones, monkeypatch):
    monkeypatch.setattr(pas, "run", _fake_clone("v1"))
    dest = _refresh(clones)
    monkeypatch.setattr(pas, "run", _fake_clone("v2"))

    def boom(a, b):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(lc, "_exchange", boom)
    with pytest.raises(pas.PluginAdminError, match="could not publish"):
        _refresh(clones)
    assert (dest / "VERSION").read_text() == "v1"
    assert not _siblings(dest, lc._NEW)


def test_without_exchange_the_refresh_fails_closed(clones, monkeypatch):
    monkeypatch.setattr(pas, "run", _fake_clone("v1"))
    dest = _refresh(clones)
    monkeypatch.setattr(pas, "run", _fake_clone("v2"))
    monkeypatch.setattr(
        lc, "_exchange", lambda a, b: (_ for _ in ()).throw(OSError(22, "x"))
    )
    with pytest.raises(pas.PluginAdminError, match="current copy is kept"):
        _refresh(clones)
    assert (dest / "VERSION").read_text() == "v1"


def test_a_reader_never_sees_a_partial_clone(clones, monkeypatch):
    monkeypatch.setattr(pas, "run", _fake_clone("v0"))
    dest = _refresh(clones)
    stop = threading.Event()
    errors: list = []

    def read():
        # A reader that opened the clone reads one whole tree: the old one or
        # the new one (what the CLI's marketplace walk relies on).
        while not stop.is_set():
            try:
                fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY)
            except OSError as exc:
                errors.append(f"open: {exc!r}")
                continue
            try:
                names = [n for n in os.listdir(fd) if n.startswith("f")]
                versions = set()
                for name in names:
                    with open(os.open(name, os.O_RDONLY, dir_fd=fd)) as f:
                        versions.add(f.read())
                if len(names) != 20 or len(versions) != 1:
                    errors.append(f"partial: {len(names)} files, {versions}")
            except OSError as exc:
                errors.append(repr(exc))
            finally:
                os.close(fd)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for n in range(1, 15):
            monkeypatch.setattr(pas, "run", _fake_clone(f"v{n}"))
            _refresh(clones)
    finally:
        stop.set()
        reader.join()
    assert errors == []


def test_concurrent_refreshes_of_one_repo_never_collide(clones, monkeypatch):
    monkeypatch.setattr(pas, "run", _fake_clone("v", delay=0.02))
    errors: list = []

    def refresh():
        try:
            for _ in range(3):
                _refresh(clones)
        except Exception as exc:  # noqa: BLE001 - collected for the assert
            errors.append(repr(exc))

    workers = [threading.Thread(target=refresh) for _ in range(5)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert errors == []
    dest = pas._clone_dir(clones, REPO)
    assert len(list(dest.glob("f*.md"))) == 20
    assert not _siblings(dest, lc._NEW)


def test_a_legacy_fixed_staging_dir_is_removed(clones, monkeypatch):
    dest = pas._clone_dir(clones, REPO)
    legacy = dest.with_name(dest.name + ".new")
    legacy.mkdir(parents=True)
    (legacy / "junk").write_text("x")
    monkeypatch.setattr(pas, "run", _fake_clone("v1"))
    _refresh(clones)
    assert not legacy.exists()


# --- the startup replay ------------------------------------------------------


def _fake_git(bin_dir: Path) -> None:
    fake = bin_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do dest="$a"; done\n'
        'mkdir -p "$dest/.claude-plugin"\n'
        'echo fresh > "$dest/VERSION"\n'
    )
    fake.chmod(0o755)
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)


def test_the_startup_replay_exchanges_the_clone(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_git(bin_dir)
    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {"version": 1, "added": [{"repo": REPO, "name": "foo"}], "removed": []}
        )
    )
    clones = tmp_path / "clones"
    dest = pas._clone_dir(clones, REPO)
    dest.mkdir(parents=True)
    (dest / "VERSION").write_text("previous")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE_PLUGIN", "CLAUDE_ISOLATION"))
    }
    env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{bin_dir}:/usr/bin:/bin",
        CLAUDE_PLUGIN_MANIFEST=str(manifest),
        CLAUDE_PLUGIN_CLONE_ROOT=str(clones),
    )
    proc = subprocess.run(
        [sys.executable, str(ROOT / "docker" / "install_plugins.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert (dest / "VERSION").read_text().strip() == "fresh"
    (old,) = _siblings(dest, lc._OLD)
    assert (old / "VERSION").read_text() == "previous"
    assert not _siblings(dest, lc._NEW)
