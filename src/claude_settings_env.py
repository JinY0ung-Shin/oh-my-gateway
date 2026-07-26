"""Gateway-managed ``env`` block of the Claude Code settings file.

Claude Code applies the ``env`` map in ``~/.claude/settings.json`` to every
session process it starts. Verified live (CLI 2.1.220, the SDK 0.2.108 bundle,
2026-07-26) by spawning a probe MCP server under a temp ``$HOME``: the child saw
the settings value, and it **won over** the same key inherited from the parent
process environment. Two consequences shape this module:

* these values reach the session process, so the agent's own Bash can read them
  (unlike :mod:`src.mcp_plugin_overlay`, which stays scoped to an MCP server);
* a key that collides with the gateway's auth injection would silently override
  it, so :data:`RESERVED_KEYS` are refused.

Layers, mirroring the MCP overlay's shape:

* ``GATEWAY_CLAUDE_SETTINGS_ENV`` — deploy-time declaration (inline JSON or a
  path to a JSON file). Read-only at runtime.
* the admin store (``data/gateway-claude-settings-env.json``) — hot-editable
  from the admin panel, and it wins **per key**.

The effective map is *projected* into the settings file: other settings keys
(``permissions``, ``hooks``, …) and env keys the gateway does not manage are
preserved untouched. The store remembers which keys it last wrote so a key
dropped from the effective map is pruned without disturbing hand-added ones.
``{{env:NAME}}`` templates resolve at projection time, so the settings file
carries a literal value and rotating the source var needs a re-projection.

Only sessions whose ``setting_sources`` include ``user`` read this file — see
:func:`applies_to_sessions` (the gateway default is ``project,local``; Docker
Compose sets ``user,project,local``).
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
_DEFAULT_STORE_PATH = _DATA_DIR / "gateway-claude-settings-env.json"

# Path of the Claude settings file to manage; default ``$HOME/.claude/settings.json``.
SETTINGS_PATH_VAR = "GATEWAY_CLAUDE_SETTINGS_PATH"
# Admin store location override.
STORE_PATH_VAR = "GATEWAY_CLAUDE_SETTINGS_ENV_STORE"
# Deploy-time declaration layer.
ENV_LAYER_VAR = "GATEWAY_CLAUDE_SETTINGS_ENV"

# Keys the gateway itself owns on the SDK subprocess. settings.json env wins over
# the passed process env (verified live), so accepting these would break auth or
# reroute the backend without any visible error.
RESERVED_KEYS = frozenset(
    {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
    }
)

# Mirrors ``src.backends.claude.client._DEFAULT_SETTING_SOURCES``. Duplicated
# because that module imports the SDK; ``test_claude_settings_env.py`` asserts the
# two stay in sync.
_DEFAULT_SETTING_SOURCES = ("project", "local")

# In-memory singletons (rebound on save/reload, never mutated in place).
_admin_env: Dict[str, str] = {}
_projected_keys: Tuple[str, ...] = ()
_env_layer: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def settings_path() -> Optional[Path]:
    """Claude settings file to manage, or ``None`` when it cannot be located."""
    override = (os.getenv(SETTINGS_PATH_VAR) or "").strip()
    if override:
        return Path(override)
    home = (os.getenv("HOME") or "").strip()
    if not home:
        logger.warning(
            "HOME is unset and %s is not set; cannot locate the Claude settings file",
            SETTINGS_PATH_VAR,
        )
        return None
    return Path(home) / ".claude" / "settings.json"


def store_path() -> Path:
    override = (os.getenv(STORE_PATH_VAR) or "").strip()
    if override:
        return Path(override)
    return _DEFAULT_STORE_PATH


def applies_to_sessions() -> bool:
    """Whether gateway sessions read the user settings file at all.

    ``settings.json`` under ``$HOME`` is the ``user`` setting source; the gateway
    default omits it, so a managed env would exist on disk yet never apply.
    """
    raw = (os.getenv("CLAUDE_SETTING_SOURCES") or "").strip()
    sources = (
        [part.strip() for part in raw.split(",") if part.strip()]
        if raw
        else list(_DEFAULT_SETTING_SOURCES)
    )
    return "user" in sources


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_env_map(value: Any) -> Dict[str, str]:
    """Keep ``dict[str, str]`` entries with a usable env var name; drop the rest."""
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = k.strip()
        if not key or not is_valid_env_name(key):
            continue
        out[key] = v
    return out


def is_valid_env_name(name: str) -> bool:
    """POSIX-ish env var name: letters, digits, underscore; no leading digit."""
    if not name or name[0].isdigit():
        return False
    return all(ch.isalnum() or ch == "_" for ch in name) and name.isascii()


# ---------------------------------------------------------------------------
# Admin store
# ---------------------------------------------------------------------------


def _defaults() -> dict:
    return {"version": 1, "env": {}, "projected": []}


def load_store() -> dict:
    """Return a well-formed store; corrupt/missing -> defaults, never raises."""
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _defaults()
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Failed to load Claude settings env store, using defaults: %s", e
        )
        return _defaults()
    if not isinstance(data, dict):
        return _defaults()
    projected = data.get("projected")
    return {
        "version": data.get("version") if isinstance(data.get("version"), int) else 1,
        "env": normalize_env_map(data.get("env")),
        "projected": sorted(
            {k for k in projected if isinstance(k, str) and k.strip()}
            if isinstance(projected, list)
            else set()
        ),
    }


def save_store(data: dict) -> None:
    """Atomically persist the admin store."""
    path = store_path()
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


# ---------------------------------------------------------------------------
# Deploy-time layer
# ---------------------------------------------------------------------------


def env_layer_source() -> str:
    """Raw ``GATEWAY_CLAUDE_SETTINGS_ENV`` value (inline JSON or path); ``""`` unset."""
    return (os.getenv(ENV_LAYER_VAR) or "").strip()


def load_env_layer() -> Dict[str, str]:
    """Parse the deploy-time layer. Never raises.

    Accepts a path to a JSON file or inline JSON, shaped either as a bare
    ``{"KEY": "value"}`` map or wrapped as ``{"env": {...}}`` (the store's own
    shape, so one document can be mounted as a secret and pointed at).
    """
    source = env_layer_source()
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
        logger.error("Failed to parse %s, ignoring the layer: %s", ENV_LAYER_VAR, e)
        return {}

    if not isinstance(raw, dict):
        logger.error("%s must be a JSON object of {NAME: value}", ENV_LAYER_VAR)
        return {}
    inner = raw.get("env") if isinstance(raw.get("env"), dict) else raw

    declared = normalize_env_map(inner)
    dropped = sorted(set(inner) - set(declared)) if isinstance(inner, dict) else []
    if dropped:
        logger.warning(
            "%s dropped %d entry/entries with a non-string value or invalid name: %s",
            ENV_LAYER_VAR,
            len(dropped),
            dropped,
        )
    reserved = sorted(k for k in declared if k in RESERVED_KEYS)
    for key in reserved:
        del declared[key]
        logger.error(
            "%s declares reserved key %r; refusing it (it would override the "
            "gateway's own Claude auth environment)",
            ENV_LAYER_VAR,
            key,
        )
    if declared:
        logger.info(
            "Loaded %d Claude settings env var(s) from %s: %s",
            len(declared),
            ENV_LAYER_VAR,
            sorted(declared),
        )
    return declared


# ---------------------------------------------------------------------------
# Effective map
# ---------------------------------------------------------------------------


def reload() -> Dict[str, str]:
    """Re-read the store + deploy layer and rebind singletons. Returns effective."""
    global _admin_env, _projected_keys, _env_layer
    data = load_store()
    _admin_env = dict(data.get("env") or {})
    _projected_keys = tuple(data.get("projected") or ())
    _env_layer = load_env_layer()
    return get_effective_env()


def get_admin_env() -> Dict[str, str]:
    """Admin-managed keys only (the panel's edit form round-trips these)."""
    return dict(_admin_env)


def get_env_layer() -> Dict[str, str]:
    """Deploy-time declared keys only (read-only in the panel)."""
    return dict(_env_layer)


def get_effective_env() -> Dict[str, str]:
    """Deploy layer as base, admin store layered on top (admin wins per key)."""
    merged = dict(_env_layer)
    merged.update(_admin_env)
    return merged


def key_sources() -> Dict[str, str]:
    """``{key: "env" | "admin" | "env+admin"}`` for the effective map."""
    out: Dict[str, str] = {}
    for key in get_effective_env():
        in_env = key in _env_layer
        in_admin = key in _admin_env
        out[key] = (
            "env+admin" if in_env and in_admin else ("admin" if in_admin else "env")
        )
    return out


def projected_keys() -> List[str]:
    """Keys the last projection wrote into the settings file."""
    return list(_projected_keys)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def replace_admin_env(env: Mapping[str, str]) -> Dict[str, Any]:
    """Replace the admin-managed map, persist, and re-project. Returns the report."""
    normalized = normalize_env_map(dict(env))
    with _lock:
        data = load_store()
        data["env"] = normalized
        save_store(data)
    reload()
    return project()


def clear_admin_env() -> Dict[str, Any]:
    """Drop every admin-managed key (the deploy layer stays) and re-project."""
    return replace_admin_env({})


# ---------------------------------------------------------------------------
# Projection into the Claude settings file
# ---------------------------------------------------------------------------


def read_settings_file() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(settings, error)``. Missing file -> ``({}, None)``. Never raises."""
    path = settings_path()
    if path is None:
        return None, f"HOME is unset and {SETTINGS_PATH_VAR} is not set"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except json.JSONDecodeError as e:
        return None, f"{path} is not valid JSON ({e})"
    except OSError as e:
        return None, f"{path} is not readable ({e})"
    if not isinstance(raw, dict):
        return None, f"{path} must contain a JSON object"
    return raw, None


def read_settings_env() -> Dict[str, str]:
    """The ``env`` block currently in the settings file (``{}`` on any problem)."""
    settings, error = read_settings_file()
    if error is not None or settings is None:
        return {}
    return normalize_env_map(settings.get("env"))


def _resolve_templates(env: Mapping[str, str]) -> Dict[str, str]:
    """Resolve ``{{env:NAME}}`` from the gateway process environment.

    Shared with the MCP overlay path so both layers accept the same reference
    syntax; a missing name resolves to ``""`` and logs a warning.
    """
    from src.mcp_config import resolve_env_refs_in_string

    return {k: resolve_env_refs_in_string(v) for k, v in env.items()}


def _write_settings(path: Path, settings: Dict[str, Any]) -> None:
    """Atomically write the settings file, creating ``~/.claude`` if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def project() -> Dict[str, Any]:
    """Write the effective managed env into the Claude settings file. Never raises.

    Preserves every other settings key and every env key the gateway does not
    manage. Keys the previous projection wrote but the effective map no longer
    contains are pruned; a hand-added key of the same name is therefore adopted
    on save and released on removal, which is the only behaviour that keeps the
    file editable by hand.

    Returns ``{"ok", "path", "written", "pruned", "kept_foreign", "error",
    "applies_to_sessions"}``.
    """
    global _projected_keys

    report: Dict[str, Any] = {
        "ok": False,
        "path": None,
        "written": [],
        "pruned": [],
        "kept_foreign": [],
        "error": None,
        "applies_to_sessions": applies_to_sessions(),
    }
    path = settings_path()
    report["path"] = str(path) if path else None
    if path is None:
        report["error"] = f"HOME is unset and {SETTINGS_PATH_VAR} is not set"
        logger.error("Claude settings env projection skipped: %s", report["error"])
        return report

    settings, error = read_settings_file()
    if error is not None or settings is None:
        # Never clobber a file we could not parse — an operator's permissions or
        # hooks live there too.
        report["error"] = error or "settings file unreadable"
        logger.error("Claude settings env projection skipped: %s", report["error"])
        return report

    effective = get_effective_env()
    resolved = _resolve_templates(effective)

    current_env = settings.get("env")
    current_env = dict(current_env) if isinstance(current_env, dict) else {}
    previously_projected = set(_projected_keys)

    pruned: List[str] = []
    kept_foreign: List[str] = []
    new_env: Dict[str, Any] = {}
    for key, value in current_env.items():
        if key in resolved:
            continue  # replaced below
        if key in previously_projected:
            pruned.append(key)
            continue  # we wrote it, we no longer manage it
        new_env[key] = value
        kept_foreign.append(key)
    new_env.update(resolved)

    if new_env:
        settings["env"] = new_env
    else:
        settings.pop("env", None)

    try:
        _write_settings(path, settings)
    except OSError as e:
        report["error"] = f"failed to write {path}: {e}"
        logger.error("Claude settings env projection failed: %s", report["error"])
        return report

    report["ok"] = True
    report["written"] = sorted(resolved)
    report["pruned"] = sorted(pruned)
    report["kept_foreign"] = sorted(kept_foreign)

    # Remember what we wrote so the next projection can prune precisely.
    with _lock:
        data = load_store()
        data["projected"] = report["written"]
        save_store(data)
    _projected_keys = tuple(report["written"])

    if report["written"] or report["pruned"]:
        logger.info(
            "Projected Claude settings env into %s: wrote %s, pruned %s (kept %d "
            "unmanaged key(s))",
            path,
            report["written"],
            report["pruned"],
            len(report["kept_foreign"]),
        )
    if report["written"] and not report["applies_to_sessions"]:
        logger.warning(
            "Claude settings env written to %s but CLAUDE_SETTING_SOURCES does not "
            "include 'user', so gateway sessions will not read it",
            path,
        )
    return report


def project_at_startup() -> Dict[str, Any]:
    """Reload both layers and project once. Safe to call before serving traffic."""
    reload()
    if not get_effective_env() and not _projected_keys:
        # Nothing declared and nothing left behind: do not create the file.
        return {
            "ok": True,
            "path": str(settings_path()) if settings_path() else None,
            "written": [],
            "pruned": [],
            "kept_foreign": [],
            "error": None,
            "applies_to_sessions": applies_to_sessions(),
            "skipped": True,
        }
    return project()


def snapshot() -> Dict[str, Any]:
    """Read-only view for the admin panel / diagnostics."""
    settings, error = read_settings_file()
    file_env = (
        normalize_env_map(settings.get("env")) if isinstance(settings, dict) else {}
    )
    effective = get_effective_env()
    return {
        "settings_path": str(settings_path()) if settings_path() else None,
        "settings_error": error,
        "store_path": str(store_path()),
        "env_layer_var": ENV_LAYER_VAR,
        "env_layer_declared": bool(_env_layer),
        "admin": copy.deepcopy(_admin_env),
        "env_layer": copy.deepcopy(_env_layer),
        "effective": effective,
        "sources": key_sources(),
        "projected": list(_projected_keys),
        "file_env_keys": sorted(file_env),
        "unmanaged_keys": sorted(set(file_env) - set(effective)),
        "applies_to_sessions": applies_to_sessions(),
        "reserved_keys": sorted(RESERVED_KEYS),
    }


# Load at import so the panel and the projection see the same maps.
reload()
