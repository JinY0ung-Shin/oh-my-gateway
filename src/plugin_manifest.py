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
- ``auto_refresh`` = the marketplace auto-refresh config
  (``{"enabled", "interval_minutes"}``) consumed by the
  :mod:`src.plugin_autorefresh` poller; admin-toggled at runtime, no restart
  needed.

The file is JSON at ``CLAUDE_PLUGIN_MANIFEST`` (or ``<project>/data/
gateway-plugins.json``). ``load()`` never raises; ``save()`` is atomic
(temp file in the same directory + ``os.replace``) so a partial write can
never corrupt the manifest under a Docker bind mount.
"""

import json
import logging
import math
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

# Auto-refresh interval bounds (minutes). Clamped on read AND write so a
# hand-edited manifest can never drive the refresh poller into a hot loop of
# git clones and claude CLI calls.
AUTO_REFRESH_DEFAULT_MINUTES = 60
AUTO_REFRESH_MIN_MINUTES = 5
AUTO_REFRESH_MAX_MINUTES = 10080  # one week


def manifest_path() -> Path:
    """Resolve the manifest path (env override > project ``data/`` default)."""
    override = os.getenv("CLAUDE_PLUGIN_MANIFEST")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def _defaults() -> dict:
    return {
        "version": 1,
        "added": [],
        "removed": [],
        "marketplaces": {},
        "auto_refresh": _normalize_auto_refresh(None),
    }


def _coerce_bool(value) -> bool:
    """Coerce a hand-edited JSON value to a bool without Python truthiness traps.

    A real bool passes through; a number is nonzero; a string is matched against
    an explicit truthy set so ``"false"``/``"off"``/``"no"``/``"0"`` read as
    False (plain ``bool("false")`` would be True). Anything else is False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return math.isfinite(value) and value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


def _normalize_auto_refresh(raw) -> dict:
    """Coerce ``auto_refresh`` into ``{"enabled": bool, "interval_minutes": int}``.

    Off by default; a non-numeric or non-finite interval falls back to the
    default and a numeric one is clamped into the allowed range. Must tolerate
    arbitrary hand-edited JSON — ``load()`` relies on this never raising (NaN and
    Infinity are accepted by ``json.loads`` and would blow up a bare ``int()``).
    """
    out = {"enabled": False, "interval_minutes": AUTO_REFRESH_DEFAULT_MINUTES}
    if not isinstance(raw, dict):
        return out
    out["enabled"] = _coerce_bool(raw.get("enabled"))
    interval = raw.get("interval_minutes")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        return out
    if not math.isfinite(interval):
        return out
    out["interval_minutes"] = max(
        AUTO_REFRESH_MIN_MINUTES, min(AUTO_REFRESH_MAX_MINUTES, int(interval))
    )
    return out


def _normalize_removed(removed) -> list:
    """Coerce ``removed`` into a list of ``{"spec","scope"}`` dicts.

    scope is part of a plugin's identity (claude stores one registry entry per
    scope), so a removal must remember which scope it targeted. Legacy spec-only
    strings are read as ``user`` scope for backward tolerance.
    """
    if not isinstance(removed, list):
        return []
    out = []
    for r in removed:
        if isinstance(r, dict) and isinstance(r.get("spec"), str) and r["spec"]:
            scope = r.get("scope")
            scope = scope if isinstance(scope, str) and scope else "user"
            out.append({"spec": r["spec"], "scope": scope})
        elif isinstance(r, str) and r:
            out.append({"spec": r, "scope": "user"})
    return out


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
    removed = _normalize_removed(removed)
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
        "auto_refresh": _normalize_auto_refresh(data.get("auto_refresh")),
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
            if not (
                spec_for(e.get("name", ""), e.get("marketplace", "")) == spec
                and e.get("scope") == scope
            )
        ]
        added.append(entry)
        data["added"] = added
        data["removed"] = [
            r
            for r in data["removed"]
            if not (r["spec"] == spec and r["scope"] == scope)
        ]
        save(data)


def remove_added(spec: str, scope: str) -> None:
    """Drop the ``added`` entry matching (``spec``, ``scope``); no-op if absent.

    scope is part of the identity: the same plugin can be installed at more than
    one scope, each a distinct managed entry.
    """
    with _lock:
        data = load()
        data["added"] = [
            e
            for e in data["added"]
            if not (
                spec_for(e.get("name", ""), e.get("marketplace", "")) == spec
                and e.get("scope") == scope
            )
        ]
        save(data)


def mark_removed(spec: str, scope: str) -> None:
    """Record (``spec``, ``scope``) as admin-removed (idempotent)."""
    with _lock:
        data = load()
        if not any(
            r["spec"] == spec and r["scope"] == scope for r in data["removed"]
        ):
            data["removed"].append({"spec": spec, "scope": scope})
        save(data)


def unmark_removed(spec: str, scope: str) -> None:
    """Clear the admin-removed mark for (``spec``, ``scope``) (idempotent)."""
    with _lock:
        data = load()
        data["removed"] = [
            r
            for r in data["removed"]
            if not (r["spec"] == spec and r["scope"] == scope)
        ]
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


def get_auto_refresh() -> dict:
    """Return the normalized auto-refresh config record."""
    return load()["auto_refresh"]


def set_auto_refresh(*, enabled: bool, interval_minutes: int) -> dict:
    """Persist the auto-refresh config; returns the stored (clamped) record."""
    record = _normalize_auto_refresh(
        {"enabled": enabled, "interval_minutes": interval_minutes}
    )
    with _lock:
        data = load()
        data["auto_refresh"] = record
        save(data)
    return record


def list_added() -> list:
    """Return the list of admin-installed plugin entries."""
    return load()["added"]


def list_removed() -> list:
    """Return the list of admin-removed specs."""
    return load()["removed"]
