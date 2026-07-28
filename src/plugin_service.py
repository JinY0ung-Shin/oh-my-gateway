"""Plugin service — read-only discovery of installed Claude Code plugins.

Reads plugin metadata from ``~/.claude/plugins/`` (user-level directory
managed by Claude Code CLI).  All operations are strictly read-only;
install/uninstall remains the responsibility of the CLI.

Key data sources:
- ``installed_plugins.json`` — registry of installed plugins
- ``known_marketplaces.json`` — registered marketplace sources
- ``blocklist.json`` — blocked plugin entries
- ``cache/{marketplace}/{plugin}/{version}/`` — plugin files
  - ``.claude-plugin/plugin.json`` — manifest (name, version, description, skills, commands)
  - ``.claude/skills/*.md`` or ``skills/*/SKILL.md`` — plugin skills
  - ``.claude/agents/*.md`` or ``agents/*.md`` — plugin agents
  - ``.claude/commands/*.md`` — plugin commands
  - ``.mcp.json`` — plugin-provided MCP servers
  - ``.claude-plugin/hooks.json`` — hook definitions
  - ``.claude-plugin/settings.json`` — plugin settings
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Maximum file size we'll read from plugin directories (256 KB).
_MAX_READ_SIZE = 256 * 1024

# Manifest keys safe to expose in the API response.
_SAFE_MANIFEST_KEYS = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "license",
        "repository",
        "homepage",
        "keywords",
        "skills",
        "commands",
    }
)


def _plugins_root() -> Optional[Path]:
    """Return the Claude plugins dir if it exists.

    Honors ``CLAUDE_CONFIG_DIR`` (the CLI's config-dir override) so the
    gateway reads the same registry the CLI it spawns will read; defaults to
    ``~/.claude/plugins``.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(override) if override else Path.home() / ".claude"
    p = base / "plugins"
    return p if p.is_dir() else None


def _read_json(path: Path) -> Any:
    """Read and parse a JSON file, returning ``None`` on any error."""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        raw = path.read_bytes()
        if len(raw) > _MAX_READ_SIZE:
            logger.warning("Skipping oversized file: %s", path)
            return None
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in %s", path)
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Failed to read %s: %s", path, exc)
        return None


def _read_text(path: Path) -> Optional[str]:
    """Read a text file, returning ``None`` on any error."""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        raw = path.read_bytes()
        if len(raw) > _MAX_READ_SIZE:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Path security
# ---------------------------------------------------------------------------


def _validate_install_path(install_path: Path) -> Optional[Path]:
    """Validate that *install_path* resolves to within the plugin cache.

    Returns the resolved path, or ``None`` if the path is invalid, a
    symlink, or escapes the expected ``~/.claude/plugins/cache/`` tree.
    """
    root = _plugins_root()
    if root is None:
        return None
    cache_dir = root / "cache"
    try:
        resolved = install_path.resolve()
        resolved.relative_to(cache_dir.resolve())
    except (ValueError, OSError):
        logger.warning("Plugin install_path outside cache: %s", install_path)
        return None
    if install_path.is_symlink():
        return None
    # Reject a symlinked PARENT component: ``resolve()`` follows all symlinks,
    # so a symlinked intermediate directory could still pass the containment
    # check above.  Walk each component from the leaf up to (but not including)
    # ``cache_dir`` and reject if any is a symlink.
    try:
        cache_anchor = cache_dir.resolve()
    except OSError:
        return None
    for parent in install_path.parents:
        try:
            if parent.resolve() == cache_anchor:
                break
        except OSError:
            return None
        if parent.is_symlink():
            logger.warning("Plugin install_path has symlinked parent: %s", parent)
            return None
    return resolved if resolved.is_dir() else None


def _marketplace_anchors() -> List[Path]:
    """Directories a marketplace ``installLocation`` may legitimately live under.

    ``claude plugin marketplace add`` stores the exact path it was handed:
    ``~/.claude/plugins/marketplaces/<name>`` when added by a remote shorthand,
    or the clone-root path when added from a local clone — which is how BOTH this
    gateway's admin ``add_marketplace`` and the startup installer register remote
    repos (clone to the clone root, then add that local path). So both roots are
    valid anchors. The clone root mirrors
    ``plugin_admin_service.clone_root()`` / ``docker/install_plugins.py``.
    """
    anchors: List[Path] = []
    root = _plugins_root()
    if root is not None:
        anchors.append(root / "marketplaces")
    configured = os.environ.get("CLAUDE_PLUGIN_CLONE_ROOT", "").strip()
    if configured:
        anchors.append(Path(configured))
    else:
        home = os.environ.get("HOME", "").strip() or str(Path.home())
        anchors.append(Path(home) / ".claude" / "plugin-marketplaces")
    return anchors


def _validate_marketplace_path(install_location: Path) -> Optional[Path]:
    """Validate *install_location* resolves within an allowed marketplace root.

    Allowed roots are ``~/.claude/plugins/marketplaces`` and the clone root
    (see :func:`_marketplace_anchors`). Returns the resolved path, or ``None`` if
    the path is a symlink (leaf or parent), escapes every allowed root, or does
    not exist as a directory.
    """
    try:
        resolved = install_location.resolve()
    except OSError:
        return None

    anchor: Optional[Path] = None
    for candidate in _marketplace_anchors():
        try:
            resolved.relative_to(candidate.resolve())
            anchor = candidate
            break
        except (ValueError, OSError):
            continue
    if anchor is None:
        logger.warning(
            "Marketplace installLocation outside allowed roots: %s",
            install_location,
        )
        return None

    if install_location.is_symlink():
        return None
    # Reject a symlinked PARENT component (see _validate_install_path).
    try:
        anchor_resolved = anchor.resolve()
    except OSError:
        return None
    for parent in install_location.parents:
        try:
            if parent.resolve() == anchor_resolved:
                break
        except OSError:
            return None
        if parent.is_symlink():
            logger.warning(
                "Marketplace installLocation has symlinked parent: %s", parent
            )
            return None
    return resolved if resolved.is_dir() else None


# ---------------------------------------------------------------------------
# Installed plugins registry
# ---------------------------------------------------------------------------


def _load_installed_registry() -> Dict[str, Any]:
    """Parse ``installed_plugins.json`` and return the registry dict."""
    root = _plugins_root()
    if root is None:
        return {}
    data = _read_json(root / "installed_plugins.json")
    if not isinstance(data, dict):
        return {}
    return data


def _managed_plugin_ids() -> set:
    """Return the set of admin-managed ``(spec, scope)`` keys.

    ``spec`` is ``name@marketplace``; scope is included because the same plugin
    can be managed at one scope and env-installed at another. Sourced from
    :mod:`src.plugin_manifest`'s ``list_added``. Imported lazily so a manifest
    import/load failure never breaks plugin listing; returns an empty set on any
    error (callers then default origin to ``"env"``).
    """
    try:
        from src import plugin_manifest

        return {
            (
                plugin_manifest.spec_for(r.get("name", ""), r.get("marketplace", "")),
                r.get("scope", "user"),
            )
            for r in plugin_manifest.list_added()
            if isinstance(r, dict)
        }
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to load managed plugin manifest", exc_info=True)
        return set()


def _load_manifest(install_path: Path) -> Dict[str, Any]:
    """Load ``.claude-plugin/plugin.json`` manifest from a plugin cache dir."""
    manifest = _read_json(install_path / ".claude-plugin" / "plugin.json")
    return manifest if isinstance(manifest, dict) else {}


# Default file a plugin's MCP servers live in when the manifest points nowhere
# else (Claude Code convention; see plugin.json ``mcpServers`` default).
_MCP_DEFAULT_FILE = ".mcp.json"


def _safe_plugin_join(base: Path, rel: str) -> Optional[Path]:
    """Resolve *rel* under *base*, refusing to escape it (or follow a symlink).

    ``mcpServers`` may name a file path; treat it as relative to the plugin
    root, never absolute, and reject any ``..`` that climbs out of the install
    tree. Returns the resolved path or ``None``.
    """
    rel = (rel or "").strip().lstrip("/")
    if rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return None
    candidate = base / rel
    try:
        resolved = candidate.resolve()
        resolved.relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    if candidate.is_symlink():
        return None
    return resolved


def _read_mcp_json(path: Path) -> Dict[str, Any]:
    """Read an ``.mcp.json``-style file into ``{server_name: config}``.

    Tolerates both observed shapes: a top-level ``mcpServers`` wrapper
    (``{"mcpServers": {name: cfg}}``) and a bare server map (``{name: cfg}``).
    Never raises; returns ``{}`` on any problem.
    """
    data = _read_json(path)
    if not isinstance(data, dict):
        return {}
    inner = data.get("mcpServers", data)
    if not isinstance(inner, dict):
        return {}
    return {k: v for k, v in inner.items() if isinstance(v, dict)}


def _plugin_mcp_servers(
    install_path: Optional[Path], manifest: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Return a plugin's declared MCP servers as ``{server_name: config}``.

    Resolves the ``mcpServers`` manifest field per the Claude Code plugin
    schema: an inline object is used directly; a string is a file path
    (relative to the plugin root); when absent the default ``./.mcp.json`` is
    tried. Path-safe and non-raising — malformed or missing config yields ``{}``.
    """
    if install_path is None:
        return {}
    field = manifest.get("mcpServers") if isinstance(manifest, dict) else None
    if isinstance(field, dict):
        # Inline definition in plugin.json — already a {name: cfg} map.
        return {k: v for k, v in field.items() if isinstance(v, dict)}
    rel = field if isinstance(field, str) and field.strip() else _MCP_DEFAULT_FILE
    target = _safe_plugin_join(install_path, rel)
    if target is None:
        return {}
    return _read_mcp_json(target)


def _discover_skills(install_path: Path) -> List[Dict[str, str]]:
    """Discover skill files inside a plugin's install directory.

    Handles two layout conventions:
    1. ``.claude/skills/*.md`` (flat — e.g. octo plugin)
    2. ``skills/*/SKILL.md`` (nested — e.g. telegram, codex plugins)
    """
    results: List[Dict[str, str]] = []

    # Layout 1: .claude/skills/*.md
    flat_dir = install_path / ".claude" / "skills"
    if flat_dir.is_dir() and not flat_dir.is_symlink():
        for f in sorted(flat_dir.iterdir()):
            if f.is_file() and f.suffix == ".md" and not f.is_symlink():
                results.append(
                    {
                        "name": f.stem,
                        "path": str(f.relative_to(install_path)),
                    }
                )

    # Layout 2: skills/*/SKILL.md
    nested_dir = install_path / "skills"
    if nested_dir.is_dir() and not nested_dir.is_symlink():
        for child in sorted(nested_dir.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            skill_file = child / "SKILL.md"
            if skill_file.is_file() and not skill_file.is_symlink():
                results.append(
                    {
                        "name": child.name,
                        "path": str(skill_file.relative_to(install_path)),
                    }
                )

    return results


def _discover_commands(install_path: Path) -> List[Dict[str, str]]:
    """Discover command files inside ``.claude/commands/*.md``."""
    results: List[Dict[str, str]] = []
    cmd_dir = install_path / ".claude" / "commands"
    if not cmd_dir.is_dir() or cmd_dir.is_symlink():
        return results
    for f in sorted(cmd_dir.iterdir()):
        if f.is_file() and f.suffix == ".md" and not f.is_symlink():
            results.append(
                {
                    "name": f.stem,
                    "path": str(f.relative_to(install_path)),
                }
            )
    return results


def _discover_agents(install_path: Path) -> List[Dict[str, str]]:
    """Discover bundled agent definitions without exposing file contents."""
    results: List[Dict[str, str]] = []

    def add_agent_file(path: Path, name: str) -> None:
        if not path.is_file() or path.is_symlink() or path.suffix != ".md":
            return
        results.append({"name": name, "path": str(path.relative_to(install_path))})

    flat_dir = install_path / ".claude" / "agents"
    if flat_dir.is_dir() and not flat_dir.is_symlink():
        for f in sorted(flat_dir.iterdir()):
            add_agent_file(f, f.stem)

    portable_dir = install_path / "agents"
    if portable_dir.is_dir() and not portable_dir.is_symlink():
        for child in sorted(portable_dir.iterdir()):
            if child.is_file():
                add_agent_file(child, child.stem)
            elif child.is_dir() and not child.is_symlink():
                for filename in ("AGENT.md", "agent.md"):
                    agent_file = child / filename
                    if agent_file.is_file() and not agent_file.is_symlink():
                        add_agent_file(agent_file, child.name)
                        break

    return results


def _mcp_server_type(config: Dict[str, Any]) -> str:
    raw_type = str(config.get("type") or "").strip()
    if raw_type:
        return raw_type
    if config.get("command"):
        return "stdio"
    if config.get("url"):
        return "http"
    return "unknown"


def _plugin_mcp_server_summaries(
    install_path: Optional[Path], manifest: Dict[str, Any]
) -> List[Dict[str, str]]:
    """Return safe display metadata for plugin MCP servers."""
    servers = _plugin_mcp_servers(install_path, manifest)
    return [
        {"name": name, "type": _mcp_server_type(config)}
        for name, config in sorted(servers.items())
    ]


def _parse_plugin_id(plugin_key: str) -> Tuple[str, str]:
    """Split ``name@marketplace`` into ``(name, marketplace)``."""
    if "@" in plugin_key:
        name, marketplace = plugin_key.rsplit("@", 1)
        return name, marketplace
    return plugin_key, "unknown"


def _entry_index_for_scope(entries: list, scope: Optional[str]) -> int:
    """Index of the registry entry matching *scope*.

    The registry stores one entry per scope under a plugin id. With no *scope*,
    returns the first entry (``0``). With an explicit *scope*, returns the
    matching entry's index, or ``-1`` when no entry has that scope — so a
    scope-specific request 404s instead of silently reading another scope's
    install path/content.
    """
    if not scope:
        return 0
    if isinstance(entries, list):
        for i, e in enumerate(entries):
            if isinstance(e, dict) and e.get("scope") == scope:
                return i
    return -1


def _resolve_plugin_entry(
    key: str, entries: list, entry_index: int = 0
) -> Optional[Dict[str, Any]]:
    """Resolve and validate a single plugin registry entry.

    The registry stores one entry per scope under a plugin id; *entry_index*
    selects which (default the first). Returns a dict with common fields or
    ``None`` if invalid. Shared by :func:`list_plugins` and
    :func:`get_plugin_detail`.
    """
    if not isinstance(entries, list) or not entries:
        return None
    if entry_index < 0 or entry_index >= len(entries):
        return None
    entry = entries[entry_index]
    name, marketplace = _parse_plugin_id(key)
    raw_path = Path(entry.get("installPath", ""))
    install_path = _validate_install_path(raw_path)

    manifest = _load_manifest(install_path) if install_path else {}
    skills = _discover_skills(install_path) if install_path else []
    agents = _discover_agents(install_path) if install_path else []
    commands = _discover_commands(install_path) if install_path else []
    mcp_servers = _plugin_mcp_server_summaries(install_path, manifest)

    return {
        "id": key,
        "name": manifest.get("name", name),
        "marketplace": marketplace,
        "version": entry.get("version", manifest.get("version", "")),
        "description": manifest.get("description", ""),
        "author": manifest.get("author", {}),
        "scope": entry.get("scope", "user"),
        "installed_at": entry.get("installedAt"),
        "last_updated": entry.get("lastUpdated"),
        "git_commit_sha": entry.get("gitCommitSha"),
        "install_path": install_path,
        "manifest": manifest,
        "skills": skills,
        "agents": agents,
        "commands": commands,
        "mcp_servers": mcp_servers,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_plugins() -> List[Dict[str, Any]]:
    """Return metadata for all installed plugins."""
    registry = _load_installed_registry()
    plugins_data = registry.get("plugins", {})
    if not isinstance(plugins_data, dict):
        return []

    managed = _managed_plugin_ids()
    results: List[Dict[str, Any]] = []
    for key, entries in plugins_data.items():
        # claude stores one entry per scope under a plugin id; emit one row each
        # so multi-scope installs are visible and removable at their real scope.
        if not isinstance(entries, list):
            continue
        for idx in range(len(entries)):
            resolved = _resolve_plugin_entry(key, entries, idx)
            if resolved is None:
                continue
            results.append(
                {
                    "id": resolved["id"],
                    "name": resolved["name"],
                    "marketplace": resolved["marketplace"],
                    "version": resolved["version"],
                    "description": resolved["description"],
                    "author": resolved["author"],
                    "scope": resolved["scope"],
                    "installed_at": resolved["installed_at"],
                    "last_updated": resolved["last_updated"],
                    "origin": (
                        "managed"
                        if (resolved["id"], resolved["scope"]) in managed
                        else "env"
                    ),
                    "skills": resolved["skills"],
                    "agents": resolved["agents"],
                    "mcp_servers": resolved["mcp_servers"],
                    "skill_count": len(resolved["skills"]),
                    "agent_count": len(resolved["agents"]),
                    "command_count": len(resolved["commands"]),
                    "mcp_count": len(resolved["mcp_servers"]),
                }
            )

    return results


def list_plugin_mcp_servers() -> List[Dict[str, Any]]:
    """Return MCP servers contributed by installed plugins.

    Each item is ``{plugin_id, plugin_name, marketplace, scope, origin,
    server_name, config, install_path}``. These servers are loaded by the Claude SDK via
    ``setting_sources`` (reading the plugin's ``.mcp.json``) and never pass
    through the gateway's ``MCP_CONFIG``/manifest effective config — so they
    are otherwise invisible to the MCP admin view. Read-only; never raises.
    """
    registry = _load_installed_registry()
    plugins_data = registry.get("plugins", {})
    if not isinstance(plugins_data, dict):
        return []

    managed = _managed_plugin_ids()
    results: List[Dict[str, Any]] = []
    for key, entries in plugins_data.items():
        if not isinstance(entries, list):
            continue
        for idx in range(len(entries)):
            resolved = _resolve_plugin_entry(key, entries, idx)
            if resolved is None:
                continue
            servers = _plugin_mcp_servers(
                resolved["install_path"], resolved["manifest"]
            )
            for server_name, config in servers.items():
                results.append(
                    {
                        "plugin_id": resolved["id"],
                        "plugin_name": resolved["name"],
                        "marketplace": resolved["marketplace"],
                        "scope": resolved["scope"],
                        "origin": (
                            "managed"
                            if (resolved["id"], resolved["scope"]) in managed
                            else "env"
                        ),
                        "server_name": server_name,
                        "config": config,
                        "install_path": str(resolved["install_path"]),
                    }
                )
    return results


def get_plugin_mcp_server_config(server_name: str) -> Optional[Dict[str, Any]]:
    """Return the config of the first plugin MCP server named *server_name*.

    Used by the admin connection-test to probe a plugin server (which is not in
    the gateway's effective config). ``None`` when no plugin declares it.
    """
    for entry in list_plugin_mcp_servers():
        if entry["server_name"] == server_name:
            cfg = entry.get("config")
            return cfg if isinstance(cfg, dict) else None
    return None


def get_plugin_detail(
    plugin_id: str, scope: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return full detail for a single installed plugin.

    *plugin_id* is the registry key (e.g. ``octo@nyldn-plugins``); *scope*
    selects which per-scope registry entry to read (defaults to the first).
    Returns ``None`` if the plugin is not found.
    """
    registry = _load_installed_registry()
    plugins_data = registry.get("plugins", {})
    entries = plugins_data.get(plugin_id)
    resolved = _resolve_plugin_entry(
        plugin_id, entries, _entry_index_for_scope(entries, scope)
    )
    if resolved is None:
        return None

    install_path = resolved["install_path"]
    manifest = resolved["manifest"]

    # Hooks — check presence only, don't expose raw content
    has_hooks = False
    if install_path:
        for hp in (
            install_path / ".claude-plugin" / "hooks.json",
            install_path / "hooks" / "hooks.json",
        ):
            if _read_json(hp) is not None:
                has_hooks = True
                break

    # Settings — check presence only
    has_settings = False
    if install_path:
        for sp in (
            install_path / ".claude-plugin" / "settings.json",
            install_path / ".claude" / "settings.json",
        ):
            if _read_json(sp) is not None:
                has_settings = True
                break

    return {
        "id": resolved["id"],
        "name": resolved["name"],
        "marketplace": resolved["marketplace"],
        "version": resolved["version"],
        "description": resolved["description"],
        "author": resolved["author"],
        "license": manifest.get("license"),
        "repository": manifest.get("repository"),
        "homepage": manifest.get("homepage"),
        "keywords": manifest.get("keywords", []),
        "scope": resolved["scope"],
        "installed_at": resolved["installed_at"],
        "last_updated": resolved["last_updated"],
        "git_commit_sha": resolved["git_commit_sha"],
        "origin": (
            "managed"
            if (resolved["id"], resolved["scope"]) in _managed_plugin_ids()
            else "env"
        ),
        "skills": resolved["skills"],
        "agents": resolved["agents"],
        "commands": resolved["commands"],
        "mcp_servers": resolved["mcp_servers"],
        "has_hooks": has_hooks,
        "has_settings": has_settings,
        "manifest": {k: v for k, v in manifest.items() if k in _SAFE_MANIFEST_KEYS},
    }


def get_plugin_skill_content(
    plugin_id: str, skill_name: str, scope: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Read the content of a specific skill from a plugin.

    *scope* selects which per-scope registry entry to read from (the install
    path can differ by scope). Returns ``None`` if plugin or skill not found.
    """
    detail = get_plugin_detail(plugin_id, scope)
    if detail is None:
        return None

    # Re-resolve install_path through validation
    registry = _load_installed_registry()
    entries = registry.get("plugins", {}).get(plugin_id)
    resolved = _resolve_plugin_entry(
        plugin_id, entries, _entry_index_for_scope(entries, scope)
    )
    if resolved is None or resolved["install_path"] is None:
        return None
    install_path = resolved["install_path"]

    # Find the matching skill entry
    matching = [s for s in detail["skills"] if s["name"] == skill_name]
    if not matching:
        return None

    skill_path = install_path / matching[0]["path"]
    content = _read_text(skill_path)
    if content is None:
        return None

    return {
        "plugin_id": plugin_id,
        "skill_name": skill_name,
        "path": matching[0]["path"],
        "content": content,
        "size": len(content.encode("utf-8")),
    }


def _marketplace_records() -> Dict[str, Any]:
    """Return the manifest's marketplace-name -> record map (``{}`` on failure).

    The admin-managed manifest is the only source of truth for a marketplace's
    scope — ``known_marketplaces.json`` does not store it.
    """
    try:
        from src import plugin_manifest

        records = plugin_manifest.list_marketplace_records()
        return records if isinstance(records, dict) else {}
    except Exception:
        logger.debug("Failed to read manifest marketplace records", exc_info=True)
        return {}


def _marketplace_scope(marketplace_name: str, records: Optional[Dict] = None) -> str:
    """Scope a marketplace was added at, defaulting to user.

    The admin manifest is the only metadata source; a marketplace registered
    outside the gateway (no manifest record) is assumed ``user``.
    """
    if records is None:
        records = _marketplace_records()
    record = records.get(marketplace_name)
    if isinstance(record, dict) and record.get("scope"):
        return record["scope"]
    return "user"


def _marketplace_record(
    marketplace_name: str, records: Optional[Dict] = None
) -> Dict[str, str]:
    """Return manifest metadata for a marketplace, normalized to strings."""
    if records is None:
        records = _marketplace_records()
    record = records.get(marketplace_name)
    if not isinstance(record, dict):
        return {}
    return {
        "repo": str(record.get("repo") or ""),
        "branch": str(record.get("branch") or ""),
        "scope": str(record.get("scope") or ""),
    }


def _source_repo(source: Dict[str, Any]) -> str:
    """Repo/url/path encoded in ``known_marketplaces.json`` source metadata."""
    for key in ("repo", "url", "path"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _strip_url_credentials(repo: str) -> str:
    """Remove embedded credentials from an http(s) repo URL before returning it.

    ``known_marketplaces.json`` may hold a ``https://user:token@host/...`` source
    (e.g. an externally-run ``claude plugin marketplace add`` with a credential
    URL). The marketplace listing/catalog API surfaces this ``repo`` field, so the
    userinfo must be stripped — a secret must never appear in an API response.
    Non-http(s) values (ssh, scp-like ``git@host:org/repo``, local paths) are
    returned unchanged; their userinfo is the ssh login, not a secret.
    """
    repo = (repo or "").strip()
    scheme = repo.split("://", 1)[0].lower() if "://" in repo else ""
    if scheme not in ("http", "https"):
        return repo
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(repo)
    except ValueError:
        return repo
    if not (parts.username or parts.password):
        return repo
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def list_marketplaces() -> List[Dict[str, Any]]:
    """Return registered marketplace sources."""
    root = _plugins_root()
    if root is None:
        return []
    data = _read_json(root / "known_marketplaces.json")
    if not isinstance(data, dict):
        return []

    records = _marketplace_records()
    results: List[Dict[str, Any]] = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        source = info.get("source", {})
        source = source if isinstance(source, dict) else {}
        record = _marketplace_record(name, records)
        scope = _marketplace_scope(name, records)
        repo = record.get("repo") or _source_repo(source)
        results.append(
            {
                "name": name,
                "source_type": source.get("source", "unknown"),
                "repo": _strip_url_credentials(repo),
                "branch": record.get("branch") or "main",
                "last_updated": info.get("lastUpdated"),
                "scope": scope,
            }
        )
    return results


def _count_catalog_skills(location: Path, source: Any) -> int:
    """Best-effort skill count for a not-yet-installed catalog plugin.

    ``marketplace.json`` entries rarely declare ``skills`` explicitly, so when a
    plugin lives in a local directory under the marketplace checkout, resolve its
    ``source`` path and scan it the same way installed plugins are scanned (see
    :func:`_discover_skills`). Returns 0 for remote/object sources or any path
    that escapes the marketplace directory.
    """
    if not isinstance(source, str) or not source or "://" in source:
        return 0
    try:
        base = location.resolve()
        plugin_dir = (location / source).resolve()
        if plugin_dir != base and base not in plugin_dir.parents:
            return 0
        if not plugin_dir.is_dir():
            return 0
        return len(_discover_skills(plugin_dir))
    except Exception:
        return 0


def list_marketplace_plugins(marketplace_name: str) -> List[Dict[str, Any]]:
    """Return the plugins a marketplace offers in its catalog.

    Resolves ``installLocation`` from ``known_marketplaces.json``, validates
    it, and parses ``<installLocation>/.claude-plugin/marketplace.json``.
    ``installed`` is judged at the marketplace's OWN scope (the scope a catalog
    install would target), so a plugin present only at another scope still shows
    as available; ``scope`` echoes that marketplace scope. Tolerates a missing
    marketplace, missing/corrupt catalog, or bad paths by returning ``[]`` —
    never raises.
    """
    root = _plugins_root()
    if root is None:
        return []
    known = _read_json(root / "known_marketplaces.json")
    if not isinstance(known, dict):
        return []
    info = known.get(marketplace_name)
    if not isinstance(info, dict):
        return []
    raw_location = info.get("installLocation")
    if not isinstance(raw_location, str) or not raw_location:
        return []
    location = _validate_marketplace_path(Path(raw_location))
    if location is None:
        return []

    catalog = _read_json(location / ".claude-plugin" / "marketplace.json")
    if not isinstance(catalog, dict):
        return []
    plugins = catalog.get("plugins")
    if not isinstance(plugins, list):
        return []

    # Availability is judged at the marketplace's own scope (the scope a catalog
    # install targets), since scope is part of a plugin's identity.
    mkt_scope = _marketplace_scope(marketplace_name)
    installed_scoped = set()
    registry = _load_installed_registry()
    plugins_data = registry.get("plugins", {})
    if isinstance(plugins_data, dict):
        for key, entries in plugins_data.items():
            if isinstance(entries, list):
                for e in entries:
                    if isinstance(e, dict):
                        installed_scoped.add((key, e.get("scope", "user")))

    results: List[Dict[str, Any]] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        version = entry.get("version")
        skills = entry.get("skills")
        if isinstance(skills, list):
            skill_count = len(skills)
        else:
            # marketplace.json omitted an explicit skills list — scan the
            # plugin's local source dir so the catalog matches the installed view.
            skill_count = _count_catalog_skills(location, entry.get("source"))
        plugin_id = f"{name}@{marketplace_name}"
        results.append(
            {
                "name": name,
                "description": entry.get("description", ""),
                "version": version if isinstance(version, str) else "",
                "skill_count": skill_count,
                "id": plugin_id,
                "installed": (plugin_id, mkt_scope) in installed_scoped,
                "scope": mkt_scope,
            }
        )
    return results


def get_marketplaces_with_plugins() -> List[Dict[str, Any]]:
    """Return each marketplace augmented with its catalog plugins.

    Convenience aggregate the admin UI can render in one call: every
    :func:`list_marketplaces` entry gets a ``plugins`` list (from
    :func:`list_marketplace_plugins`) and a ``plugin_count``.
    """
    results: List[Dict[str, Any]] = []
    for mkt in list_marketplaces():
        plugins = list_marketplace_plugins(mkt["name"])
        results.append({**mkt, "plugins": plugins, "plugin_count": len(plugins)})
    return results


def get_plugin_blocklist() -> List[Dict[str, Any]]:
    """Return the plugin blocklist."""
    root = _plugins_root()
    if root is None:
        return []
    data = _read_json(root / "blocklist.json")
    if not isinstance(data, dict):
        return []
    plugins = data.get("plugins", [])
    if not isinstance(plugins, list):
        return []

    results: List[Dict[str, Any]] = []
    for entry in plugins:
        if not isinstance(entry, dict):
            continue
        results.append(
            {
                "plugin": entry.get("plugin", ""),
                "reason": entry.get("reason", ""),
                "text": entry.get("text", ""),
                "added_at": entry.get("added_at"),
            }
        )
    return results
