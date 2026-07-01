"""Tests for the persistent admin-managed MCP server manifest."""

import json
from unittest.mock import patch

import pytest

from src import mcp_manifest


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    path = tmp_path / "gateway-mcp.json"
    monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(path))
    return path


def test_manifest_path_env_override(manifest_file):
    assert mcp_manifest.manifest_path() == manifest_file


class TestLoad:
    """load() never raises; degrades to defaults on any bad input."""

    def test_missing_returns_defaults(self, manifest_file):
        assert mcp_manifest.load() == {"version": 1, "servers": {}}

    def test_corrupt_json_returns_defaults(self, manifest_file):
        manifest_file.write_text("{not valid json", encoding="utf-8")
        assert mcp_manifest.load() == {"version": 1, "servers": {}}

    def test_non_dict_top_level_returns_defaults(self, manifest_file):
        manifest_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert mcp_manifest.load() == {"version": 1, "servers": {}}

    def test_non_dict_servers_coerced_to_empty(self, manifest_file):
        manifest_file.write_text(
            json.dumps({"version": 1, "servers": "nope"}), encoding="utf-8"
        )
        assert mcp_manifest.load() == {"version": 1, "servers": {}}

    def test_non_dict_server_entries_filtered(self, manifest_file):
        manifest_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": {
                        "good": {"type": "stdio", "command": "ls"},
                        "junk": "a string",
                        "also-junk": [1, 2],
                    },
                }
            ),
            encoding="utf-8",
        )
        data = mcp_manifest.load()
        assert list(data["servers"]) == ["good"]

    def test_bad_version_coerced_to_one(self, manifest_file):
        manifest_file.write_text(
            json.dumps({"version": "x", "servers": {}}), encoding="utf-8"
        )
        assert mcp_manifest.load()["version"] == 1


class TestSave:
    """save() is atomic: round-trips and leaves no temp file behind."""

    def test_save_load_roundtrip(self, manifest_file):
        data = {
            "version": 1,
            "servers": {"docs": {"type": "stdio", "command": "uvx"}},
        }
        mcp_manifest.save(data)
        assert json.loads(manifest_file.read_text(encoding="utf-8")) == data
        assert mcp_manifest.load() == data

    def test_save_leaves_no_tmp_file(self, manifest_file):
        mcp_manifest.save({"version": 1, "servers": {}})
        leftovers = list(manifest_file.parent.glob("*.tmp"))
        assert leftovers == []

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nested" / "gateway-mcp.json"
        monkeypatch.setenv("GATEWAY_MCP_MANIFEST", str(nested))
        mcp_manifest.save(mcp_manifest.load())
        assert nested.is_file()


class TestUpsertDelete:
    """upsert/delete round-trip and hot-reload wiring."""

    def test_upsert_then_get_and_list(self, manifest_file):
        cfg = {"type": "stdio", "command": "echo"}
        with patch("src.mcp_config.reload_mcp_config"):
            mcp_manifest.upsert_server("srv", cfg)
        assert mcp_manifest.get_server("srv") == cfg
        assert mcp_manifest.list_servers() == {"srv": cfg}

    def test_upsert_replaces_existing(self, manifest_file):
        with patch("src.mcp_config.reload_mcp_config"):
            mcp_manifest.upsert_server("srv", {"type": "stdio", "command": "a"})
            mcp_manifest.upsert_server("srv", {"type": "stdio", "command": "b"})
        assert mcp_manifest.get_server("srv")["command"] == "b"
        assert list(mcp_manifest.list_servers()) == ["srv"]

    def test_get_missing_returns_empty_dict(self, manifest_file):
        assert mcp_manifest.get_server("nope") == {}

    def test_delete_returns_existed_true_then_false(self, manifest_file):
        with patch("src.mcp_config.reload_mcp_config"):
            mcp_manifest.upsert_server("srv", {"type": "stdio", "command": "echo"})
            assert mcp_manifest.delete_server("srv") is True
            assert mcp_manifest.delete_server("srv") is False
        assert mcp_manifest.get_server("srv") == {}

    def test_upsert_triggers_reload(self, manifest_file):
        with patch("src.mcp_config.reload_mcp_config") as mock_reload:
            mcp_manifest.upsert_server("srv", {"type": "stdio", "command": "echo"})
        mock_reload.assert_called_once()

    def test_delete_triggers_reload(self, manifest_file):
        with patch("src.mcp_config.reload_mcp_config"):
            mcp_manifest.upsert_server("srv", {"type": "stdio", "command": "echo"})
        with patch("src.mcp_config.reload_mcp_config") as mock_reload:
            mcp_manifest.delete_server("srv")
        mock_reload.assert_called_once()
