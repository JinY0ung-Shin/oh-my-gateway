"""The shared Claude config-dir / plugin-root resolver (issue #241, PR-B)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src import claude_config_paths as cp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(cp.CONFIG_DIR_ENV, raising=False)
    monkeypatch.delenv(cp.PLUGIN_CLONE_ROOT_ENV, raising=False)


class TestConfigDir:
    def test_defaults_to_home_claude(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cp.config_dir() == tmp_path / ".claude"

    def test_config_dir_env_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(cp.CONFIG_DIR_ENV, str(tmp_path / "custom"))
        assert cp.config_dir() == tmp_path / "custom"

    def test_blank_config_dir_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(cp.CONFIG_DIR_ENV, "   ")
        assert cp.config_dir() == tmp_path / ".claude"

    def test_projects_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cp.projects_root() == tmp_path / ".claude" / "projects"


class TestConfigDirStrict:
    def test_home_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cp.config_dir_strict() == tmp_path / ".claude"

    def test_home_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("HOME", raising=False)
        assert cp.config_dir_strict() is None

    def test_override_wins_even_without_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setenv(cp.CONFIG_DIR_ENV, str(tmp_path / "c"))
        assert cp.config_dir_strict() == tmp_path / "c"


class TestSettingsPathDefault:
    def test_home_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cp.settings_path_default() == tmp_path / ".claude" / "settings.json"

    def test_home_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("HOME", raising=False)
        assert cp.settings_path_default() is None


class TestPluginCloneRoot:
    def test_env_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(cp.PLUGIN_CLONE_ROOT_ENV, str(tmp_path / "clones"))
        assert cp.plugin_clone_root() == tmp_path / "clones"

    def test_defaults_to_home_marketplaces(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert (
            cp.plugin_clone_root()
            == tmp_path / ".claude" / "plugin-marketplaces"
        )

    def test_home_unset_falls_back_to_tempdir(self, monkeypatch):
        monkeypatch.delenv("HOME", raising=False)
        # Path.home() is not consulted here; the fallback is the temp dir.
        root = cp.plugin_clone_root()
        assert root == Path(tempfile.gettempdir()) / ".claude" / "plugin-marketplaces"

    def test_ignores_config_dir_env(self, monkeypatch, tmp_path):
        # The clone root keys off HOME, not CLAUDE_CONFIG_DIR (matches the CLI).
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(cp.CONFIG_DIR_ENV, str(tmp_path / "custom"))
        assert (
            cp.plugin_clone_root()
            == tmp_path / ".claude" / "plugin-marketplaces"
        )
