"""Custom system prompt management.

Provides a thread-safe store for the global base system prompt.
Completely separate from ``RuntimeConfig`` to avoid logging prompt
content and to support large text values cleanly.

Priority: persisted override > file default > None (preset mode).

The admin override is persisted to a JSON file in the project data
directory so it survives server restarts.
"""

import json
import logging
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional

logger = logging.getLogger(__name__)

_lock = Lock()
_default_prompt: Optional[str] = None  # loaded from file at startup (resolved)
_default_prompt_raw: Optional[str] = None  # loaded from file at startup (original)
_runtime_prompt: Optional[str] = None  # admin override (resolved)
_runtime_prompt_raw: Optional[str] = None  # admin override (original)
_preset_text: Optional[str] = None  # cached preset reference text
_active_prompt_name: Optional[str] = None  # name of the currently active named prompt
# Version/deploy metadata of the live override (``prompt_library`` deploys):
# ``{"version": int, "deployed_at": iso, "deployed_by": str}``; empty for a
# legacy/direct override that no library version backs.
_active_meta: dict = {}

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PERSIST_FILE = _DATA_DIR / "system_prompt.json"
_PROMPTS_DIR = _DATA_DIR / "prompts"


def _load_persisted() -> Optional[str]:
    """Load the persisted admin override from disk."""
    global _active_prompt_name, _active_meta
    if not _PERSIST_FILE.is_file():
        return None
    try:
        data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Persisted system prompt has invalid structure, ignoring")
            return None
        with _lock:
            _active_prompt_name = data.get("active_name")
            _active_meta = _clean_meta(data)
        value = data.get("prompt")
        if not isinstance(value, str) or not value.strip():
            logger.warning("Persisted system prompt has invalid value, ignoring")
            return None
        return value
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load persisted system prompt: %s", e)
        return None


def _clean_meta(data: dict) -> dict:
    meta: dict = {}
    version = data.get("active_version")
    if isinstance(version, int) and not isinstance(version, bool) and version > 0:
        meta["version"] = version
    for key in ("deployed_at", "deployed_by"):
        if isinstance(data.get(key), str) and data[key]:
            meta[key] = data[key]
    return meta


def _save_persisted(
    text: Optional[str],
    *,
    active_name: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """Save or delete the persisted admin override.

    Raises ``OSError`` on failure so callers can avoid in-memory/disk divergence.

    The lock is held across the file I/O so concurrent callers cannot observe
    a memory/disk mismatch, and ``_active_prompt_name`` is only updated after
    the file mutation succeeds (file becomes the source of truth).
    """
    global _active_prompt_name, _active_meta
    if text is None:
        with _lock:
            if _PERSIST_FILE.is_file():
                _PERSIST_FILE.unlink()
                logger.info("System prompt: persisted file removed")
            _active_prompt_name = None
            _active_meta = {}
    else:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict = {"prompt": text}
        if active_name:
            payload["active_name"] = active_name
        clean = _clean_meta(
            {
                "active_version": (meta or {}).get("version"),
                "deployed_at": (meta or {}).get("deployed_at"),
                "deployed_by": (meta or {}).get("deployed_by"),
            }
        )
        if clean.get("version") and active_name:
            payload["active_version"] = clean["version"]
        else:
            clean.pop("version", None)
        for key in ("deployed_at", "deployed_by"):
            if key in clean:
                payload[key] = clean[key]
        with _lock:
            _PERSIST_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _active_prompt_name = active_name
            _active_meta = clean
            logger.info("System prompt: persisted to %s", _PERSIST_FILE)


def _resolve_placeholders(text: str) -> str:
    """Replace ``{{PLACEHOLDER}}`` tokens with runtime values.

    ``{{WORKING_DIRECTORY}}`` and ``{{MEMORY_PATH}}`` are intentionally left
    unresolved here because they vary per-user workspace.  Use
    :func:`resolve_request_placeholders` at request time to fill them in.
    """
    from src.constants import PROMPT_LANGUAGE

    replacements = {
        "LANGUAGE": PROMPT_LANGUAGE,
        "PLATFORM": platform.system().lower(),
        "SHELL": os.environ.get("SHELL", ""),
        "OS_VERSION": platform.platform(),
    }
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def resolve_request_placeholders(text: Optional[str], cwd: str) -> Optional[str]:
    """Replace per-request ``{{PLACEHOLDER}}`` tokens against *cwd*.

    Resolves ``{{WORKING_DIRECTORY}}`` to *cwd* and ``{{MEMORY_PATH}}`` to
    ``<cwd>/.memory`` (creating the directory so the prompt's "directory
    already exists" assurance holds).

    Safe to call with ``None`` — returns ``None`` unchanged.
    """
    if text is None:
        return text
    if "{{MEMORY_PATH}}" in text:
        memory_dir = Path(cwd) / ".memory"
        memory_dir.mkdir(exist_ok=True)
        text = text.replace("{{MEMORY_PATH}}", str(memory_dir))
    if "{{WORKING_DIRECTORY}}" in text:
        text = text.replace("{{WORKING_DIRECTORY}}", cwd)
    return text


def load_default_prompt(file_path: str = "") -> None:
    """Load the default system prompt from *file_path*.

    Also restores any previously persisted admin override.
    Placeholders like ``{{LANGUAGE}}`` are resolved at load time.

    * If *file_path* is empty/blank, preset mode is used (no custom prompt).
    * If the file does not exist, ``FileNotFoundError`` is raised (fail-fast).
    """
    global _default_prompt, _default_prompt_raw, _runtime_prompt, _runtime_prompt_raw

    if not file_path or not file_path.strip():
        _default_prompt = None
        _default_prompt_raw = None
        logger.info("System prompt: using claude_code preset (no file configured)")
    else:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"SYSTEM_PROMPT_FILE not found: {file_path}")

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            _default_prompt = None
            _default_prompt_raw = None
            logger.warning("System prompt file is empty, falling back to preset mode")
        else:
            _default_prompt_raw = content
            _default_prompt = _resolve_placeholders(content)
            logger.info("System prompt: loaded from file (%d chars)", len(_default_prompt))

    # Restore persisted admin override
    persisted = _load_persisted()
    if persisted:
        resolved = _resolve_placeholders(persisted)
        with _lock:
            _runtime_prompt_raw = persisted
            _runtime_prompt = resolved
        logger.info("System prompt: restored persisted override (%d chars)", len(resolved))


def get_system_prompt() -> Optional[str]:
    """Return the active base system prompt.

    Returns ``None`` when in preset mode (no custom prompt active).
    """
    with _lock:
        if _runtime_prompt is not None:
            return _runtime_prompt
    return _default_prompt


def get_raw_system_prompt() -> Optional[str]:
    """Return the active prompt with original ``{{PLACEHOLDER}}`` tokens intact.

    Used by the admin UI so editors see placeholders, not resolved values.
    """
    with _lock:
        if _runtime_prompt_raw is not None:
            return _runtime_prompt_raw
    return _default_prompt_raw


def set_system_prompt(
    text: str,
    *,
    active_name: Optional[str] = None,
    active_version: Optional[int] = None,
    deployed_by: Optional[str] = None,
) -> None:
    """Set a runtime override for the system prompt and persist to disk.

    *active_name*/*active_version* record which library version backs the
    override (``prompt_library.deploy``); a direct edit leaves both unset.

    Raises ``ValueError`` if *text* is empty or whitespace-only.
    Raises ``OSError`` if the persist file cannot be written.
    """
    global _runtime_prompt, _runtime_prompt_raw
    stripped = text.strip()
    if not stripped:
        raise ValueError("System prompt cannot be empty. Use reset to revert to default.")
    meta = {
        "version": active_version,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "deployed_by": deployed_by,
    }
    _save_persisted(stripped, active_name=active_name, meta=meta)
    resolved = _resolve_placeholders(stripped)
    with _lock:
        _runtime_prompt_raw = stripped
        _runtime_prompt = resolved
    logger.info(
        "System prompt: runtime override set (%d chars, name=%s)", len(stripped), active_name
    )


def reset_system_prompt() -> None:
    """Clear the runtime override, reverting to file default or preset.

    Raises ``OSError`` if the persist file cannot be removed.
    """
    global _runtime_prompt, _runtime_prompt_raw
    _save_persisted(None)
    with _lock:
        _runtime_prompt = None
        _runtime_prompt_raw = None
    logger.info("System prompt: runtime override cleared")


def get_prompt_mode() -> str:
    """Return the current prompt mode as a string label."""
    with _lock:
        if _runtime_prompt is not None:
            return "custom"
    if _default_prompt is not None:
        return "file"
    return "preset"


def _load_preset_text() -> Optional[str]:
    """Load the claude_code preset reference from docs/, stripping the markdown header."""
    ref_path = (
        Path(__file__).resolve().parent.parent / "docs" / "claude-code-system-prompt-reference.md"
    )
    if not ref_path.is_file():
        return None
    raw = ref_path.read_text(encoding="utf-8")
    # Strip the markdown front-matter (title, blockquote, hr) — keep only the prompt body
    body = re.sub(r"\A#[^\n]*\n+(?:>[^\n]*\n)*\n*---\n*", "", raw).strip()
    return body or None


def get_preset_text() -> Optional[str]:
    """Return the cached claude_code preset reference text."""
    global _preset_text
    if _preset_text is None:
        _preset_text = _load_preset_text()
    return _preset_text


def get_active_prompt_name() -> Optional[str]:
    """Return the name of the currently active named prompt, or ``None``."""
    with _lock:
        return _active_prompt_name


def _live_ref_locked() -> dict:
    if _runtime_prompt is not None:
        mode = "custom"
    elif _default_prompt is not None:
        mode = "file"
    else:
        mode = "preset"
    ref: dict = {"mode": mode, "name": None, "version": None}
    if mode == "custom":
        ref["name"] = _active_prompt_name
        ref["version"] = _active_meta.get("version") if _active_prompt_name else None
        ref["deployed_at"] = _active_meta.get("deployed_at")
        ref["deployed_by"] = _active_meta.get("deployed_by")
    return ref


def get_live_ref() -> dict:
    """Which prompt is live: ``{mode, name, version, deployed_at?, deployed_by?}``.

    ``name``/``version`` are set only when a library version backs the live
    override; ``mode`` is ``custom`` (override), ``file`` or ``preset``.
    """
    with _lock:
        return _live_ref_locked()


def get_live_snapshot() -> tuple[Optional[str], dict]:
    """The resolved live prompt and its ref, read together (no deploy in between)."""
    with _lock:
        text = _runtime_prompt if _runtime_prompt is not None else _default_prompt
        return text, _live_ref_locked()


# ---------------------------------------------------------------------------
# Named Prompts CRUD
# ---------------------------------------------------------------------------

_PROMPT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _validate_prompt_name(name: str) -> str:
    """Validate and return a sanitised prompt name.

    Raises ``ValueError`` on invalid names.
    """
    stripped = name.strip()
    if not stripped:
        raise ValueError("Prompt name cannot be empty")
    if not _PROMPT_NAME_RE.match(stripped):
        raise ValueError(
            "Prompt name must start with a letter/digit, "
            "contain only letters, digits, hyphens, or underscores, "
            "and be at most 64 characters"
        )
    return stripped


def _prompt_path(name: str) -> Path:
    """Return the file path for a named prompt."""
    return _PROMPTS_DIR / f"{name}.json"


def list_named_prompts() -> list[dict[str, Any]]:
    """Return a list of all saved named prompts (metadata only)."""
    if not _PROMPTS_DIR.is_dir():
        return []
    prompts = []
    for f in sorted(_PROMPTS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            prompts.append(
                {
                    "name": data.get("name", f.stem),
                    "char_count": len(data.get("content", "")),
                    "updated_at": data.get("updated_at"),
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return prompts


def get_named_prompt(name: str) -> Optional[dict]:
    """Load a single named prompt by name. Returns ``None`` if not found."""
    name = _validate_prompt_name(name)
    path = _prompt_path(name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load named prompt %r: %s", name, e)
        return None


def save_named_prompt(name: str, content: str, *, author: Optional[str] = None) -> dict:
    """Create or update a named prompt (legacy upsert). Returns the saved data dict.

    An update appends a new version through ``prompt_library`` instead of
    overwriting, so the legacy API can never destroy history; saving identical
    content is a no-op.

    Raises ``ValueError`` on invalid name or empty content.
    Raises ``OSError`` on write failure.
    """
    from src import prompt_library

    name = _validate_prompt_name(name)
    if not content.strip():
        raise ValueError("Prompt content cannot be empty")
    try:
        return prompt_library.commit_version(name, content, author=author)
    except prompt_library.NoChange:
        return prompt_library.get_prompt(name)
    except prompt_library.PromptNotFound:
        pass
    try:
        return prompt_library.create_prompt(name, content, author=author)
    except prompt_library.PromptExists:
        # Lost a create race: the winner's file exists now, so append to it.
        try:
            return prompt_library.commit_version(name, content, author=author)
        except prompt_library.NoChange:
            return prompt_library.get_prompt(name)


def delete_named_prompt(name: str) -> bool:
    """Delete a named prompt by name. Returns ``True`` if deleted.

    Raises ``ValueError`` on invalid name.
    """
    name = _validate_prompt_name(name)
    path = _prompt_path(name)
    if not path.is_file():
        return False
    path.unlink()
    logger.info("Named prompt deleted: %s", name)

    with _lock:
        active = _active_prompt_name
    if active == name:
        reset_system_prompt()
    return True


def activate_named_prompt(name: str, *, author: Optional[str] = None) -> None:
    """Activate (deploy) the latest version of a named prompt.

    Raises ``ValueError`` if the prompt does not exist or has invalid name.
    Raises ``OSError`` on persist failure.
    """
    from src import prompt_library

    data = get_named_prompt(name)
    if data is None:
        raise ValueError(f"Named prompt not found: {name}")
    latest = prompt_library.get_prompt(name)["versions"][-1]["version"]
    prompt_library.deploy(name, latest, author=author)
