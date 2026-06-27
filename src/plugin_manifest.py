"""Persistent managed plugin manifest.

The manifest is the admin-managed layer that sits on top of the env-var
bootstrap (``CLAUDE_PLUGIN_*`` read by ``docker/install_plugins.py`` at
container start). The admin panel mutates it at runtime; startup replays it.

- ``added``  = plugins the admin explicitly installed, so startup reinstalls
  them (survives a wipe of the plugin cache volume).
- ``removed`` = specs the admin uninstalled that ALSO came from the env
  bootstrap, so startup uninstalls them and skips reinstall.
- ``marketplaces`` = the original remote/branch/scope the admin used to add each
  marketplace, keyed by marketplace name. ``claude plugin marketplace add`` is
  given a local clone path and records ``source: directory`` (losing the
  remote), so this map is the authoritative record for replay and for telling
  the UI a marketplace's scope.

The file is JSON at ``CLAUDE_PLUGIN_MANIFEST`` (or ``<project>/data/
gateway-plugins.json``). ``load()`` never raises; ``save()`` is atomic
(temp file in the same directory + ``os.replace``) so a partial write can
never corrupt the manifest under a Docker bind mount.
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
_DEFAULT_PATH = _DATA_DIR / "gateway-plugins.json"


def manifest_path() -> Path:
    """Resolve the manifest path (env override > project ``data/`` default)."""
    override = os.getenv("CLAUDE_PLUGIN_MANIFEST")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def _defaults() -> dict:
    return {"version": 1, "added": [], "removed": [], "marketplaces": {}}


def load() -> dict:
    """Return a well-formed manifest; corrupt/missing -> defaults, never raises."""
    path = manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _defaults()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load plugin manifest, using defaults: %s", e)
        return _defaults()
    if not isinstance(data, dict):
        logger.warning("Plugin manifest is not an object, using defaults")
        return _defaults()
    added = data.get("added")
    removed = data.get("removed")
    if not isinstance(added, list):
        added = []
    else:
        added = [e for e in added if isinstance(e, dict)]
    if not isinstance(removed, list):
        removed = []
    else:
        removed = [s for s in removed if isinstance(s, str)]
    marketplaces = data.get("marketplaces")
    if not isinstance(marketplaces, dict):
        marketplaces = {}
    else:
        marketplaces = {
            k: v for k, v in marketplaces.items() if isinstance(v, dict)
        }
    version = data.get("version")
    if not isinstance(version, int):
        version = 1
    return {
        "version": version,
        "added": added,
        "removed": removed,
        "marketplaces": marketplaces,
    }


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


def spec_for(name: str, marketplace: str) -> str:
    """``name@marketplace`` when marketplace is truthy, else ``name``."""
    return f"{name}@{marketplace}" if marketplace else name


def add_plugin(
    *, repo: str, name: str, marketplace: str, scope: str, branch: str
) -> None:
    """Upsert an entry into ``added`` keyed by spec; clears any ``removed`` mark."""
    spec = spec_for(name, marketplace)
    entry = {
        "repo": repo,
        "name": name,
        "marketplace": marketplace,
        "scope": scope,
        "branch": branch,
    }
    with _lock:
        data = load()
        added = [
            e
            for e in data["added"]
            if spec_for(e.get("name", ""), e.get("marketplace", "")) != spec
        ]
        added.append(entry)
        data["added"] = added
        data["removed"] = [s for s in data["removed"] if s != spec]
        save(data)


def remove_added(spec: str) -> None:
    """Drop the ``added`` entry matching ``spec`` (no-op if absent)."""
    with _lock:
        data = load()
        data["added"] = [
            e
            for e in data["added"]
            if spec_for(e.get("name", ""), e.get("marketplace", "")) != spec
        ]
        save(data)


def mark_removed(spec: str) -> None:
    """Record ``spec`` as admin-removed (idempotent)."""
    with _lock:
        data = load()
        if spec not in data["removed"]:
            data["removed"].append(spec)
        save(data)


def unmark_removed(spec: str) -> None:
    """Clear the admin-removed mark for ``spec`` (idempotent)."""
    with _lock:
        data = load()
        data["removed"] = [s for s in data["removed"] if s != spec]
        save(data)


def remove_marketplace_entries(marketplace: str) -> None:
    """Drop a marketplace's ``added`` entries and its marketplace record."""
    with _lock:
        data = load()
        data["added"] = [
            e for e in data["added"] if e.get("marketplace") != marketplace
        ]
        data["marketplaces"].pop(marketplace, None)
        save(data)


def set_marketplace(name: str, *, repo: str, branch: str, scope: str) -> None:
    """Record the original remote/branch/scope an admin used to add a marketplace.

    This preserves the remote even though ``claude plugin marketplace add`` is
    given a local clone path (and records ``source: directory``), so startup can
    re-clone it after a volume wipe and the UI knows the marketplace's scope.
    """
    with _lock:
        data = load()
        data["marketplaces"][name] = {"repo": repo, "branch": branch, "scope": scope}
        save(data)


def get_marketplace(name: str) -> dict:
    """Return the marketplace record for ``name`` (empty dict if absent)."""
    rec = load()["marketplaces"].get(name)
    return rec if isinstance(rec, dict) else {}


def list_marketplace_records() -> dict:
    """Return the full marketplace-name -> record map."""
    return load()["marketplaces"]


def list_added() -> list:
    """Return the list of admin-installed plugin entries."""
    return load()["added"]


def list_removed() -> list:
    """Return the list of admin-removed specs."""
    return load()["removed"]
