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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PERSIST_FILE = _DATA_DIR / "system_prompt.json"
_PROMPTS_DIR = _DATA_DIR / "prompts"


def _load_persisted() -> Optional[str]:
    """Load the persisted admin override from disk."""
    global _active_prompt_name
    if not _PERSIST_FILE.is_file():
        return None
    try:
        data = json.loads(_PERSIST_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("Persisted system prompt has invalid structure, ignoring")
            return None
        with _lock:
            _active_prompt_name = data.get("active_name")
        value = data.get("prompt")
        if not isinstance(value, str) or not value.strip():
            logger.warning("Persisted system prompt has invalid value, ignoring")
            return None
        return value
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load persisted system prompt: %s", e)
        return None


def _save_persisted(text: Optional[str], *, active_name: Optional[str] = None) -> None:
    """Save or delete the persisted admin override.

    Raises ``OSError`` on failure so callers can avoid in-memory/disk divergence.

    The lock is held across the file I/O so concurrent callers cannot observe
    a memory/disk mismatch, and ``_active_prompt_name`` is only updated after
    the file mutation succeeds (file becomes the source of truth).
    """
    global _active_prompt_name
    if text is None:
        with _lock:
            if _PERSIST_FILE.is_file():
                _PERSIST_FILE.unlink()
                logger.info("System prompt: persisted file removed")
            _active_prompt_name = None
    else:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict = {"prompt": text}
        if active_name:
            payload["active_name"] = active_name
        with _lock:
            _PERSIST_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _active_prompt_name = active_name
            logger.info("System prompt: persisted to %s", _PERSIST_FILE)


def _resolve_placeholders(text: str) -> str:
    """Replace ``{{PLACEHOLDER}}`` tokens with runtime values.

    ``{{WORKING_DIRECTORY}}`` is intentionally left unresolved here because
    the actual working directory varies per-user workspace.  Use
    :func:`resolve_cwd_placeholder` at request time to fill it in.
    """
    from src.constants import PROMPT_LANGUAGE, PROMPT_MEMORY_PATH

    replacements = {
        "LANGUAGE": PROMPT_LANGUAGE,
        "PLATFORM": platform.system().lower(),
        "SHELL": os.environ.get("SHELL", ""),
        "OS_VERSION": platform.platform(),
        "MEMORY_PATH": PROMPT_MEMORY_PATH,
    }
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def resolve_cwd_placeholder(text: Optional[str], cwd: str) -> Optional[str]:
    """Replace ``{{WORKING_DIRECTORY}}`` with the actual per-user *cwd*.

    Safe to call with ``None`` — returns ``None`` unchanged.
    """
    if text is None or "{{WORKING_DIRECTORY}}" not in text:
        return text
    return text.replace("{{WORKING_DIRECTORY}}", cwd)


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


def set_system_prompt(text: str, *, active_name: Optional[str] = None) -> None:
    """Set a runtime override for the system prompt and persist to disk.

    Raises ``ValueError`` if *text* is empty or whitespace-only.
    Raises ``OSError`` if the persist file cannot be written.
    """
    global _runtime_prompt, _runtime_prompt_raw
    stripped = text.strip()
    if not stripped:
        raise ValueError("System prompt cannot be empty. Use reset to revert to default.")
    _save_persisted(stripped, active_name=active_name)
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


def save_named_prompt(name: str, content: str) -> dict:
    """Create or update a named prompt. Returns the saved data dict.

    Raises ``ValueError`` on invalid name or empty content.
    Raises ``OSError`` on write failure.
    """
    name = _validate_prompt_name(name)
    content = content.strip()
    if not content:
        raise ValueError("Prompt content cannot be empty")

    _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _prompt_path(name)

    existing = None
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "content": content,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Named prompt saved: %s (%d chars)", name, len(content))
    return data


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


def activate_named_prompt(name: str) -> None:
    """Activate a named prompt as the current system prompt.

    Raises ``ValueError`` if the prompt does not exist or has invalid name.
    Raises ``OSError`` on persist failure.
    """
    data = get_named_prompt(name)
    if data is None:
        raise ValueError(f"Named prompt not found: {name}")
    set_system_prompt(data["content"], active_name=data["name"])
