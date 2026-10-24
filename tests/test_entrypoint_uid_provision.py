"""uid mode's root-step provisioning in the image entrypoint.

In uid mode ``docker/entrypoint.py`` adds the passwd/group entries the broker
and the session uids need and prepares the shared and per-uid config dirs
before it forks the broker (docs/isolation-uid-ownership-design.md §2.1, §4,
§8). Its root-only effects are container-verify-only
(docs/isolation-uid-verification.md); these tests drive the same code on tmp
files and dirs as the test user — never this host's /etc.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def entrypoint():
    spec = importlib.util.spec_from_file_location(
        "docker_entrypoint_uid_provision", ROOT / "docker" / "entrypoint.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ENVS = (
    "CLAUDE_ISOLATION_MODE",
    "CLAUDE_ISOLATION_FALLBACK",
    "CLAUDE_ISOLATION_UID_MIN",
    "CLAUDE_ISOLATION_UID_MAX",
    "CLAUDE_ISOLATION_EPHEMERAL_UIDS",
    "CLAUDE_ISOLATION_BROKER_UID",
    "CLAUDE_ISOLATION_BROKER_GID",
    "CLAUDE_ISOLATION_UID_REGISTRY",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_ISOLATION_CONFIG_BASE",
    "CLAUDE_ISOLATION_MIGRATE_FROM",
    "CLAUDE_PLUGIN_CLONE_ROOT",
    "USER_WORKSPACES_DIR",
    "GATEWAY_ISOLATION_TICKET_SECRET",
    "APP_UID",
    "APP_GID",
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)
    # Never this host's /app/data registry.
    registry = tmp_path / "isolation_uids.json"
    monkeypatch.setenv("CLAUDE_ISOLATION_UID_REGISTRY", str(registry))

    def apply(**values):
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return apply


class _Proxy:
    """A module stand-in: the given overrides, everything else delegated."""

    def __init__(self, real, **overrides):
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


# ---------------------------------------------------------------------------
# /etc/passwd and /etc/group
# ---------------------------------------------------------------------------

# Lines of the image's own files (python:3.12-slim-trixie + useradd app).
_IMAGE_PASSWD = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
    "_apt:x:42:65534::/nonexistent:/usr/sbin/nologin\n"
    "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
    "app:x:1000:1000::/home/app:/bin/bash\n"
)
_IMAGE_GROUP = "root:x:0:\ndaemon:x:1:\nusers:x:100:\nnogroup:x:65534:\napp:x:1000:\n"

_SMALL_BAND = {"CLAUDE_ISOLATION_UID_MIN": "20000", "CLAUDE_ISOLATION_UID_MAX": "20004"}


@pytest.fixture
def etc(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    for name, text in (("passwd", _IMAGE_PASSWD), ("group", _IMAGE_GROUP)):
        (etc / name).write_text(text)
        (etc / name).chmod(0o644)
    return etc


def _provision(entrypoint, etc, app_uid=1000, app_gid=1000):
    entrypoint.provision_session_accounts(
        app_uid, app_gid, passwd_path=etc / "passwd", group_path=etc / "group"
    )


def _lines(path: Path) -> list[list[str]]:
    return [line.split(":") for line in path.read_text().splitlines()]


def _session_uids(path: Path) -> list[int]:
    """The uids (or gids) of the claude-u<uid> lines of *path*."""
    return [int(f[2]) for f in _lines(path) if f[0].startswith("claude-u")]


def _added(path: Path, before: str) -> list[list[str]]:
    data = path.read_text()
    assert data.startswith(before)
    return [line.split(":") for line in data[len(before) :].splitlines()]


def test_adds_the_broker_and_every_band_uid(entrypoint, env, etc):
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    assert _added(etc / "passwd", _IMAGE_PASSWD) == [
        [
            "claude-broker",
            "x",
            "19999",
            "1000",
            "claude isolation broker",
            "/nonexistent",
            "/usr/sbin/nologin",
        ],
        *(
            [
                f"claude-u{uid}",
                "x",
                str(uid),
                str(uid),
                "claude session uid",
                "/nonexistent",
                "/usr/sbin/nologin",
            ]
            for uid in range(20000, 20005)
        ),
    ]
    # The broker gid defaults to APP_GID, whose group (app) exists already.
    assert _added(etc / "group", _IMAGE_GROUP) == [
        [f"claude-u{uid}", "x", str(uid), ""] for uid in range(20000, 20005)
    ]


def test_the_default_band_is_provisioned_whole(entrypoint, env, etc):
    _provision(entrypoint, etc)

    assert _session_uids(etc / "passwd") == list(range(20000, 30000))
    assert _session_uids(etc / "group") == list(range(20000, 30000))


def test_added_lines_are_well_formed(entrypoint, env, etc):
    env(**_SMALL_BAND, CLAUDE_ISOLATION_BROKER_GID="19998")
    _provision(entrypoint, etc)

    for fields in _added(etc / "passwd", _IMAGE_PASSWD):
        name, password, uid, gid, _gecos, home, shell = fields
        # useradd's NAME_REGEX, and the 32-character utmp limit.
        assert re.fullmatch(r"[a-z][-a-z0-9]*", name) and len(name) <= 32
        assert password == "x" and int(uid) > 0 and int(gid) > 0
        assert (home, shell) == ("/nonexistent", "/usr/sbin/nologin")
    for fields in _added(etc / "group", _IMAGE_GROUP):
        name, password, gid, members = fields
        assert re.fullmatch(r"[a-z][-a-z0-9]*", name)
        assert (password, members) == ("x", "") and int(gid) > 0


def test_a_second_start_writes_nothing(entrypoint, env, etc, monkeypatch):
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)
    snapshot = {p: (p.read_bytes(), p.stat().st_ino) for p in etc.iterdir()}

    def no_write(*_args):
        raise AssertionError("nothing is missing: no file is replaced")

    monkeypatch.setattr(entrypoint, "_replace_file", no_write)
    _provision(entrypoint, etc)

    assert {p: (p.read_bytes(), p.stat().st_ino) for p in etc.iterdir()} == snapshot


def test_existing_names_and_ids_win(entrypoint, env, etc, capsys):
    passwd = _IMAGE_PASSWD + (
        "alice:x:20001:20001::/home/alice:/bin/sh\n"  # the uid is taken
        "claude-u20003:x:31000:31000::/x:/bin/sh\n"  # the name is taken
    )
    group = _IMAGE_GROUP + "devs:x:20002:\nclaude-u20004:x:41000:\n"
    (etc / "passwd").write_text(passwd)
    (etc / "group").write_text(group)
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    users = [f for f in _added(etc / "passwd", passwd) if f[0] != "claude-broker"]
    assert [(f[0], int(f[2])) for f in users] == [
        ("claude-u20000", 20000),
        ("claude-u20002", 20002),
        ("claude-u20004", 20004),
    ]
    assert [(f[0], int(f[2])) for f in _added(etc / "group", group)] == [
        ("claude-u20000", 20000),
        ("claude-u20001", 20001),
        ("claude-u20003", 20003),
    ]
    # Nothing is claimed twice.
    for path in (etc / "passwd", etc / "group"):
        names = [f[0] for f in _lines(path)]
        ids = [f[2] for f in _lines(path)]
        assert len(names) == len(set(names)) and len(ids) == len(set(ids))
    err = capsys.readouterr().err
    assert f"{etc / 'passwd'}: no entry added for 1 id(s) (20003)" in err
    assert f"{etc / 'group'}: no entry added for 1 id(s) (20004)" in err


@pytest.mark.parametrize(
    "values, missing",
    [
        # App uid and gid inside the band, apart from the image's app account.
        (
            {
                "CLAUDE_ISOLATION_UID_MIN": "1499",
                "CLAUDE_ISOLATION_UID_MAX": "1502",
                "APP_UID": "1500",
                "APP_GID": "1501",
            },
            {1500, 1501},
        ),
        ({**_SMALL_BAND, "CLAUDE_ISOLATION_BROKER_UID": "20002"}, {20002}),
        ({**_SMALL_BAND, "CLAUDE_ISOLATION_BROKER_GID": "20003"}, {20003}),
        ({"CLAUDE_ISOLATION_UID_MIN": "0", "CLAUDE_ISOLATION_UID_MAX": "3"}, set()),
    ],
    ids=["app-in-band", "broker-uid-in-band", "broker-gid-in-band", "invalid"],
)
def test_reserved_ids_get_no_session_account(entrypoint, env, etc, values, missing):
    env(**values)
    app_uid = int(values.get("APP_UID", "1000"))
    app_gid = int(values.get("APP_GID", "1000"))
    _provision(entrypoint, etc, app_uid, app_gid)

    lo = int(values["CLAUDE_ISOLATION_UID_MIN"])
    hi = int(values["CLAUDE_ISOLATION_UID_MAX"])
    expected = [] if lo < 1 else [u for u in range(lo, hi + 1) if u not in missing]
    assert _session_uids(etc / "passwd") == expected
    assert _session_uids(etc / "group") == expected


def test_a_broker_id_in_the_band_keeps_its_own_account(entrypoint, env, etc):
    env(
        **_SMALL_BAND,
        CLAUDE_ISOLATION_BROKER_UID="20002",
        CLAUDE_ISOLATION_BROKER_GID="20003",
    )
    _provision(entrypoint, etc)

    broker = next(f for f in _lines(etc / "passwd") if f[0] == "claude-broker")
    assert broker[2:4] == ["20002", "20003"]
    group = next(f for f in _lines(etc / "group") if f[0] == "claude-broker")
    assert group[2] == "20003"


@pytest.mark.parametrize(
    "values",
    [
        {},
        _SMALL_BAND,
        {"CLAUDE_ISOLATION_UID_MIN": "990", "CLAUDE_ISOLATION_UID_MAX": "1010"},
        {**_SMALL_BAND, "CLAUDE_ISOLATION_BROKER_UID": "20001"},
        {**_SMALL_BAND, "APP_GID": "20004"},
        {**_SMALL_BAND, "CLAUDE_ISOLATION_BROKER_UID": "x"},  # the gateway: 19999
    ],
    ids=["defaults", "small", "around-app", "broker-in-band", "app-gid", "bad"],
)
def test_accounts_cover_what_the_gateway_can_assign(entrypoint, env, etc, values):
    # Every uid the gateway's allocators can hand a session (the band minus
    # reserved_uids()) has an account, and no other uid gets one.
    from src.backends.claude import uid_isolation as ui

    env(**values)
    _provision(entrypoint, etc, app_gid=int(values.get("APP_GID", "1000")))

    lo, hi = ui.uid_range()
    image_ids = {0, 1, 42, 65534, 1000}
    assignable = set(range(lo, hi + 1)) - ui.reserved_uids()
    assert set(_session_uids(etc / "passwd")) == assignable - image_ids
    assert entrypoint._reserved_ids(1000) == ui.reserved_uids()


def test_malformed_lines_are_kept_verbatim(entrypoint, env, etc, capsys):
    passwd = (
        "root:x:0:0:root:/root:/bin/bash\n"
        "# a comment\n"
        "\n"
        "garbage-without-colons\n"
        "bad:x:notanumber:1:::\n"
        "   \n"
        "short:x:20001\n"  # claims uid 20001
        "claude-u20002\n"  # claims the name claude-u20002
        "+::::::\n"
        "app:x:1000:1000::/home/app:/bin/bash"  # no newline at the end
    )
    group = "root:x:0:\nweird line\nclaude-u20003:x\napp:x:1000:"
    (etc / "passwd").write_text(passwd)
    (etc / "group").write_text(group)
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    users = _added(etc / "passwd", passwd + "\n")
    assert [f[0] for f in users] == [
        "claude-broker",
        "claude-u20000",
        "claude-u20003",
        "claude-u20004",
    ]
    groups = _added(etc / "group", group + "\n")
    assert [f[0] for f in groups] == [
        "claude-u20000",
        "claude-u20001",
        "claude-u20002",
        "claude-u20004",
    ]
    err = capsys.readouterr().err
    assert "(20002): the name is taken" in err and "(20003): the name" in err


def test_the_band_cap(entrypoint, env, etc, monkeypatch, capsys):
    assert entrypoint.MAX_PROVISIONED_UIDS == 65536
    monkeypatch.setattr(entrypoint, "MAX_PROVISIONED_UIDS", 4)
    env(**_SMALL_BAND)  # 5 uids
    _provision(entrypoint, etc)

    assert _session_uids(etc / "passwd") == []
    assert _session_uids(etc / "group") == []
    assert "claude-broker" in (etc / "passwd").read_text()
    assert "provision NSS entries for them" in capsys.readouterr().err

    env(CLAUDE_ISOLATION_UID_MAX="20003")  # 4 uids
    _provision(entrypoint, etc)
    assert _session_uids(etc / "passwd") == [20000, 20001, 20002, 20003]


@pytest.mark.parametrize(
    "values, warning",
    [
        ({"CLAUDE_ISOLATION_UID_MIN": "0"}, "outside [1, 4294967294]"),
        ({"CLAUDE_ISOLATION_UID_MAX": "4294967295"}, "outside [1, 4294967294]"),
        (
            {"CLAUDE_ISOLATION_UID_MIN": "30000", "CLAUDE_ISOLATION_UID_MAX": "20000"},
            None,  # an empty band: the gateway reports it
        ),
    ],
    ids=["min-0", "max-unchanged-id", "empty"],
)
def test_a_band_the_gateway_refuses_gets_no_accounts(
    entrypoint, env, etc, capsys, values, warning
):
    env(**values)
    _provision(entrypoint, etc)

    assert _session_uids(etc / "passwd") == []
    assert "claude-broker" in (etc / "passwd").read_text()
    err = capsys.readouterr().err
    assert (warning in err) if warning else err == ""


@pytest.mark.parametrize(
    "name, value",
    [("CLAUDE_ISOLATION_BROKER_UID", "x"), ("CLAUDE_ISOLATION_BROKER_GID", "0")],
)
def test_an_invalid_broker_id_skips_only_the_broker(
    entrypoint, env, etc, capsys, name, value
):
    env(**_SMALL_BAND, **{name: value})
    _provision(entrypoint, etc)

    assert "claude-broker" not in (etc / "passwd").read_text()
    assert "claude-broker" not in (etc / "group").read_text()
    assert _session_uids(etc / "passwd") == list(range(20000, 20005))
    assert "warning: not adding the broker account" in capsys.readouterr().err


@pytest.mark.parametrize(
    "gid, existing, added",
    [
        (None, "", False),  # APP_GID: the app group
        ("19998", "", True),
        ("19998", "claude-broker:x:555:\n", False),  # the name is taken
        ("100", "", False),  # the users group
    ],
    ids=["app-gid", "own-gid", "name-taken", "existing-gid"],
)
def test_the_broker_group_is_added_only_when_its_gid_has_none(
    entrypoint, env, etc, capsys, gid, existing, added
):
    if gid is not None:
        env(CLAUDE_ISOLATION_BROKER_GID=gid)
    (etc / "group").write_text(_IMAGE_GROUP + existing)
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    broker_groups = [f for f in _lines(etc / "group") if f[0] == "claude-broker"]
    if added:
        assert broker_groups == [["claude-broker", "x", gid, ""]]
    else:
        assert len(broker_groups) == (1 if existing else 0)
    if existing:
        assert "(19998): the name is taken" in capsys.readouterr().err
    broker = next(f for f in _lines(etc / "passwd") if f[0] == "claude-broker")
    assert broker[3] == (gid or "1000")


def test_owner_and_mode_are_kept_and_no_temp_file_is_left(entrypoint, env, etc):
    (etc / "passwd").chmod(0o640)
    (etc / "group").chmod(0o600)
    before = {p.name: p.stat() for p in etc.iterdir()}
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    assert sorted(p.name for p in etc.iterdir()) == ["group", "passwd"]
    for path in etc.iterdir():
        info, old = path.stat(), before[path.name]
        assert stat.S_IMODE(info.st_mode) == stat.S_IMODE(old.st_mode)
        assert (info.st_uid, info.st_gid) == (old.st_uid, old.st_gid)
        assert info.st_ino != old.st_ino  # replaced, not rewritten in place


def test_a_failed_replace_leaves_the_file_alone(
    entrypoint, env, etc, monkeypatch, capsys
):
    # /etc/passwd bind-mounted into the container: rename(2) gets EBUSY.
    def replace(*_args):
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

    monkeypatch.setattr(entrypoint, "os", _Proxy(os, replace=replace))
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    assert (etc / "passwd").read_text() == _IMAGE_PASSWD
    assert (etc / "group").read_text() == _IMAGE_GROUP
    assert sorted(p.name for p in etc.iterdir()) == ["group", "passwd"]
    err = capsys.readouterr().err
    assert f"could not add 6 session accounts to {etc / 'passwd'}" in err
    assert f"could not add 5 session accounts to {etc / 'group'}" in err


def test_a_symlinked_account_file_is_left_alone(entrypoint, env, etc, capsys):
    real = etc.parent / "real-passwd"
    real.write_text(_IMAGE_PASSWD)
    (etc / "passwd").unlink()
    (etc / "passwd").symlink_to(real)
    env(**_SMALL_BAND)
    _provision(entrypoint, etc)

    assert (etc / "passwd").is_symlink()
    assert real.read_text() == _IMAGE_PASSWD
    assert _session_uids(etc / "group") == list(range(20000, 20005))
    err = capsys.readouterr().err
    assert f"not adding session accounts to {etc / 'passwd'}: not a regular" in err


# ---------------------------------------------------------------------------
# The shared config dir and the per-uid config base
# ---------------------------------------------------------------------------


def _beneath(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@pytest.fixture
def dirs(entrypoint, env, monkeypatch, tmp_path):
    root = tmp_path / "root"
    home = root / "home" / "app"
    app = root / "app"
    workspaces = root / "data" / "workspaces"
    for path in (home, app, workspaces):
        path.mkdir(parents=True)
    for path in (root, home.parent, app, root / "data", workspaces):
        path.chmod(0o755)
    home.chmod(0o700)
    shared = root / "data" / "claude" / "shared"
    base = root / "data" / "claude" / "users"
    env(
        CLAUDE_ISOLATION_MODE="uid",
        CLAUDE_CONFIG_DIR=str(shared),
        CLAUDE_ISOLATION_CONFIG_BASE=str(base),
        CLAUDE_PLUGIN_CLONE_ROOT=str(shared / "plugin-marketplaces"),
        USER_WORKSPACES_DIR=str(workspaces),
    )
    # tmp_path lies in a system dir (/tmp): drop the ones it is in.
    real = os.path.realpath(tmp_path)
    kept = tuple(
        d for d in entrypoint.SYSTEM_DIRS if not _beneath(real, os.path.realpath(d))
    )
    monkeypatch.setattr(entrypoint, "SYSTEM_DIRS", kept)
    return SimpleNamespace(
        root=root, home=home, app=app, workspaces=workspaces, shared=shared, base=base
    )


def _prepare(entrypoint, d, uid=None, **kwargs):
    uid = os.getuid() if uid is None else uid
    kwargs.setdefault("skip_owners", entrypoint._session_owned_uids(uid))
    entrypoint.prepare_isolation_dirs(
        uid=uid, gid=os.getgid(), home=d.home, app_root=d.app, **kwargs
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def strict_umask():
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


class _Recorder(_Proxy):
    """``entrypoint.os`` noting every absolute path it is asked about, with
    lstat reporting chosen owners and chown recorded instead of applied."""

    def __init__(self, owners=None):
        super().__init__(os)
        self.owners = {Path(p): uid for p, uid in (owners or {}).items()}
        self.touched: list[Path] = []
        self.chowned: list[Path] = []

    def _note(self, path):
        if isinstance(path, (str, os.PathLike)) and os.path.isabs(path):
            self.touched.append(Path(path))

    def lstat(self, path, *args, **kwargs):
        self._note(path)
        result = os.lstat(path, *args, **kwargs)
        owner = self.owners.get(Path(path))
        if owner is None:
            return result
        fields = list(result)
        fields[4] = owner  # st_uid
        return os.stat_result(fields)

    def stat(self, path, *args, **kwargs):
        self._note(path)
        return os.stat(path, *args, **kwargs)

    def walk(self, top, *args, **kwargs):
        self._note(top)
        return os.walk(top, *args, **kwargs)

    def chown(self, path, uid, gid, *, follow_symlinks=True):
        assert follow_symlinks is False
        self._note(path)
        self.chowned.append(Path(path))


def test_creates_the_shared_dir_and_the_base(entrypoint, dirs, strict_umask, capsys):
    _prepare(entrypoint, dirs)

    assert _mode(dirs.shared) == 0o755
    assert _mode(dirs.base) == 0o711
    assert _mode(dirs.root / "data" / "claude") == 0o755  # a parent made here
    for path in (dirs.shared, dirs.base):
        assert (path.stat().st_uid, path.stat().st_gid) == (os.getuid(), os.getgid())
    err = capsys.readouterr().err
    assert "not preparing" not in err and "PLUGIN_CLONE_ROOT" not in err


def test_the_shared_tree_is_handed_to_the_app_uid(entrypoint, dirs, monkeypatch):
    demo = dirs.shared / "skills" / "demo"
    demo.mkdir(parents=True)
    (demo / "SKILL.md").write_text("x")
    owned = dirs.shared / "stray"  # a session uid's entry: left alone
    (owned / "deep").mkdir(parents=True)
    fake = _Recorder({owned: 20000, owned / "deep": 20000})
    monkeypatch.setattr(entrypoint, "os", fake)
    _prepare(entrypoint, dirs)

    for path in (dirs.shared, dirs.shared / "skills", demo, demo / "SKILL.md"):
        assert path in fake.chowned, path
    assert owned not in fake.chowned
    assert [p for p in fake.touched if owned in p.parents] == []  # not walked


def test_the_base_is_never_walked(entrypoint, dirs, monkeypatch):
    transcript = dirs.base / "20000" / "projects" / "-ws" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    transcript.chmod(0o600)
    (dirs.base / "20000").chmod(0o750)
    dirs.base.chmod(0o755)
    fake = _Recorder()
    monkeypatch.setattr(entrypoint, "os", fake)
    _prepare(entrypoint, dirs)

    assert _mode(dirs.base) == 0o711
    assert _mode(dirs.base / "20000") == 0o750
    assert _mode(transcript) == 0o600
    assert [p for p in fake.touched if dirs.base in p.parents] == []
    assert dirs.base not in fake.chowned  # the dir itself: by its fd


def test_existing_dirs_get_their_modes(entrypoint, dirs):
    (dirs.shared / "skills").mkdir(parents=True)
    (dirs.shared / "skills").chmod(0o700)
    dirs.shared.chmod(0o700)
    dirs.base.mkdir()
    dirs.base.chmod(0o755)
    _prepare(entrypoint, dirs)

    assert _mode(dirs.shared) == 0o755
    assert _mode(dirs.shared / "skills") == 0o700  # only S itself
    assert _mode(dirs.base) == 0o711


@pytest.mark.parametrize("where", ["parent", "shared", "base", "dangling"])
def test_a_symlink_on_the_way_is_refused(entrypoint, dirs, capsys, where):
    elsewhere = dirs.root / "elsewhere"
    elsewhere.mkdir()
    claude = dirs.root / "data" / "claude"
    if where == "parent":
        claude.symlink_to(elsewhere, target_is_directory=True)
        link = claude
    else:
        claude.mkdir()
        link = {"shared": dirs.shared, "base": dirs.base}.get(where, dirs.shared)
        target = elsewhere / "missing" if where == "dangling" else elsewhere
        link.symlink_to(target, target_is_directory=True)
    _prepare(entrypoint, dirs)

    assert list(elsewhere.iterdir()) == []  # nothing created through a link
    assert link.is_symlink()
    err = capsys.readouterr().err
    assert f"{link} is a symlink" in err
    if where == "shared":
        assert _mode(dirs.base) == 0o711  # the other one is still prepared
    if where == "base":
        assert _mode(dirs.shared) == 0o755


def test_a_file_in_place_of_a_dir_is_refused(entrypoint, dirs, capsys):
    dirs.shared.parent.mkdir()
    dirs.shared.write_text("x")
    _prepare(entrypoint, dirs)

    assert dirs.shared.read_text() == "x"
    assert f"{dirs.shared} is not a directory" in capsys.readouterr().err


@pytest.mark.parametrize("value", [None, "home/.claude", "home"])
def test_a_shared_dir_in_home_is_only_a_warning(
    entrypoint, dirs, monkeypatch, capsys, value
):
    if value is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        said = f"is unset, so the shared Claude config dir is {dirs.home}/.claude"
    else:
        path = dirs.home if value == "home" else dirs.home / ".claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
        said = f"is {path}"
    _prepare(entrypoint, dirs)

    assert not (dirs.home / ".claude").exists()
    assert _mode(dirs.home) == 0o700
    assert _mode(dirs.base) == 0o711  # the base does not depend on it
    err = capsys.readouterr().err
    assert f"CLAUDE_CONFIG_DIR {said}, inside $HOME {dirs.home}: session" in err
    assert "session uids cannot reach it" in err
    assert "PLUGIN_CLONE_ROOT" not in err


@pytest.mark.parametrize(
    "where, relation",
    [
        ("home-parent", "contains $HOME"),
        ("app", "is the gateway's app root"),
        ("app/claude", "lies inside the gateway's app root"),
        ("workspaces", "is the workspace base"),
        ("workspaces/claude", "lies inside the workspace base"),
        ("data", "contains the workspace base"),
    ],
)
def test_a_shared_dir_that_overlaps_gateway_state_is_refused(
    entrypoint, dirs, monkeypatch, capsys, where, relation
):
    paths = {
        "home-parent": dirs.home.parent,
        "app": dirs.app,
        "app/claude": dirs.app / "claude",
        "workspaces": dirs.workspaces,
        "workspaces/claude": dirs.workspaces / "claude",
        "data": dirs.root / "data",
    }
    shared = paths[where]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.setenv("CLAUDE_ISOLATION_CONFIG_BASE", str(dirs.root / "users"))
    modes = {p: _mode(p) for p in paths.values() if p.exists()}
    _prepare(entrypoint, dirs)

    assert {p: _mode(p) for p in modes} == modes
    assert not (dirs.app / "claude").exists()
    assert not (dirs.workspaces / "claude").exists()
    err = capsys.readouterr().err
    assert f"not preparing CLAUDE_CONFIG_DIR {shared}: it {relation}" in err


@pytest.mark.parametrize(
    "where, relation",
    [
        ("shared", "is CLAUDE_CONFIG_DIR"),
        ("shared/users", "lies inside CLAUDE_CONFIG_DIR"),
        ("claude", "contains CLAUDE_CONFIG_DIR"),
        ("home", "is $HOME"),
        ("home/users", "lies inside $HOME"),
        ("home-parent", "contains $HOME"),
        ("app", "is the gateway's app root"),
        ("app/users", "lies inside the gateway's app root"),
        ("workspaces", "is the workspace base"),
        ("workspaces/users", "lies inside the workspace base"),
        ("data", "contains CLAUDE_CONFIG_DIR"),  # and the workspace base
        ("/", "is the filesystem root"),
    ],
)
def test_a_base_that_overlaps_gateway_state_is_refused(
    entrypoint, dirs, monkeypatch, capsys, where, relation
):
    paths = {
        "shared": dirs.shared,
        "shared/users": dirs.shared / "users",
        "claude": dirs.shared.parent,
        "home": dirs.home,
        "home/users": dirs.home / "users",
        "home-parent": dirs.home.parent,
        "app": dirs.app,
        "app/users": dirs.app / "users",
        "workspaces": dirs.workspaces,
        "workspaces/users": dirs.workspaces / "users",
        "data": dirs.root / "data",
        "/": Path("/"),
    }
    base = paths[where]
    monkeypatch.setenv("CLAUDE_ISOLATION_CONFIG_BASE", str(base))
    _prepare(entrypoint, dirs)

    assert _mode(dirs.shared) == 0o755  # S is still prepared, and not 0711
    for path in (dirs.home, dirs.app, dirs.workspaces, dirs.root / "data"):
        assert _mode(path) in (0o700, 0o755)
    assert not any((p / "users").exists() for p in (dirs.shared, dirs.home, dirs.app))
    err = capsys.readouterr().err
    assert f"not preparing CLAUDE_ISOLATION_CONFIG_BASE {base}: it {relation}" in err


@pytest.mark.parametrize("value", ["data/users", "./users", "~/users"])
def test_a_relative_base_is_refused(entrypoint, dirs, monkeypatch, capsys, value):
    monkeypatch.setenv("CLAUDE_ISOLATION_CONFIG_BASE", value)
    _prepare(entrypoint, dirs)

    assert f"{value}: not an absolute path" in capsys.readouterr().err


def test_an_unset_base_warns(entrypoint, dirs, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_ISOLATION_CONFIG_BASE")
    _prepare(entrypoint, dirs)

    assert _mode(dirs.shared) == 0o755
    assert "CLAUDE_ISOLATION_CONFIG_BASE is unset" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path, reason",
    [
        ("/", "it is the filesystem root"),
        # With merged /usr, /usr also contains /bin (-> usr/bin): either says so.
        ("/usr", "the system directory /"),
        ("/usr/local/claude-shared", "it lies inside the system directory /usr"),
        # The launcher grants /opt read-only to every confined session.
        ("/opt/claude-shared", "it lies inside the system directory /opt"),
        ("/etc/claude-users", "it lies inside the system directory /etc"),
        ("/tmp/claude-users", "it lies inside the system directory /tmp"),
        ("/run/claude-users", "it lies inside the system directory /run"),
        ("/var", "it contains the system directory /var/run"),
    ],
)
def test_system_directories_are_refused(entrypoint, path, reason):
    assert reason in (entrypoint._placement_refusal(path, []) or "")


def test_refused_system_paths_are_never_touched(
    entrypoint, env, monkeypatch, tmp_path, capsys
):
    def untouched(*_args, **_kwargs):
        raise AssertionError("a refused path must never be created or chowned")

    monkeypatch.setattr(entrypoint, "_make_dir_nofollow", untouched)
    monkeypatch.setattr(entrypoint, "_chown_tree", untouched)
    env(
        CLAUDE_CONFIG_DIR="/opt/claude-shared",
        CLAUDE_ISOLATION_CONFIG_BASE="/etc/claude-users",
        USER_WORKSPACES_DIR=str(tmp_path / "workspaces"),
    )
    entrypoint.prepare_isolation_dirs(
        uid=os.getuid(), gid=os.getgid(), home=tmp_path / "home", app_root=tmp_path
    )

    err = capsys.readouterr().err
    assert "not preparing CLAUDE_CONFIG_DIR /opt/claude-shared: it lies" in err
    assert "not preparing CLAUDE_ISOLATION_CONFIG_BASE /etc/claude-users: it" in err


def test_unsearchable_dirs_on_the_way_warn(entrypoint, dirs, capsys):
    data = dirs.root / "data"
    data.chmod(0o750)
    try:
        _prepare(entrypoint, dirs)
    finally:
        data.chmod(0o755)

    err = capsys.readouterr().err
    for label, path in (
        ("CLAUDE_CONFIG_DIR", dirs.shared),
        ("CLAUDE_ISOLATION_CONFIG_BASE", dirs.base),
        ("USER_WORKSPACES_DIR", dirs.workspaces),
    ):
        line = next(
            ln for ln in err.splitlines() if f"cannot reach {label} {path}:" in ln
        )
        assert f"{data} (mode 0750)" in line
        assert f"{dirs.root} (mode" not in line  # 0755: searchable


def test_a_searchable_layout_does_not_warn_about_its_own_dirs(entrypoint, dirs, capsys):
    _prepare(entrypoint, dirs)

    for line in capsys.readouterr().err.splitlines():
        # pytest's own tmp dirs above tmp_path are 0700; ours are not named.
        blocked = line.split(": others may not search ")[-1]
        assert f"{dirs.root} (mode" not in blocked
        assert f"{dirs.root}/" not in blocked


@pytest.mark.parametrize("where", [None, "outside", "shared", "inside"])
def test_the_clone_root_belongs_inside_the_shared_dir(
    entrypoint, dirs, monkeypatch, capsys, where
):
    paths = {
        "outside": dirs.root / "clones",
        "shared": dirs.shared,  # clones beside skills/, plugins/, ...
        "inside": dirs.shared / "plugin-marketplaces",
    }
    if where is None:
        monkeypatch.delenv("CLAUDE_PLUGIN_CLONE_ROOT")
    else:
        monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(paths[where]))
    _prepare(entrypoint, dirs)

    err = capsys.readouterr().err
    if where == "inside":
        assert "CLAUDE_PLUGIN_CLONE_ROOT" not in err
        return
    if where is None:
        default = dirs.home / ".claude" / "plugin-marketplaces"
        assert f"is unset, so marketplaces are cloned under {default}" in err
    else:
        assert (
            f"CLAUDE_PLUGIN_CLONE_ROOT {paths[where]} is not inside "
            f"CLAUDE_CONFIG_DIR {dirs.shared}"
        ) in err
    assert f"(for example {dirs.shared}/plugin-marketplaces)" in err


def test_a_shared_dir_the_app_uid_does_not_own_is_not_opened(
    entrypoint, dirs, monkeypatch, capsys
):
    dirs.shared.mkdir(parents=True)
    dirs.shared.chmod(0o700)
    monkeypatch.setattr(entrypoint, "os", _Recorder())  # chown is not applied
    app_uid = os.getuid() + 1
    _prepare(entrypoint, dirs, uid=app_uid, skip_owners=())

    assert _mode(dirs.shared) == 0o700
    err = capsys.readouterr().err
    assert f"it is owned by uid {os.getuid()}, not the app uid {app_uid}" in err


# ---------------------------------------------------------------------------
# main(): the order of the root steps
# ---------------------------------------------------------------------------

_STEPS = (
    "prepare_writable_paths",
    "provision_session_accounts",
    "prepare_isolation_dirs",
    "launch_isolation_broker",
    "drop_privileges",
)


def _run_main(entrypoint, monkeypatch, euid):
    calls = []

    class _Exec(Exception):
        pass

    def execvp(*_args):
        raise _Exec

    def step(name):
        return lambda *args, **kwargs: calls.append((name, args, kwargs))

    monkeypatch.setattr(
        entrypoint, "os", _Proxy(os, geteuid=lambda: euid, execvp=execvp)
    )
    for name in _STEPS:
        monkeypatch.setattr(entrypoint, name, step(name))
    with pytest.raises(_Exec):
        entrypoint.main(["true"])
    return calls


def test_uid_mode_provisions_before_the_broker_starts(entrypoint, env, monkeypatch):
    env(CLAUDE_ISOLATION_MODE="uid", APP_UID="1000", APP_GID="1000")
    calls = _run_main(entrypoint, monkeypatch, euid=0)

    assert [name for name, _args, _kwargs in calls] == list(_STEPS)
    steps = {name: (args, kwargs) for name, args, kwargs in calls}
    assert steps["provision_session_accounts"] == ((1000, 1000), {})
    _args, dirs_kwargs = steps["prepare_isolation_dirs"]
    # The same skip set the ownership repair got.
    assert (
        dirs_kwargs["skip_owners"] is steps["prepare_writable_paths"][1]["skip_owners"]
    )
    assert (dirs_kwargs["uid"], dirs_kwargs["gid"]) == (1000, 1000)
    assert 20000 in dirs_kwargs["skip_owners"]


@pytest.mark.parametrize("mode", [None, "off", "landlock", "uidx"])
def test_other_modes_provision_nothing(entrypoint, env, monkeypatch, mode):
    if mode is not None:
        env(CLAUDE_ISOLATION_MODE=mode)
    calls = _run_main(entrypoint, monkeypatch, euid=0)

    assert [name for name, _args, _kwargs in calls] == [
        "prepare_writable_paths",
        "launch_isolation_broker",
        "drop_privileges",
    ]


def test_a_non_root_start_runs_no_root_step(entrypoint, env, monkeypatch):
    env(CLAUDE_ISOLATION_MODE="uid")
    assert _run_main(entrypoint, monkeypatch, euid=1000) == []


# ---------------------------------------------------------------------------
# Parity with the gateway
# ---------------------------------------------------------------------------


def test_names_and_limits_match_the_gateway(entrypoint):
    from src import claude_config_paths as cp
    from src.backends.claude import uid_isolation as ui

    assert entrypoint.CONFIG_DIR_ENV == cp.CONFIG_DIR_ENV
    assert entrypoint.CONFIG_BASE_ENV == cp.CONFIG_BASE_ENV
    assert entrypoint.PLUGIN_CLONE_ROOT_ENV == cp.PLUGIN_CLONE_ROOT_ENV
    assert entrypoint.BROKER_GID_ENV == ui.BROKER_GID_ENV
    assert entrypoint.MAX_SESSION_ID == ui._MAX_ID


@pytest.mark.parametrize("raw", ["/data/claude/users", " /data/x/ ", "/a/./b/../c"])
def test_the_base_reads_like_the_gateway(entrypoint, env, monkeypatch, raw):
    # The dir the root step creates is the one the gateway puts P(uid) in.
    from src import claude_config_paths as cp

    created = []

    def make(path, _mode, _label):
        created.append(Path(path))

    monkeypatch.setattr(entrypoint, "_make_dir_nofollow", make)
    env(CLAUDE_ISOLATION_CONFIG_BASE=raw)
    entrypoint._prepare_config_base(uid=1, gid=1, protected=[])
    assert created == [cp.config_base()]


@pytest.mark.parametrize("raw", ["", "/data/workspaces", "rel/ws"])
def test_the_workspace_base_reads_like_the_gateway(entrypoint, env, monkeypatch, raw):
    import src.workspace_manager as wm

    monkeypatch.setattr(wm, "USER_WORKSPACES_DIR", raw)
    env(USER_WORKSPACES_DIR=raw)
    assert entrypoint._workspaces_dir() == os.path.abspath(wm._resolve_base_path())


def test_system_dirs_cover_the_gateways_refusals(entrypoint):
    # The gateway refuses its state under these (landlock._system_ro_errors,
    # _GrantFilter): the root step never creates the shared dir or base there.
    from src.backends.claude import landlock

    roots = {*landlock._LAUNCHER_SYSTEM_RO, *landlock._PRIVATE_SYSTEM_ROOTS, "/tmp"}
    assert roots <= set(entrypoint.SYSTEM_DIRS)


# ---------------------------------------------------------------------------
# The image and the compose override
# ---------------------------------------------------------------------------


def _instructions(dockerfile: str) -> list[list[str]]:
    text = dockerfile.replace("\\\n", " ")
    return [line.split() for line in text.splitlines() if not line.startswith("#")]


def test_the_image_ships_the_probe_beside_the_isolation_files():
    instructions = _instructions((ROOT / "Dockerfile").read_text())
    copy = next(i for i in instructions if "scripts/isolation_probe.py" in i)
    assert copy[0] == "COPY" and not any(a.startswith("--chown") for a in copy)
    assert copy[-1] == "/usr/local/bin/isolation_probe.py"
    chmod = next(
        i
        for i in instructions
        if i[:1] == ["RUN"] and "/usr/local/bin/isolation_probe.py" in i
    )
    assert chmod[1:3] == ["chmod", "0755"]
    assert "/usr/local/bin/landlock_launcher.py" in chmod
    # Root-owned: copied before nothing re-owns /usr/local/bin.
    assert not any(
        "chown" in i and "/usr/local/bin" in " ".join(i) for i in instructions
    )


def _override():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "docker-compose.uid.yml").read_text())


def test_the_compose_override_turns_uid_mode_on():
    yaml = pytest.importorskip("yaml")
    override = _override()
    gateway = override["services"]["gateway"]
    environment = dict(item.split("=", 1) for item in gateway["environment"])
    assert environment == {
        "CLAUDE_ISOLATION_MODE": "uid",
        "CLAUDE_CONFIG_DIR": "/data/claude/shared",
        "CLAUDE_PLUGIN_CLONE_ROOT": "/data/claude/shared/plugin-marketplaces",
        "CLAUDE_ISOLATION_CONFIG_BASE": "/data/claude/users",
        "CLAUDE_ISOLATION_MIGRATE_FROM": "/home/app/.claude",
    }
    assert gateway["volumes"] == ["claude_isolation:/data/claude"]
    assert "claude_isolation" in override["volumes"]
    # The old shared dir it migrates from is the main file's claude_home.
    main = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "claude_home:/home/app/.claude" in main["services"]["gateway"]["volumes"]
    assert not any(
        str(item).startswith("CLAUDE_ISOLATION_MODE")
        for item in main["services"]["gateway"]["environment"]
    )


def test_the_override_layout_passes_the_entrypoint_checks(entrypoint):
    environment = dict(
        item.split("=", 1) for item in _override()["services"]["gateway"]["environment"]
    )
    shared = environment["CLAUDE_CONFIG_DIR"]
    base = environment["CLAUDE_ISOLATION_CONFIG_BASE"]
    gateway_state = [
        ("$HOME", "/home/app"),
        ("the gateway's app root", "/app"),
        ("the workspace base", "/data/workspaces"),  # docker-compose.yml
    ]
    assert entrypoint._placement_refusal(shared, gateway_state) is None
    assert entrypoint._placement_refusal(base, [("S", shared), *gateway_state]) is None
    clone = environment["CLAUDE_PLUGIN_CLONE_ROOT"]
    assert entrypoint._overlap(clone, shared) == "lies inside"
    assert entrypoint._overlap(base, "/data/claude") == "lies inside"  # the volume


def test_env_example_documents_the_uid_layout():
    text = (ROOT / ".env.example").read_text()
    for knob in (
        "# CLAUDE_ISOLATION_CONFIG_BASE=/data/claude/users",
        "# CLAUDE_ISOLATION_MIGRATE_FROM=/home/app/.claude",
        "# CLAUDE_CONFIG_DIR=/data/claude/shared",
    ):
        assert knob in text, knob
    assert "docker-compose.uid.yml" in text
