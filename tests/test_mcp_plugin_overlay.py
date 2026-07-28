"""Tests for plugin MCP credential overlays."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import patch

import pytest

from src import mcp_plugin_overlay, mcp_plugin_overlay_service
from src.mcp_plugin_overlay_service import (
    McpPluginOverlayError,
    McpPluginOverlayNotFound,
)


@pytest.fixture
def overlay_file(tmp_path, monkeypatch):
    path = tmp_path / "plugin-overlay.json"
    monkeypatch.setenv("GATEWAY_MCP_PLUGIN_OVERLAY", str(path))
    mcp_plugin_overlay.reload_overlays()
    yield path
    # This teardown runs BEFORE monkeypatch undo, so re-point the store at a
    # path that never exists and reload — otherwise overlays this test saved
    # would stay in the module singleton and leak into later tests.
    monkeypatch.setenv("GATEWAY_MCP_PLUGIN_OVERLAY", str(tmp_path / "absent.json"))
    mcp_plugin_overlay.reload_overlays()


@pytest.fixture
def env_overlay(monkeypatch):
    """Setter for the ``GATEWAY_MCP_SERVER_ENV`` layer (inline JSON or a path).

    Teardown clears the var and reloads BEFORE monkeypatch undo, so a declared
    layer never leaks into later tests through the module singleton.
    """

    def _set(value: str) -> None:
        monkeypatch.setenv(mcp_plugin_overlay.ENV_OVERLAY_VAR, value)
        mcp_plugin_overlay.reload_overlays()

    yield _set
    monkeypatch.delenv(mcp_plugin_overlay.ENV_OVERLAY_VAR, raising=False)
    mcp_plugin_overlay.reload_overlays()


def _plugin_entry(name="ctx", plugin_id="p@mp", config=None, install_path=None):
    return {
        "plugin_id": plugin_id,
        "plugin_name": "p",
        "marketplace": "mp",
        "scope": "user",
        "origin": "managed",
        "server_name": name,
        "config": config
        or {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "svc"],
            "env": {"BASE": "1"},
        },
        "install_path": install_path,
    }


class TestOverlayStore:
    def test_upsert_and_get(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "secret"}, headers={})
        assert mcp_plugin_overlay.get_overlay("ctx")["env"]["TOKEN"] == "secret"
        assert "ctx" in mcp_plugin_overlay.list_overlay_names()

    def test_empty_overlay_deletes(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"A": "1"})
        mcp_plugin_overlay.upsert_overlay("ctx", env={}, headers={})
        assert mcp_plugin_overlay.get_overlay("ctx") == {}

    def test_merge_overlay_into_config(self):
        base = {
            "type": "stdio",
            "command": "echo",
            "env": {"BASE": "1", "SHARED": "old"},
        }
        overlay = {"env": {"SHARED": "new", "EXTRA": "x"}}
        merged = mcp_plugin_overlay.merge_overlay_into_config(base, overlay)
        assert merged["env"]["BASE"] == "1"
        assert merged["env"]["SHARED"] == "new"
        assert merged["env"]["EXTRA"] == "x"
        assert base["env"]["SHARED"] == "old"  # no mutate

    def test_materialize_skips_stale(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("gone", env={"T": "1"})
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            assert mcp_plugin_overlay.materialize_overlaid_plugin_servers() == {}

    def test_materialize_merges(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "{{env:TOK}}"})
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            out = mcp_plugin_overlay.materialize_overlaid_plugin_servers()
        assert out["ctx"]["command"] == "npx"
        assert out["ctx"]["env"]["BASE"] == "1"
        assert out["ctx"]["env"]["TOKEN"] == "{{env:TOK}}"

    def test_materialize_expands_plugin_root(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "t"})
        entry = _plugin_entry(
            config={
                "type": "stdio",
                "command": "${CLAUDE_PLUGIN_ROOT}/bin/serve",
                "args": ["--root", "${CLAUDE_PLUGIN_ROOT}/data"],
                "env": {"BASE": "${CLAUDE_PLUGIN_ROOT}/cfg"},
            },
            install_path="/opt/plugins/p",
        )
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[entry]):
            out = mcp_plugin_overlay.materialize_overlaid_plugin_servers()
        assert out["ctx"]["command"] == "/opt/plugins/p/bin/serve"
        assert out["ctx"]["args"] == ["--root", "/opt/plugins/p/data"]
        assert out["ctx"]["env"]["BASE"] == "/opt/plugins/p/cfg"
        assert out["ctx"]["env"]["TOKEN"] == "t"

    def test_materialize_plugin_server_single(self, overlay_file):
        """Single-server materialization works with and without an overlay."""
        entry = _plugin_entry(
            config={"type": "stdio", "command": "${CLAUDE_PLUGIN_ROOT}/run"},
            install_path="/opt/plugins/p",
        )
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[entry]):
            cfg = mcp_plugin_overlay.materialize_plugin_server("ctx")
            assert cfg == {"type": "stdio", "command": "/opt/plugins/p/run"}
            assert mcp_plugin_overlay.materialize_plugin_server("nope") is None

            mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "t"})
            cfg = mcp_plugin_overlay.materialize_plugin_server("ctx")
            assert cfg["env"]["TOKEN"] == "t"
            assert cfg["command"] == "/opt/plugins/p/run"


class TestOverlayService:
    def test_reject_non_plugin(self, overlay_file):
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            with pytest.raises(McpPluginOverlayError, match="not a plugin"):
                mcp_plugin_overlay_service.upsert_overlay("x", env={"A": "1"})

    def test_reject_non_string_env(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            with pytest.raises(McpPluginOverlayError, match="env"):
                mcp_plugin_overlay_service.upsert_overlay(
                    "ctx", env={"A": 1}  # type: ignore[dict-item]
                )

    def test_happy_path(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            result = mcp_plugin_overlay_service.upsert_overlay(
                "ctx", env={"TOKEN": "s3cret"}
            )
        assert result["status"] == "saved"
        assert result["server"] == "ctx"
        # Redacted in public response (TOKEN key is secret-looking).
        assert result["overlay"]["env"]["TOKEN"] == "***REDACTED***"
        # Stored raw.
        assert mcp_plugin_overlay.get_overlay("ctx")["env"]["TOKEN"] == "s3cret"

    def test_redacted_roundtrip_preserves_secret(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay(
                "ctx", env={"TOKEN": "real", "REGION": "us"}
            )
            mcp_plugin_overlay_service.upsert_overlay(
                "ctx",
                env={"TOKEN": "***REDACTED***", "REGION": "eu"},
            )
        stored = mcp_plugin_overlay.get_overlay("ctx")
        assert stored["env"]["TOKEN"] == "real"
        assert stored["env"]["REGION"] == "eu"

    def test_delete(self, overlay_file):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay("ctx", env={"A": "1"})
            mcp_plugin_overlay_service.delete_overlay("ctx")
        assert mcp_plugin_overlay.get_overlay("ctx") == {}
        with pytest.raises(McpPluginOverlayError, match="no overlay"):
            mcp_plugin_overlay_service.delete_overlay("ctx")

    def test_not_found_error_types(self, overlay_file):
        """Missing targets raise the NotFound subtype (routes map it to 404)."""
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            with pytest.raises(McpPluginOverlayNotFound):
                mcp_plugin_overlay_service.upsert_overlay("x", env={"A": "1"})
        with pytest.raises(McpPluginOverlayNotFound):
            mcp_plugin_overlay_service.delete_overlay("nope")

    def test_reject_placeholder_for_unknown_key(self, overlay_file):
        """A ***REDACTED*** value with no stored secret behind it is an error,
        not something to persist literally (e.g. a renamed key)."""
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay("ctx", env={"TOKEN": "real"})
            with pytest.raises(McpPluginOverlayError, match="placeholder"):
                mcp_plugin_overlay_service.upsert_overlay(
                    "ctx", env={"RENAMED": "***REDACTED***"}
                )
        # Stored overlay unchanged.
        assert mcp_plugin_overlay.get_overlay("ctx")["env"] == {"TOKEN": "real"}


class TestClaudeMerge:
    def test_merge_plugin_overlays_into_mcp_servers(self, overlay_file):
        mcp_plugin_overlay.upsert_overlay(
            "ctx", env={"TOKEN": "{{env:TOK}}", "PLAIN": "x"}
        )
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from src.backends.claude.client import ClaudeCodeCLI

            # Minimal instance without full init
            cli = object.__new__(ClaudeCodeCLI)
            merged = cli._merge_plugin_mcp_overlays(
                {"gw": {"type": "stdio", "command": "gw"}}
            )
            assert "gw" in merged
            assert "ctx" in merged
            # Env refs stay templated here; _configure_mcp_servers resolves
            # the merged map at session create.
            assert merged["ctx"]["env"]["TOKEN"] == "{{env:TOK}}"
            assert merged["ctx"]["env"]["PLAIN"] == "x"

    def test_stale_overlay_materializes_nothing(self, overlay_file):
        """An overlay without an installed plugin does not materialize."""
        mcp_plugin_overlay.upsert_overlay("gone", env={"LEAK": "v"})
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            merged = cli._merge_plugin_mcp_overlays(
                {"gw": {"type": "stdio", "command": "gw"}}
            )
        assert merged == {"gw": {"type": "stdio", "command": "gw"}}

    def test_plugin_server_materializes_without_overlay(self, overlay_file):
        """Every installed plugin MCP server rides mcp_servers now (strict MCP
        config) — an overlay is no longer required for materialization."""
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            merged, applied = mcp_plugin_overlay.apply_overlays(None)
        assert "ctx" in merged
        assert applied["plugin"] == ["ctx"]
        assert applied["plugin_overlaid"] == []
        assert applied["overridden"] == []

    def test_first_plugin_wins_for_duplicate_server_name(self, overlay_file):
        """Two plugins declaring the same server name: the first installed
        entry is materialized (same first-wins rule as
        ``get_plugin_mcp_server_config``)."""
        first = _plugin_entry(config={"type": "stdio", "command": "first"})
        second = _plugin_entry(
            plugin_id="q@mp", config={"type": "stdio", "command": "second"}
        )
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[first, second],
        ):
            merged, applied = mcp_plugin_overlay.apply_overlays(None)
        assert merged["ctx"]["command"] == "first"
        assert applied["plugin"] == ["ctx"]

    def test_gateway_config_overrides_plugin_without_overlay(self, overlay_file):
        """A same-named gateway server wins over the plugin bundle unless a
        credential overlay says otherwise (operator config > bundle default)."""
        gw = {"ctx": {"type": "stdio", "command": "operator-defined"}}
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            merged, applied = mcp_plugin_overlay.apply_overlays(gw)
        assert merged["ctx"]["command"] == "operator-defined"
        assert applied["overridden"] == ["ctx"]
        assert applied["plugin"] == []

    def test_materialization_logs_counts(self, overlay_file, caplog):
        """Materialization logs how many plugin servers were materialized and
        how many of them carried credential overlays."""
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "t"})
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            with caplog.at_level(logging.INFO):
                cli._merge_plugin_mcp_overlays(None)
        assert any(
            "Materialized 1 plugin MCP server(s) into mcp_servers "
            "(1 with credential overlays)" in r.getMessage()
            for r in caplog.records
        )

    def test_remote_header_overlay_lands_in_config(self, overlay_file):
        """Header overlays ride the materialized config — the copy the CLI
        actually registers and runs — so they apply to remote plugin servers."""
        mcp_plugin_overlay.upsert_overlay(
            "ctx", headers={"Authorization": "Bearer x"}
        )
        entry = _plugin_entry(config={"type": "http", "url": "https://mcp.example"})
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[entry]):
            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            merged = cli._merge_plugin_mcp_overlays(None)
        assert merged["ctx"]["headers"]["Authorization"] == "Bearer x"

    def test_overlay_env_never_reaches_session_process_env(self, overlay_file):
        """Regression for the scoped-env fix: overlay credentials must land in
        the materialized server config only. If session-process env injection
        is ever reintroduced, the agent's Bash would see the secret via `env`
        — this asserts options.env stays clean through the real MCP configure
        path."""
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "s3cret"})
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from claude_agent_sdk import ClaudeAgentOptions

            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            options = ClaudeAgentOptions()
            cli._configure_mcp_servers(options, None, ["mcp__ctx__*"])
        assert options.mcp_servers["ctx"]["env"]["TOKEN"] == "s3cret"
        assert "TOKEN" not in options.env


class TestEnvDeclaredOverlay:
    """The GATEWAY_MCP_SERVER_ENV layer: deploy-time credentials by server name."""

    def test_inline_json_lands_on_plugin_server(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"TOKEN": "from-env"}}}')
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            merged, applied = mcp_plugin_overlay.apply_overlays(None)
        assert applied["plugin"] == ["ctx"]
        assert merged["ctx"]["command"] == "npx"  # plugin base preserved
        assert merged["ctx"]["env"]["BASE"] == "1"
        assert merged["ctx"]["env"]["TOKEN"] == "from-env"

    def test_file_path_source(self, overlay_file, env_overlay, tmp_path):
        path = tmp_path / "mcp-server-env.json"
        path.write_text('{"ctx": {"env": {"TOKEN": "from-file"}}}', encoding="utf-8")
        env_overlay(str(path))
        assert mcp_plugin_overlay.get_env_overlay("ctx")["env"]["TOKEN"] == "from-file"
        assert mcp_plugin_overlay.list_env_overlay_names() == ["ctx"]

    def test_overlay_file_wrapper_shape_accepted(self, overlay_file, env_overlay):
        """The on-disk overlay document can be mounted and pointed at directly."""
        env_overlay('{"version": 1, "overlays": {"ctx": {"env": {"A": "1"}}}}')
        assert mcp_plugin_overlay.get_env_overlay("ctx")["env"]["A"] == "1"

    def test_invalid_json_is_ignored(self, overlay_file, env_overlay, caplog):
        with caplog.at_level(logging.ERROR):
            env_overlay("{not json")
        assert mcp_plugin_overlay.list_env_overlay_names() == []
        assert any("GATEWAY_MCP_SERVER_ENV" in r.getMessage() for r in caplog.records)

    def test_non_object_json_is_ignored(self, overlay_file, env_overlay):
        env_overlay('["ctx"]')
        assert mcp_plugin_overlay.list_env_overlay_names() == []

    def test_entry_without_usable_values_is_skipped(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"N": 5}}, "ok": {"env": {"A": "1"}}}')
        assert mcp_plugin_overlay.list_env_overlay_names() == ["ok"]

    def test_admin_file_wins_per_key(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"TOKEN": "from-env", "KEEP": "env"}}}')
        mcp_plugin_overlay.upsert_overlay("ctx", env={"TOKEN": "from-admin"})
        effective = mcp_plugin_overlay.get_effective_overlay("ctx")
        assert effective["env"]["TOKEN"] == "from-admin"
        assert effective["env"]["KEEP"] == "env"
        # Stored layer stays file-only — the admin edit form's contract.
        assert mcp_plugin_overlay.get_overlay("ctx")["env"] == {"TOKEN": "from-admin"}

    def test_admin_save_never_persists_env_declared_keys(
        self, overlay_file, env_overlay
    ):
        """Regression: freezing an env-declared value into the file would pin a
        rotated secret forever."""
        env_overlay('{"ctx": {"env": {"FROM_ENV": "v"}}}')
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            mcp_plugin_overlay_service.upsert_overlay("ctx", env={"FROM_ADMIN": "w"})
        on_disk = json.loads(overlay_file.read_text(encoding="utf-8"))
        assert on_disk["overlays"]["ctx"]["env"] == {"FROM_ADMIN": "w"}

    def test_gateway_server_gets_the_overlay(self, overlay_file, env_overlay):
        """A name no plugin declares falls back to the gateway-declared server."""
        env_overlay('{"gw": {"env": {"TOKEN": "t"}, "headers": {"X-A": "1"}}}')
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            merged, applied = mcp_plugin_overlay.apply_overlays(
                {"gw": {"type": "stdio", "command": "gw", "env": {"BASE": "1"}}}
            )
        assert applied == {
            "plugin": [],
            "plugin_overlaid": [],
            "overridden": [],
            "gateway": ["gw"],
            "stale": [],
        }
        assert merged["gw"]["env"] == {"BASE": "1", "TOKEN": "t"}
        assert merged["gw"]["headers"] == {"X-A": "1"}

    def test_plugin_wins_over_gateway_server_of_same_name(
        self, overlay_file, env_overlay
    ):
        env_overlay('{"ctx": {"env": {"TOKEN": "t"}}}')
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            merged, applied = mcp_plugin_overlay.apply_overlays(
                {"ctx": {"type": "stdio", "command": "gateway-copy"}}
            )
        assert applied["plugin"] == ["ctx"]
        assert merged["ctx"]["command"] == "npx"

    def test_stale_name_warns_and_changes_nothing(
        self, overlay_file, env_overlay, caplog
    ):
        env_overlay('{"nowhere": {"env": {"LEAK": "v"}}}')
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            with caplog.at_level(logging.WARNING):
                merged, applied = mcp_plugin_overlay.apply_overlays(
                    {"gw": {"type": "stdio", "command": "gw"}}
                )
        assert applied["stale"] == ["nowhere"]
        assert merged == {"gw": {"type": "stdio", "command": "gw"}}
        assert any("nowhere" in r.getMessage() for r in caplog.records)

    def test_env_ref_resolves_at_session_create_and_stays_scoped(
        self, overlay_file, env_overlay, monkeypatch
    ):
        """{{env:NAME}} keeps the real secret in a plain gateway env var, and the
        resolved value must reach the MCP server config only."""
        monkeypatch.setenv("MCP_TOK", "s3cret")
        env_overlay('{"gw": {"env": {"TOKEN": "{{env:MCP_TOK}}"}}}')
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            from claude_agent_sdk import ClaudeAgentOptions

            from src.backends.claude.client import ClaudeCodeCLI

            cli = object.__new__(ClaudeCodeCLI)
            options = ClaudeAgentOptions()
            cli._configure_mcp_servers(
                options, {"gw": {"type": "stdio", "command": "gw"}}, ["mcp__gw__*"]
            )
        assert options.mcp_servers["gw"]["env"]["TOKEN"] == "s3cret"
        assert "TOKEN" not in options.env
        assert "MCP_TOK" not in options.env

    def test_reload_picks_up_env_changes(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"A": "1"}}}')
        env_overlay('{"ctx": {"env": {"A": "2"}}}')
        assert mcp_plugin_overlay.get_env_overlay("ctx")["env"]["A"] == "2"

    def test_detail_reports_env_layer_separately(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"FROM_ENV": "v"}}}')
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            detail = mcp_plugin_overlay_service.get_overlay_detail("ctx")
        # No stored overlay: the edit form sees nothing to round-trip.
        assert detail["exists"] is False
        assert detail["overlay"] == {}
        assert detail["env_declared"] is True
        assert detail["env_declared_var"] == "GATEWAY_MCP_SERVER_ENV"
        assert detail["env_overlay_env_keys"] == ["FROM_ENV"]
        assert "FROM_ENV" in detail["effective"]["env"]

    def test_delete_points_at_the_env_var(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"A": "1"}}}')
        with pytest.raises(McpPluginOverlayNotFound) as exc:
            mcp_plugin_overlay_service.delete_overlay("ctx")
        assert "GATEWAY_MCP_SERVER_ENV" in str(exc.value)

    async def test_connection_test_probes_with_env_overlay(
        self, overlay_file, env_overlay
    ):
        """The admin probe must see the same credentials a session would get,
        otherwise an authenticated remote server tests as unreachable."""
        env_overlay('{"gw": {"headers": {"Authorization": "Bearer x"}}}')
        probed = {}

        async def fake_probe(name, config):
            probed[name] = config
            return {"ok": True, "detail": "probed"}

        with patch(
            "src.mcp_config.get_mcp_servers",
            return_value={"gw": {"type": "http", "url": "https://mcp.example"}},
        ):
            with patch("src.mcp_connection_test.test_mcp_server", fake_probe):
                from src import mcp_admin_service

                result = await mcp_admin_service.test_connection("gw")
        assert result["ok"] is True
        assert probed["gw"]["headers"] == {"Authorization": "Bearer x"}

    def test_admin_row_flags_env_layer(self, overlay_file, env_overlay):
        env_overlay('{"ctx": {"env": {"A": "1"}}}')
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            from src.admin_service import get_plugin_mcp_servers_detail

            rows = {r["name"]: r for r in get_plugin_mcp_servers_detail()}
        assert rows["ctx"]["has_env_overlay"] is True
        assert rows["ctx"]["has_overlay"] is False
        assert rows["ctx"]["overlay"] == {}  # form prefill stays file-only
        assert rows["ctx"]["overlay_env_key_count"] == 1


class TestOverlayRoutes:
    @pytest.fixture
    def admin_client(self):
        """FastAPI TestClient with admin auth bypassed (same shape as
        the fixture in test_admin_features)."""
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"ADMIN_API_KEY": "test-key"}):
            from src.admin_auth import require_admin
            from src.main import app

            app.dependency_overrides[require_admin] = lambda: True
            client = TestClient(app)
            yield client
            app.dependency_overrides.pop(require_admin, None)

    def test_requires_admin(self, overlay_file):
        from fastapi.testclient import TestClient

        from src.main import app

        client = TestClient(app)
        r = client.get("/admin/api/mcp-servers/ctx/plugin-overlay")
        assert r.status_code == 401

    def test_put_get_delete_roundtrip(self, overlay_file, admin_client):
        with patch(
            "src.plugin_service.list_plugin_mcp_servers",
            return_value=[_plugin_entry()],
        ):
            r = admin_client.put(
                "/admin/api/mcp-servers/ctx/plugin-overlay",
                json={"env": {"TOKEN": "s3cret"}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "saved"
            # Secrets never echo back.
            assert body["overlay"]["env"]["TOKEN"] == "***REDACTED***"

            r = admin_client.get("/admin/api/mcp-servers/ctx/plugin-overlay")
            assert r.status_code == 200
            detail = r.json()
            assert detail["exists"] is True
            assert detail["overlay"]["env"]["TOKEN"] == "***REDACTED***"

        r = admin_client.delete("/admin/api/mcp-servers/ctx/plugin-overlay")
        assert r.status_code == 200
        r = admin_client.delete("/admin/api/mcp-servers/ctx/plugin-overlay")
        assert r.status_code == 404

    def test_put_non_plugin_is_404(self, overlay_file, admin_client):
        with patch("src.plugin_service.list_plugin_mcp_servers", return_value=[]):
            r = admin_client.put(
                "/admin/api/mcp-servers/ghost/plugin-overlay",
                json={"env": {"A": "1"}},
            )
        assert r.status_code == 404

    def test_put_invalid_name_is_400(self, overlay_file, admin_client):
        r = admin_client.put(
            "/admin/api/mcp-servers/bad%20name/plugin-overlay",
            json={"env": {"A": "1"}},
        )
        assert r.status_code == 400
