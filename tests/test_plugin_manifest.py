"""Tests for the persistent managed plugin manifest."""

import json
import threading

import pytest

from src import plugin_manifest


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    path = tmp_path / "gateway-plugins.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(path))
    return path


_AUTO_REFRESH_DEFAULTS = {"enabled": False, "interval_minutes": 60}


def test_manifest_path_env_override(manifest_file):
    assert plugin_manifest.manifest_path() == manifest_file


def test_load_missing_returns_defaults(manifest_file):
    assert plugin_manifest.load() == {
        "version": 1,
        "added": [],
        "removed": [],
        "marketplaces": {},
        "auto_refresh": _AUTO_REFRESH_DEFAULTS,
    }


def test_save_load_roundtrip(manifest_file):
    data = {
        "version": 1,
        "added": [
            {
                "repo": "https://example.com/x.git",
                "name": "octo",
                "marketplace": "nyldn-plugins",
                "scope": "user",
                "branch": "main",
            }
        ],
        "removed": [{"spec": "foo@bar", "scope": "user"}],
        "marketplaces": {},
    }
    plugin_manifest.save(data)
    assert json.loads(manifest_file.read_text(encoding="utf-8")) == data
    # load() normalizes in the auto_refresh section even when absent on disk
    assert plugin_manifest.load() == {**data, "auto_refresh": _AUTO_REFRESH_DEFAULTS}


def test_load_corrupt_file_fallback(manifest_file):
    manifest_file.write_text("{not valid json", encoding="utf-8")
    assert plugin_manifest.load() == {
        "version": 1,
        "added": [],
        "removed": [],
        "marketplaces": {},
        "auto_refresh": _AUTO_REFRESH_DEFAULTS,
    }


def test_load_wrong_types_fallback(manifest_file):
    manifest_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert plugin_manifest.load() == {
        "version": 1,
        "added": [],
        "removed": [],
        "marketplaces": {},
        "auto_refresh": _AUTO_REFRESH_DEFAULTS,
    }


def test_load_coerces_bad_member_types(manifest_file):
    manifest_file.write_text(
        json.dumps(
            {
                "version": "x",
                "added": [{"name": "a", "marketplace": "m"}, "junk"],
                "removed": ["ok", 5, {"spec": "p@m", "scope": "project"}],
            }
        ),
        encoding="utf-8",
    )
    data = plugin_manifest.load()
    assert data["version"] == 1
    assert data["added"] == [{"name": "a", "marketplace": "m"}]
    # legacy string -> user scope; bad member dropped; dict form preserved
    assert data["removed"] == [
        {"spec": "ok", "scope": "user"},
        {"spec": "p@m", "scope": "project"},
    ]


def test_spec_for():
    assert plugin_manifest.spec_for("octo", "nyldn-plugins") == "octo@nyldn-plugins"
    assert plugin_manifest.spec_for("octo", "") == "octo"


def test_add_plugin_upsert_same_scope_and_unmark_removed(manifest_file):
    spec = "octo@nyldn-plugins"
    plugin_manifest.mark_removed(spec, "user")
    plugin_manifest.add_plugin(
        repo="https://example.com/x.git",
        name="octo",
        marketplace="nyldn-plugins",
        scope="user",
        branch="main",
    )
    # re-add at the SAME scope with a different branch -> upsert (no duplicate)
    plugin_manifest.add_plugin(
        repo="https://example.com/x.git",
        name="octo",
        marketplace="nyldn-plugins",
        scope="user",
        branch="dev",
    )
    added = plugin_manifest.list_added()
    assert len(added) == 1
    assert added[0]["branch"] == "dev"
    # add_plugin cleared the (spec, user) removed mark
    assert plugin_manifest.list_removed() == []


def test_add_plugin_distinct_scopes_coexist(manifest_file):
    # The same plugin installed at two scopes is two managed entries (scope is
    # part of the identity), so both replay on startup.
    for sc in ("user", "project"):
        plugin_manifest.add_plugin(
            repo="r", name="octo", marketplace="m", scope=sc, branch="main"
        )
    scopes = sorted(e["scope"] for e in plugin_manifest.list_added())
    assert scopes == ["project", "user"]


def test_remove_added_is_scope_specific(manifest_file):
    for sc in ("user", "project"):
        plugin_manifest.add_plugin(
            repo="r", name="octo", marketplace="m", scope=sc, branch="main"
        )
    plugin_manifest.remove_added("octo@m", "user")
    remaining = plugin_manifest.list_added()
    assert [e["scope"] for e in remaining] == ["project"]
    # no-op when absent
    plugin_manifest.remove_added("missing@x", "user")


def test_mark_unmark_removed_idempotent(manifest_file):
    plugin_manifest.mark_removed("foo@bar", "project")
    plugin_manifest.mark_removed("foo@bar", "project")
    assert plugin_manifest.list_removed() == [{"spec": "foo@bar", "scope": "project"}]
    # a different scope is a distinct removal
    plugin_manifest.mark_removed("foo@bar", "user")
    assert len(plugin_manifest.list_removed()) == 2
    plugin_manifest.unmark_removed("foo@bar", "project")
    plugin_manifest.unmark_removed("foo@bar", "project")
    assert plugin_manifest.list_removed() == [{"spec": "foo@bar", "scope": "user"}]


def test_remove_marketplace_entries(manifest_file):
    plugin_manifest.add_plugin(
        repo="r", name="a", marketplace="mkt1", scope="user", branch="main"
    )
    plugin_manifest.add_plugin(
        repo="r", name="b", marketplace="mkt1", scope="user", branch="main"
    )
    plugin_manifest.add_plugin(
        repo="r", name="c", marketplace="mkt2", scope="user", branch="main"
    )
    plugin_manifest.remove_marketplace_entries("mkt1")
    names = [e["name"] for e in plugin_manifest.list_added()]
    assert names == ["c"]


def test_marketplace_record_set_get_list(manifest_file):
    plugin_manifest.set_marketplace(
        "mkt1", repo="https://github.com/acme/mkt1.git", branch="dev", scope="project"
    )
    assert plugin_manifest.get_marketplace("mkt1") == {
        "repo": "https://github.com/acme/mkt1.git",
        "branch": "dev",
        "scope": "project",
    }
    assert plugin_manifest.get_marketplace("missing") == {}
    assert "mkt1" in plugin_manifest.list_marketplace_records()


def test_remove_marketplace_entries_drops_record(manifest_file):
    plugin_manifest.set_marketplace("mkt1", repo="r", branch="main", scope="user")
    plugin_manifest.add_plugin(
        repo="r", name="a", marketplace="mkt1", scope="user", branch="main"
    )
    plugin_manifest.remove_marketplace_entries("mkt1")
    assert plugin_manifest.get_marketplace("mkt1") == {}
    assert plugin_manifest.list_added() == []


def test_auto_refresh_defaults(manifest_file):
    assert plugin_manifest.get_auto_refresh() == _AUTO_REFRESH_DEFAULTS


def test_auto_refresh_set_get_roundtrip(manifest_file):
    rec = plugin_manifest.set_auto_refresh(enabled=True, interval_minutes=30)
    assert rec == {"enabled": True, "interval_minutes": 30}
    assert plugin_manifest.get_auto_refresh() == rec
    # survives an unrelated read-modify-write mutation (load() must carry the
    # section through, or any add_plugin would silently drop it)
    plugin_manifest.add_plugin(
        repo="r", name="a", marketplace="m", scope="user", branch="main"
    )
    assert plugin_manifest.get_auto_refresh() == rec


def test_auto_refresh_clamps_interval(manifest_file):
    low = plugin_manifest.set_auto_refresh(enabled=True, interval_minutes=1)
    assert low["interval_minutes"] == plugin_manifest.AUTO_REFRESH_MIN_MINUTES
    high = plugin_manifest.set_auto_refresh(enabled=True, interval_minutes=10**9)
    assert high["interval_minutes"] == plugin_manifest.AUTO_REFRESH_MAX_MINUTES


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        # the bug: falsy strings must NOT read as True (plain bool() would)
        ("false", False),
        ("off", False),
        ("no", False),
        ("0", False),
        (None, False),
    ],
)
def test_auto_refresh_enabled_coercion(manifest_file, value, expected):
    manifest_file.write_text(
        json.dumps({"auto_refresh": {"enabled": value, "interval_minutes": 60}}),
        encoding="utf-8",
    )
    assert plugin_manifest.get_auto_refresh()["enabled"] is expected


def test_auto_refresh_non_finite_interval_does_not_raise(manifest_file):
    # json.loads accepts NaN/Infinity literals; int(nan)/int(inf) would raise and
    # escape load()'s try/except, so _normalize_auto_refresh must reject them.
    for literal in ("NaN", "Infinity", "-Infinity", "1e400"):
        manifest_file.write_text(
            '{"auto_refresh": {"enabled": true, "interval_minutes": ' + literal + "}}",
            encoding="utf-8",
        )
        # must not raise, and must fall back to the default interval
        rec = plugin_manifest.get_auto_refresh()
        assert rec["interval_minutes"] == plugin_manifest.AUTO_REFRESH_DEFAULT_MINUTES
        assert rec["enabled"] is True


def test_auto_refresh_normalizes_corrupt_record(manifest_file):
    manifest_file.write_text(
        json.dumps({"auto_refresh": {"enabled": 1, "interval_minutes": "soon"}}),
        encoding="utf-8",
    )
    assert plugin_manifest.get_auto_refresh() == {
        "enabled": True,
        "interval_minutes": plugin_manifest.AUTO_REFRESH_DEFAULT_MINUTES,
    }
    manifest_file.write_text(json.dumps({"auto_refresh": "junk"}), encoding="utf-8")
    assert plugin_manifest.get_auto_refresh() == _AUTO_REFRESH_DEFAULTS


def test_concurrent_add_plugin_no_lost_updates(manifest_file):
    # Concurrent admin mutations (run_in_threadpool) must not clobber each
    # other: each add_plugin is a read-modify-write that has to run under the
    # lock, else two requests read the same manifest and the last save wins.
    n = 12
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()  # force all writers to contend at once
        plugin_manifest.add_plugin(
            repo="r", name=f"p{i}", marketplace="m", scope="user", branch="main"
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    names = sorted(e["name"] for e in plugin_manifest.list_added())
    assert names == sorted(f"p{i}" for i in range(n))


def test_save_creates_parent_dirs(tmp_path, monkeypatch):
    nested = tmp_path / "deep" / "nested" / "gateway-plugins.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_MANIFEST", str(nested))
    plugin_manifest.save(plugin_manifest.load())
    assert nested.is_file()
