"""The confined CLI's session environment (PR #244 review 4).

Both ends of a confined spawn — the launcher (landlock mode) and the broker's
CLI child (uid mode) — set it up through ``docker/_landlock_rules.py``:

* the session shell wrapper ``docker/claude-session-bash``: confined, the CLI
  cannot write its shell snapshot and ran every Bash tool call as a login shell
  (``bash -c -l``), which printed "Permission denied" for the denied
  ``~/.profile`` and let /etc/profile reset PATH;
* the per-session git global config: ``~/.gitconfig`` / ``~/.config/git/*``
  are outside the ruleset, and git died with exit 128 when one existed;
* the ``.cli-tmp`` repair: a planted regular file, FIFO or socket — or a
  ``.cli-tmp``, or the CLI's own ``claude-<uid>`` root in it, made unusable —
  used to fail every later spawn in the workspace.

Unit tests load the rules module directly; the real-Landlock tests run probes
under the launcher with a system python as the "real CLI", and
``TestBundledCliAfterTmpSabotage`` starts the bundled CLI against the fake
Messages API. The broker child's side is
``tests/test_isolation_broker.py::TestCliChildSessionEnv``; the bundled CLI's
Bash tool is ``tests/test_cli_read_boundary.py::TestLandlockConfinement``.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.backends.claude import landlock

_REPO = Path(__file__).resolve().parents[1]
_DOCKER = _REPO / "docker"
_RULES = _DOCKER / "_landlock_rules.py"
_LAUNCHER = _DOCKER / "landlock_launcher.py"
_WRAPPER = _DOCKER / "claude-session-bash"
_SYS_PY = "/usr/bin/python3"
_SYSTEM_BASHES = ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash")
_BASH = next((b for b in _SYSTEM_BASHES if os.access(b, os.X_OK)), None)
_GIT = shutil.which("git")
_CLI_ROOT = "claude-%d" % os.getuid()  # CLI 2.1.283's per-uid temp root
# A fake "bash" that prints its argv NUL-separated.
_ARGV_PRINTER = '#!/bin/sh\nfor a in "$@"; do printf \'%s\\0\' "$a"; done\n'


def _landlock_skip() -> str:
    if landlock.landlock_abi() < landlock.MIN_LANDLOCK_ABI:
        return f"Landlock ABI < {landlock.MIN_LANDLOCK_ABI}"
    if not os.access(_SYS_PY, os.X_OK):
        return "no /usr/bin/python3"
    return ""


_needs_landlock = pytest.mark.skipif(
    bool(_landlock_skip()), reason=_landlock_skip() or ""
)
_needs_bash = pytest.mark.skipif(_BASH is None, reason="no system bash")
_needs_git = pytest.mark.skipif(_GIT is None, reason="no git")


@pytest.fixture(scope="module")
def rules():
    spec = importlib.util.spec_from_file_location("landlock_rules_ut", _RULES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plant(path: Path, kind: str) -> None:
    if kind == "file":
        path.write_text("planted\n")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "socket":
        os.mknod(path, 0o600 | stat.S_IFSOCK)
    elif kind == "symlink":
        os.symlink("/", path)
    elif kind == "dangling":
        os.symlink(path.parent / "nowhere", path)
    else:
        raise AssertionError(kind)


# A confined "CLI" that writes into its TMPDIR and prints it.
_TMP_PROBE = (
    "import os\n"
    "tmp = os.environ['TMPDIR']\n"
    "open(os.path.join(tmp, 'x'), 'w').write('x')\n"
    "print(tmp)\n"
)


def _exe(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# .cli-tmp
# ---------------------------------------------------------------------------


class TestSafeTmpdir:
    @pytest.mark.parametrize("kind", ["file", "fifo", "socket", "symlink", "dangling"])
    def test_a_planted_non_directory_is_replaced(self, rules, tmp_path, kind):
        _plant(tmp_path / ".cli-tmp", kind)
        tmp = rules.safe_tmpdir(str(tmp_path))
        assert tmp == str(tmp_path / ".cli-tmp")
        assert stat.S_ISDIR(os.lstat(tmp).st_mode)
        assert not (tmp_path / "nowhere").exists()  # a link is never followed

    def test_an_existing_dir_is_kept(self, rules, tmp_path):
        (tmp_path / ".cli-tmp").mkdir()
        (tmp_path / ".cli-tmp" / "keep").write_text("k")
        rules.safe_tmpdir(str(tmp_path))
        assert (tmp_path / ".cli-tmp" / "keep").read_text() == "k"

    def test_the_symlink_target_is_untouched(self, rules, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("keep")
        (tmp_path / "ws").mkdir()
        os.symlink(target, tmp_path / "ws" / ".cli-tmp")
        rules.safe_tmpdir(str(tmp_path / "ws"))
        assert target.read_text() == "keep"

    @pytest.mark.parametrize("mode", [0o000, 0o500, 0o300, 0o650])
    def test_a_dir_made_unusable_gets_its_owner_rwx_back(self, rules, tmp_path, mode):
        # chmod 000 .cli-tmp was the same lockout: the CLI exits 1 on its mkdir.
        (tmp_path / ".cli-tmp").mkdir()
        (tmp_path / ".cli-tmp").chmod(mode)
        rules.safe_tmpdir(str(tmp_path))
        got = stat.S_IMODE(os.lstat(tmp_path / ".cli-tmp").st_mode)
        assert got == mode | 0o700

    @pytest.mark.parametrize("entry", [".cli-tmp", ".cli-tmp/" + _CLI_ROOT])
    def test_a_linked_dir_keeps_its_mode(self, rules, tmp_path, entry):
        # The launcher repairs unconfined: a link to a directory of ours that
        # lost its owner bits is replaced, never chmodded through.
        target = tmp_path / "target"
        target.mkdir()
        target.chmod(0o500)
        ws = tmp_path / "ws"
        (ws / entry).parent.mkdir(parents=True)
        os.symlink(target, ws / entry)
        try:
            rules.safe_tmpdir(str(ws))
            assert stat.S_IMODE(target.stat().st_mode) == 0o500
        finally:
            target.chmod(0o700)
        assert not (ws / entry).is_symlink()

    @pytest.mark.parametrize("kind", ["file", "fifo", "symlink", "dangling"])
    def test_the_clis_own_root_is_repaired(self, rules, tmp_path, kind):
        # CLI 2.1.283 refuses to start when <tmpdir>/claude-<uid> is not a
        # directory it can open (O_DIRECTORY|O_NOFOLLOW); it recreates one.
        (tmp_path / ".cli-tmp").mkdir()
        root = tmp_path / ".cli-tmp" / _CLI_ROOT
        _plant(root, kind)
        rules.safe_tmpdir(str(tmp_path))
        assert not os.path.lexists(root)

    def test_the_clis_own_root_gets_its_mode_back(self, rules, tmp_path):
        root = tmp_path / ".cli-tmp" / _CLI_ROOT
        root.mkdir(parents=True)
        (root / "keep").write_text("k")
        root.chmod(0)
        rules.safe_tmpdir(str(tmp_path))
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert (root / "keep").read_text() == "k"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_a_workspace_without_read_permission_works(self, rules, tmp_path):
        # Search and write are all the repair needs on the workspace itself.
        (tmp_path / "ws").mkdir(mode=0o300)
        try:
            tmp = rules.safe_tmpdir(str(tmp_path / "ws"))
            assert stat.S_ISDIR(os.lstat(tmp).st_mode)
        finally:
            (tmp_path / "ws").chmod(0o700)

    def test_only_the_git_config_is_made(self, rules, tmp_path):
        # The CLI's own root is left for the CLI to create.
        tmp = rules.safe_tmpdir(str(tmp_path))
        assert os.listdir(tmp) == ["gitconfig"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_still_raises_when_it_cannot_be_made(self, rules, tmp_path):
        _plant(tmp_path / ".cli-tmp", "file")
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                rules.safe_tmpdir(str(tmp_path))
        finally:
            tmp_path.chmod(0o755)


class TestAnotherUidsTmpdir:
    """N5 (#244 review 4): a .cli-tmp that is not the session uid's — a named
    user's first preflight runs landlock-shaped as the gateway uid and leaves a
    gateway-owned one — moves aside; the session uid could not use it (the CLI
    exits 1 on its mkdir of claude-<uid>). Same-uid here, so the ownership
    check is replaced for the one directory standing in for another uid's."""

    @staticmethod
    def _foreign(rules, monkeypatch, path):
        ino = os.lstat(path).st_ino
        ours = rules._is_ours
        monkeypatch.setattr(rules, "_is_ours", lambda st: st.st_ino != ino and ours(st))

    def test_it_moves_aside(self, rules, tmp_path, monkeypatch):
        old = tmp_path / ".cli-tmp"
        old.mkdir()
        (old / "theirs").write_text("t")
        self._foreign(rules, monkeypatch, old)
        tmp = rules.safe_tmpdir(str(tmp_path))
        assert (tmp_path / ".cli-tmp.stale-1" / "theirs").read_text() == "t"
        assert os.listdir(tmp) == ["gitconfig"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
    def test_an_unwritable_workspace_fails_closed(self, rules, tmp_path, monkeypatch):
        # The spawn then dies with its tmpdir message (launcher 70, broker 118)
        # instead of the CLI's bare exit 1.
        (tmp_path / ".cli-tmp").mkdir()
        self._foreign(rules, monkeypatch, tmp_path / ".cli-tmp")
        tmp_path.chmod(0o555)
        try:
            with pytest.raises(OSError):
                rules.safe_tmpdir(str(tmp_path))
        finally:
            tmp_path.chmod(0o755)
        assert os.listdir(tmp_path) == [".cli-tmp"]

    def test_another_uids_cli_root_moves_aside(self, rules, tmp_path, monkeypatch):
        root = tmp_path / ".cli-tmp" / _CLI_ROOT
        root.mkdir(parents=True)
        self._foreign(rules, monkeypatch, root)
        rules.safe_tmpdir(str(tmp_path))
        assert (tmp_path / ".cli-tmp" / (_CLI_ROOT + ".stale-1")).is_dir()
        assert not root.exists()


class _OsProxy:
    """The os module with some functions replaced, for the rules module only."""

    def __init__(self, **overrides):
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(os, name)


# An agent's loop in its workspace: move .cli-tmp aside, plant a link, repeat.
_RACER = (
    "import os, sys\n"
    "p, t = sys.argv[1], sys.argv[2]\n"
    "i = 0\n"
    "while True:\n"
    "    i += 1\n"
    "    try:\n"
    "        os.rename(p, p + '.old%d' % i)\n"
    "    except OSError:\n"
    "        pass\n"
    "    try:\n"
    "        os.symlink(t, p)\n"
    "    except OSError:\n"
    "        pass\n"
)


class TestRepairFollowsNoLink:
    """N1 (#244 review 4): the repair worked on path strings, so a link
    re-planted between two of its steps steered the unconfined launcher's
    unlink, mkdir or chmod outside the workspace (a racing loop deleted a file
    there on most calls). Every step is now relative to an fd and follows no
    link; the path returned named the directory that was repaired."""

    @staticmethod
    def _victims(tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / _CLI_ROOT).write_text("victim\n")  # what the race unlinked
        (target / "gitconfig").write_text("victim\n")
        return target

    @staticmethod
    def _untouched(target):
        assert sorted(os.listdir(target)) == sorted([_CLI_ROOT, "gitconfig"])
        assert (target / _CLI_ROOT).read_text() == "victim\n"
        assert (target / "gitconfig").read_text() == "victim\n"

    def test_a_link_planted_after_the_mkdir(self, rules, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = self._victims(tmp_path)
        swaps = []

        def mkdir(name, mode=0o777, *, dir_fd=None):
            os.mkdir(name, mode, dir_fd=dir_fd)
            if name == ".cli-tmp" and not swaps:
                swaps.append(name)
                os.rename(name, name + ".old", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                os.symlink(target, name, dir_fd=dir_fd)

        monkeypatch.setattr(rules, "os", _OsProxy(mkdir=mkdir))
        tmp = rules.safe_tmpdir(str(ws))
        assert swaps
        self._untouched(target)
        assert stat.S_ISDIR(os.lstat(tmp).st_mode)

    def test_a_link_planted_while_it_repairs(self, rules, tmp_path, monkeypatch):
        # The repairs stay in the directory that was opened, and its path is
        # returned only once the name points at a real directory again.
        ws = tmp_path / "ws"
        (ws / ".cli-tmp").mkdir(parents=True)
        (ws / ".cli-tmp" / _CLI_ROOT).write_text("planted\n")
        target = self._victims(tmp_path)
        lstat_at = rules._lstat_at
        swaps = []

        def swap_then_lstat(dir_fd, name):
            if name == _CLI_ROOT and not swaps:
                swaps.append(name)
                os.rename(ws / ".cli-tmp", ws / ".cli-tmp.old")
                os.symlink(target, ws / ".cli-tmp")
            return lstat_at(dir_fd, name)

        monkeypatch.setattr(rules, "_lstat_at", swap_then_lstat)
        tmp = rules.safe_tmpdir(str(ws))
        assert swaps
        self._untouched(target)
        assert not (ws / ".cli-tmp.old" / _CLI_ROOT).exists()  # repaired there
        assert stat.S_ISDIR(os.lstat(tmp).st_mode)

    def test_a_link_planted_at_the_cli_root(self, rules, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        root = ws / ".cli-tmp" / _CLI_ROOT
        root.mkdir(parents=True)
        root.chmod(0)
        theirs = tmp_path / "theirs"
        theirs.mkdir()
        theirs.chmod(0o500)
        repair = rules._repair_dir_at

        def swap_then_repair(dir_fd, name):
            if name == _CLI_ROOT and not os.path.islink(root):
                root.chmod(0o700)
                root.rmdir()
                os.symlink(theirs, root)
            return repair(dir_fd, name)

        monkeypatch.setattr(rules, "_repair_dir_at", swap_then_repair)
        try:
            rules.safe_tmpdir(str(ws))
            assert stat.S_IMODE(theirs.stat().st_mode) == 0o500
        finally:
            theirs.chmod(0o700)
        assert not os.path.lexists(root)

    def test_a_racing_agent_never_steers_it(self, rules, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        target = self._victims(tmp_path)
        racer = subprocess.Popen(
            [sys.executable, "-I", "-c", _RACER, str(ws / ".cli-tmp"), str(target)]
        )
        calls = 0
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                calls += 1
                try:
                    tmp = rules.safe_tmpdir(str(ws))
                except OSError:
                    continue  # the agent made its own spawn give up: fail closed
                assert tmp == str(ws / ".cli-tmp")
        finally:
            racer.kill()
            racer.wait()
        assert calls > 10
        self._untouched(target)


# ---------------------------------------------------------------------------
# the gateway's entries: walked from / following no link
# ---------------------------------------------------------------------------


class TestGatewayEntriesFollowNoLink:
    """G (#244 review 4): the gateway resolves and checks every entry it sends,
    then the launcher opened them following links, so a link swapped in between
    (``agents -> /app``, a planted ``projects/<name>`` into another user's
    workspace) widened the grant. They are walked from / without following any
    (``_open_nofollow``); the real-kernel half is in the launcher and broker
    tests."""

    def test_a_plain_entry_opens(self, rules, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "f").write_text("f")
        for entry in ("a/b", "a/f"):
            fd = rules._open_nofollow(str(tmp_path / entry), False)
            try:
                assert os.path.samestat(os.fstat(fd), os.stat(tmp_path / entry))
            finally:
                os.close(fd)

    @pytest.mark.parametrize("entry", ["link", "link/b", "link/b/c"])
    def test_a_symlink_anywhere_is_refused(self, rules, tmp_path, entry):
        (tmp_path / "real" / "b" / "c").mkdir(parents=True)
        os.symlink(tmp_path / "real", tmp_path / "link")
        with pytest.raises(rules._Refused, match="link is a symlink"):
            rules._open_nofollow(str(tmp_path / entry), False)

    def test_a_file_above_the_entry_is_refused(self, rules, tmp_path):
        (tmp_path / "f").write_text("f")
        with pytest.raises(rules._Refused, match="not a directory"):
            rules._open_nofollow(str(tmp_path / "f" / "x"), False)

    @pytest.mark.parametrize("entry", ["rel/x", "/a/../b", "/a/./b", "/a//b", "/"])
    def test_an_unnormalized_entry_is_refused(self, rules, entry):
        with pytest.raises(rules._Refused):
            rules._open_nofollow(entry, False)

    def test_a_missing_entry_is_none_or_made(self, rules, tmp_path):
        assert rules._open_nofollow(str(tmp_path / "x" / "y"), False) is None
        assert not (tmp_path / "x").exists()
        os.close(rules._open_nofollow(str(tmp_path / "x" / "y"), True))
        assert (tmp_path / "x" / "y").is_dir()

    def test_making_one_follows_no_link(self, rules, tmp_path):
        (tmp_path / "target").mkdir()
        os.symlink(tmp_path / "target", tmp_path / "link")
        with pytest.raises(rules._Refused):
            rules._open_nofollow(str(tmp_path / "link" / "new"), True)
        assert list((tmp_path / "target").iterdir()) == []

    @pytest.mark.parametrize(
        "cli_dir, workspace, refused",
        [
            ("/", "/data/ws/alice", True),
            ("/data", "/data/ws/alice", True),
            ("/data/ws/alice", "/data/ws/alice", True),
            ("/usr/local/lib/sdk/_bundled", "/data/ws/alice", False),
            ("/data/ws/al", "/data/ws/alice", False),  # a prefix, not a parent
        ],
    )
    def test_the_real_clis_dir_must_not_hold_the_workspace(
        self, rules, cli_dir, workspace, refused
    ):
        # The launcher and the broker grant it past the gateway's filter.
        assert bool(rules.cli_dir_problem(cli_dir, workspace)) is refused


# ---------------------------------------------------------------------------
# git global config
# ---------------------------------------------------------------------------


def _git_get(config: Path, key: str) -> str:
    return subprocess.run(
        [_GIT, "config", "--file", str(config), "--get", key],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(config.parent)},
    ).stdout.rstrip("\n")


def _usable(rules, tmp: Path):
    fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return rules._git_config_usable(fd, str(tmp))
    finally:
        os.close(fd)


def _no_space(fd, data):
    raise OSError(errno.ENOSPC, "No space left on device")


class TestGitConfig:
    def test_the_session_config_is_written(self, rules, tmp_path):
        env = rules.session_tmp_env(rules.safe_tmpdir(str(tmp_path)))
        config = tmp_path / ".cli-tmp" / "gitconfig"
        assert env["GIT_CONFIG_GLOBAL"] == str(config)
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
        assert not (tmp_path / ".cli-tmp" / "git").exists()  # defaults stay absent

    def test_the_env_alone_writes_nothing(self, rules, tmp_path):
        rules.session_tmp_env(str(tmp_path))
        assert list(tmp_path.iterdir()) == []

    @_needs_git
    def test_defaults_resolve_beneath_the_tmp_dir(self, rules, tmp_path):
        tmp = Path(rules.safe_tmpdir(str(tmp_path)))
        config = tmp / "gitconfig"
        assert _git_get(config, "core.excludesFile") == str(tmp / "git/ignore")
        assert _git_get(config, "core.attributesFile") == str(tmp / "git/attributes")

    @_needs_git
    def test_paths_are_quoted_for_git(self, rules, tmp_path):
        # Inside quotes ; and # are literal; \ " newline and tab need escapes.
        ws = tmp_path / 'a "q" \\ b;c #d\te\nf'
        ws.mkdir()
        tmp = Path(rules.safe_tmpdir(str(ws)))
        assert _git_get(tmp / "gitconfig", "core.excludesFile") == str(
            tmp / "git" / "ignore"
        )

    @_needs_git
    def test_the_sessions_own_edits_are_kept(self, rules, tmp_path):
        # `git config --global` rewrites the file around our lines, verbatim.
        config = Path(rules.safe_tmpdir(str(tmp_path))) / "gitconfig"
        subprocess.run(
            [_GIT, "config", "--global", "user.name", "me"],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "GIT_CONFIG_GLOBAL": str(config),
            },
            check=True,
        )
        edited = config.read_bytes()
        assert b"name = me" in edited
        rules.safe_tmpdir(str(tmp_path))
        assert config.read_bytes() == edited
        assert sorted(os.listdir(config.parent)) == ["gitconfig"]

    @pytest.mark.parametrize(
        "kind",
        [
            "dir",
            "fifo",
            "socket",
            "symlink",
            "dangling",
            "unreadable",
            "empty",
            "garbage",
            "not-ours",
        ],
    )
    def test_a_broken_entry_moves_aside(self, rules, tmp_path, kind):
        # A directory there made every config-reading git command of every
        # later session exit 128; an unreadable or empty one (an ENOSPC
        # mid-write) fell back to the denied ~/.config/git/ignore; a FIFO
        # would hang git. Each moves aside and a fresh one is written.
        if kind == "unreadable" and os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        tmp = tmp_path / ".cli-tmp"
        tmp.mkdir()
        config = tmp / "gitconfig"
        victim = tmp_path / "victim"
        victim.write_text("keep\n")
        if kind == "dir":
            config.mkdir()
            (config / "x").write_text("x")
        elif kind == "symlink":
            os.symlink(victim, config)
        elif kind == "unreadable":
            config.write_text("".join(rules._git_config_lines(str(tmp))))
            config.chmod(0)
        elif kind == "empty":
            config.write_text("")
        elif kind == "garbage":
            config.write_text("this is not a git config\n")
        elif kind == "not-ours":
            config.write_text("[core]\n\texcludesFile = /elsewhere/ignore\n")
        else:
            _plant(config, kind)
        rules.safe_tmpdir(str(tmp_path))
        aside = tmp / "gitconfig.broken-1"
        assert os.path.lexists(aside), sorted(os.listdir(tmp))
        assert stat.S_ISREG(os.lstat(config).st_mode)
        assert _usable(rules, tmp)
        assert victim.read_text() == "keep\n"

    def test_a_failed_write_leaves_nothing_behind(self, rules, tmp_path, monkeypatch):
        # Written through a temp name: an ENOSPC must not leave an empty file
        # that the next spawn keeps (the old O_EXCL-then-write did).
        monkeypatch.setattr(rules, "_write_all", _no_space)
        tmp = Path(rules.safe_tmpdir(str(tmp_path)))  # never fails over git
        assert os.listdir(tmp) == []
        monkeypatch.undo()
        rules.safe_tmpdir(str(tmp_path))
        assert (tmp / "gitconfig").stat().st_size > 0

    def test_a_failed_rewrite_keeps_the_broken_one_aside(
        self, rules, tmp_path, monkeypatch
    ):
        (tmp_path / ".cli-tmp" / "gitconfig").mkdir(parents=True)
        monkeypatch.setattr(rules, "_write_all", _no_space)
        tmp = Path(rules.safe_tmpdir(str(tmp_path)))
        assert sorted(os.listdir(tmp)) == ["gitconfig.broken-1"]


# ---------------------------------------------------------------------------
# uv: managed Pythons and tool envs off $HOME (#246)
# ---------------------------------------------------------------------------

_UV = shutil.which("uv")
_UVX = shutil.which("uvx")


def _uv_home(tmp_path: Path) -> Path:
    """A HOME whose ``~/.local`` the caller cannot enter, like ``/home/app``
    (0700, app-owned) seen from a session uid."""
    home = tmp_path / "home"
    (home / ".local" / "share" / "uv" / "python").mkdir(parents=True)
    (home / ".local").chmod(0)
    return home


@pytest.fixture
def uv_env(rules, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root enters a 0000 dir")
    home = _uv_home(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    tmp = rules.safe_tmpdir(str(ws))
    env = {
        "PATH": os.pathsep.join(
            {os.path.dirname(_UV or "/"), "/usr/local/bin", "/usr/bin", "/bin"}
        ),
        "HOME": str(home),
        **rules.session_tmp_env(tmp),
    }
    yield ws, env
    (home / ".local").chmod(0o700)


def _uv_run(argv, ws: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=ws, env=env, capture_output=True, text=True, timeout=120
    )


class TestUvOffHome:
    def test_every_path_lies_beneath_the_tmp_dir(self, rules, tmp_path):
        tmp = str(tmp_path / ".cli-tmp")
        env = rules.session_tmp_env(tmp)
        assert env["UV_PYTHON_INSTALL_DIR"] == tmp + "/uv-python"
        assert env["UV_TOOL_DIR"] == tmp + "/uv-tools"
        for value in env.values():
            assert value == tmp or value.startswith(tmp + os.sep)

    def test_no_shared_project_venv(self, rules, tmp_path):
        # A project venv stays beside its project, in the workspace.
        assert "UV_PROJECT_ENVIRONMENT" not in rules.session_tmp_env(str(tmp_path))

    @pytest.mark.skipif(_UV is None, reason="no uv")
    def test_uv_run_works_with_an_unreadable_home(self, uv_env):
        ws, env = uv_env
        argv = [_UV, "run", "--offline", "--no-project", "python", "-c", "print(1)"]
        proc = _uv_run(argv, ws, env)
        assert proc.returncode == 0, proc.stderr
        # The control: the same run without the override is what #246 saw.
        del env["UV_PYTHON_INSTALL_DIR"]
        proc = _uv_run(argv, ws, env)
        assert proc.returncode != 0
        assert "managed Python installations" in proc.stderr

    @pytest.mark.skipif(_UVX is None, reason="no uvx")
    def test_uvx_reaches_its_tool_dir_beneath_the_tmp_dir(self, uv_env):
        # No package needed: uvx creates its tool dir before resolving, so an
        # unresolvable offline request fails *after* that point when it works.
        ws, env = uv_env
        argv = [_UVX, "--offline", "gateway-test-no-such-package"]
        proc = _uv_run(argv, ws, env)
        assert "uv/tools" not in proc.stderr, proc.stderr
        del env["UV_TOOL_DIR"]
        proc = _uv_run(argv, ws, env)
        assert "uv/tools" in proc.stderr and "Permission denied" in proc.stderr


# ---------------------------------------------------------------------------
# session shell: which bash, and when
# ---------------------------------------------------------------------------


@pytest.fixture
def shell_dirs(rules, tmp_path, monkeypatch):
    """Two empty stand-ins for the CLI's fixed shell dirs, in search order."""
    dirs = (tmp_path / "sys-bin", tmp_path / "local-bin")
    for d in dirs:
        d.mkdir()
    monkeypatch.setattr(rules, "_SHELL_DIRS", tuple(map(str, dirs)))
    return dirs


def _wraps(env: dict, bash) -> bool:
    return env == {"CLAUDE_CODE_SHELL": str(_WRAPPER), "GATEWAY_REAL_BASH": str(bash)}


class TestSessionShellEnv:
    """The wrapper runs the bash CLI 2.1.283 itself would pick (``QAn``), so
    which shell runs never changes; a zsh the CLI would pick is left alone."""

    def test_wrapper_ships_executable_with_a_bash_name(self, rules):
        # The CLI honours CLAUDE_CODE_SHELL only for a path containing "bash"
        # or "zsh", and takes its dialect from the name: bash, never zsh.
        assert rules.SESSION_SHELL == str(_WRAPPER)
        assert os.access(_WRAPPER, os.X_OK)
        assert "bash" in _WRAPPER.name and "zsh" not in _WRAPPER.name
        assert _WRAPPER.read_text().startswith("#!/bin/sh\n")

    def test_the_image_layout_wraps_the_system_bash(self, rules, shell_dirs):
        # No SHELL, no zsh: the CLI's pick is the first fixed-dir bash.
        bash = _exe(shell_dirs[1] / "bash")
        assert _wraps(rules.session_shell_env({"PATH": "/nowhere"}), bash)

    @pytest.mark.parametrize("where", ["fixed", "path"])
    def test_a_zsh_the_cli_prefers_is_left_alone(
        self, rules, shell_dirs, tmp_path, where
    ):
        # Without a bash SHELL the CLI tries zsh first: no -l noise to fix.
        _exe(shell_dirs[0] / "bash")
        zsh_dir = shell_dirs[1] if where == "fixed" else tmp_path / "path-bin"
        _exe(zsh_dir / "zsh")
        env = {"PATH": str(tmp_path / "path-bin"), "SHELL": "/bin/sh"}
        assert rules.session_shell_env(env) == {}

    def test_a_bash_login_shell_comes_first(self, rules, shell_dirs, tmp_path):
        _exe(shell_dirs[0] / "zsh")
        _exe(shell_dirs[0] / "bash")
        login = _exe(tmp_path / "opt" / "bash")
        assert _wraps(rules.session_shell_env({"SHELL": str(login)}), login)

    def test_a_bash_login_shell_orders_bash_first(self, rules, shell_dirs, tmp_path):
        # Even when SHELL itself is unusable, a SHELL naming bash makes the CLI
        # search bash before zsh.
        _exe(shell_dirs[0] / "zsh")
        bash = _exe(shell_dirs[1] / "bash")
        env = {"SHELL": str(tmp_path / "missing" / "bash")}
        assert _wraps(rules.session_shell_env(env), bash)

    def test_a_zsh_login_shell_is_left_alone(self, rules, shell_dirs, tmp_path):
        _exe(shell_dirs[0] / "bash")
        zsh = _exe(tmp_path / "opt" / "zsh")
        assert rules.session_shell_env({"SHELL": str(zsh)}) == {}

    def test_the_path_lookup_precedes_the_fixed_dirs(
        self, rules, shell_dirs, tmp_path, monkeypatch
    ):
        # Relative PATH entries are skipped: they resolve in the workspace.
        monkeypatch.chdir(tmp_path)
        _exe(tmp_path / "rel" / "bash")
        _exe(shell_dirs[0] / "bash")
        on_path = _exe(tmp_path / "path-bin" / "bash")
        env = {"PATH": f"rel:{tmp_path / 'path-bin'}", "SHELL": "/x/bash"}
        assert _wraps(rules.session_shell_env(env), on_path)

    def test_an_operator_bash_wins(self, rules, shell_dirs, tmp_path):
        _exe(shell_dirs[0] / "bash")
        bash = _exe(tmp_path / "opt" / "bash5" / "bash")
        zsh = _exe(tmp_path / "opt" / "zsh")
        env = {"CLAUDE_CODE_SHELL": str(bash), "SHELL": str(zsh)}
        assert _wraps(rules.session_shell_env(env), bash)

    def test_an_operator_zsh_is_left_alone(self, rules, shell_dirs, tmp_path):
        _exe(shell_dirs[0] / "bash")
        zsh = _exe(tmp_path / "opt" / "zsh")
        assert rules.session_shell_env({"CLAUDE_CODE_SHELL": str(zsh)}) == {}

    @pytest.mark.parametrize("case", ["relative", "missing", "not-exec", "wrapper"])
    def test_an_unusable_operator_shell_falls_back(
        self, rules, shell_dirs, tmp_path, case
    ):
        # The CLI ignores an unusable one; the wrapper would exec itself.
        bash = _exe(shell_dirs[0] / "bash")
        plain = tmp_path / "plain-bash"
        plain.write_text("#!/bin/sh\n")
        configured = {
            "relative": "bash",
            "missing": str(tmp_path / "missing-bash"),
            "not-exec": str(plain),
            "wrapper": str(_WRAPPER),
        }[case]
        env = {"CLAUDE_CODE_SHELL": configured, "SHELL": str(_WRAPPER)}
        assert _wraps(rules.session_shell_env(env), bash)

    def test_a_non_executable_bash_is_skipped(self, rules, shell_dirs):
        (shell_dirs[0] / "bash").write_text("#!/bin/sh\n")
        bash = _exe(shell_dirs[1] / "bash")
        assert _wraps(rules.session_shell_env({}), bash)

    def test_no_shell_leaves_it_alone(self, rules, shell_dirs):
        assert rules.session_shell_env({"SHELL": "/nowhere/bash"}) == {}

    def test_a_missing_wrapper_leaves_the_shell_alone(
        self, rules, shell_dirs, tmp_path, monkeypatch
    ):
        _exe(shell_dirs[0] / "bash")
        monkeypatch.setattr(rules, "SESSION_SHELL", str(tmp_path / "gone-bash"))
        assert rules.session_shell_env({}) == {}

    @_needs_bash
    def test_this_hosts_bash_login_shell(self, rules):
        assert _wraps(rules.session_shell_env({"SHELL": _BASH}), _BASH)


# ---------------------------------------------------------------------------
# the wrapper's argv
# ---------------------------------------------------------------------------


def _wrapped(args, env: dict, stdin: bytes = b""):
    return subprocess.run(
        [str(_WRAPPER), *args],
        env={"PATH": "/usr/bin:/bin", **env},
        input=stdin,
        capture_output=True,
        timeout=30,
    )


class TestWrapperArgv:
    @pytest.mark.parametrize(
        "argv, expected",
        [
            # exactly the CLI's no-snapshot form: -c -l <cmd> [name [args]],
            # run without the login and without the ssh-session ~/.bashrc
            (["-c", "-l", "echo hi"], ["--norc", "-c", "echo hi"]),
            (
                ["-c", "-l", "echo $0", "nm", "a b"],
                ["--norc", "-c", "echo $0", "nm", "a b"],
            ),
            (["-c", "-l", "x 'q' \"d\"\n\ty"], ["--norc", "-c", "x 'q' \"d\"\n\ty"]),
            (["-c", "-l", ""], ["--norc", "-c", ""]),
            # everything else passes through unchanged
            (["-c", "echo hi"], ["-c", "echo hi"]),  # the CLI with a snapshot
            (["-c", "env"], ["-c", "env"]),  # the CLI's spawn-env probe
            (["-l", "-c", "echo hi"], ["-l", "-c", "echo hi"]),
            (["-c", "-l"], ["-c", "-l"]),
            (["-c", "-lx", "echo"], ["-c", "-lx", "echo"]),
            (["-c", "-i", "echo"], ["-c", "-i", "echo"]),
            (["--version"], ["--version"]),
            ([], []),
        ],
    )
    def test_argv(self, tmp_path, argv, expected):
        fake = _exe(tmp_path / "fake-bash", _ARGV_PRINTER)
        proc = _wrapped(argv, {"GATEWAY_REAL_BASH": str(fake)})
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split(b"\0")[:-1] == [a.encode() for a in expected]

    @_needs_bash
    @pytest.mark.parametrize("real", [None, "/nonexistent/bash", str(_WRAPPER)])
    def test_an_unusable_real_bash_falls_back(self, real):
        # Unset (an env the CLI did not inherit), missing, or the wrapper itself.
        env = {} if real is None else {"GATEWAY_REAL_BASH": real}
        proc = _wrapped(["-c", "-l", 'echo "$BASH"'], env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.decode().strip() in _SYSTEM_BASHES

    @_needs_bash
    def test_exit_status_and_stdin_pass_through(self):
        proc = _wrapped(
            ["-c", "-l", "read x; echo got=$x; exit 7"],
            {"GATEWAY_REAL_BASH": _BASH},
            stdin=b"in\n",
        )
        assert proc.returncode == 7
        assert proc.stdout == b"got=in\n"

    @_needs_bash
    def test_an_ssh_sessions_bashrc_is_not_read(self, tmp_path):
        # N6 (#244 review 4): Debian's bash (SSH_SOURCE_BASHRC) reads ~/.bashrc
        # even for a non-login -c when SSH_CLIENT is set and SHLVL < 2; denied
        # under the ruleset, that was "Permission denied" in every result.
        (tmp_path / ".bashrc").write_text("echo FROM_BASHRC\n")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "SSH_CLIENT": "10.0.0.1 2222 22",
            "GATEWAY_REAL_BASH": _BASH,
        }
        plain = subprocess.run(
            [_BASH, "-c", "echo hi"], env=env, capture_output=True, timeout=30
        )
        if b"FROM_BASHRC" not in plain.stdout:
            pytest.skip("this bash does not read ~/.bashrc for an ssh session")
        proc = _wrapped(["-c", "-l", "echo hi"], env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == b"hi\n"


def test_dockerfile_ships_the_wrapper_beside_the_launcher():
    # Root-owned and 0755 in /usr/local/bin, outside the app-owned /app.
    text = (_REPO / "Dockerfile").read_text().replace("\\\n", " ")
    instructions = [
        line.split() for line in text.splitlines() if not line.startswith("#")
    ]
    copy = next(i for i in instructions if "docker/claude-session-bash" in i)
    assert copy[0] == "COPY" and copy[-1] == "/usr/local/bin/"
    assert "docker/landlock_launcher.py" in copy
    chmod = next(
        i
        for i in instructions
        if i[:1] == ["RUN"] and "/usr/local/bin/claude-session-bash" in i
    )
    assert chmod[1:3] == ["chmod", "0755"]


# ---------------------------------------------------------------------------
# real Landlock, through the launcher
# ---------------------------------------------------------------------------


def _launch(ws: Path, probe: str, env: dict) -> dict:
    """Run *probe* as the 'real CLI' under the launcher; its JSON output."""
    (ws / "probe.py").write_text(probe)
    proc = subprocess.run(
        [sys.executable, str(_LAUNCHER), "-I", str(ws / "probe.py")],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _launch_env(ws: Path, home: Path, ro=(), **extra) -> dict:
    # SHELL names a bash, as the CLI's pick would be in the image (no zsh);
    # without it a host with zsh installed gets zsh, which is left alone.
    return {
        "PATH": f"{ws.parent / 'operator-bin'}:/usr/bin:/bin",
        "HOME": str(home),
        "PWD": str(ws),
        "SHELL": _BASH or "/bin/sh",
        landlock.REAL_CLI_ENV: _SYS_PY,
        landlock.RW_ENV: json.dumps([str(ws)]),
        landlock.RO_ENV: json.dumps([str(p) for p in ro]),
        **extra,
    }


@pytest.fixture
def ws(tmp_path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


# Runs the same command as the CLI would without a snapshot, once through
# CLAUDE_CODE_SHELL and once with the real bash directly (the control).
_SHELL_PROBE = r"""
import json, os, subprocess
cmd = 'shopt -q login_shell && echo LOGIN || echo NOLOGIN; echo "P=$PATH"'
def run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except OSError as e:
        return {"rc": None, "out": "", "err": "%s: %s" % (type(e).__name__, e)}
    return {"rc": r.returncode, "out": r.stdout, "err": r.stderr}
shell = os.environ.get("CLAUDE_CODE_SHELL", "")
real = os.environ.get("GATEWAY_REAL_BASH", "")
try:
    open(os.path.join(os.path.dirname(shell), "landlock_launcher.py")).read()
    sibling = "READ"
except OSError as e:
    sibling = "DENY:%d" % e.errno
print(json.dumps({
    "shell": shell,
    "real": real,
    "sibling": sibling,
    "wrapped": run([shell, "-c", "-l", cmd]),
    "direct": run([real, "-c", "-l", cmd]),
    "plain": run([real, "-c", cmd]),
}))
"""


@_needs_landlock
@_needs_bash
class TestLauncherSessionShell:
    def test_bash_calls_run_without_a_login_shell(self, ws, home):
        # ~/.profile exists but is outside the ruleset: a login shell's open()
        # of it is denied and bash says so in every result.
        (home / ".profile").write_text("PATH=/profile/reset\n")
        env = _launch_env(ws, home)
        r = _launch(ws, _SHELL_PROBE, env)
        assert r["shell"] == str(_WRAPPER)
        assert r["real"] == _BASH
        assert r["wrapped"] == {
            "rc": 0,
            "out": f"NOLOGIN\nP={env['PATH']}\n",
            "err": "",
        }
        # Control: the same call without the wrapper is the reported noise.
        assert r["direct"]["out"].startswith("LOGIN\n"), r
        assert ".profile: Permission denied" in r["direct"]["err"], r

    def test_a_profile_cannot_reset_path(self, ws, home):
        # Readable this time: the wrapper really drops -l, it does not merely
        # fail to read the profile.
        (home / ".profile").write_text("PATH=/profile/reset\n")
        env = _launch_env(ws, home, ro=[home])
        r = _launch(ws, _SHELL_PROBE, env)
        assert r["wrapped"]["out"] == f"NOLOGIN\nP={env['PATH']}\n"
        assert r["direct"]["out"] == "LOGIN\nP=/profile/reset\n"

    def test_an_ssh_sessions_bashrc_stays_unread(self, ws, home):
        # N6: with SSH_CLIENT set, Debian's non-login `bash -c` reads the denied
        # ~/.bashrc too; the wrapper's --norc keeps it out of every result.
        (home / ".bashrc").write_text("echo FROM_BASHRC\n")
        env = _launch_env(ws, home, SSH_CLIENT="10.0.0.1 2222 22")
        r = _launch(ws, _SHELL_PROBE, env)
        if ".bashrc: Permission denied" not in r["plain"]["err"]:
            pytest.skip("this bash does not read ~/.bashrc for an ssh session")
        assert r["wrapped"] == {
            "rc": 0,
            "out": f"NOLOGIN\nP={env['PATH']}\n",
            "err": "",
        }

    def test_only_the_wrapper_file_is_granted(self, ws, home):
        # In a checkout the wrapper lives in the gateway's docker/: its
        # siblings stay denied.
        r = _launch(ws, _SHELL_PROBE, _launch_env(ws, home))
        assert r["sibling"].startswith("DENY"), r

    @pytest.mark.parametrize("var", ["CLAUDE_CODE_SHELL", "SHELL"])
    def test_a_zsh_the_cli_would_run_is_kept(self, ws, home, tmp_path, var):
        zsh = _exe(tmp_path / "bin" / "zsh")
        probe = (
            "import json, os\n"
            "print(json.dumps([os.environ.get(k) for k in\n"
            "                  ('CLAUDE_CODE_SHELL', 'GATEWAY_REAL_BASH')]))\n"
        )
        env = _launch_env(ws, home, **{var: str(zsh)})
        expected = str(zsh) if var == "CLAUDE_CODE_SHELL" else None
        assert _launch(ws, probe, env) == [expected, None]


_GIT_PROBE = r"""
import json, os, subprocess
def git(*args, env=None):
    r = subprocess.run(["git", *args], cwd="repo", env=env, capture_output=True,
                       text=True, timeout=30)
    return [r.returncode, r.stdout.strip(), r.stderr.strip()[-400:]]
os.mkdir("repo")
out = {"global": os.environ.get("GIT_CONFIG_GLOBAL")}
out["init"] = git("init", "-q")
out["commit"] = git("-c", "user.name=t", "-c", "user.email=t@e", "commit",
                    "--allow-empty", "-qm", "m")
out["status"] = git("status", "--short")
out["log"] = git("log", "--oneline")
out["set"] = git("config", "--global", "user.name", "agent")
out["get"] = git("config", "--global", "--get", "user.name")
bare = {k: v for k, v in os.environ.items() if k != "GIT_CONFIG_GLOBAL"}
out["control_config"] = git("status", "--short", env=bare)
out["control_ignore"] = git("status", "--short",
                            env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null"))
print(json.dumps(out))
"""


def _git_home(home: Path) -> None:
    (home / ".gitconfig").write_text("[user]\n\tname = operator\n")
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "config").write_text("[core]\n\tpager = cat\n")
    (home / ".config" / "git" / "ignore").write_text("*.log\n")
    (home / ".config" / "git" / "attributes").write_text("*.bin binary\n")


def _assert_git_works(r: dict, tmp: Path) -> None:
    assert r["global"] == str(tmp / "gitconfig")
    for step in ("init", "commit", "status", "log", "set", "get"):
        assert r[step][0] == 0, (step, r[step])
    assert r["get"][1] == "agent"
    assert "name = agent" in (tmp / "gitconfig").read_text()
    # Controls: the denied files are really there, and really fatal without
    # the session's own global config.
    assert r["control_config"][0] == 128, r["control_config"]
    assert r["control_ignore"][0] == 128, r["control_ignore"]
    assert "exclude file" in r["control_ignore"][2], r["control_ignore"]


@_needs_landlock
@_needs_git
class TestLauncherGit:
    def test_git_works_with_a_home_git_config(self, ws, home):
        _git_home(home)
        # An inherited GIT_CONFIG_GLOBAL is overridden too (operator-wide git
        # config belongs in /etc/gitconfig, which the ruleset reads).
        env = _launch_env(ws, home, GIT_CONFIG_GLOBAL=str(home / ".gitconfig"))
        _assert_git_works(_launch(ws, _GIT_PROBE, env), ws / ".cli-tmp")

    def test_a_session_cannot_break_git_for_the_next(self, ws, home):
        # N3: a session that replaced its git config with a directory made
        # every config-reading git command of every later session exit 128.
        _git_home(home)
        plant = (
            "import json, os\n"
            "os.unlink('.cli-tmp/gitconfig')\n"
            "os.mkdir('.cli-tmp/gitconfig')\n"
            "print(json.dumps(sorted(os.listdir('.cli-tmp'))))\n"
        )
        assert _launch(ws, plant, _launch_env(ws, home)) == ["gitconfig"]
        _assert_git_works(
            _launch(ws, _GIT_PROBE, _launch_env(ws, home)), ws / ".cli-tmp"
        )
        assert (ws / ".cli-tmp" / "gitconfig.broken-1").is_dir()


# ---------------------------------------------------------------------------
# the bundled CLI, after a session sabotaged its workspace's .cli-tmp
# ---------------------------------------------------------------------------

_HARNESS_ENV = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR")


def _sabotage(tmp: Path, how: str) -> None:
    if how in ("file", "fifo", "socket"):
        _plant(tmp, how)
        return
    tmp.mkdir()
    if how.startswith("mode"):
        tmp.chmod(int(how[4:], 8))
    elif how == "root-mode000":
        (tmp / _CLI_ROOT).mkdir()
        (tmp / _CLI_ROOT).chmod(0)
    elif how == "root-file":
        _plant(tmp / _CLI_ROOT, "file")
    else:  # root-symlink: the CLI refuses it as attacker-planted
        (tmp.parent / "elsewhere").mkdir()
        os.symlink(tmp.parent / "elsewhere", tmp / _CLI_ROOT)


def _unlock(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        path.chmod(0o700)
        for child in path.iterdir():
            _unlock(child)


@pytest.fixture
def cli_landlock(monkeypatch, tmp_path):
    """Landlock mode with the gateway's config dir at the CLI's test HOME."""
    for key in list(os.environ):
        if key.startswith("CLAUDE_CODE_") or key in _HARNESS_ENV:
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()
    yield home
    landlock.reset_mode_cache()


async def _bash_turn(ws: Path, home: Path, command: str) -> str:
    """One CLI turn through the launcher that runs *command* with the Bash
    tool; every request body the fake Messages API saw, as JSON."""
    import asyncio
    import uuid

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from tests.fixtures.fake_anthropic_api import FakeAnthropicAPI

    def plan(body):
        for message in body.get("messages", []):
            content = message.get("content")
            if message["role"] == "user" and isinstance(content, list):
                if any(b.get("type") == "tool_result" for b in content):
                    return {"text": "done"}
        return {"tool_use": {"name": "Bash", "input": {"command": command}}}

    sid = str(uuid.uuid4())
    confinement = landlock.options_env(ws, session_id=sid)
    with FakeAnthropicAPI(plan) as api:
        client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cwd=str(ws),
                model="claude-sonnet-5",
                env={
                    **api.cli_env(home),
                    "CLAUDE_CODE_HARBOR_KITE": "0",
                    **confinement,
                },
                permission_mode="bypassPermissions",
                max_turns=3,
                session_id=sid,
                cli_path=landlock.launcher_path(),
            )
        )
        await asyncio.wait_for(client.connect(), timeout=120)
        try:

            async def turn():
                await client.query("go")
                async for _ in client.receive_response():
                    pass

            await asyncio.wait_for(turn(), timeout=120)
        finally:
            await client.disconnect()
        return json.dumps([r["body"] for r in api.model_requests()])


class TestSystemCaches:
    def test_a_real_cache_dir_is_granted(self, rules, tmp_path, monkeypatch):
        cache = tmp_path / "fontconfig"
        cache.mkdir()
        monkeypatch.setattr(rules, "SYSTEM_RO_CACHES", (str(cache),))
        assert rules.system_ro_caches() == [str(cache)]

    def test_a_link_or_a_missing_one_is_not(self, rules, tmp_path, monkeypatch):
        # _add_rule follows links: a cache dir planted as one could grant /.
        link = tmp_path / "fontconfig"
        link.symlink_to("/")
        monkeypatch.setattr(rules, "SYSTEM_RO_CACHES", (str(link), "/nonexistent"))
        assert rules.system_ro_caches() == []

    def test_the_cache_home_lies_beneath_the_tmp_dir(self, rules, tmp_path):
        tmp = str(tmp_path / ".cli-tmp")
        assert rules.session_tmp_env(tmp)["XDG_CACHE_HOME"] == tmp + "/cache"


_FONT_PROBE = (
    "import json, os\n"
    "out = {'cache_home': os.environ.get('XDG_CACHE_HOME')}\n"
    "try:\n"
    "    out['cache'] = sorted(os.listdir('/var/cache/fontconfig'))\n"
    "except OSError as e:\n"
    "    out['cache'] = e.errno\n"
    "print(json.dumps(out))\n"
)


@_needs_landlock
@pytest.mark.skipif(
    not os.path.isdir("/var/cache/fontconfig")
    or os.path.islink("/var/cache/fontconfig")
    or not os.access("/var/cache/fontconfig", os.R_OK),
    reason="no fontconfig cache",
)
class TestLauncherFontCache:
    def test_the_font_cache_is_readable_confined(self, ws, home):
        # Without it every fontconfig client rescanned every font per start.
        out = _launch(ws, _FONT_PROBE, _launch_env(ws, home))
        assert out["cache"] == sorted(os.listdir("/var/cache/fontconfig"))
        assert out["cache_home"] == str(ws / ".cli-tmp" / "cache")


@pytest.mark.integration
@_needs_landlock
@pytest.mark.skipif(not landlock.resolve_real_cli(), reason="no bundled CLI")
class TestBundledCliAfterTmpSabotage:
    """The real CLI 2.1.283 starts in a workspace whose .cli-tmp an earlier
    session sabotaged; each of these failed every later spawn there (launcher
    exit 70, or the CLI's own exit 1) until an operator stepped in."""

    @pytest.mark.parametrize(
        "how",
        [
            "file",
            "fifo",
            "socket",
            "mode000",
            "mode500",
            "root-mode000",
            "root-file",
            "root-symlink",
        ],
    )
    async def test_the_cli_starts(self, cli_landlock, tmp_path, how):
        ws = tmp_path / "ws"
        ws.mkdir()
        _sabotage(ws / ".cli-tmp", how)
        try:
            bodies = await _bash_turn(ws, cli_landlock, "echo hi-$((6*7))")
        finally:
            _unlock(ws / ".cli-tmp")  # or a failed run leaves undeletable dirs
        assert "hi-42" in bodies
