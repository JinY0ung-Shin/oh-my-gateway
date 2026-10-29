"""Runtime-editable configuration for admin hot-reload.

Provides a thread-safe singleton that stores configuration overrides.
Getters check overrides first, then fall back to the original constants
loaded at startup.

Only values listed in ``EDITABLE_KEYS`` can be changed at runtime.
Changes take effect on the **next request** — already-running requests
and already-created sessions are not retroactively affected.

Overrides are persisted to ``data/runtime_config.json`` (``RUNTIME_CONFIG_FILE``
overrides the path) after every ``set`` / ``reset`` / ``reset_all`` and loaded
when the module is imported, so a restarted gateway — or the second color of a
blue/green deploy (docs/zero-downtime-deploy.md §5.5) — starts with the admin's
overrides instead of the env defaults. Loading never fails startup: an
unreadable file, an unknown key or a value ``_coerce`` rejects is logged and
dropped. The file holds no secrets (see ``EDITABLE_KEYS``) but is written 0600
anyway: only the gateway itself reads it.

Rate limits and timeout are intentionally excluded because slowapi
caches limit strings at decorator time and the backend captures
timeout at init time.  Changing those requires a server restart.
"""

import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Multipart file uploads carry a small boundary/header envelope in addition to
# file bytes. The request-boundary middleware and /files/upload route share this
# one reserve so the advertised file ceiling and the accepted request body stay
# in lockstep.
WORKSPACE_UPLOAD_MULTIPART_RESERVE = 8192


def _int_env(name: str) -> int:
    """Read an int env var, treating unset/junk as 0 ("not configured")."""
    try:
        return int(os.environ.get(name, "") or 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Editable key definitions
# ---------------------------------------------------------------------------

# Each key maps to display metadata and type/validation information.
EDITABLE_KEYS: Dict[str, Dict[str, Any]] = {
    "default_model": {
        "label": "Default Model",
        "type": "string",
        "description": "Fallback model when none specified in request",
    },
    "default_max_turns": {
        "label": "Max Turns",
        "type": "int",
        "description": "Maximum agentic turns per request",
    },
    "session_max_age_minutes": {
        "label": "Session TTL (min)",
        "type": "int",
        "description": "TTL for new sessions (existing sessions keep their original TTL)",
    },
    "session_eviction_policy": {
        "label": "Session Eviction",
        "type": "string",
        "options": ["reject", "lru"],
        "description": (
            "At the MAX_LIVE_SESSIONS cap: reject new sessions with 503, or evict "
            "the least-recently-used idle session (erases its in-memory "
            "conversation; resuming via previous_response_id restores only the "
            "turn counter and workspace)"
        ),
    },
    "thinking_mode": {
        "label": "Thinking Mode",
        "type": "string",
        "options": ["disabled", "adaptive", "enabled"],
        "description": (
            "Claude thinking mode. enabled = fixed budget (THINKING_BUDGET_TOKENS). "
            "Requests that send reasoning.effort / effort override this."
        ),
    },
    "token_streaming": {
        "label": "Token Streaming",
        "type": "bool",
        "description": "Stream individual tokens vs batched chunks",
    },
    "sanitizer_enabled": {
        "label": "Sanitizer (/v1/messages)",
        "type": "bool",
        "description": "Anthropic SSE sanitizer; effective only when ANTHROPIC_BASE_URL is set",
    },
    "auto_compact_window": {
        "label": "Auto-compact window (tokens)",
        "type": "int",
        "min": 20000,
        "description": (
            "Context size at which the CLI auto-compacts a session "
            "(CLAUDE_CODE_AUTO_COMPACT_WINDOW). Lower = compacts sooner, so long "
            "agentic turns stay under a small upstream window at the cost of "
            "losing older detail. 0 = leave the CLI default alone. Applies to NEW "
            "sessions; an override wins over the gateway process env."
        ),
    },
    "workspace_upload_max_bytes": {
        "label": "Workspace upload limit (bytes)",
        "type": "int",
        "min": 0,
        "description": (
            "Maximum size of one file accepted by POST /files/upload. This is the "
            "single file-upload ceiling used by both request-boundary enforcement "
            "and the workspace file route. Applies on the next upload request. "
            "0 disables workspace uploads entirely: /files/limits then publishes 0 "
            "so clients report 'uploads unavailable' instead of offering a control "
            "whose every use ends in a 413."
        ),
    },
    "sysinfo_redaction_enabled": {
        "label": "Host/secret redaction",
        "type": "bool",
        "description": (
            "Redact this host's name, addresses, container id and secret env "
            "values (plus private IP ranges and SYSINFO_REDACT_* extras) from "
            "successful tool results before the model sees them, and from "
            "every response streamed to clients. Failed tool calls cannot be "
            "rewritten before the model sees them (CLI limit), so keep the "
            "container hostname neutral as well. The tool-result hook applies "
            "to NEW sessions; the response filter from the next turn"
        ),
    },
    # Turn admission (src/concurrency.py). Unlike most keys these apply to the
    # live limiter at once: a raised cap admits queued turns immediately. A turn
    # already waiting keeps the wait it started with; a lowered cap never cuts a
    # turn that is already running. 0 means "no limit" / "queue off" as in env.
    "max_concurrent_turns": {
        "label": "Concurrent turns",
        "type": "int",
        "min": 0,
        "max": 1000,
        "description": (
            "How many agent turns may run at once across the gateway "
            "(MAX_CONCURRENT_TURNS). Beyond it a turn waits in the queue. "
            "0 = no limit. Applies at once"
        ),
    },
    "max_concurrent_turns_per_user": {
        "label": "Concurrent turns per user",
        "type": "int",
        "min": 0,
        "max": 1000,
        "description": (
            "Fairness hint for requests that carry a `user` "
            "(MAX_CONCURRENT_TURNS_PER_USER) — not a security control. "
            "0 = no limit. Applies at once"
        ),
    },
    "turn_queue_timeout_seconds": {
        "label": "Queue wait (s)",
        "type": "int",
        "min": 0,
        "max": 3600,
        "description": (
            "How long a turn over a limit waits for a slot before 503 + "
            "Retry-After (TURN_QUEUE_TIMEOUT_SECONDS). 0 = no queue (fail fast). "
            "Keep it under the client's and proxies' read timeout — no response "
            "header is sent while a turn waits. Applies to turns that start "
            "waiting from now on"
        ),
    },
    "turn_queue_max_waiters": {
        "label": "Queue length",
        "type": "int",
        "min": 0,
        "max": 100000,
        "description": (
            "How many turns may wait at once; the next one is refused with 503 "
            "straight away (TURN_QUEUE_MAX_WAITERS). 0 = no bound. Turns already "
            "waiting are never dropped. Applies at once"
        ),
    },
    "agent_teams_enabled": {
        "label": "Agent Teams",
        "type": "bool",
        "description": (
            "CLI agent teams (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS, on by default) "
            "for NEW Claude sessions: in SDK mode this gives the Agent tool its "
            "`name` parameter (named agents SendMessage can address and resume); "
            "it never starts in-process teammates. An override wins over the "
            "gateway process env, and the CLI's account-side feature gate can "
            "still veto it"
        ),
    },
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_FILE_VERSION = 1
_MAX_FILE_BYTES = 1 << 20

# The JSON type a persisted value must already have (the file is written from
# coerced values, so anything else was edited by hand or by another build).
_JSON_TYPES = {"int": int, "bool": bool, "string": str}


def runtime_config_path() -> Path:
    raw = os.getenv("RUNTIME_CONFIG_FILE", "").strip()
    return Path(raw) if raw else _DATA_DIR / "runtime_config.json"


# ---------------------------------------------------------------------------
# RuntimeConfig singleton
# ---------------------------------------------------------------------------


class RuntimeConfig:
    """Thread-safe runtime configuration store.

    With ``persist=True`` (the module singleton) the file at
    :func:`runtime_config_path` is the source of truth, shared by every gateway
    process on the same ``data/`` (blue/green colors during a deploy):

    * reads are read-through — :meth:`refresh` re-reads the file whenever its
      identity (inode, mtime, size) changed, so a value one color saved is what
      the other one serves next;
    * writes are read-modify-write of **one key** under an ``flock`` on
      ``<file>.lock``: the latest file is read, only the changed key is applied,
      and the result is replaced atomically — a color never writes back a stale
      copy of the other keys (#266 review). Keys this build does not know are
      kept as they are.

    A bare ``RuntimeConfig()`` stays memory-only.
    """

    def __init__(self, persist: bool = False) -> None:
        self._overrides: Dict[str, Any] = {}
        self._lock = Lock()
        self._listeners: List[Callable[[Optional[str]], None]] = []
        self._persist = persist
        # Identity of the file the overrides were last read from / written as.
        self._stamp: Optional[tuple] = None

    @staticmethod
    def _file_stamp(path: Path) -> Optional[tuple]:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    @staticmethod
    def _write_file(path: Path, raw_overrides: Dict[str, Any]) -> None:
        body = json.dumps(
            {"version": _FILE_VERSION, "overrides": raw_overrides},
            indent=2,
            sort_keys=True,
        )
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp creates the file 0600; replace() gives it a new inode, which
            # is what other processes' refresh() notices.
            fd, tmp = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _mutate(self, apply: Callable[[Dict[str, Any]], None]) -> None:
        """Apply *apply* to the overrides: in memory, and (persisted) to the
        latest file under the cross-process write lock. Never raises on I/O: a
        failed write is logged and the change stays live in this process."""
        if not self._persist:
            with self._lock:
                apply(self._overrides)
            return
        path = runtime_config_path()
        lock_fd = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(
                str(path) + ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                raw = self._read_file(path)
            except FileNotFoundError:
                raw = {}
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                logger.warning(
                    "Runtime config file %s unreadable (%s); rewriting it from "
                    "this change",
                    path,
                    exc,
                )
                raw = {}
            apply(raw)
            self._write_file(path, raw)
            loaded = self._validated(raw)
            stamp = self._file_stamp(path)
            with self._lock:
                self._overrides = loaded
                self._stamp = stamp
        except OSError as exc:
            logger.error(
                "Runtime config overrides not persisted to %s (%s); they are "
                "live in this process but will not survive a restart",
                path,
                exc,
            )
            with self._lock:
                apply(self._overrides)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    @staticmethod
    def _read_file(path: Path) -> Dict[str, Any]:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError(f"larger than {_MAX_FILE_BYTES} bytes")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("overrides"), dict):
            raise ValueError("expected an object with an 'overrides' object")
        if data.get("version") != _FILE_VERSION:
            raise ValueError(f"unsupported version {data.get('version')!r}")
        return data["overrides"]

    def _validated(self, stored: Dict[str, Any]) -> Dict[str, Any]:
        """The editable, valid subset of a file's overrides (the rest is logged)."""
        loaded: Dict[str, Any] = {}
        for key, value in stored.items():
            meta = EDITABLE_KEYS.get(key)
            if meta is None:
                logger.warning(
                    "Runtime config file: unknown key %s ignored (kept on disk)", key
                )
                continue
            expected = _JSON_TYPES[meta["type"]]
            # bool is an int subclass: an int key must not accept true/false.
            if not isinstance(value, expected) or (
                expected is int and isinstance(value, bool)
            ):
                logger.warning(
                    "Runtime config file: %s has a %s, expected %s; ignored",
                    key,
                    type(value).__name__,
                    meta["type"],
                )
                continue
            try:
                loaded[key] = self._coerce(key, value)
            except (TypeError, ValueError) as exc:
                logger.warning("Runtime config file: %s ignored (%s)", key, exc)
        return loaded

    def load(self) -> None:
        """Read the persisted overrides (startup). Same as :meth:`refresh`, but
        logs what was loaded."""
        if not self._persist:
            return
        if self.refresh(force=True):
            with self._lock:
                names = sorted(self._overrides)
            if names:
                logger.info(
                    "Runtime config: loaded %d persisted override(s) from %s: %s",
                    len(names),
                    runtime_config_path(),
                    ", ".join(names),
                )

    def refresh(self, force: bool = False) -> bool:
        """Re-read the file when another process (or anyone) replaced it since
        this one last read or wrote it. ``True`` when the overrides were
        reloaded; listeners are then notified like a ``reset_all``. Cheap when
        nothing changed (one ``stat``). Never raises."""
        if not self._persist:
            return False
        path = runtime_config_path()
        try:
            stamp = self._file_stamp(path)
        except OSError as exc:  # e.g. a parent that is not a directory
            logger.debug("Runtime config file %s not checked (%s)", path, exc)
            return False
        with self._lock:
            if not force and stamp == self._stamp:
                return False
        if stamp is None:
            loaded: Dict[str, Any] = {}
        else:
            try:
                loaded = self._validated(self._read_file(path))
            except FileNotFoundError:
                loaded = {}
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                logger.warning(
                    "Runtime config file %s ignored (%s); keeping the current "
                    "overrides",
                    path,
                    exc,
                )
                with self._lock:
                    self._stamp = stamp  # do not re-read a broken file every call
                return False
        with self._lock:
            changed = loaded != self._overrides
            self._overrides = loaded
            self._stamp = stamp
        if changed or force:
            self._notify(None)
        return True

    def on_change(self, listener: Callable[[Optional[str]], None]) -> None:
        """Call *listener* with the changed key (``None`` = all) after a change.

        For values that live in long-lived objects rather than being read per
        request (the turn limiter). A failing listener is logged, never raised:
        the override itself has been stored.
        """
        self._listeners.append(listener)

    def _notify(self, key: Optional[str]) -> None:
        for listener in list(self._listeners):
            try:
                listener(key)
            except Exception:  # noqa: BLE001 — a listener must not undo the write
                logger.exception("Runtime config listener failed for %s", key)

    def get(self, key: str) -> Any:
        """Return the runtime value for *key* (override or original constant)."""
        self.refresh()
        with self._lock:
            if key in self._overrides:
                return self._overrides[key]
        return self._get_original(key)

    def set(self, key: str, value: Any) -> None:
        """Set a runtime override. Raises ``KeyError`` for unknown keys."""
        if key not in EDITABLE_KEYS:
            raise KeyError(f"Key '{key}' is not editable at runtime")
        coerced = self._coerce(key, value)

        def apply(overrides: Dict[str, Any]) -> None:
            overrides[key] = coerced

        self._mutate(apply)
        logger.info(f"Runtime config updated: {key} = {coerced!r}")
        self._notify(key)

    def is_overridden(self, key: str) -> bool:
        """Return ``True`` if *key* has a runtime override."""
        self.refresh()
        with self._lock:
            return key in self._overrides

    def reset(self, key: str) -> None:
        """Remove a runtime override, reverting to the startup value.

        Raises ``KeyError`` for unknown keys.
        """
        if key not in EDITABLE_KEYS:
            raise KeyError(f"Key '{key}' is not editable at runtime")
        self._mutate(lambda overrides: overrides.pop(key, None))
        logger.info(f"Runtime config reset: {key}")
        self._notify(key)

    def reset_all(self) -> None:
        """Remove all runtime overrides."""
        def apply(overrides: Dict[str, Any]) -> None:
            # Only the keys this build manages: another build's entries stay.
            for name in list(overrides):
                if name in EDITABLE_KEYS:
                    del overrides[name]

        self._mutate(apply)
        logger.info("Runtime config: all overrides cleared")
        self._notify(None)

    def get_all(self) -> Dict[str, Any]:
        """Return all editable keys with their current effective values."""
        # Snapshot overrides once under a single lock, then compute everything
        # from the snapshot without re-acquiring the lock per key.
        self.refresh()
        with self._lock:
            overrides = dict(self._overrides)
        result = {}
        for key, meta in EDITABLE_KEYS.items():
            original = self._get_original(key)
            is_overridden = key in overrides
            value = overrides[key] if is_overridden else original
            result[key] = {
                **meta,
                "value": value,
                "original": original,
                "overridden": is_overridden,
            }
        return result

    @staticmethod
    def _get_original(key: str) -> Any:
        """Return the original startup value from constants."""
        from src.constants import (
            DEFAULT_MAX_TURNS,
            DEFAULT_MODEL,
            MAX_CONCURRENT_TURNS,
            MAX_CONCURRENT_TURNS_PER_USER,
            SESSION_EVICTION_POLICY,
            SESSION_MAX_AGE_MINUTES,
            TURN_QUEUE_MAX_WAITERS,
            TURN_QUEUE_TIMEOUT_SECONDS,
            WORKSPACE_UPLOAD_MAX_BYTES,
        )
        from src.env_utils import parse_bool_env
        from src.backends.claude.constants import (
            THINKING_MODE,
            TOKEN_STREAMING,
        )

        # Lazy import to avoid a circular dependency: sanitizer.config imports
        # back from runtime_config to honor admin overrides.
        from src.sanitizer.config import _env_enabled as _sanitizer_env_enabled

        _map = {
            "default_model": DEFAULT_MODEL,
            "default_max_turns": DEFAULT_MAX_TURNS,
            "session_max_age_minutes": SESSION_MAX_AGE_MINUTES,
            "session_eviction_policy": SESSION_EVICTION_POLICY,
            "thinking_mode": THINKING_MODE,
            "token_streaming": TOKEN_STREAMING,
            "sanitizer_enabled": _sanitizer_env_enabled(),
            "workspace_upload_max_bytes": WORKSPACE_UPLOAD_MAX_BYTES,
            "max_concurrent_turns": max(0, MAX_CONCURRENT_TURNS),
            "max_concurrent_turns_per_user": max(0, MAX_CONCURRENT_TURNS_PER_USER),
            "turn_queue_timeout_seconds": TURN_QUEUE_TIMEOUT_SECONDS,
            "turn_queue_max_waiters": TURN_QUEUE_MAX_WAITERS,
            "sysinfo_redaction_enabled": parse_bool_env("SYSINFO_REDACTION", "true"),
            # Mirrors the CLI's own parse of the gate (2.1.283): only
            # 1/true/yes/on, case- and whitespace-insensitive, turn it on.
            # src/constants.py defaults it to "1" when unset or blank.
            "agent_teams_enabled": (
                (os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") or "")
                .strip()
                .lower()
                in ("1", "true", "yes", "on")
            ),
            # 0 = not set — the CLI picks its own window from the model.
            # A junk value reads as 0 rather than crashing the config read;
            # the CLI ignores junk the same way.
            "auto_compact_window": _int_env("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
        }
        return _map.get(key)

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        """Coerce *value* to the expected type for *key*."""
        meta = EDITABLE_KEYS[key]
        expected = meta["type"]
        if expected == "int":
            v = int(value)
            # A per-key floor where 1 is meaningless — a 1-token compaction
            # window would compact on every turn and lose the conversation.
            low = meta.get("min", 1)
            if v < low:
                raise ValueError(f"{key} must be >= {low}, got {v}")
            high = meta.get("max")
            if high is not None and v > high:
                raise ValueError(f"{key} must be <= {high}, got {v}")
            return v
        if expected == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                low = value.lower()
                if low in ("true", "1", "yes", "on"):
                    return True
                if low in ("false", "0", "no", "off"):
                    return False
                raise ValueError(
                    f"{key} must be a boolean (true/false/yes/no/1/0), got {value!r}"
                )
            if isinstance(value, (int, float)):
                return bool(value)
            raise ValueError(f"{key} must be a boolean, got {type(value).__name__}")
        s = str(value)
        options = meta.get("options")
        if options and s not in options:
            raise ValueError(f"{key} must be one of {options}, got {s!r}")
        return s


# ---------------------------------------------------------------------------
# Convenience getters — import these instead of raw constants
# ---------------------------------------------------------------------------

runtime_config = RuntimeConfig(persist=True)
runtime_config.load()


def get_default_model() -> str:
    return runtime_config.get("default_model")


def get_default_max_turns() -> int:
    return runtime_config.get("default_max_turns")


def get_thinking_mode() -> str:
    return runtime_config.get("thinking_mode")


def get_token_streaming() -> bool:
    return runtime_config.get("token_streaming")


def get_workspace_upload_max_bytes() -> int:
    return runtime_config.get("workspace_upload_max_bytes")


def get_workspace_upload_request_max_bytes() -> int:
    """Maximum raw multipart body accepted by POST /files/upload."""
    return max(0, get_workspace_upload_max_bytes()) + WORKSPACE_UPLOAD_MULTIPART_RESERVE
