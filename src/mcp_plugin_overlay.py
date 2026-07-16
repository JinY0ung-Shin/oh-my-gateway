"""Admin-managed env/headers overlays for plugin-provided MCP servers.

Plugin MCP definitions live in each plugin's ``.mcp.json`` and are loaded by
the Claude CLI via ``setting_sources``. They are not part of the gateway
``MCP_CONFIG``/manifest effective set, so the main Admin MCP CRUD cannot edit
them.

This module stores a **credentials-only overlay** (``env`` + ``headers``) keyed
by MCP server name. On new Claude sessions the gateway:

1. Reads the plugin's base server config
2. Merges the overlay (overlay wins on key collision)
3. Materializes the result into ``options.mcp_servers`` (same path as gateway MCP)
4. Also injects resolved overlay ``env`` into ``ClaudeAgentOptions.env`` so
   stdio children that inherit the CLI process environment still see the values

Hot-reload: overlay mutations rebind the in-memory map; already-running sessions
keep the MCP set pinned at create time (same model as the main MCP manifest).

File: ``GATEWAY_MCP_PLUGIN_OVERLAY`` or ``data/gateway-mcp-plugin-overlay.json``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

_lock = RLock()

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_PATH = _DATA_DIR / "gateway-mcp-plugin-overlay.json"

# In-memory singleton (rebound on save, never mutated in place).
_overlays: Dict[str, Dict[str, Any]] = {}


def overlay_path() -> Path:
    override = os.getenv("GATEWAY_MCP_PLUGIN_OVERLAY")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def _defaults() -> dict:
    return {"version": 1, "overlays": {}}


def _normalize_string_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k.strip():
            continue
        if not isinstance(v, str):
            continue
        out[k] = v
    return out


def _normalize_overlay_entry(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    entry: Dict[str, Any] = {}
    env = _normalize_string_map(raw.get("env"))
    headers = _normalize_string_map(raw.get("headers"))
    if env:
        entry["env"] = env
    if headers:
        entry["headers"] = headers
    plugin_id = raw.get("plugin_id")
    if isinstance(plugin_id, str) and plugin_id.strip():
        entry["plugin_id"] = plugin_id.strip()
    # Empty overlay (no env, no headers) is useless — treat as absent.
    if "env" not in entry and "headers" not in entry:
        return None
    return entry


def load() -> dict:
    """Return a well-formed overlay file; corrupt/missing -> defaults, never raises."""
    path = overlay_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _defaults()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load plugin MCP overlay, using defaults: %s", e)
        return _defaults()
    if not isinstance(data, dict):
        return _defaults()
    raw_overlays = data.get("overlays")
    if not isinstance(raw_overlays, dict):
        raw_overlays = {}
    overlays: Dict[str, Dict[str, Any]] = {}
    for name, rec in raw_overlays.items():
        if not isinstance(name, str) or not name.strip():
            continue
        entry = _normalize_overlay_entry(rec)
        if entry is not None:
            overlays[name] = entry
    version = data.get("version")
    if not isinstance(version, int):
        version = 1
    return {"version": version, "overlays": overlays}


def save(data: dict) -> None:
    """Atomically persist the overlay file."""
    path = overlay_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def reload_overlays() -> Dict[str, Dict[str, Any]]:
    """Re-read disk and rebind the singleton. Returns the new map."""
    global _overlays
    data = load()
    _overlays = dict(data.get("overlays") or {})
    return _overlays


def get_overlays() -> Dict[str, Dict[str, Any]]:
    """Return the in-memory overlay map (do not mutate)."""
    return _overlays


def get_overlay(server_name: str) -> Dict[str, Any]:
    """Return a copy of the overlay for *server_name*, or ``{}``."""
    rec = _overlays.get(server_name)
    return copy.deepcopy(rec) if isinstance(rec, dict) else {}


def list_overlay_names() -> List[str]:
    return sorted(_overlays.keys())


def upsert_overlay(
    server_name: str,
    *,
    env: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, Any]] = None,
    plugin_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or replace an overlay entry, then hot-reload.

    Passing empty env and empty headers deletes the overlay (same as delete).
    """
    name = (server_name or "").strip()
    if not name:
        raise ValueError("server name is required")

    entry_raw: Dict[str, Any] = {
        "env": dict(env or {}),
        "headers": dict(headers or {}),
    }
    if plugin_id:
        entry_raw["plugin_id"] = plugin_id
    entry = _normalize_overlay_entry(entry_raw)

    with _lock:
        data = load()
        if entry is None:
            data["overlays"].pop(name, None)
        else:
            data["overlays"][name] = entry
        save(data)
    reload_overlays()
    return get_overlay(name)


def delete_overlay(server_name: str) -> bool:
    """Remove an overlay. Returns whether it existed."""
    name = (server_name or "").strip()
    with _lock:
        data = load()
        existed = name in data.get("overlays", {})
        data.get("overlays", {}).pop(name, None)
        save(data)
    reload_overlays()
    return existed


def merge_overlay_into_config(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> Dict[str, Any]:
    """Deep-copy *base* and merge overlay env/headers (overlay keys win)."""
    merged = copy.deepcopy(dict(base)) if isinstance(base, Mapping) else {}
    if not isinstance(overlay, Mapping):
        return merged

    for field in ("env", "headers"):
        base_map = merged.get(field) if isinstance(merged.get(field), dict) else {}
        over_map = overlay.get(field) if isinstance(overlay.get(field), dict) else {}
        if not base_map and not over_map:
            continue
        combined = dict(base_map)
        # Overlay wins; only string values from overlay (already normalized on save).
        for k, v in over_map.items():
            if isinstance(k, str) and isinstance(v, str):
                combined[k] = v
        if combined:
            merged[field] = combined
        elif field in merged:
            del merged[field]
    return merged


def materialize_overlaid_plugin_servers() -> Dict[str, Dict[str, Any]]:
    """Build ``{server_name: config}`` for plugin MCP servers that have overlays.

    Base config comes from installed plugins; overlay env/headers are merged in.
    Servers without a matching installed plugin are skipped (stale overlay).
    Never raises.
    """
    if not _overlays:
        return {}
    try:
        from src import plugin_service

        entries = plugin_service.list_plugin_mcp_servers()
    except Exception:
        logger.warning("Failed to list plugin MCP servers for overlay", exc_info=True)
        return {}

    # First installed plugin that declares the name wins (same as get_plugin_mcp_server_config).
    by_name: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        name = entry.get("server_name")
        cfg = entry.get("config")
        if not isinstance(name, str) or not isinstance(cfg, dict):
            continue
        if name not in by_name:
            by_name[name] = cfg

    out: Dict[str, Dict[str, Any]] = {}
    for name, overlay in _overlays.items():
        base = by_name.get(name)
        if base is None:
            logger.warning(
                "Plugin MCP overlay for %r has no matching installed plugin server; skipping",
                name,
            )
            continue
        out[name] = merge_overlay_into_config(base, overlay)
    return out


def collect_overlay_env_for_process(
    overlays: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Flatten all overlay ``env`` maps for ClaudeAgentOptions.env injection.

    Later servers overwrite earlier keys on collision (deterministic sorted names).
    Values are **not** resolved here — call :func:`resolve_string_map_env_refs`.
    """
    src = overlays if overlays is not None else _overlays
    flat: Dict[str, str] = {}
    for name in sorted(src.keys()):
        rec = src[name]
        if not isinstance(rec, Mapping):
            continue
        env = rec.get("env")
        if not isinstance(env, dict):
            continue
        for k, v in env.items():
            if isinstance(k, str) and isinstance(v, str):
                flat[k] = v
    return flat


# Load at import so get_overlays() works without an explicit reload.
reload_overlays()
