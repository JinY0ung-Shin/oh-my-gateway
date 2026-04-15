"""Tests for src/system_prompt module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src import system_prompt as sp


@pytest.fixture(autouse=True)
def _reset_module(tmp_path):
    """Reset module-level state and isolate persistence to a temp dir."""
    sp._default_prompt = None
    sp._runtime_prompt = None
    orig_data_dir = sp._DATA_DIR
    orig_persist = sp._PERSIST_FILE
    sp._DATA_DIR = tmp_path
    sp._PERSIST_FILE = tmp_path / "system_prompt.json"
    yield
    sp._default_prompt = None
    sp._runtime_prompt = None
    sp._DATA_DIR = orig_data_dir
    sp._PERSIST_FILE = orig_persist


class TestLoadDefaultPrompt:
    def test_empty_path_uses_preset(self):
        sp.load_default_prompt("")
        assert sp._default_prompt is None

    def test_blank_path_uses_preset(self):
        sp.load_default_prompt("   ")
        assert sp._default_prompt is None

    def test_valid_file_loads_content(self, tmp_path):
        f = tmp_path / "prompt.txt"
        f.write_text("You are a helpful assistant.", encoding="utf-8")
        sp.load_default_prompt(str(f))
        assert sp._default_prompt == "You are a helpful assistant."

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            sp.load_default_prompt("/nonexistent/path.txt")

    def test_empty_file_falls_back_to_preset(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        sp.load_default_prompt(str(f))
        assert sp._default_prompt is None

    def test_whitespace_only_file_falls_back(self, tmp_path):
        f = tmp_path / "blank.txt"
        f.write_text("   \n  \n  ", encoding="utf-8")
        sp.load_default_prompt(str(f))
        assert sp._default_prompt is None


class TestGetSetReset:
    def test_preset_mode_returns_none(self):
        assert sp.get_system_prompt() is None
        assert sp.get_prompt_mode() == "preset"

    def test_file_default(self):
        sp._default_prompt = "from file"
        assert sp.get_system_prompt() == "from file"
        assert sp.get_prompt_mode() == "file"

    def test_runtime_override_takes_priority(self):
        sp._default_prompt = "from file"
        sp.set_system_prompt("runtime override")
        assert sp.get_system_prompt() == "runtime override"
        assert sp.get_prompt_mode() == "custom"

    def test_reset_reverts_to_file_default(self):
        sp._default_prompt = "from file"
        sp.set_system_prompt("override")
        sp.reset_system_prompt()
        assert sp.get_system_prompt() == "from file"
        assert sp.get_prompt_mode() == "file"

    def test_reset_reverts_to_preset(self):
        sp.set_system_prompt("override")
        sp.reset_system_prompt()
        assert sp.get_system_prompt() is None
        assert sp.get_prompt_mode() == "preset"

    def test_set_empty_raises_value_error(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            sp.set_system_prompt("")

    def test_set_whitespace_raises_value_error(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            sp.set_system_prompt("   \n  ")

    def test_set_strips_whitespace(self):
        sp.set_system_prompt("  hello world  ")
        assert sp.get_system_prompt() == "hello world"


class TestGetPromptMode:
    def test_preset(self):
        assert sp.get_prompt_mode() == "preset"

    def test_file(self):
        sp._default_prompt = "file content"
        assert sp.get_prompt_mode() == "file"

    def test_custom(self):
        sp.set_system_prompt("custom content")
        assert sp.get_prompt_mode() == "custom"

    def test_custom_over_file(self):
        sp._default_prompt = "file"
        sp.set_system_prompt("custom")
        assert sp.get_prompt_mode() == "custom"


class TestPersistence:
    def test_set_persists_to_file(self):
        sp.set_system_prompt("persisted prompt")
        assert sp._PERSIST_FILE.is_file()
        data = json.loads(sp._PERSIST_FILE.read_text(encoding="utf-8"))
        assert data["prompt"] == "persisted prompt"

    def test_reset_removes_file(self):
        sp.set_system_prompt("to delete")
        assert sp._PERSIST_FILE.is_file()
        sp.reset_system_prompt()
        assert not sp._PERSIST_FILE.is_file()

    def test_restore_on_startup(self):
        sp.set_system_prompt("saved prompt")
        sp._runtime_prompt = None  # simulate process restart
        sp.load_default_prompt("")
        assert sp.get_system_prompt() == "saved prompt"
        assert sp.get_prompt_mode() == "custom"

    def test_malformed_json_ignored(self):
        sp._PERSIST_FILE.write_text("{bad json", encoding="utf-8")
        sp.load_default_prompt("")
        assert sp.get_system_prompt() is None

    def test_non_string_prompt_ignored(self):
        sp._PERSIST_FILE.write_text(json.dumps({"prompt": 123}), encoding="utf-8")
        sp.load_default_prompt("")
        assert sp.get_system_prompt() is None

    def test_empty_string_prompt_ignored(self):
        sp._PERSIST_FILE.write_text(json.dumps({"prompt": "  "}), encoding="utf-8")
        sp.load_default_prompt("")
        assert sp.get_system_prompt() is None

    def test_non_dict_json_ignored(self):
        sp._PERSIST_FILE.write_text("[]", encoding="utf-8")
        sp.load_default_prompt("")
        assert sp.get_system_prompt() is None

    def test_write_failure_prevents_memory_update(self):
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                sp.set_system_prompt("should fail")
        assert sp.get_system_prompt() is None  # memory unchanged

    def test_delete_failure_prevents_memory_update(self):
        sp.set_system_prompt("active")
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            with pytest.raises(OSError, match="permission denied"):
                sp.reset_system_prompt()
        assert sp.get_system_prompt() == "active"  # memory unchanged
