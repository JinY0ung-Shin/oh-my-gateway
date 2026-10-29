"""A local marketplace a confined session cannot read is copied, not lost (#258).

With Landlock confinement on, a local marketplace registered in place under
the gateway app root (``/app/data/plugins/<mp>``), the workspace base or the
temp dir installs and lists fine while every session is refused its files
(#255 only warns). ``src/plugin_local_copy.py`` copies it into the clone root,
which sessions read, for both the admin path (``plugin_admin_service``) and the
startup replay (``docker/install_plugins.py``). The copy lands where every
session reads, so these pin that it never carries more than the marketplace's
own regular files: no followed or escaping link, no hard link, no special
file, bounded size, and no broad source such as ``/app`` or ``/app/data``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from src import plugin_local_copy as lc

ROOT = Path(__file__).resolve().parents[1]


def _marketplace(path: Path, name: str = "inside-mp") -> Path:
    (path / ".claude-plugin").mkdir(parents=True)
    (path / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": name, "owner": {"name": "t"}, "plugins": []})
    )
    skill = path / "tool" / "skills" / "probe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: probe\n---\nbody\n")
    return path


@pytest.fixture
def app(tmp_path) -> Path:
    """A fake gateway app root holding a secret and a marketplace."""
    root = tmp_path / "app"
    (root / "data").mkdir(parents=True)
    (root / "data" / "secret.json").write_text('{"token": "s3cret"}')
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("")
    _marketplace(root / "data" / "plugins" / "inside-mp")
    return root


CONFINED = {"CLAUDE_ISOLATION_MODE": "landlock"}


class TestNeedsCopy:
    def test_only_with_confinement_on(self, app):
        source = app / "data" / "plugins" / "inside-mp"
        assert lc.needs_copy(source, app_root=app, env=CONFINED)
        assert lc.needs_copy(source, app_root=app, env={"CLAUDE_ISOLATION_MODE": "uid"})
        assert not lc.needs_copy(source, app_root=app, env={})
        assert not lc.needs_copy(
            source, app_root=app, env={"CLAUDE_ISOLATION_MODE": "off"}
        )

    def test_a_readable_path_stays_in_place(self, app, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(app / "systmp"))
        assert not lc.needs_copy("/srv/plugins/mp", app_root=app, env=CONFINED)

    def test_the_workspace_base_and_temp_dir(self, app, tmp_path):
        env = {**CONFINED, "USER_WORKSPACES_DIR": str(tmp_path / "ws")}
        assert lc.needs_copy(tmp_path / "ws" / "alice" / "mp", app_root=app, env=env)
        assert lc.needs_copy(
            Path(tempfile.gettempdir()) / "mp", app_root=app, env=CONFINED
        )


class TestSourceProblem:
    @pytest.mark.parametrize("rel", ["", "data"])
    def test_the_app_root_and_its_data_dir_are_refused(self, app, rel):
        _marketplace(app / rel, name="broad")  # even when it looks like one
        problem = lc.source_problem(app / rel, app_root=app, env={})
        assert problem and "gateway's" in problem

    def test_a_dir_holding_home_is_refused(self, app, tmp_path):
        home = tmp_path / "outer" / "home"
        home.mkdir(parents=True)
        _marketplace(tmp_path / "outer")
        problem = lc.source_problem(
            tmp_path / "outer", app_root=app, env={"HOME": str(home)}
        )
        assert problem and "contains $HOME" in problem

    def test_a_non_marketplace_is_refused(self, app, tmp_path):
        (tmp_path / "plain").mkdir()
        assert "not a marketplace" in lc.source_problem(
            tmp_path / "plain", app_root=app, env={}
        )

    def test_a_linked_manifest_is_not_a_marketplace(self, app, tmp_path):
        mp = tmp_path / "linked"
        (mp / ".claude-plugin").mkdir(parents=True)
        (mp / ".claude-plugin" / "marketplace.json").symlink_to(
            app / "data" / "secret.json"
        )
        assert "not a marketplace" in lc.source_problem(mp, app_root=app, env={})


class TestCopy:
    def test_copies_the_marketplace(self, app, tmp_path):
        source = app / "data" / "plugins" / "inside-mp"
        script = source / "tool" / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o700)
        dest = tmp_path / "clones" / "inside-mp-x"
        assert lc.copy_marketplace(source, dest, app_root=app, env={}) == []
        assert (dest / "tool" / "skills" / "probe" / "SKILL.md").read_text()
        assert (dest / "tool" / "run.sh").stat().st_mode & 0o777 == 0o755
        assert (
            dest / ".claude-plugin" / "marketplace.json"
        ).stat().st_mode & 0o777 == (0o644), "readable by every session uid"
        assert not dest.with_name(dest.name + ".new").exists()

    def test_links_out_of_the_tree_are_dropped_inside_ones_kept(self, app, tmp_path):
        source = app / "data" / "plugins" / "inside-mp"
        (source / "abs").symlink_to(app / "data" / "secret.json")
        (source / "up").symlink_to("../../secret.json")
        (source / "tool" / "esc").symlink_to("../../../secret.json")
        (source / "tool" / "skills" / "alias").symlink_to("probe")
        (source / "dirlink").symlink_to(app / "data")
        dest = tmp_path / "copy"
        skipped = lc.copy_marketplace(source, dest, app_root=app, env={})
        assert sorted(skipped) == ["abs", "dirlink", "tool/esc", "up"]
        assert os.readlink(dest / "tool" / "skills" / "alias") == "probe"
        found = [p for p in dest.rglob("*") if p.is_file()]
        assert all("s3cret" not in p.read_text() for p in found)

    def test_hard_links_and_special_files_are_skipped(self, app, tmp_path):
        source = app / "data" / "plugins" / "inside-mp"
        os.link(app / "data" / "secret.json", source / "hard.json")
        os.mkfifo(source / "fifo")
        skipped = lc.copy_marketplace(source, tmp_path / "copy", app_root=app, env={})
        assert sorted(skipped) == ["fifo", "hard.json"]
        assert not (tmp_path / "copy" / "hard.json").exists()

    def test_a_cap_fails_and_keeps_the_previous_copy(self, app, tmp_path):
        source = app / "data" / "plugins" / "inside-mp"
        dest = tmp_path / "copy"
        lc.copy_marketplace(source, dest, app_root=app, env={})
        (source / "big.bin").write_bytes(b"x" * 4096)
        with pytest.raises(lc.LocalCopyError, match="bytes"):
            lc.copy_marketplace(source, dest, app_root=app, env={}, max_bytes=1024)
        with pytest.raises(lc.LocalCopyError, match="entries"):
            lc.copy_marketplace(source, dest, app_root=app, env={}, max_files=3)
        assert (dest / "tool" / "skills" / "probe" / "SKILL.md").is_file()
        assert not (dest / "big.bin").exists()
        assert not list(tmp_path.glob("copy.new-*"))

    def test_a_refresh_replaces_the_copy(self, app, tmp_path):
        source = app / "data" / "plugins" / "inside-mp"
        dest = tmp_path / "copy"
        lc.copy_marketplace(source, dest, app_root=app, env={})
        (source / "tool" / "skills" / "probe" / "SKILL.md").write_text("v2")
        (source / "tool" / "new.md").write_text("n")
        lc.copy_marketplace(source, dest, app_root=app, env={})
        assert (dest / "tool" / "skills" / "probe" / "SKILL.md").read_text() == "v2"
        assert (dest / "tool" / "new.md").is_file()

    def test_a_broad_source_is_refused_before_copying(self, app, tmp_path):
        with pytest.raises(lc.LocalCopyError, match="gateway's data dir"):
            lc.copy_marketplace(app / "data", tmp_path / "copy", app_root=app, env={})
        assert not (tmp_path / "copy").exists()


def _siblings(dest: Path, marker: str) -> list:
    return sorted(dest.parent.glob(dest.name + marker + "*"))


class TestPublish:
    """The swap keeps the published copy readable, whatever fails (#259 review)."""

    @pytest.fixture
    def source(self, app):
        return app / "data" / "plugins" / "inside-mp"

    def _skill(self, root: Path) -> Path:
        return root / "tool" / "skills" / "probe" / "SKILL.md"

    def test_a_failed_exchange_keeps_the_copy(self, app, source, tmp_path, monkeypatch):
        dest = tmp_path / "copy"
        lc.copy_marketplace(source, dest, app_root=app, env={})
        self._skill(source).write_text("v2")

        def boom(a, b):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(lc, "_exchange", boom)
        with pytest.raises(OSError, match="I/O error"):
            lc.copy_marketplace(source, dest, app_root=app, env={})
        assert self._skill(dest).read_text().startswith("---"), "the old copy"
        assert not _siblings(dest, lc._NEW) and not _siblings(dest, lc._OLD)

    @pytest.mark.parametrize("err", [22, 38, 95])  # EINVAL, ENOSYS, EOPNOTSUPP
    def test_without_exchange_the_update_fails_closed(
        self, app, source, tmp_path, monkeypatch, err
    ):
        """No two-rename fallback: dest would be missing between them."""
        dest = tmp_path / "copy"
        lc.copy_marketplace(source, dest, app_root=app, env={})

        def unsupported(a, b):
            raise OSError(err, os.strerror(err))

        monkeypatch.setattr(lc, "_exchange", unsupported)
        self._skill(source).write_text("v2")
        with pytest.raises(lc.LocalCopyError, match="current copy is kept"):
            lc.copy_marketplace(source, dest, app_root=app, env={})
        assert self._skill(dest).read_text().startswith("---"), "the old copy"
        assert not _siblings(dest, lc._NEW) and not _siblings(dest, lc._OLD)

    def test_without_exchange_a_first_copy_still_publishes(
        self, app, source, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            lc, "_exchange", lambda a, b: (_ for _ in ()).throw(OSError(22, "x"))
        )
        dest = tmp_path / "copy"
        lc.copy_marketplace(source, dest, app_root=app, env={})
        assert self._skill(dest).is_file()

    @pytest.mark.parametrize("exc", [OSError(5, "I/O error"), KeyboardInterrupt()])
    def test_a_failure_after_the_exchange_keeps_both_trees(
        self, app, source, tmp_path, monkeypatch, exc
    ):
        """The exchange is the commit: parking the old tree may fail, but
        never deletes it or un-publishes the new one."""
        dest = tmp_path / "copy"
        lc.copy_marketplace(source, dest, app_root=app, env={})
        self._skill(source).write_text("v2")
        real_rename = os.rename

        def rename(a, b):
            if lc._OLD in Path(b).name:
                raise exc
            return real_rename(a, b)

        monkeypatch.setattr(lc.os, "rename", rename)
        if isinstance(exc, OSError):
            lc.copy_marketplace(source, dest, app_root=app, env={})  # published
        else:
            with pytest.raises(KeyboardInterrupt):
                lc.copy_marketplace(source, dest, app_root=app, env={})
        assert self._skill(dest).read_text() == "v2"
        (left,) = _siblings(dest, lc._NEW)
        assert self._skill(left).read_text().startswith("---"), "old tree kept"

        monkeypatch.setattr(lc.os, "rename", real_rename)
        monkeypatch.setattr(lc, "STALE_STAGING_SECONDS", -1)
        lc.copy_marketplace(source, dest, app_root=app, env={})
        assert not _siblings(dest, lc._NEW), "swept by a later copy"

    def test_a_reader_never_sees_a_partial_tree(self, app, source, tmp_path):
        dest = tmp_path / "copy"
        for i in range(30):
            (source / "tool" / f"f{i}.md").write_text("x" * 1000)
        lc.copy_marketplace(source, dest, app_root=app, env={})
        stop = threading.Event()
        errors: list = []

        def read():
            while not stop.is_set():
                try:
                    files = len(list((dest / "tool").glob("f*.md")))
                    text = self._skill(dest).read_text()
                    json.loads(
                        (dest / ".claude-plugin" / "marketplace.json").read_text()
                    )
                    if files != 30 or not text:
                        errors.append(f"partial: {files} files, {text!r}")
                except OSError as exc:
                    errors.append(repr(exc))

        reader = threading.Thread(target=read)
        reader.start()
        try:
            for n in range(25):
                self._skill(source).write_text(f"v{n}")
                lc.copy_marketplace(source, dest, app_root=app, env={})
        finally:
            stop.set()
            reader.join()
        assert errors == []
        assert self._skill(dest).read_text() == "v24"

    def test_concurrent_copies_of_one_source_never_collide(self, app, source, tmp_path):
        dest = tmp_path / "copy"
        errors: list = []

        def refresh():
            try:
                for _ in range(5):
                    lc.copy_marketplace(source, dest, app_root=app, env={})
            except Exception as exc:  # noqa: BLE001 - collected for the assert
                errors.append(repr(exc))

        workers = [threading.Thread(target=refresh) for _ in range(6)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        assert errors == []
        assert self._skill(dest).read_text().startswith("---")
        assert not _siblings(dest, lc._NEW)

    def test_concurrent_processes_serialize_on_the_lock(self, app, source, tmp_path):
        """The admin path and the startup replay are separate processes."""
        dest = tmp_path / "copy"
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from src import plugin_local_copy as lc\n"
            "for _ in range(5):\n"
            "    lc.copy_marketplace(sys.argv[2], sys.argv[3], app_root=sys.argv[4], env={})"
        )
        argv = [sys.executable, "-c", code, str(ROOT), str(source), str(dest), str(app)]
        env = {**os.environ, "GATEWAY_SKIP_DOTENV": "1"}
        procs = [
            subprocess.Popen(argv, env=env, stderr=subprocess.PIPE, text=True)
            for _ in range(3)
        ]
        for proc in procs:
            _out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
        assert self._skill(dest).read_text().startswith("---")
        assert not _siblings(dest, lc._NEW)

    def test_replaced_trees_are_swept_after_their_grace(
        self, app, source, tmp_path, monkeypatch
    ):
        dest = tmp_path / "copy"
        for _ in range(3):
            lc.copy_marketplace(source, dest, app_root=app, env={})
        assert len(_siblings(dest, lc._OLD)) == 2, "kept for readers mid-path"
        monkeypatch.setattr(lc, "OLD_GRACE_SECONDS", -1)
        lc.copy_marketplace(source, dest, app_root=app, env={})
        assert len(_siblings(dest, lc._OLD)) == 1, "only the one just replaced"


# --- the admin path ----------------------------------------------------------


@pytest.fixture
def confined(monkeypatch, app, tmp_path):
    from src.backends.claude import landlock

    cfg = tmp_path / "cfg"
    (cfg / "plugins").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("CLAUDE_PLUGIN_CLONE_ROOT", raising=False)
    monkeypatch.setattr(landlock, "_APP_ROOT", app)
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()
    yield cfg
    landlock.reset_mode_cache()


def test_the_admin_path_registers_a_copy(confined, app, tmp_path):
    from src import plugin_admin_service as pas

    source = str(app / "data" / "plugins" / "inside-mp")
    root = tmp_path / "srv" / "clones"
    path = pas.prepare_repo(source, branch="main", token="", root=root)
    assert path == pas._clone_dir(root, source)
    assert (path / "tool" / "skills" / "probe" / "SKILL.md").is_file()


def test_the_admin_path_keeps_a_readable_path_in_place(confined, tmp_path):
    from src import plugin_admin_service as pas

    outside = _marketplace(tmp_path / "srv" / "outside-mp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
        path = pas.prepare_repo(
            str(outside), branch="main", token="", root=tmp_path / "srv" / "c"
        )
    assert path == outside


def test_the_admin_path_refuses_a_broad_source(confined, app, tmp_path):
    from src import plugin_admin_service as pas

    with pytest.raises(pas.PluginAdminError, match="gateway's data dir"):
        pas.prepare_repo(
            str(app / "data"), branch="main", token="", root=tmp_path / "c"
        )


def test_the_admin_path_is_unchanged_with_confinement_off(confined, app, tmp_path):
    from src import plugin_admin_service as pas
    from src.backends.claude import landlock

    os.environ.pop(landlock.MODE_ENV)
    landlock.reset_mode_cache()
    source = app / "data" / "plugins" / "inside-mp"
    assert (
        pas.prepare_repo(str(source), branch="main", token="", root=tmp_path / "c")
        == source
    )


def test_the_copy_clears_the_startup_warning(confined, app, tmp_path):
    """#255's warning names the in-place root; the registered copy is granted."""
    from src import plugin_admin_service as pas
    from src.backends.claude import landlock

    source = str(app / "data" / "plugins" / "inside-mp")
    root = tmp_path / "srv" / "clones"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tempfile, "tempdir", str(tmp_path / "systmp"))
        (tmp_path / "systmp").mkdir()
        copy = pas.prepare_repo(source, branch="main", token="", root=root)
        known = confined / "plugins" / "known_marketplaces.json"
        for loc, expected in ((source, 1), (copy, 0)):
            known.write_text(
                json.dumps(
                    {
                        "inside-mp": {
                            "source": {"source": "directory"},
                            "installLocation": str(loc),
                        }
                    }
                )
            )
            assert len(landlock.refused_plugin_roots()) == expected


# --- the startup replay ------------------------------------------------------


def _fake_claude(bin_dir: Path) -> None:
    fake = bin_dir / "claude"
    fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FAKE_CLAUDE_LOG"\n')
    fake.chmod(0o755)


def _replay(tmp_path: Path, app: Path, source: Path, **env_extra):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_claude(bin_dir)
    manifest = tmp_path / "gateway-plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "added": [
                    {"repo": str(source), "name": "tool", "marketplace": "inside-mp"}
                ],
                "removed": [],
            }
        )
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE_PLUGIN", "CLAUDE_ISOLATION"))
    }
    env.update(
        HOME=str(tmp_path / "home"),
        PATH=f"{bin_dir}:/usr/bin:/bin",
        FAKE_CLAUDE_LOG=str(tmp_path / "claude.log"),
        CLAUDE_PLUGIN_MANIFEST=str(manifest),
        CLAUDE_PLUGIN_CLONE_ROOT=str(tmp_path / "clones"),
        **env_extra,
    )
    (tmp_path / "claude.log").unlink(missing_ok=True)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "docker" / "install_plugins.py")],
        cwd=str(app),  # the CMD runs from the app root
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc, (tmp_path / "claude.log").read_text()


def test_the_startup_replay_registers_a_fresh_copy(app, tmp_path):
    source = app / "data" / "plugins" / "inside-mp"
    proc, log = _replay(tmp_path, app, source, CLAUDE_ISOLATION_MODE="landlock")
    (copy,) = [p for p in (tmp_path / "clones").iterdir() if p.is_dir()]
    assert f"plugin marketplace add {copy} --scope user" in log
    assert "copying local" in proc.stdout + proc.stderr
    # An edit to the source arrives on the next start.
    (source / "tool" / "skills" / "probe" / "SKILL.md").write_text("v2")
    _replay(tmp_path, app, source, CLAUDE_ISOLATION_MODE="landlock")
    assert (copy / "tool" / "skills" / "probe" / "SKILL.md").read_text() == "v2"


def test_the_startup_replay_is_unchanged_with_confinement_off(app, tmp_path):
    source = app / "data" / "plugins" / "inside-mp"
    _proc, log = _replay(tmp_path, app, source)
    assert f"plugin marketplace add {source} --scope user" in log
    assert not (tmp_path / "clones").exists()


def test_the_image_ships_the_module_beside_the_installer():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert (
        "COPY src/plugin_local_copy.py /usr/local/bin/plugin_local_copy.py"
        in dockerfile
    )
