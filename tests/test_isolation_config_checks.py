"""Startup checks around session isolation (PR #244 review 2).

* a Landlock mode silently overrode an explicit ``CLAUDE_SANDBOX_ENABLED=true``
  and a managed policy that enables the CLI sandbox — now both are reported;
* a damaged uid registry used to load as empty and reassign uids — uid mode now
  refuses to start on it.
"""

from __future__ import annotations

import json

import pytest

from src import config_check as cc
from src.backends.claude import landlock
from src.backends.claude import uid_isolation as ui


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv(ui.UID_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(cc, "_MANAGED_SETTINGS_DIR", str(tmp_path / "no-managed"))
    monkeypatch.setattr(ui, "_registry", None)
    monkeypatch.setattr(ui, "_ephemeral_pool", None)


def _confined(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(landlock, "confine_enabled", lambda: on)


class TestSandboxConflict:
    @pytest.mark.parametrize("raw", ["true", "1", "YES", "on"])
    def test_explicit_sandbox_true_under_confinement_warns(self, monkeypatch, raw):
        _confined(monkeypatch, True)
        monkeypatch.setenv("CLAUDE_SANDBOX_ENABLED", raw)
        issues = cc._check_isolation_sandbox_conflict()
        assert [i.severity for i in issues] == ["warning"]
        assert "CLAUDE_SANDBOX_ENABLED" in issues[0].message

    @pytest.mark.parametrize("raw", ["false", "0", ""])
    def test_sandbox_false_or_unset_is_quiet(self, monkeypatch, raw):
        _confined(monkeypatch, True)
        monkeypatch.setenv("CLAUDE_SANDBOX_ENABLED", raw)
        assert cc._check_isolation_sandbox_conflict() == []

    def test_no_confinement_is_quiet(self, monkeypatch):
        _confined(monkeypatch, False)
        monkeypatch.setenv("CLAUDE_SANDBOX_ENABLED", "true")
        assert cc._check_isolation_sandbox_conflict() == []

    def test_managed_policy_enabling_the_sandbox_warns(self, monkeypatch, tmp_path):
        managed = tmp_path / "managed"
        (managed / "managed-settings.d").mkdir(parents=True)
        (managed / "managed-settings.json").write_text(
            json.dumps({"sandbox": {"enabled": True}})
        )
        (managed / "managed-settings.d" / "10-x.json").write_text(
            json.dumps({"sandbox": {"enabled": True}})
        )
        (managed / "managed-settings.d" / "20-off.json").write_text(
            json.dumps({"sandbox": {"enabled": False}})
        )
        (managed / "managed-settings.d" / "30-bad.json").write_text("{nope")
        monkeypatch.setattr(cc, "_MANAGED_SETTINGS_DIR", str(managed))
        _confined(monkeypatch, True)
        issues = cc._check_isolation_sandbox_conflict()
        assert len(issues) == 2
        assert all(i.severity == "warning" for i in issues)
        assert "managed-settings.json" in issues[0].message
        assert "10-x.json" in issues[1].message

    def test_check_config_includes_it(self, monkeypatch):
        _confined(monkeypatch, True)
        monkeypatch.setenv("CLAUDE_SANDBOX_ENABLED", "true")
        monkeypatch.setattr(cc, "_check_landlock_confinement", lambda: [])
        assert any(
            "CLAUDE_SANDBOX_ENABLED" in i.message for i in cc.check_config()
        )


class TestUidRegistryCheck:
    def _uid_mode(self, monkeypatch, mode: str = "uid") -> None:
        monkeypatch.setattr(landlock, "isolation_mode", lambda: mode)
        monkeypatch.setattr(landlock, "effective_mode", lambda: mode)

    def test_corrupt_registry_refuses_uid_mode(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text('{"alice": 2')
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        self._uid_mode(monkeypatch)
        issues = cc._check_uid_registry()
        assert [i.severity for i in issues] == ["error"]
        assert "corrupt" in issues[0].message

    def test_healthy_or_missing_registry_is_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(tmp_path / "uids.json"))
        self._uid_mode(monkeypatch)
        assert cc._check_uid_registry() == []

    def test_other_modes_never_load_it(self, monkeypatch, tmp_path):
        path = tmp_path / "uids.json"
        path.write_text("garbage")
        monkeypatch.setenv(ui.UID_REGISTRY_ENV, str(path))
        self._uid_mode(monkeypatch, "landlock")
        assert cc._check_uid_registry() == []
