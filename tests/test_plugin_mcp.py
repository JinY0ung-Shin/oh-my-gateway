"""Tests for plugin-provided MCP server enumeration (plugin_service).

Covers the read-only discovery that lets the admin MCP tab surface servers a
plugin declares (which the SDK loads via ``setting_sources``, bypassing the
gateway's ``MCP_CONFIG``/manifest effective config).
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.plugin_service import (
    _plugin_mcp_servers,
    _read_mcp_json,
    _safe_plugin_join,
    get_plugin_mcp_server_config,
    list_plugin_mcp_servers,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_plugin(
    cache_root: Path,
    marketplace: str,
    name: str,
    *,
    version: str = "1.0.0",
    manifest: dict | None = None,
    mcp_file=None,
    mcp_file_name: str = ".mcp.json",
    extra_files: dict | None = None,
) -> Path:
    """Create a plugin cache dir with plugin.json (+ optional files). Return installPath.

    *mcp_file* may be a dict (json-encoded) or a raw string (to inject invalid
    JSON). *extra_files* maps relative paths -> dict/str content.
    """
    install = cache_root / marketplace / name / version
    meta = install / ".claude-plugin"
    meta.mkdir(parents=True)
    man = {"name": name, "version": version}
    if manifest:
        man.update(manifest)
    (meta / "plugin.json").write_text(json.dumps(man))
    if mcp_file is not None:
        target = install / mcp_file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            mcp_file if isinstance(mcp_file, str) else json.dumps(mcp_file)
        )
    for rel, content in (extra_files or {}).items():
        p = install / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
    return install


@pytest.fixture
def mcp_plugins_root(tmp_path):
    """Fake ~/.claude/plugins with plugins declaring MCP in every supported shape."""
    root = tmp_path / "plugins"
    cache = root / "cache"
    cache.mkdir(parents=True)

    registry: dict = {"version": 2, "plugins": {}}

    def reg(key: str, install: Path) -> None:
        registry["plugins"][key] = [{"scope": "user", "installPath": str(install)}]

    # Flat .mcp.json — {name: cfg}
    reg(
        "flatmcp@mkt",
        _write_plugin(
            cache,
            "mkt",
            "flatmcp",
            mcp_file={"flat-server": {"command": "npx", "args": ["-y", "x"]}},
        ),
    )
    # Wrapped .mcp.json — {"mcpServers": {name: cfg}}
    reg(
        "wrapmcp@mkt",
        _write_plugin(
            cache,
            "mkt",
            "wrapmcp",
            mcp_file={
                "mcpServers": {"wrap-server": {"type": "http", "url": "https://a.test"}}
            },
        ),
    )
    # Inline plugin.json mcpServers object
    reg(
        "inlinemcp@mkt",
        _write_plugin(
            cache,
            "mkt",
            "inlinemcp",
            manifest={
                "mcpServers": {"inline-server": {"command": "node", "args": ["s.js"]}}
            },
        ),
    )
    # String-path plugin.json mcpServers -> custom file
    reg(
        "pathmcp@mkt",
        _write_plugin(
            cache,
            "mkt",
            "pathmcp",
            manifest={"mcpServers": "servers/custom.json"},
            extra_files={"servers/custom.json": {"path-server": {"command": "bun"}}},
        ),
    )
    # No MCP at all
    reg("nomcp@mkt", _write_plugin(cache, "mkt", "nomcp"))
    # Invalid .mcp.json — must not raise, yields nothing
    reg("badjson@mkt", _write_plugin(cache, "mkt", "badjson", mcp_file="{not json"))
    # Path-escape attempt in mcpServers string — must be refused
    (tmp_path / "evil.json").write_text(json.dumps({"evil": {"command": "x"}}))
    reg(
        "escape@mkt",
        _write_plugin(
            cache, "mkt", "escape", manifest={"mcpServers": "../../../evil.json"}
        ),
    )

    (root / "installed_plugins.json").write_text(json.dumps(registry))

    with patch("src.plugin_service._plugins_root", return_value=root):
        yield root


# ---------------------------------------------------------------------------
# list_plugin_mcp_servers
# ---------------------------------------------------------------------------


class TestListPluginMcpServers:
    def test_enumerates_every_shape(self, mcp_plugins_root):
        names = {s["server_name"] for s in list_plugin_mcp_servers()}
        assert names == {"flat-server", "wrap-server", "inline-server", "path-server"}

    def test_skips_missing_invalid_and_escape(self, mcp_plugins_root):
        names = {s["server_name"] for s in list_plugin_mcp_servers()}
        # nomcp -> nothing; badjson -> nothing; escape must not read outside the tree
        assert "evil" not in names

    def test_carries_plugin_id_and_config(self, mcp_plugins_root):
        by = {s["server_name"]: s for s in list_plugin_mcp_servers()}
        assert by["flat-server"]["plugin_id"] == "flatmcp@mkt"
        assert by["flat-server"]["plugin_name"] == "flatmcp"
        assert by["flat-server"]["scope"] == "user"
        assert by["flat-server"]["config"]["command"] == "npx"
        assert by["wrap-server"]["config"]["type"] == "http"
        assert by["path-server"]["config"]["command"] == "bun"

    def test_no_registry_returns_empty(self):
        with patch("src.plugin_service._plugins_root", return_value=None):
            assert list_plugin_mcp_servers() == []


# ---------------------------------------------------------------------------
# get_plugin_mcp_server_config
# ---------------------------------------------------------------------------


class TestGetPluginMcpServerConfig:
    def test_hit(self, mcp_plugins_root):
        cfg = get_plugin_mcp_server_config("inline-server")
        assert cfg is not None and cfg["command"] == "node"

    def test_miss(self, mcp_plugins_root):
        assert get_plugin_mcp_server_config("does-not-exist") is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class TestReadMcpJson:
    def test_flat(self, tmp_path):
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"a": {"command": "x"}}))
        assert _read_mcp_json(f) == {"a": {"command": "x"}}

    def test_wrapped(self, tmp_path):
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"mcpServers": {"a": {"url": "u"}}}))
        assert _read_mcp_json(f) == {"a": {"url": "u"}}

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "m.json"
        f.write_text("{bad")
        assert _read_mcp_json(f) == {}

    def test_filters_non_dict_entries(self, tmp_path):
        f = tmp_path / "m.json"
        f.write_text(json.dumps({"a": {"command": "x"}, "b": "notdict"}))
        assert _read_mcp_json(f) == {"a": {"command": "x"}}


class TestSafePluginJoin:
    def test_normal(self, tmp_path):
        base = tmp_path
        assert (
            _safe_plugin_join(base, "sub/x.json") == (base / "sub" / "x.json").resolve()
        )

    def test_dot_slash_prefix(self, tmp_path):
        base = tmp_path
        assert _safe_plugin_join(base, "./x.json") == (base / "x.json").resolve()

    def test_escape_refused(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        assert _safe_plugin_join(base, "../evil.json") is None

    def test_absolute_neutralised_to_relative(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        # A leading slash is stripped so the path can never point at the real FS root.
        assert (
            _safe_plugin_join(base, "/etc/passwd")
            == (base / "etc" / "passwd").resolve()
        )

    def test_empty(self, tmp_path):
        assert _safe_plugin_join(tmp_path, "") is None


class TestPluginMcpServers:
    def test_default_file(self, tmp_path):
        install = tmp_path / "p"
        install.mkdir()
        (install / ".mcp.json").write_text(json.dumps({"s": {"command": "x"}}))
        assert _plugin_mcp_servers(install, {}) == {"s": {"command": "x"}}

    def test_inline_takes_precedence_over_file(self, tmp_path):
        install = tmp_path / "p"
        install.mkdir()
        (install / ".mcp.json").write_text(json.dumps({"file": {"command": "x"}}))
        out = _plugin_mcp_servers(install, {"mcpServers": {"inline": {"command": "y"}}})
        assert out == {"inline": {"command": "y"}}

    def test_none_install(self):
        assert _plugin_mcp_servers(None, {"mcpServers": {"x": {}}}) == {}

    def test_no_field_no_file(self, tmp_path):
        install = tmp_path / "p"
        install.mkdir()
        assert _plugin_mcp_servers(install, {}) == {}
