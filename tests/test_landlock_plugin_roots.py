"""A plugin root confinement refuses is reported, not silently dropped (#255).

With Landlock confinement on, every spawn passes the registry's plugin roots
through ``_GrantFilter``; one under the gateway app root (``/app/data/...``, a
natural place for an operator-persisted marketplace) is refused, so the CLI
never loads its skills while the admin page and ``/v1/agent-resources`` still
list them. The refusal stays (``/app`` holds the gateway's credentials); these
pin that it now surfaces at startup and on an admin install.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from src import config_check
from src.backends.claude import landlock


@pytest.fixture
def layout(monkeypatch, tmp_path):
    """A config dir, a fake gateway app root and a temp dir of its own, all
    under tmp_path (pytest's tmp dir is itself inside the system temp dir,
    which the grant filter refuses)."""
    cfg = tmp_path / "cfg"
    (cfg / "plugins").mkdir(parents=True)
    app = tmp_path / "app"
    (app / "data" / "plugins" / "inside-mp").mkdir(parents=True)
    outside = tmp_path / "srv" / "outside-mp"
    outside.mkdir(parents=True)
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("CLAUDE_PLUGIN_CLONE_ROOT", raising=False)
    monkeypatch.setattr(landlock, "_APP_ROOT", app)
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    monkeypatch.setenv(landlock.MODE_ENV, "landlock")
    landlock.reset_mode_cache()
    yield cfg, app / "data" / "plugins" / "inside-mp", outside
    landlock.reset_mode_cache()


def _register(cfg, marketplaces=None, plugins=None):
    plugins_dir = cfg / "plugins"
    (plugins_dir / "known_marketplaces.json").write_text(
        json.dumps(
            {
                name: {"source": {"source": "directory"}, "installLocation": str(loc)}
                for name, loc in (marketplaces or {}).items()
            }
        )
    )
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    pid: [{"scope": "user", "installPath": str(path)}]
                    for pid, path in (plugins or {}).items()
                },
            }
        )
    )


def test_a_marketplace_under_the_app_root_is_reported(layout):
    cfg, inside, outside = layout
    _register(
        cfg,
        marketplaces={"inside-mp": inside, "outside-mp": outside},
        plugins={"tool@inside-mp": inside},
    )
    refused = landlock.refused_plugin_roots()
    assert [(root, sorted(sources)) for root, sources, _ in refused] == [
        (str(inside), ["marketplace inside-mp", "plugin tool@inside-mp"])
    ], "only the root under the app root, with everything installed from it"
    assert "gateway app root" in refused[0][2]

    (warning,) = landlock.refused_plugin_root_warnings()
    assert str(inside) in warning and "marketplace inside-mp" in warning
    assert "missing from every session" in warning
    assert "CLAUDE_PLUGIN_CLONE_ROOT" in warning


def test_startup_check_warns_but_does_not_refuse_to_start(layout):
    cfg, inside, _outside = layout
    _register(cfg, marketplaces={"inside-mp": inside})
    issues = config_check._check_confined_plugin_roots()
    assert [i.severity for i in issues] == ["warning"]
    assert str(inside) in issues[0].message


def test_roots_outside_and_stale_entries_are_quiet(layout, tmp_path):
    cfg, _inside, outside = layout
    gone = tmp_path / "app" / "data" / "plugins" / "removed-mp"
    _register(cfg, marketplaces={"outside-mp": outside, "removed-mp": gone})
    assert landlock.refused_plugin_roots() == []
    assert landlock.refused_plugin_root_warnings() == []


def test_nothing_is_reported_with_confinement_off(layout, monkeypatch):
    cfg, inside, _outside = layout
    _register(cfg, marketplaces={"inside-mp": inside})
    monkeypatch.delenv(landlock.MODE_ENV, raising=False)
    landlock.reset_mode_cache()
    assert landlock.refused_plugin_root_warnings() == []
    assert config_check._check_confined_plugin_roots() == []


def test_the_operator_clone_root_is_named_as_its_source(layout, monkeypatch):
    _cfg, inside, _outside = layout
    monkeypatch.setenv("CLAUDE_PLUGIN_CLONE_ROOT", str(inside.parent))
    ((root, sources, _reason),) = landlock.refused_plugin_roots()
    assert root == str(inside.parent)
    assert sources == ["CLAUDE_PLUGIN_CLONE_ROOT"]


def test_the_spawn_refuses_the_same_root(layout):
    """The check reports exactly what a spawn's grant list leaves out."""
    cfg, inside, outside = layout
    _register(cfg, marketplaces={"inside-mp": inside, "outside-mp": outside})
    from src.backends.claude import workspace_sandbox as ws

    grants = landlock._GrantFilter(ws._claude_home(), None)
    assert grants.ro(inside) is None
    assert grants.ro(outside) == str(outside)


def test_an_admin_install_carries_the_warning(layout):
    from src import plugin_admin_service as pas

    cfg, inside, _outside = layout
    _register(cfg, marketplaces={"inside-mp": inside})
    result = pas._with_confinement_warnings({"status": "added"})
    assert result["status"] == "added"
    assert len(result["warnings"]) == 1 and str(inside) in result["warnings"][0]

    _register(cfg)
    assert "warnings" not in pas._with_confinement_warnings({"status": "added"})


# --- plugin bin/ on a confined spawn's PATH (#267) ---------------------------


def _install(cfg, plugins, enabled):
    """*plugins*: id -> (scope, installPath); *enabled*: id -> bool."""
    (cfg / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}))
    (cfg / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    pid: [{"scope": scope, "installPath": str(path)}]
                    for pid, (scope, path) in plugins.items()
                },
            }
        )
    )


def _plugin(root, name):
    (root / name / "bin").mkdir(parents=True)
    return root / name


def test_only_enabled_user_scope_bins_are_listed(layout):
    cfg, _inside, outside = layout
    on = _plugin(outside, "on")
    off = _plugin(outside, "off")
    proj = _plugin(outside, "proj")
    nobin = outside / "nobin"
    nobin.mkdir()
    _install(
        cfg,
        {
            "on@m": ("user", on),
            "off@m": ("user", off),
            "proj@m": ("project", proj),
            "nobin@m": ("user", nobin),
        },
        {"on@m": True, "off@m": False, "proj@m": True, "nobin@m": True},
    )
    assert landlock.enabled_plugin_bin_dirs() == [str(on / "bin")]


def test_bins_are_appended_and_only_when_granted(layout, monkeypatch):
    cfg, inside, outside = layout
    granted = _plugin(outside, "granted")
    (inside / "refused" / "bin").mkdir(parents=True)
    _install(
        cfg,
        {"g@m": ("user", granted), "r@m": ("user", inside / "refused")},
        {"g@m": True, "r@m": True},
    )
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = landlock._plugin_path_env([str(outside)])
    # Last, so no plugin shadows the CLI's own shell, git or rg lookups.
    assert env == {"PATH": f"/usr/bin:/bin:{granted / 'bin'}"}
    assert landlock._plugin_path_env([]) == {}


def test_a_bin_dir_with_a_path_separator_is_dropped(layout, monkeypatch):
    cfg, _inside, outside = layout
    odd = _plugin(outside, "a:b")
    _install(cfg, {"odd@m": ("user", odd)}, {"odd@m": True})
    assert landlock._plugin_path_env([str(outside)]) == {}


def test_options_env_carries_the_path(layout, monkeypatch, tmp_path):
    cfg, _inside, outside = layout
    tool = _plugin(outside, "tool")
    _install(cfg, {"tool@m": ("user", tool)}, {"tool@m": True})
    monkeypatch.setattr(landlock, "_ro_rw_roots", lambda *a, **k: ([str(outside)], []))
    env = landlock.options_env(tmp_path / "ws")
    assert env["PATH"].split(":")[-1] == str(tool / "bin")
