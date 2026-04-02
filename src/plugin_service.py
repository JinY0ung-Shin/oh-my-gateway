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
  - ``.claude/commands/*.md`` — plugin commands
  - ``.claude-plugin/hooks.json`` — hook definitions
  - ``.claude-plugin/settings.json`` — plugin settings
"""

import json
import logging
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
    """Return ``~/.claude/plugins`` if it exists."""
    p = Path.home() / ".claude" / "plugins"
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


def _load_manifest(install_path: Path) -> Dict[str, Any]:
    """Load ``.claude-plugin/plugin.json`` manifest from a plugin cache dir."""
    manifest = _read_json(install_path / ".claude-plugin" / "plugin.json")
    return manifest if isinstance(manifest, dict) else {}


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


def _parse_plugin_id(plugin_key: str) -> Tuple[str, str]:
    """Split ``name@marketplace`` into ``(name, marketplace)``."""
    if "@" in plugin_key:
        name, marketplace = plugin_key.rsplit("@", 1)
        return name, marketplace
    return plugin_key, "unknown"


def _resolve_plugin_entry(key: str, entries: list) -> Optional[Dict[str, Any]]:
    """Resolve and validate a single plugin registry entry.

    Returns a dict with common fields or ``None`` if invalid.
    Shared by :func:`list_plugins` and :func:`get_plugin_detail`.
    """
    if not isinstance(entries, list) or not entries:
        return None
    entry = entries[0]
    name, marketplace = _parse_plugin_id(key)
    raw_path = Path(entry.get("installPath", ""))
    install_path = _validate_install_path(raw_path)

    manifest = _load_manifest(install_path) if install_path else {}
    skills = _discover_skills(install_path) if install_path else []
    commands = _discover_commands(install_path) if install_path else []

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
        "commands": commands,
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

    results: List[Dict[str, Any]] = []
    for key, entries in plugins_data.items():
        resolved = _resolve_plugin_entry(key, entries)
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
                "skills": resolved["skills"],
                "skill_count": len(resolved["skills"]),
                "command_count": len(resolved["commands"]),
            }
        )

    return results


def get_plugin_detail(plugin_id: str) -> Optional[Dict[str, Any]]:
    """Return full detail for a single installed plugin.

    *plugin_id* is the registry key (e.g. ``octo@nyldn-plugins``).
    Returns ``None`` if the plugin is not found.
    """
    registry = _load_installed_registry()
    plugins_data = registry.get("plugins", {})
    entries = plugins_data.get(plugin_id)
    resolved = _resolve_plugin_entry(plugin_id, entries)
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
        "skills": resolved["skills"],
        "commands": resolved["commands"],
        "has_hooks": has_hooks,
        "has_settings": has_settings,
        "manifest": {k: v for k, v in manifest.items() if k in _SAFE_MANIFEST_KEYS},
    }


def get_plugin_skill_content(plugin_id: str, skill_name: str) -> Optional[Dict[str, Any]]:
    """Read the content of a specific skill from a plugin.

    Returns ``None`` if plugin or skill not found.
    """
    detail = get_plugin_detail(plugin_id)
    if detail is None:
        return None

    # Re-resolve install_path through validation
    registry = _load_installed_registry()
    entries = registry.get("plugins", {}).get(plugin_id)
    resolved = _resolve_plugin_entry(plugin_id, entries)
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


def list_marketplaces() -> List[Dict[str, Any]]:
    """Return registered marketplace sources."""
    root = _plugins_root()
    if root is None:
        return []
    data = _read_json(root / "known_marketplaces.json")
    if not isinstance(data, dict):
        return []

    results: List[Dict[str, Any]] = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        source = info.get("source", {})
        results.append(
            {
                "name": name,
                "source_type": source.get("source", "unknown"),
                "repo": source.get("repo", source.get("url", "")),
                "last_updated": info.get("lastUpdated"),
            }
        )
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
