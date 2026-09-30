"""Admin API for the versioned system-prompt library (``src/prompt_library.py``).

Everything lives under ``/admin/api/prompt-library``. Actions are attributed to the
``X-Admin-Actor`` header when a trusted admin front end (the ChatDRAGON BFF, which
authenticates with the admin key) forwards the operator's name; the gateway's own
dashboard falls back to ``gateway-admin``.

``POST .../try`` runs one throwaway turn against a candidate prompt in a temporary
workspace — the same isolation as ``/v1/agents/messages`` — so an operator can see
how a draft behaves before deploying it to every new session.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src import prompt_library, system_prompt
from src.admin_auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

_PREFIX = "/api/prompt-library"
_DEFAULT_ACTOR = "gateway-admin"
MAX_PROMPT_CHARS = 200_000
MAX_TRY_MESSAGE_CHARS = 8_000


class CreateBody(BaseModel):
    name: str
    content: str = Field(max_length=MAX_PROMPT_CHARS)
    description: str = ""
    message: str = ""


class VersionBody(BaseModel):
    content: str = Field(max_length=MAX_PROMPT_CHARS)
    message: str = ""
    base_version: Optional[int] = None


class DeployBody(BaseModel):
    version: int
    note: str = ""
    # The live ref the operator confirmed against ("name@vN", "untracked", "file",
    # "preset"). A mismatch means someone else changed live meanwhile → 409.
    expected_live: Optional[str] = None


class DescriptionBody(BaseModel):
    description: str = ""


class ResetBody(BaseModel):
    note: str = ""
    expected_live: Optional[str] = None


class ImportBody(BaseModel):
    name: str
    description: str = ""


class AnalyzeBody(BaseModel):
    content: str = Field(max_length=MAX_PROMPT_CHARS)


class TryBody(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_TRY_MESSAGE_CHARS)
    # ``content`` omitted → run against the live prompt (for side-by-side compare).
    content: Optional[str] = Field(default=None, max_length=MAX_PROMPT_CHARS)
    model: Optional[str] = None


def _actor(request: Request) -> str:
    # Front ends percent-encode non-ASCII names (headers carry latin-1 only).
    value = unquote(request.headers.get("x-admin-actor", ""))
    return " ".join(value.split())[:120] or _DEFAULT_ACTOR


def _err(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, "code": code, **extra})


def _live_label(ref: Dict[str, Any]) -> str:
    return prompt_library._ref_label(ref) or "preset"


def _live_view() -> Dict[str, Any]:
    ref = system_prompt.get_live_ref()
    view = dict(ref)
    view["label"] = _live_label(ref)
    view["char_count"] = len(system_prompt.get_raw_system_prompt() or "")
    if ref.get("name") and ref.get("version"):
        try:
            prompt = prompt_library.get_prompt(ref["name"])
            match = next((v for v in prompt["versions"] if v["version"] == ref["version"]), None)
            view["sha"] = match["sha"] if match else None
        except (prompt_library.PromptNotFound, ValueError):
            view["sha"] = None
    if ref.get("mode") == "custom" and not ref.get("name"):
        # Untracked override (legacy direct edit): expose the text so it can be
        # imported into the library instead of lost on the next deploy.
        view["content"] = system_prompt.get_raw_system_prompt()
    return view


def _session_usage() -> Dict[str, Any]:
    from src.session_manager import session_manager

    counts: Dict[str, int] = {}
    total = 0
    # Copy under the manager's lock: cleanup/eviction mutate the dict under it,
    # and this sync route runs in the threadpool alongside them.
    with session_manager.lock:
        sessions = list(session_manager.sessions.values())
    for session in sessions:
        if session.is_expired():
            continue
        total += 1
        ref = session.base_prompt_ref
        label = _live_label(ref) if isinstance(ref, dict) else "unknown"
        counts[label] = counts.get(label, 0) + 1
    return {"total": total, "by_ref": counts}


@router.get(_PREFIX)
def library_overview(_=Depends(require_admin)):
    return {
        "live": _live_view(),
        "prompts": prompt_library.list_prompts(),
        "default": prompt_library.default_prompt_info(),
        "sessions": _session_usage(),
        "placeholders": [
            {"name": k, "label": v} for k, v in prompt_library.KNOWN_PLACEHOLDERS.items()
        ],
    }


@router.get(f"{_PREFIX}/deployments")
def library_deployments(limit: int = 50, _=Depends(require_admin)):
    return {"deployments": prompt_library.list_deployments(max(1, min(limit, 200)))}


@router.post(f"{_PREFIX}/analyze")
def library_analyze(body: AnalyzeBody, _=Depends(require_admin)):
    return prompt_library.analyze(body.content)


@router.post(f"{_PREFIX}/prompts")
def library_create(body: CreateBody, request: Request, _=Depends(require_admin)):
    try:
        return prompt_library.create_prompt(
            body.name,
            body.content,
            description=body.description,
            message=body.message,
            author=_actor(request),
        )
    except prompt_library.PromptExists:
        return _err(409, "exists", f"Prompt already exists: {body.name}")
    except ValueError as exc:
        return _err(422, "invalid", str(exc))
    except OSError as exc:
        return _err(500, "io", f"Failed to save: {exc}")


@router.get(f"{_PREFIX}/prompts/{{name}}")
def library_get(name: str, _=Depends(require_admin)):
    try:
        return prompt_library.get_prompt(name)
    except prompt_library.PromptNotFound:
        return _err(404, "not_found", f"Prompt not found: {name}")
    except ValueError as exc:
        return _err(422, "invalid", str(exc))


@router.patch(f"{_PREFIX}/prompts/{{name}}")
def library_describe(name: str, body: DescriptionBody, _=Depends(require_admin)):
    try:
        return prompt_library.update_description(name, body.description)
    except prompt_library.PromptNotFound:
        return _err(404, "not_found", f"Prompt not found: {name}")
    except ValueError as exc:
        return _err(422, "invalid", str(exc))
    except OSError as exc:
        return _err(500, "io", f"Failed to save: {exc}")


@router.delete(f"{_PREFIX}/prompts/{{name}}")
def library_delete(name: str, _=Depends(require_admin)):
    try:
        prompt_library.delete_prompt(name)
    except prompt_library.PromptIsLive:
        return _err(409, "live", "This prompt is live. Deploy another prompt or reset first.")
    except prompt_library.PromptNotFound:
        return _err(404, "not_found", f"Prompt not found: {name}")
    except ValueError as exc:
        return _err(422, "invalid", str(exc))
    except OSError as exc:
        return _err(500, "io", f"Failed to delete: {exc}")
    return {"status": "deleted", "name": name}


@router.post(f"{_PREFIX}/prompts/{{name}}/versions")
def library_commit(name: str, body: VersionBody, request: Request, _=Depends(require_admin)):
    try:
        return prompt_library.commit_version(
            name,
            body.content,
            message=body.message,
            author=_actor(request),
            base_version=body.base_version,
        )
    except prompt_library.VersionConflict as exc:
        return _err(
            409,
            "conflict",
            f"Version {exc.latest} was saved after this draft was opened.",
            latest_version=exc.latest,
        )
    except prompt_library.NoChange as exc:
        return _err(422, "no_change", str(exc))
    except prompt_library.PromptNotFound:
        return _err(404, "not_found", f"Prompt not found: {name}")
    except ValueError as exc:
        return _err(422, "invalid", str(exc))
    except OSError as exc:
        return _err(500, "io", f"Failed to save: {exc}")


def _live_changed(expected: Optional[str]) -> Optional[JSONResponse]:
    if expected is None:
        return None
    current = _live_label(system_prompt.get_live_ref())
    if current != expected:
        return _err(409, "live_changed", f"Live prompt changed to {current}.", live=current)
    return None


@router.post(f"{_PREFIX}/prompts/{{name}}/deploy")
def library_deploy(name: str, body: DeployBody, request: Request, _=Depends(require_admin)):
    with prompt_library._lock:
        conflict = _live_changed(body.expected_live)
        if conflict is not None:
            return conflict
        try:
            entry = prompt_library.deploy(
                name, body.version, author=_actor(request), note=body.note
            )
        except prompt_library.PromptNotFound:
            return _err(404, "not_found", f"Version not found: {name}@{body.version}")
        except ValueError as exc:
            return _err(422, "invalid", str(exc))
        except OSError as exc:
            return _err(500, "io", f"Failed to deploy: {exc}")
    return {"deployment": entry, "live": _live_view()}


@router.post(f"{_PREFIX}/reset")
def library_reset(body: ResetBody, request: Request, _=Depends(require_admin)):
    with prompt_library._lock:
        conflict = _live_changed(body.expected_live)
        if conflict is not None:
            return conflict
        try:
            entry = prompt_library.reset_to_default(author=_actor(request), note=body.note)
        except OSError as exc:
            return _err(500, "io", f"Failed to reset: {exc}")
    return {"deployment": entry, "live": _live_view()}


@router.post(f"{_PREFIX}/import-live")
def library_import_live(body: ImportBody, request: Request, _=Depends(require_admin)):
    try:
        prompt = prompt_library.import_live(
            body.name, description=body.description, author=_actor(request)
        )
    except prompt_library.PromptExists:
        return _err(409, "exists", f"Prompt already exists: {body.name}")
    except ValueError as exc:
        return _err(422, "invalid", str(exc))
    except OSError as exc:
        return _err(500, "io", f"Failed to import: {exc}")
    return {"prompt": prompt, "live": _live_view()}


# ---------------------------------------------------------------------------
# Try a prompt: one throwaway turn in a temporary workspace
# ---------------------------------------------------------------------------


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


_KIND_BY_CLASS = {"AssistantMessage": "assistant", "ResultMessage": "result"}


def _events_for(message: Any) -> list:
    """Reduce one backend message to the few events the try pane renders.

    ``run_completion_with_client`` yields converted dicts (``{"type": "assistant",
    "content": [...]}``) whose blocks may still be SDK objects; raw SDK messages
    are accepted too.
    """
    if isinstance(message, dict):
        kind = message.get("type")
        if kind == "error":
            return [{"type": "error", "message": str(message.get("error") or "backend error")}]
    else:
        kind = _KIND_BY_CLASS.get(type(message).__name__)
    if kind == "assistant":
        out = []
        for block in _attr(message, "content", []) or []:
            bkind = _attr(block, "type") or type(block).__name__
            if bkind in ("text", "TextBlock") and _attr(block, "text"):
                out.append({"type": "text", "text": _attr(block, "text")})
            elif bkind in ("tool_use", "ToolUseBlock"):
                out.append({"type": "tool", "name": _attr(block, "name", "")})
        return out
    if kind == "result":
        usage = _attr(message, "usage") or {}
        event = {
            "type": "result",
            "is_error": bool(_attr(message, "is_error", False)),
            "num_turns": _attr(message, "num_turns"),
            "input_tokens": usage.get("input_tokens") if isinstance(usage, dict) else None,
            "output_tokens": usage.get("output_tokens") if isinstance(usage, dict) else None,
        }
        if event["is_error"]:
            event["message"] = str(_attr(message, "error_message") or _attr(message, "result") or "")
        return [event]
    return []


async def _run_try(body: TryBody, resolved, backend) -> AsyncIterator[str]:
    from src.mcp_config import get_mcp_servers
    from src.routes.agent_messages import (
        _cleanup_sdk_transcript,
        _disconnect_client,
        _spawn_cancelled_turn_teardown,
    )
    from src.session_manager import Session
    from src.workspace_manager import workspace_manager

    started = time.monotonic()
    workspace = client = session = None
    cancelled = False
    try:
        workspace = workspace_manager.resolve(None, backend="claude")
        if body.content is None:
            base_raw = system_prompt.get_system_prompt()
        else:
            base_raw = system_prompt._resolve_placeholders(body.content.strip())
        base = system_prompt.resolve_request_placeholders(base_raw, str(workspace))
        session = Session(session_id=str(uuid.uuid4()), backend="claude", workspace=str(workspace))
        yield _sse(
            {
                "type": "start",
                "model": resolved.public_model,
                "prompt": "live" if body.content is None else "draft",
                "char_count": len(base or ""),
            }
        )
        client = await backend.create_client(
            session=session,
            model=resolved.provider_model,
            disallowed_tools=["AskUserQuestion"],
            permission_mode=os.getenv("PERMISSION_MODE") or None,
            mcp_servers=get_mcp_servers(),
            cwd=str(workspace),
            _custom_base=base,
        )
        session.client = client
        async for message in backend.run_completion_with_client(client, body.message, session):
            for event in _events_for(message):
                yield _sse(event)
        yield _sse({"type": "done", "duration_ms": int((time.monotonic() - started) * 1000)})
    except (asyncio.CancelledError, GeneratorExit):
        cancelled = True
        raise
    except Exception as exc:  # noqa: BLE001 — surface to the operator, never a 500 mid-stream
        logger.warning("Prompt try run failed", exc_info=True)
        yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        if session is not None:
            session.client = None
        if cancelled:
            _spawn_cancelled_turn_teardown(client, session, workspace)
        else:
            if client is not None:
                await _disconnect_client(client)
            _cleanup_sdk_transcript(session)
            if workspace is not None:
                workspace_manager.cleanup_temp_workspace(workspace)


@router.post(f"{_PREFIX}/try")
async def library_try(body: TryBody, _=Depends(require_admin)):
    from fastapi import HTTPException

    from src.constants import DEFAULT_MODEL, SSE_KEEPALIVE_INTERVAL
    from src import streaming_utils
    from src.routes.deps import resolve_and_get_backend, validate_backend_auth_or_raise

    if body.content is not None and not body.content.strip():
        return _err(422, "invalid", "Prompt content cannot be empty")
    try:
        resolved, backend = resolve_and_get_backend(body.model or DEFAULT_MODEL)
        if resolved.backend != "claude":
            return _err(400, "unsupported", "Prompt try runs support Claude models only")
        validate_backend_auth_or_raise("claude")
    except HTTPException as exc:
        return _err(exc.status_code, "unavailable", str(exc.detail))
    stream = streaming_utils._keepalive_wrapper(
        _run_try(body, resolved, backend), SSE_KEEPALIVE_INTERVAL
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
