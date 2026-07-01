"""Persistent admin-managed MCP server manifest.

The manifest is the admin-managed overlay on top of the ``MCP_CONFIG`` env
base. ``MCP_CONFIG`` stays the respected base; this file layers on top
(manifest wins on name collision) and hot-reloads into NEW sessions via
``mcp_config.reload_mcp_config()``.

The file is JSON at ``GATEWAY_MCP_MANIFEST`` (or ``<project>/data/
gateway-mcp.json``). ``load()`` never raises; ``save()`` is atomic (temp file
in the same directory + ``os.replace``) so a partial write can never corrupt
the manifest under a Docker bind mount.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from threading import RLock

logger = logging.getLogger(__name__)

# Reentrant so a mutator can hold the lock across its whole read-modify-write
# while still calling save() (which re-acquires it). This serializes concurrent
# admin mutations — run_in_threadpool can issue several at once — so two requests
# can't both read the same manifest and clobber each other's entry.
_lock = RLock()

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_PATH = _DATA_DIR / "gateway-mcp.json"


def manifest_path() -> Path:
    """Resolve the manifest path (env override > project ``data/`` default)."""
    override = os.getenv("GATEWAY_MCP_MANIFEST")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def _defaults() -> dict:
    return {"version": 1, "servers": {}}


def load() -> dict:
    """Return a well-formed manifest; corrupt/missing -> defaults, never raises."""
    path = manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _defaults()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load MCP manifest, using defaults: %s", e)
        return _defaults()
    if not isinstance(data, dict):
        return _defaults()
    servers = data.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    else:
        servers = {k: v for k, v in servers.items() if isinstance(v, dict)}
    version = data.get("version")
    if not isinstance(version, int):
        version = 1
    return {"version": version, "servers": servers}


def save(data: dict) -> None:
    """Atomically persist the manifest (temp file in the same dir + replace)."""
    path = manifest_path()
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


def list_servers() -> dict:
    """Return the name -> config map. Read-only; never raises."""
    return load()["servers"]


def get_server(name: str) -> dict:
    """Return the config for ``name`` (empty dict if absent)."""
    rec = load()["servers"].get(name)
    return rec if isinstance(rec, dict) else {}


def upsert_server(name: str, config: dict) -> None:
    """Add or replace a server entry, then hot-reload the effective config."""
    with _lock:
        data = load()
        data["servers"][name] = config
        save(data)
    _reload()


def delete_server(name: str) -> bool:
    """Remove a server entry (returns whether it existed), then hot-reload."""
    with _lock:
        data = load()
        existed = name in data["servers"]
        data["servers"].pop(name, None)
        save(data)
    _reload()
    return existed


def _reload() -> None:
    """Hot-apply to new sessions. OUTSIDE the lock: reload re-load()s the file
    (the source of truth), so a lost in-memory race self-heals. Never raises."""
    try:
        from src import mcp_config

        mcp_config.reload_mcp_config()
    except Exception:
        logger.warning("MCP manifest saved but reload failed", exc_info=True)
