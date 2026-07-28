"""Per-server env/headers overlays for MCP servers, keyed by server name.

Plugin MCP definitions live in each plugin's ``.mcp.json`` and are loaded by
the Claude CLI via ``setting_sources``. They are not part of the gateway
``MCP_CONFIG``/manifest effective set, so the main Admin MCP CRUD cannot edit
them.

Two layers feed this module, both **credentials-only** (``env`` + ``headers``):

* ``GATEWAY_MCP_SERVER_ENV`` — deploy-time declaration in the gateway
  environment (inline JSON or a path to a JSON file, same dual form as
  ``MCP_CONFIG``). Read-only at runtime; admin CRUD never writes here.
* the admin overlay file — hot-editable from the admin panel.

The admin file wins **per key**, mirroring ``MCP_CONFIG`` (env base) overlaid by
the admin manifest, so an operator can ship defaults in the environment and
still hot-fix one key from the panel. The two maps stay separate in memory: an
admin save must never freeze an env-declared value into the overlay file.

On new Claude sessions the gateway materializes **every** installed plugin MCP
server into ``options.mcp_servers`` (base config, overlay merged where one
exists, ``${CLAUDE_PLUGIN_ROOT}`` expanded to the registry install path — the
CLI only defines that variable while loading plugin-scoped config, so the
materialized copy must be self-contained) and the Claude backend sets
``strict_mcp_config`` so the CLI loads MCP servers from ``--mcp-config`` only.
The plugin's own ``setting_sources`` registration never loads, so exactly one
copy of each server runs and overlay values stay scoped to that server config —
never injected into the session process environment.

History: the design used to materialize only overlaid servers and rely on the
CLI deregistering the plugin's same-named copy. That dedup turned out to be
conditional on the configs matching exactly (command/args; env/headers
differences are ignored), and the CLI resolves ``${CLAUDE_PLUGIN_ROOT}`` to the
**marketplace source/clone directory** while the registry ``installPath``
points at ``plugins/cache/...`` — different path strings, so the dedup never
fired for directory-source marketplaces and both copies ran (found live
2026-07-28; the two copies can even run different code, since clones
auto-refresh while the cache is pinned). ``strict_mcp_config`` removes the
dependence on both undocumented behaviors.

Per plugin server name, the first installed plugin that declares it wins. A
same-named gateway server (``MCP_CONFIG``/manifest) is overridden by the
materialized plugin copy when an overlay exists (credentials attach to the
plugin-defined command/url); without an overlay the gateway config wins — the
operator's explicit definition overrides the bundle.

An overlay whose name no plugin declares falls back to the gateway-declared
server of that name (``MCP_CONFIG``/manifest): the env/headers merge into that
config instead. A name neither side declares is stale and contributes nothing.

Hot-reload: overlay mutations rebind the in-memory map; already-running sessions
keep the MCP set pinned at create time (same model as the main MCP manifest).

File: ``GATEWAY_MCP_PLUGIN_OVERLAY`` or ``data/gateway-mcp-plugin-overlay.json``.
Env layer: ``GATEWAY_MCP_SERVER_ENV``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

_lock = RLock()

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_PATH = _DATA_DIR / "gateway-mcp-plugin-overlay.json"

# Deploy-time overlay layer declared in the gateway environment.
ENV_OVERLAY_VAR = "GATEWAY_MCP_SERVER_ENV"

# In-memory singletons (rebound on save/reload, never mutated in place).
# ``_overlays`` is the admin file map, ``_env_overlays`` the read-only env layer.
_overlays: Dict[str, Dict[str, Any]] = {}
_env_overlays: Dict[str, Dict[str, Any]] = {}


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


def env_overlay_source() -> str:
    """Raw ``GATEWAY_MCP_SERVER_ENV`` value (inline JSON or file path); ``""`` unset."""
    return (os.getenv(ENV_OVERLAY_VAR) or "").strip()


def load_env_overlays() -> Dict[str, Dict[str, Any]]:
    """Parse the ``GATEWAY_MCP_SERVER_ENV`` layer. Never raises.

    Accepts a path to a JSON file or an inline JSON object (``MCP_CONFIG``'s dual
    form), shaped ``{"<server>": {"env": {...}, "headers": {...}}}``. The overlay
    file's own ``{"version": 1, "overlays": {...}}`` wrapper is also accepted so
    one document can be mounted as a secret and pointed at by the env var.
    """
    source = env_overlay_source()
    if not source:
        return {}

    try:
        # A long inline JSON blob is not a valid path on every platform; treat any
        # stat failure as "not a file" and fall through to inline parsing.
        is_file = Path(source).is_file()
    except (OSError, ValueError):
        is_file = False

    try:
        raw = json.loads(
            Path(source).read_text(encoding="utf-8") if is_file else source
        )
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.error("Failed to parse %s, ignoring the layer: %s", ENV_OVERLAY_VAR, e)
        return {}

    if not isinstance(raw, dict):
        logger.error(
            "%s must be a JSON object of {server: {env, headers}}", ENV_OVERLAY_VAR
        )
        return {}
    inner = raw.get("overlays") if isinstance(raw.get("overlays"), dict) else raw

    out: Dict[str, Dict[str, Any]] = {}
    for name, rec in inner.items():
        if not isinstance(name, str) or not name.strip():
            continue
        entry = _normalize_overlay_entry(rec)
        if entry is None:
            logger.warning(
                "%s entry %r has no usable string env/headers values; skipping",
                ENV_OVERLAY_VAR,
                name,
            )
            continue
        out[name.strip()] = entry
    if out:
        logger.info(
            "Loaded %d MCP server credential overlay(s) from %s: %s",
            len(out),
            ENV_OVERLAY_VAR,
            sorted(out),
        )
    return out


def reload_overlays() -> Dict[str, Dict[str, Any]]:
    """Re-read disk + env layer and rebind the singletons. Returns the file map."""
    global _overlays, _env_overlays
    data = load()
    _overlays = dict(data.get("overlays") or {})
    _env_overlays = load_env_overlays()
    return _overlays


def get_overlays() -> Dict[str, Dict[str, Any]]:
    """Return the in-memory overlay map (do not mutate)."""
    return _overlays


def get_overlay(server_name: str) -> Dict[str, Any]:
    """Return a copy of the **stored (admin file)** overlay, or ``{}``.

    File-only on purpose: this is what the admin edit form round-trips, and an
    env-declared value must never be written back into the overlay file.
    """
    rec = _overlays.get(server_name)
    return copy.deepcopy(rec) if isinstance(rec, dict) else {}


def get_env_overlay(server_name: str) -> Dict[str, Any]:
    """Return a copy of the env-declared (read-only) overlay, or ``{}``."""
    rec = _env_overlays.get(server_name)
    return copy.deepcopy(rec) if isinstance(rec, dict) else {}


def list_overlay_names() -> List[str]:
    return sorted(_overlays.keys())


def list_env_overlay_names() -> List[str]:
    return sorted(_env_overlays.keys())


def _merge_entries(base: Mapping[str, Any], top: Mapping[str, Any]) -> Dict[str, Any]:
    """Per-key merge of two overlay entries; *top* wins on key collision."""
    merged: Dict[str, Any] = {}
    for field in ("env", "headers"):
        combined = dict(base.get(field) or {})
        combined.update(top.get(field) or {})
        if combined:
            merged[field] = combined
    plugin_id = top.get("plugin_id") or base.get("plugin_id")
    if isinstance(plugin_id, str) and plugin_id:
        merged["plugin_id"] = plugin_id
    return merged


def get_effective_overlays() -> Dict[str, Dict[str, Any]]:
    """Env layer as base, admin file layered on top (file wins per key).

    Same direction as ``MCP_CONFIG`` < admin manifest. Callers get fresh dicts.
    """
    if not _env_overlays:
        return {name: copy.deepcopy(rec) for name, rec in _overlays.items()}
    names = set(_env_overlays) | set(_overlays)
    return {
        name: _merge_entries(_env_overlays.get(name) or {}, _overlays.get(name) or {})
        for name in names
    }


def get_effective_overlay(server_name: str) -> Dict[str, Any]:
    """Effective (env + admin file) overlay for one server, or ``{}``."""
    env_rec = _env_overlays.get(server_name)
    file_rec = _overlays.get(server_name)
    if not env_rec:
        return copy.deepcopy(file_rec) if isinstance(file_rec, dict) else {}
    return _merge_entries(env_rec, file_rec or {})


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


_PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"


def _expand_plugin_root(value: Any, root: str) -> Any:
    """Replace ``${CLAUDE_PLUGIN_ROOT}`` in every string leaf of *value*.

    The CLI only defines that variable while loading a plugin's own config, so
    a config materialized into gateway ``mcp_servers`` must carry the resolved
    install path itself or its command/args would not resolve.
    """
    if isinstance(value, str):
        return value.replace(_PLUGIN_ROOT_TOKEN, root)
    if isinstance(value, dict):
        return {k: _expand_plugin_root(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_plugin_root(v, root) for v in value]
    return value


def _merged_expanded(
    base: Dict[str, Any], overlay: Mapping[str, Any], install_path: Any
) -> Dict[str, Any]:
    merged = merge_overlay_into_config(base, overlay)
    if isinstance(install_path, str) and install_path:
        merged = _expand_plugin_root(merged, install_path)
    return merged


def _plugin_entries_by_name() -> Dict[str, Dict[str, Any]]:
    """``{server_name: plugin entry}`` for installed plugins. Never raises.

    First installed plugin that declares a name wins (same as
    ``get_plugin_mcp_server_config``).
    """
    try:
        from src import plugin_service

        entries = plugin_service.list_plugin_mcp_servers()
    except Exception:
        logger.warning("Failed to list plugin MCP servers for overlay", exc_info=True)
        return {}

    by_name: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        name = entry.get("server_name")
        cfg = entry.get("config")
        if not isinstance(name, str) or not isinstance(cfg, dict):
            continue
        if name not in by_name:
            by_name[name] = entry
    return by_name


def materialize_overlaid_plugin_servers() -> Dict[str, Dict[str, Any]]:
    """Build ``{server_name: config}`` for plugin MCP servers that have overlays.

    Base config comes from installed plugins; effective overlay env/headers are
    merged in and ``${CLAUDE_PLUGIN_ROOT}`` is expanded to the plugin's install
    path. Names no installed plugin declares are skipped silently here — they may
    still land on a gateway server, and :func:`apply_overlays` (the session path)
    owns the stale-overlay warning. Never raises.
    """
    overlays = get_effective_overlays()
    if not overlays:
        return {}
    by_name = _plugin_entries_by_name()

    out: Dict[str, Dict[str, Any]] = {}
    for name, overlay in overlays.items():
        entry = by_name.get(name)
        if entry is None:
            continue
        out[name] = _merged_expanded(
            entry["config"], overlay, entry.get("install_path")
        )
    return out


def apply_overlays(
    mcp_servers: Optional[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """Materialize every installed plugin MCP server into the gateway map.

    The Claude backend sets ``strict_mcp_config`` whenever the merged map is
    non-empty, so this map is the session's complete MCP set — a plugin server
    that is not materialized here does not exist for the session.

    Per plugin server name (first installed plugin wins):

    * with an effective overlay, the plugin base + overlay is materialized and
      wins over a same-named gateway server (credentials attach to the
      plugin-defined command/url);
    * without an overlay, a same-named gateway (``MCP_CONFIG``/manifest)
      server wins — the operator's explicit config overrides the bundle;
    * otherwise the plugin base is materialized as-is.

    Overlay names no plugin declares merge into the gateway config of that
    name; a name neither side declares is stale — warned about and skipped.

    Returns ``(merged_servers, applied)`` where ``applied`` maps ``plugin``
    (materialized plugin servers), ``plugin_overlaid`` (the subset that had an
    overlay), ``overridden`` (plugin servers skipped for a same-named gateway
    config), ``gateway`` (gateway servers that received overlay values) and
    ``stale``. Does not mutate the input. Never raises.
    """
    merged: Dict[str, Any] = dict(mcp_servers or {})
    applied: Dict[str, List[str]] = {
        "plugin": [],
        "plugin_overlaid": [],
        "overridden": [],
        "gateway": [],
        "stale": [],
    }

    overlays = get_effective_overlays()
    by_name = _plugin_entries_by_name()

    for name in sorted(by_name):
        overlay = overlays.get(name) or {}
        if not overlay and isinstance(merged.get(name), dict):
            applied["overridden"].append(name)
            continue
        entry = by_name[name]
        merged[name] = _merged_expanded(
            entry["config"], overlay, entry.get("install_path")
        )
        applied["plugin"].append(name)
        if overlay:
            applied["plugin_overlaid"].append(name)

    for name in sorted(overlays):
        if name in by_name:
            continue
        base = merged.get(name)
        if isinstance(base, dict):
            merged[name] = merge_overlay_into_config(base, overlays[name])
            applied["gateway"].append(name)
            continue
        applied["stale"].append(name)
        logger.warning(
            "MCP credential overlay for %r matches no installed plugin server and "
            "no gateway MCP server; skipping",
            name,
        )
    return merged, applied


def materialize_plugin_server(server_name: str) -> Optional[Dict[str, Any]]:
    """Session-equivalent config for one plugin MCP server, or ``None``.

    Same materialization as :func:`materialize_overlaid_plugin_servers` —
    effective overlay (env layer + admin file, if any) merged in,
    ``${CLAUDE_PLUGIN_ROOT}`` expanded — but also for servers without an
    overlay, so the admin connection test probes exactly what a new Claude
    session would run. ``None`` when no installed plugin declares *server_name*.
    Never raises.
    """
    entry = _plugin_entries_by_name().get(server_name)
    if entry is None:
        return None
    return _merged_expanded(
        entry["config"], get_effective_overlay(server_name), entry.get("install_path")
    )


# Load at import so get_overlays() works without an explicit reload.
reload_overlays()
