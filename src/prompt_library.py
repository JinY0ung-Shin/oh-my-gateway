"""Versioned system-prompt library: immutable versions, one live pointer, a deploy log.

The shape follows what prompt-management tools converge on (Langfuse, LangSmith,
PromptLayer, Portkey, Vellum): saving a prompt appends an immutable, numbered
version with an author and a change note; *deploying* points the live system
prompt at one existing version; rolling back is deploying an older version. Saving
never changes what new sessions receive, and deploying never writes content.

Storage stays the named-prompt files (``data/prompts/<name>.json``) so the legacy
admin API keeps working: ``content`` is always the latest version's text and a
legacy file without ``versions`` reads as a single version 1. The live override is
still ``system_prompt.set_system_prompt`` — this module only records *which*
version it came from (``active_name`` + ``active_version``) and appends every
live change to ``data/prompt_deployments.jsonl``.

A deploy applies to NEW sessions only: a session snapshots the base prompt on its
first turn (``session_guard``) and the CLI replays it on resume.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import system_prompt

logger = logging.getLogger(__name__)

# The same re-entrant lock serializes prompt-file writes and every live change,
# so a check-then-act (``expected_live``, import's "still untracked?") holds
# against deploys, resets, direct edits and legacy activate/delete alike.
_lock = system_prompt.mutation_lock
MAX_MESSAGE_CHARS = 200
MAX_DESCRIPTION_CHARS = 500
MAX_ACTOR_CHARS = 120
DEPLOY_LOG_LIMIT = 200

# Tokens ``system_prompt`` resolves. Anything else in ``{{...}}`` reaches the
# model verbatim, which is almost always a typo worth flagging before deploy.
KNOWN_PLACEHOLDERS: Dict[str, str] = {
    "LANGUAGE": "응답 언어 (PROMPT_LANGUAGE)",
    "PLATFORM": "서버 OS 이름",
    "SHELL": "서버 셸",
    "OS_VERSION": "서버 OS 버전",
    "WORKING_DIRECTORY": "세션 작업 폴더 (세션마다 다름)",
    "MEMORY_PATH": "세션 메모리 폴더 (세션마다 다름)",
}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class PromptNotFound(LookupError):
    pass


class PromptExists(Exception):
    pass


class NoChange(ValueError):
    """The new content is identical to the latest version."""


class VersionConflict(Exception):
    """The draft was based on a version that is no longer the latest."""

    def __init__(self, latest: int):
        super().__init__(f"latest version is {latest}")
        self.latest = latest


class PromptIsLive(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())[:MAX_ACTOR_CHARS]
    return cleaned or None


def _clip(value: Optional[str], limit: int) -> str:
    return " ".join((value or "").split())[:limit]


def _deploy_log_path() -> Path:
    # Beside the live-override file: whatever relocates that (tests, data dir
    # moves) relocates the history of what was live with it.
    return system_prompt._PERSIST_FILE.parent / "prompt_deployments.jsonl"


def _write_json_atomic(path: Path, data: dict) -> None:
    if isinstance(data.get("versions"), list):
        # ``parent`` is derived on read; store only what a version is.
        data = {
            **data,
            "versions": [{k: v for k, v in e.items() if k != "parent"} for e in data["versions"]],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def version_sha(name: str, version: dict) -> str:
    """A git-style commit id for one version (40 hex; the UI shows the first 7).

    Content-addressed over what the version *is* — prompt, number, text, author,
    time, note — so the id is stable across reads and a legacy file without a
    stored id gets the same one every time.
    """
    payload = "\0".join(
        [
            name,
            str(version.get("version")),
            str(version.get("content") or ""),
            str(version.get("author") or ""),
            str(version.get("created_at") or ""),
            str(version.get("message") or ""),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _normalize(data: dict, name: str) -> dict:
    """Give a stored prompt (legacy or versioned) the versioned shape."""
    versions = data.get("versions")
    if not isinstance(versions, list) or not versions:
        content = str(data.get("content") or "")
        versions = [
            {
                "version": 1,
                "content": content,
                "message": "",
                "author": None,
                "created_at": data.get("updated_at") or data.get("created_at"),
            }
        ]
    clean: List[dict] = []
    for v in versions:
        if isinstance(v, dict) and isinstance(v.get("version"), int):
            entry = {
                "version": v["version"],
                "content": str(v.get("content") or ""),
                "message": str(v.get("message") or ""),
                "author": v.get("author"),
                "created_at": v.get("created_at"),
            }
            stored = v.get("sha")
            entry["sha"] = (
                stored
                if isinstance(stored, str) and re.fullmatch(r"[0-9a-f]{40}", stored)
                else version_sha(data.get("name") or name, entry)
            )
            clean.append(entry)
    clean.sort(key=lambda v: v["version"])
    for i, entry in enumerate(clean):
        entry["parent"] = clean[i - 1]["sha"] if i else None
    latest = clean[-1]
    return {
        "name": data.get("name") or name,
        "description": str(data.get("description") or ""),
        "created_at": data.get("created_at") or latest["created_at"],
        "updated_at": latest["created_at"],
        "content": latest["content"],
        "versions": clean,
    }


def _read(name: str, *, strict: bool = False) -> Optional[dict]:
    """Load a prompt; ``None`` when absent.

    An existing file that cannot be read or parsed is skipped for listing, but a
    writer passes ``strict=True`` and gets ``OSError`` instead of ``None``: writing
    "version 1" over an unreadable file would erase its whole history.
    """
    path = system_prompt._prompt_path(name)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read prompt %r: %s", name, exc)
        if strict:
            raise OSError(f"Prompt {name!r} exists but cannot be read: {exc}") from exc
        return None
    if not isinstance(raw, dict):
        if strict:
            raise OSError(f"Prompt {name!r} exists but is not a prompt document")
        return None
    return _normalize(raw, name)


def _live_for(name: str, ref: Optional[dict] = None) -> Optional[int]:
    ref = ref if ref is not None else system_prompt.get_live_ref()
    if ref.get("mode") == "custom" and ref.get("name") == name:
        return ref.get("version")
    return None


def _summary(data: dict, ref: dict) -> dict:
    latest = data["versions"][-1]
    return {
        "name": data["name"],
        "description": data["description"],
        "latest_version": latest["version"],
        "latest_sha": latest["sha"],
        "version_count": len(data["versions"]),
        "updated_at": latest["created_at"],
        "updated_by": latest["author"],
        "char_count": len(latest["content"]),
        "live_version": _live_for(data["name"], ref),
    }


def list_prompts() -> List[dict]:
    prompts_dir = system_prompt._PROMPTS_DIR
    if not prompts_dir.is_dir():
        return []
    ref = system_prompt.get_live_ref()
    out = []
    for f in sorted(prompts_dir.glob("*.json")):
        data = _read(f.stem)
        if data is not None:
            out.append(_summary(data, ref))
    out.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    return out


def get_prompt(name: str) -> dict:
    name = system_prompt._validate_prompt_name(name)
    data = _read(name)
    if data is None:
        raise PromptNotFound(name)
    data["live_version"] = _live_for(name)
    return data


def create_prompt(
    name: str,
    content: str,
    *,
    description: str = "",
    message: str = "",
    author: Optional[str] = None,
) -> dict:
    """Create a prompt with version 1. Raises ``PromptExists`` for a taken name."""
    name = system_prompt._validate_prompt_name(name)
    content = content.strip()
    if not content:
        raise ValueError("Prompt content cannot be empty")
    now = _now()
    data = {
        "name": name,
        "description": _clip(description, MAX_DESCRIPTION_CHARS),
        "created_at": now,
        "updated_at": now,
        "content": content,
        "versions": [
            {
                "version": 1,
                "content": content,
                "message": _clip(message, MAX_MESSAGE_CHARS) or "처음 만듦",
                "author": _actor(author),
                "created_at": now,
            }
        ],
    }
    data["versions"][0]["sha"] = version_sha(name, data["versions"][0])
    path = system_prompt._prompt_path(name)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same create-only publish as ``named_prompt_create``: a complete temp file
        # hard-linked into place, so a concurrent creator can never be replaced.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError as exc:
                raise PromptExists(name) from exc
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    logger.info("Prompt created: %s v1 (%d chars)", name, len(content))
    return get_prompt(name)


def commit_version(
    name: str,
    content: str,
    *,
    message: str = "",
    author: Optional[str] = None,
    base_version: Optional[int] = None,
) -> dict:
    """Append a new immutable version. Returns the prompt.

    ``base_version`` is the version the editor started from: when someone else
    committed in between, ``VersionConflict`` is raised instead of silently
    stacking an edit that never saw theirs. Identical content raises ``NoChange``.
    """
    name = system_prompt._validate_prompt_name(name)
    content = content.strip()
    if not content:
        raise ValueError("Prompt content cannot be empty")
    with _lock:
        data = _read(name, strict=True)
        if data is None:
            raise PromptNotFound(name)
        latest = data["versions"][-1]
        if base_version is not None and base_version != latest["version"]:
            raise VersionConflict(latest["version"])
        if latest["content"] == content:
            raise NoChange(f"identical to version {latest['version']}")
        version = latest["version"] + 1
        now = _now()
        entry = {
            "version": version,
            "content": content,
            "message": _clip(message, MAX_MESSAGE_CHARS),
            "author": _actor(author),
            "created_at": now,
        }
        entry["sha"] = version_sha(name, entry)
        data["versions"].append(entry)
        data["content"] = content
        data["updated_at"] = now
        _write_json_atomic(system_prompt._prompt_path(name), data)
    logger.info("Prompt %s: version %d committed (%d chars)", name, version, len(content))
    return get_prompt(name)


def update_description(name: str, description: str) -> dict:
    name = system_prompt._validate_prompt_name(name)
    with _lock:
        data = _read(name, strict=True)
        if data is None:
            raise PromptNotFound(name)
        data["description"] = _clip(description, MAX_DESCRIPTION_CHARS)
        _write_json_atomic(system_prompt._prompt_path(name), data)
    return get_prompt(name)


def delete_prompt(name: str) -> None:
    """Delete a prompt and its history. Refuses while one of its versions is live."""
    name = system_prompt._validate_prompt_name(name)
    with _lock:
        if _live_for(name) is not None or system_prompt.get_live_ref().get("name") == name:
            raise PromptIsLive(name)
        path = system_prompt._prompt_path(name)
        if not path.is_file():
            raise PromptNotFound(name)
        path.unlink()
    logger.info("Prompt deleted: %s", name)


def _log_deploy(entry: dict) -> None:
    path = _deploy_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def deploy(
    name: str,
    version: int,
    *,
    author: Optional[str] = None,
    note: str = "",
) -> dict:
    """Point the live system prompt at ``name@version`` (new sessions only)."""
    with _lock:
        data = get_prompt(name)
        match = next((v for v in data["versions"] if v["version"] == version), None)
        if match is None:
            raise PromptNotFound(f"{name}@{version}")
        before = system_prompt.get_live_ref()
        system_prompt.set_system_prompt(
            match["content"],
            active_name=data["name"],
            active_version=version,
            deployed_by=_actor(author),
        )
        action = "deploy"
        if before.get("name") == data["name"] and isinstance(before.get("version"), int):
            if version < before["version"]:
                action = "rollback"
        entry = {
            "at": _now(),
            "action": action,
            "name": data["name"],
            "version": version,
            "sha": match["sha"],
            "by": _actor(author),
            "note": _clip(note, MAX_MESSAGE_CHARS),
            "from": _ref_label(before),
            "char_count": len(match["content"]),
        }
        try:
            _log_deploy(entry)
        except OSError:
            # The live change already happened; losing one history line must not
            # turn a successful deploy into a failure the operator retries.
            logger.warning("Prompt deploy log write failed", exc_info=True)
    logger.info("Prompt deployed: %s v%d (%s)", data["name"], version, action)
    return entry


def reset_to_default(*, author: Optional[str] = None, note: str = "") -> dict:
    """Drop the override: new sessions get the file default or the built-in preset."""
    with _lock:
        before = system_prompt.get_live_ref()
        system_prompt.reset_system_prompt()
        after = system_prompt.get_live_ref()
        entry = {
            "at": _now(),
            "action": "reset",
            "name": None,
            "version": None,
            "mode": after["mode"],
            "by": _actor(author),
            "note": _clip(note, MAX_MESSAGE_CHARS),
            "from": _ref_label(before),
        }
        try:
            _log_deploy(entry)
        except OSError:
            logger.warning("Prompt deploy log write failed", exc_info=True)
    return entry


def record_direct_edit(
    author: Optional[str], char_count: int, before: Optional[dict] = None
) -> None:
    """Log a legacy untracked edit of the live prompt (``PUT /api/system-prompt``).

    ``before`` is the live ref the edit replaced (read under ``mutation_lock``).
    """
    try:
        _log_deploy(
            {
                "at": _now(),
                "action": "direct",
                "name": None,
                "version": None,
                "by": _actor(author),
                "note": "",
                "from": _ref_label(before) if before else None,
                "char_count": char_count,
            }
        )
    except OSError:
        logger.warning("Prompt deploy log write failed", exc_info=True)


def _ref_label(ref: dict) -> Optional[str]:
    if ref.get("name") and ref.get("version"):
        return f"{ref['name']}@v{ref['version']}"
    if ref.get("mode") == "custom":
        return "untracked"
    return ref.get("mode")


def list_deployments(limit: int = 50) -> List[dict]:
    path = _deploy_log_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[dict] = []
    for line in reversed(lines[-DEPLOY_LOG_LIMIT:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def import_live(name: str, *, description: str = "", author: Optional[str] = None) -> dict:
    """Adopt the current untracked live override as ``name`` v1 without changing it."""
    with _lock:
        # One transaction under the mutation lock: read the live override, create
        # v1 from it and re-point live at it. No deploy can land in between and be
        # silently overwritten by the older text read here.
        text = system_prompt.get_raw_system_prompt()
        ref = system_prompt.get_live_ref()
        if ref.get("mode") != "custom" or not text:
            raise ValueError("Live prompt is not a custom override")
        if ref.get("name") and ref.get("version"):
            raise ValueError("Live prompt already belongs to a library prompt")
        created = create_prompt(
            name,
            text,
            description=description,
            message="적용 중이던 프롬프트를 가져옴",
            author=author,
        )
        # Same text, now pointed at its library version — what new sessions
        # receive is unchanged, but the live ref moved, so it is logged.
        system_prompt.set_system_prompt(
            text, active_name=created["name"], active_version=1, deployed_by=ref.get("deployed_by")
        )
        try:
            _log_deploy(
                {
                    "at": _now(),
                    "action": "import",
                    "name": created["name"],
                    "version": 1,
                    "sha": created["versions"][0]["sha"],
                    "by": _actor(author),
                    "note": "",
                    "from": "untracked",
                    "char_count": len(text),
                }
            )
        except OSError:
            logger.warning("Prompt deploy log write failed", exc_info=True)
    return get_prompt(created["name"])


def analyze(content: str, sample_cwd: str = "/workspace/<session>") -> dict:
    """Placeholder report + a rendered preview with sample per-session values."""
    found: List[dict] = []
    seen = set()
    for match in _PLACEHOLDER_RE.finditer(content or ""):
        token = match.group(1)
        if token in seen:
            continue
        seen.add(token)
        found.append(
            {
                "name": token,
                "known": token in KNOWN_PLACEHOLDERS,
                "label": KNOWN_PLACEHOLDERS.get(token, ""),
                "exact": match.group(0) == "{{" + token + "}}",
            }
        )
    rendered = system_prompt._resolve_placeholders(content or "")
    rendered = rendered.replace("{{MEMORY_PATH}}", f"{sample_cwd}/.memory")
    rendered = rendered.replace("{{WORKING_DIRECTORY}}", sample_cwd)
    return {
        "placeholders": found,
        "known": [{"name": k, "label": v} for k, v in KNOWN_PLACEHOLDERS.items()],
        "rendered": rendered,
        "char_count": len(content or ""),
    }


def default_prompt_info() -> Dict[str, Any]:
    """What a reset falls back to: the file default, else the built-in preset."""
    raw = system_prompt._default_prompt_raw
    if raw:
        return {"mode": "file", "content": raw}
    return {"mode": "preset", "content": system_prompt.get_preset_text() or ""}
