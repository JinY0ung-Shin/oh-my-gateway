"""Tests for plugin_service — read-only plugin discovery."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.plugin_service import (
    _discover_commands,
    _discover_skills,
    _load_manifest,
    _parse_plugin_id,
    _read_json,
    _validate_install_path,
    _validate_marketplace_path,
    get_marketplaces_with_plugins,
    get_plugin_blocklist,
    get_plugin_detail,
    get_plugin_skill_content,
    list_marketplace_plugins,
    list_marketplaces,
    list_plugins,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_plugin_tree(plugins_root: Path) -> Path:
    """Build a realistic ~/.claude/plugins/ tree and return its path."""
    plugins_root.mkdir(parents=True, exist_ok=True)

    # installed_plugins.json
    cache_dir = plugins_root / "cache" / "test-mkt" / "demo-plugin" / "1.0.0"
    cache_dir.mkdir(parents=True)
    (plugins_root / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "demo-plugin@test-mkt": [
                        {
                            "scope": "user",
                            "installPath": str(cache_dir),
                            "version": "1.0.0",
                            "installedAt": "2026-01-01T00:00:00Z",
                            "lastUpdated": "2026-02-01T00:00:00Z",
                            "gitCommitSha": "abc123",
                        }
                    ],
                },
            }
        )
    )

    # plugin.json manifest
    meta_dir = cache_dir / ".claude-plugin"
    meta_dir.mkdir()
    (meta_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo-plugin",
                "version": "1.0.0",
                "description": "A demo plugin for testing",
                "author": {"name": "tester"},
                "keywords": ["test", "demo"],
            }
        )
    )

    # Flat skills (.claude/skills/*.md)
    skills_dir = cache_dir / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "greet.md").write_text("# Greet skill\nHello!")
    (skills_dir / "farewell.md").write_text("# Farewell skill\nBye!")

    # Commands
    commands_dir = cache_dir / ".claude" / "commands"
    commands_dir.mkdir(parents=True)
    (commands_dir / "run.md").write_text("# Run command")

    # hooks.json and settings.json
    (meta_dir / "hooks.json").write_text(json.dumps({"event:tool_call": []}))
    (meta_dir / "settings.json").write_text(json.dumps({"theme": "dark"}))

    # known_marketplaces.json
    (plugins_root / "known_marketplaces.json").write_text(
        json.dumps(
            {
                "test-mkt": {
                    "source": {"source": "github", "repo": "test/marketplace"},
                    "installLocation": str(plugins_root / "marketplaces" / "test-mkt"),
                    "lastUpdated": "2026-03-01T00:00:00Z",
                }
            }
        )
    )

    # marketplace catalog (.claude-plugin/marketplace.json)
    mkt_meta = plugins_root / "marketplaces" / "test-mkt" / ".claude-plugin"
    mkt_meta.mkdir(parents=True)
    (mkt_meta / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "test-mkt",
                "owner": {"name": "tester"},
                "metadata": {},
                "plugins": [
                    {
                        "name": "demo-plugin",
                        "description": "A demo plugin for testing",
                        "source": "./demo-plugin",
                        "strict": True,
                        "version": "1.0.0",
                        "skills": ["greet", "farewell"],
                    },
                    {
                        "name": "other-plugin",
                        "description": "Not installed",
                        "source": "./other-plugin",
                        "strict": False,
                    },
                ],
            }
        )
    )

    # blocklist.json
    (plugins_root / "blocklist.json").write_text(
        json.dumps(
            {
                "fetchedAt": "2026-04-01T00:00:00Z",
                "plugins": [
                    {
                        "plugin": "bad-plugin@evil-mkt",
                        "added_at": "2026-01-01T00:00:00Z",
                        "reason": "security",
                        "text": "Blocked for security",
                    }
                ],
            }
        )
    )

    return plugins_root


@pytest.fixture
def plugins_dir(tmp_path):
    """Fake ~/.claude/plugins/ tree."""
    root = _make_plugin_tree(tmp_path / "plugins")
    with patch("src.plugin_service._plugins_root", return_value=root):
        yield root


@pytest.fixture
def empty_plugins_dir(tmp_path):
    """Empty plugins root."""
    root = tmp_path / "plugins"
    root.mkdir()
    with patch("src.plugin_service._plugins_root", return_value=root):
        yield root


@pytest.fixture
def no_plugins_dir():
    """No plugins directory at all."""
    with patch("src.plugin_service._plugins_root", return_value=None):
        yield


# ---------------------------------------------------------------------------
# _parse_plugin_id
# ---------------------------------------------------------------------------


class TestParsePluginId:
    def test_standard_format(self):
        assert _parse_plugin_id("octo@nyldn-plugins") == ("octo", "nyldn-plugins")

    def test_no_marketplace(self):
        assert _parse_plugin_id("standalone") == ("standalone", "unknown")

    def test_multiple_at_signs(self):
        name, mkt = _parse_plugin_id("a@b@c")
        assert name == "a@b"
        assert mkt == "c"


# ---------------------------------------------------------------------------
# _read_json
# ---------------------------------------------------------------------------


class TestReadJson:
    def test_valid_json(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        assert _read_json(f) == {"key": "value"}

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{{")
        assert _read_json(f) is None

    def test_missing_file(self, tmp_path):
        assert _read_json(tmp_path / "nope.json") is None

    def test_oversized_file(self, tmp_path):
        f = tmp_path / "big.json"
        f.write_bytes(b"x" * (256 * 1024 + 1))
        assert _read_json(f) is None

    def test_symlink_rejected(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text('{"ok": true}')
        link = tmp_path / "link.json"
        link.symlink_to(real)
        assert _read_json(link) is None


# ---------------------------------------------------------------------------
# _validate_install_path
# ---------------------------------------------------------------------------


class TestValidateInstallPath:
    def test_valid_path_in_cache(self, plugins_dir):
        cache_dir = plugins_dir / "cache" / "test-mkt" / "demo-plugin" / "1.0.0"
        result = _validate_install_path(cache_dir)
        assert result is not None
        assert result.is_dir()

    def test_path_outside_cache_rejected(self, plugins_dir):
        assert _validate_install_path(Path("/tmp")) is None

    def test_empty_path_rejected(self, plugins_dir):
        """Empty string Path resolves to CWD — must be rejected."""
        assert _validate_install_path(Path("")) is None

    def test_traversal_path_rejected(self, plugins_dir):
        evil = plugins_dir / "cache" / ".." / ".." / ".."
        assert _validate_install_path(evil) is None

    def test_no_plugins_root(self, no_plugins_dir):
        assert _validate_install_path(Path("/anything")) is None

    def test_nonexistent_dir_rejected(self, plugins_dir):
        assert _validate_install_path(plugins_dir / "cache" / "nope") is None

    def test_symlink_rejected(self, plugins_dir):
        real_dir = plugins_dir / "cache" / "test-mkt" / "demo-plugin" / "1.0.0"
        link = plugins_dir / "cache" / "test-mkt" / "evil-link"
        link.symlink_to(real_dir)
        assert _validate_install_path(link) is None


# ---------------------------------------------------------------------------
# _load_manifest
# ---------------------------------------------------------------------------


class TestLoadManifest:
    def test_valid_manifest(self, plugins_dir):
        install_path = next((plugins_dir / "cache").rglob("plugin.json")).parent.parent
        manifest = _load_manifest(install_path)
        assert manifest["name"] == "demo-plugin"
        assert manifest["version"] == "1.0.0"

    def test_missing_manifest(self, tmp_path):
        assert _load_manifest(tmp_path) == {}

    def test_invalid_json(self, tmp_path):
        meta = tmp_path / ".claude-plugin"
        meta.mkdir()
        (meta / "plugin.json").write_text("not json {{{")
        assert _load_manifest(tmp_path) == {}


# ---------------------------------------------------------------------------
# _discover_skills
# ---------------------------------------------------------------------------


class TestDiscoverSkills:
    def test_flat_layout(self, plugins_dir):
        install_path = next((plugins_dir / "cache").rglob("plugin.json")).parent.parent
        skills = _discover_skills(install_path)
        names = [s["name"] for s in skills]
        assert "farewell" in names
        assert "greet" in names

    def test_nested_layout(self, tmp_path):
        # skills/my-skill/SKILL.md layout
        skill_dir = tmp_path / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Skill")
        skills = _discover_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0]["name"] == "my-skill"

    def test_empty_dir(self, tmp_path):
        assert _discover_skills(tmp_path) == []

    def test_symlink_skill_dir_rejected(self, tmp_path):
        real = tmp_path / "real_skills"
        real.mkdir()
        (real / "skill.md").write_text("# Skill")
        link = tmp_path / ".claude" / "skills"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        skills = _discover_skills(tmp_path)
        assert len(skills) == 0


# ---------------------------------------------------------------------------
# _discover_commands
# ---------------------------------------------------------------------------


class TestDiscoverCommands:
    def test_finds_commands(self, plugins_dir):
        install_path = next((plugins_dir / "cache").rglob("plugin.json")).parent.parent
        commands = _discover_commands(install_path)
        assert len(commands) == 1
        assert commands[0]["name"] == "run"

    def test_empty(self, tmp_path):
        assert _discover_commands(tmp_path) == []


# ---------------------------------------------------------------------------
# list_plugins
# ---------------------------------------------------------------------------


class TestListPlugins:
    def test_lists_installed(self, plugins_dir):
        plugins = list_plugins()
        assert len(plugins) == 1
        p = plugins[0]
        assert p["id"] == "demo-plugin@test-mkt"
        assert p["name"] == "demo-plugin"
        assert p["marketplace"] == "test-mkt"
        assert p["version"] == "1.0.0"
        assert p["skill_count"] == 2
        assert p["command_count"] == 1

    def test_no_plugins_dir(self, no_plugins_dir):
        assert list_plugins() == []

    def test_empty_registry(self, empty_plugins_dir):
        assert list_plugins() == []

    def test_install_path_outside_cache_skipped(self, plugins_dir):
        """Plugins with installPath outside cache/ should be skipped."""
        reg = plugins_dir / "installed_plugins.json"
        reg.write_text(
            json.dumps(
                {
                    "plugins": {
                        "evil@mkt": [{"installPath": "/etc", "version": "1.0.0"}],
                        "empty@mkt": [{"installPath": "", "version": "1.0.0"}],
                    }
                }
            )
        )
        plugins = list_plugins()
        # Both should produce entries with zero skills/commands (path rejected)
        for p in plugins:
            assert p["skill_count"] == 0
            assert p["command_count"] == 0

    def test_malformed_plugins_field_is_list(self, plugins_dir):
        """'plugins' being a list instead of dict should not crash."""
        reg = plugins_dir / "installed_plugins.json"
        reg.write_text(json.dumps({"plugins": ["not", "a", "dict"]}))
        assert list_plugins() == []

    def test_entries_as_dict_instead_of_list(self, plugins_dir):
        """Entry value being a dict instead of list should be skipped."""
        reg = plugins_dir / "installed_plugins.json"
        reg.write_text(json.dumps({"plugins": {"a@b": {"not": "a list"}}}))
        assert list_plugins() == []


# ---------------------------------------------------------------------------
# get_plugin_detail
# ---------------------------------------------------------------------------


class TestGetPluginDetail:
    def test_existing_plugin(self, plugins_dir):
        detail = get_plugin_detail("demo-plugin@test-mkt")
        assert detail is not None
        assert detail["name"] == "demo-plugin"
        assert detail["description"] == "A demo plugin for testing"
        assert detail["keywords"] == ["test", "demo"]
        assert len(detail["skills"]) == 2
        assert len(detail["commands"]) == 1
        assert detail["has_hooks"] is True
        assert detail["has_settings"] is True
        assert detail["git_commit_sha"] == "abc123"

    def test_manifest_only_safe_keys(self, plugins_dir):
        """Manifest in response should only contain allowlisted keys."""
        detail = get_plugin_detail("demo-plugin@test-mkt")
        assert detail is not None
        from src.plugin_service import _SAFE_MANIFEST_KEYS

        for key in detail["manifest"]:
            assert key in _SAFE_MANIFEST_KEYS

    def test_no_install_path_in_response(self, plugins_dir):
        """install_path should not be leaked to the API response."""
        detail = get_plugin_detail("demo-plugin@test-mkt")
        assert detail is not None
        assert "install_path" not in detail

    def test_no_raw_hooks_settings_in_response(self, plugins_dir):
        """Raw hooks/settings blobs should not be in the response."""
        detail = get_plugin_detail("demo-plugin@test-mkt")
        assert detail is not None
        assert "hooks" not in detail
        assert "settings" not in detail
        # Only boolean flags
        assert isinstance(detail["has_hooks"], bool)
        assert isinstance(detail["has_settings"], bool)

    def test_missing_plugin(self, plugins_dir):
        assert get_plugin_detail("nonexistent@mkt") is None

    def test_no_plugins_dir(self, no_plugins_dir):
        assert get_plugin_detail("demo-plugin@test-mkt") is None


# ---------------------------------------------------------------------------
# get_plugin_skill_content
# ---------------------------------------------------------------------------


class TestGetPluginSkillContent:
    def test_reads_skill(self, plugins_dir):
        result = get_plugin_skill_content("demo-plugin@test-mkt", "greet")
        assert result is not None
        assert "Hello!" in result["content"]
        assert result["skill_name"] == "greet"

    def test_missing_skill(self, plugins_dir):
        assert get_plugin_skill_content("demo-plugin@test-mkt", "nonexistent") is None

    def test_missing_plugin(self, plugins_dir):
        assert get_plugin_skill_content("nope@mkt", "greet") is None


# ---------------------------------------------------------------------------
# list_marketplaces
# ---------------------------------------------------------------------------


class TestListMarketplaces:
    def test_lists_marketplaces(self, plugins_dir):
        mkts = list_marketplaces()
        assert len(mkts) == 1
        assert mkts[0]["name"] == "test-mkt"
        assert mkts[0]["source_type"] == "github"
        assert mkts[0]["repo"] == "test/marketplace"

    def test_scope_defaults_user_without_manifest_record(self, plugins_dir, monkeypatch):
        from src import plugin_manifest

        monkeypatch.setattr(plugin_manifest, "list_marketplace_records", lambda: {})
        assert list_marketplaces()[0]["scope"] == "user"

    def test_scope_from_manifest_record(self, plugins_dir, monkeypatch):
        from src import plugin_manifest

        monkeypatch.setattr(
            plugin_manifest,
            "list_marketplace_records",
            lambda: {"test-mkt": {"repo": "r", "branch": "main", "scope": "project"}},
        )
        assert list_marketplaces()[0]["scope"] == "project"

    def test_no_plugins_dir(self, no_plugins_dir):
        assert list_marketplaces() == []


# ---------------------------------------------------------------------------
# get_plugin_blocklist
# ---------------------------------------------------------------------------


class TestGetPluginBlocklist:
    def test_returns_blocklist(self, plugins_dir):
        bl = get_plugin_blocklist()
        assert len(bl) == 1
        assert bl[0]["plugin"] == "bad-plugin@evil-mkt"
        assert bl[0]["reason"] == "security"

    def test_no_plugins_dir(self, no_plugins_dir):
        assert get_plugin_blocklist() == []

    def test_empty_blocklist(self, empty_plugins_dir):
        assert get_plugin_blocklist() == []


# ---------------------------------------------------------------------------
# Route integration tests (using TestClient)
# ---------------------------------------------------------------------------


class TestPluginRoutes:
    """Test admin plugin endpoints via the FastAPI test client."""

    @pytest.fixture
    def client(self, plugins_dir):
        from fastapi.testclient import TestClient

        from src.main import app
        from src.routes.admin import require_admin

        app.dependency_overrides[require_admin] = lambda: True
        yield TestClient(app)
        app.dependency_overrides.pop(require_admin, None)

    def test_list_plugins(self, client):
        resp = client.get("/admin/api/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert "plugins" in data
        assert len(data["plugins"]) == 1

    def test_get_plugin_detail(self, client):
        resp = client.get("/admin/api/plugins/demo-plugin@test-mkt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "demo-plugin"
        assert len(data["skills"]) == 2
        # Should not leak install_path or raw blobs
        assert "install_path" not in data
        assert "hooks" not in data
        assert "settings" not in data

    def test_get_plugin_not_found(self, client):
        resp = client.get("/admin/api/plugins/nonexistent@mkt")
        assert resp.status_code == 404

    def test_get_plugin_skill(self, client):
        resp = client.get("/admin/api/plugins/demo-plugin@test-mkt/skills/greet")
        assert resp.status_code == 200
        assert "Hello!" in resp.json()["content"]

    def test_get_plugin_skill_not_found(self, client):
        resp = client.get("/admin/api/plugins/demo-plugin@test-mkt/skills/nope")
        assert resp.status_code == 404

    def test_list_marketplaces(self, client):
        resp = client.get("/admin/api/marketplaces")
        assert resp.status_code == 200
        assert len(resp.json()["marketplaces"]) == 1

    def test_get_blocklist(self, client):
        resp = client.get("/admin/api/plugins/blocklist")
        assert resp.status_code == 200
        assert len(resp.json()["blocklist"]) == 1


# ---------------------------------------------------------------------------
# _validate_marketplace_path
# ---------------------------------------------------------------------------


class TestValidateMarketplacePath:
    def test_valid_path(self, plugins_dir):
        loc = plugins_dir / "marketplaces" / "test-mkt"
        result = _validate_marketplace_path(loc)
        assert result is not None
        assert result.is_dir()

    def test_outside_marketplaces_rejected(self, plugins_dir):
        assert _validate_marketplace_path(Path("/tmp")) is None

    def test_no_plugins_root(self, no_plugins_dir):
        assert _validate_marketplace_path(Path("/anything")) is None

    def test_nonexistent_rejected(self, plugins_dir):
        assert (
            _validate_marketplace_path(plugins_dir / "marketplaces" / "nope")
            is None
        )

    def test_symlink_rejected(self, plugins_dir):
        real = plugins_dir / "marketplaces" / "test-mkt"
        link = plugins_dir / "marketplaces" / "evil-link"
        link.symlink_to(real)
        assert _validate_marketplace_path(link) is None


# ---------------------------------------------------------------------------
# list_marketplace_plugins
# ---------------------------------------------------------------------------


class TestListMarketplacePlugins:
    def test_installed_flag(self, plugins_dir):
        plugins = list_marketplace_plugins("test-mkt")
        by_name = {p["name"]: p for p in plugins}
        assert by_name["demo-plugin"]["installed"] is True
        assert by_name["demo-plugin"]["id"] == "demo-plugin@test-mkt"
        assert by_name["demo-plugin"]["version"] == "1.0.0"
        assert by_name["demo-plugin"]["skill_count"] == 2
        assert by_name["other-plugin"]["installed"] is False
        assert by_name["other-plugin"]["version"] == ""
        assert by_name["other-plugin"]["skill_count"] == 0

    def test_installed_scope_reflects_registry(self, plugins_dir):
        # An installed catalog entry exposes its real registry scope (so the UI
        # can uninstall with the correct scope); not-installed entries default
        # to "user".
        known = plugins_dir / "installed_plugins.json"
        known.write_text(
            json.dumps(
                {
                    "version": 2,
                    "plugins": {
                        "demo-plugin@test-mkt": [
                            {"scope": "local", "installPath": "/x", "version": "1.0.0"}
                        ]
                    },
                }
            )
        )
        by_name = {p["name"]: p for p in list_marketplace_plugins("test-mkt")}
        assert by_name["demo-plugin"]["scope"] == "local"
        assert by_name["other-plugin"]["scope"] == "user"

    def test_unknown_marketplace(self, plugins_dir):
        assert list_marketplace_plugins("does-not-exist") == []

    def test_missing_catalog(self, plugins_dir):
        # installLocation present + valid but no marketplace.json inside.
        empty_loc = plugins_dir / "marketplaces" / "empty-mkt"
        empty_loc.mkdir(parents=True)
        known = plugins_dir / "known_marketplaces.json"
        known.write_text(
            json.dumps(
                {
                    "empty-mkt": {
                        "source": {"source": "github", "repo": "x/y"},
                        "installLocation": str(empty_loc),
                        "lastUpdated": "2026-03-01T00:00:00Z",
                    }
                }
            )
        )
        assert list_marketplace_plugins("empty-mkt") == []

    def test_corrupt_catalog(self, plugins_dir):
        catalog = (
            plugins_dir
            / "marketplaces"
            / "test-mkt"
            / ".claude-plugin"
            / "marketplace.json"
        )
        catalog.write_text("not json {{{")
        assert list_marketplace_plugins("test-mkt") == []

    def test_symlinked_install_location_rejected(self, plugins_dir):
        real = plugins_dir / "marketplaces" / "test-mkt"
        link = plugins_dir / "marketplaces" / "linked-mkt"
        link.symlink_to(real)
        known = plugins_dir / "known_marketplaces.json"
        known.write_text(
            json.dumps(
                {
                    "linked-mkt": {
                        "source": {"source": "github", "repo": "x/y"},
                        "installLocation": str(link),
                        "lastUpdated": "2026-03-01T00:00:00Z",
                    }
                }
            )
        )
        assert list_marketplace_plugins("linked-mkt") == []

    def test_no_plugins_dir(self, no_plugins_dir):
        assert list_marketplace_plugins("test-mkt") == []


# ---------------------------------------------------------------------------
# get_marketplaces_with_plugins
# ---------------------------------------------------------------------------


class TestGetMarketplacesWithPlugins:
    def test_aggregation(self, plugins_dir):
        result = get_marketplaces_with_plugins()
        assert len(result) == 1
        mkt = result[0]
        assert mkt["name"] == "test-mkt"
        assert mkt["plugin_count"] == 2
        assert len(mkt["plugins"]) == 2
        names = {p["name"] for p in mkt["plugins"]}
        assert names == {"demo-plugin", "other-plugin"}

    def test_no_plugins_dir(self, no_plugins_dir):
        assert get_marketplaces_with_plugins() == []


# ---------------------------------------------------------------------------
# origin field (managed vs env)
# ---------------------------------------------------------------------------


class TestPluginOrigin:
    def test_env_origin_when_not_managed(self, plugins_dir):
        with patch("src.plugin_manifest.list_added", return_value=[]):
            plugins = list_plugins()
            assert plugins[0]["origin"] == "env"
            detail = get_plugin_detail("demo-plugin@test-mkt")
            assert detail["origin"] == "env"

    def test_managed_origin(self, plugins_dir):
        fake = [
            {
                "repo": "test/marketplace",
                "name": "demo-plugin",
                "marketplace": "test-mkt",
                "scope": "user",
                "branch": "main",
            }
        ]
        with patch("src.plugin_manifest.list_added", return_value=fake):
            plugins = list_plugins()
            assert plugins[0]["origin"] == "managed"
            detail = get_plugin_detail("demo-plugin@test-mkt")
            assert detail["origin"] == "managed"

    def test_origin_defaults_env_on_manifest_failure(self, plugins_dir):
        with patch(
            "src.plugin_manifest.list_added", side_effect=RuntimeError("boom")
        ):
            plugins = list_plugins()
            assert plugins[0]["origin"] == "env"
