"""Chat Completions response -> OpenAI Responses object translation (#173 checkpoint-2A PR-2).

Pure, HTTP-independent. The *response half* of the Codex data-plane bridge: the
request half (``request.py``) turned a Responses request into a chat request;
this turns the chat/completions RESPONSE back into the lean Responses object the
Codex runtime consumes. Ported from the reference converter
(``JinY0ung-Shin/UniBridge`` ``llm-converter/app/responses_bridge.py``) but
retargeted to THIS gateway's Responses object shape (``src/response_models.py``)
and hardened to the fail-closed contract (an unrepresentable response shape is
refused, never silently degraded).

Objects are built as PLAIN DICTS (not the pydantic models) for two reasons: a
``function_call`` item must carry a response-path-only ``namespace`` overlay that
``FunctionCallOutputItem`` cannot hold (Codex routes a call by ``{namespace,
name}``), and the streaming half emits the same dict shapes. The dicts mirror the
local models exactly, minus ``None`` fields (the local SSE builder dumps with
``exclude_none=True``).

Deliberately deferred (checkpoint-2, as in PR-1): the ``previous_response_id``
conversation store, and any live HTTP route -- this is pure translation.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from .errors import BridgeCapabilityError


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce an upstream token counter to int, guarding ``null``/garbage.

    A strict Codex client 500s on a null counter, and vLLM/SGLang occasionally
    emit one, so every counter passes through here rather than straight from the
    upstream payload.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _new_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def _new_reasoning_id() -> str:
    return f"rs_{uuid.uuid4().hex}"


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def _finish_to_status(
    finish: Any, length_as_completed: bool = False
) -> tuple[str, Optional[dict]]:
    """Map a chat ``finish_reason`` to a Responses ``(status, incomplete_details)``.

    ``length`` -> ``incomplete`` with ``max_output_tokens`` (or ``completed`` when
    the caller opts into Codex length-as-completed, which avoids Codex re-sending
    a whole truncated turn); ``content_filter`` -> ``incomplete``; everything else
    (``stop``/``tool_calls``/``function_call``/absent) -> ``completed``.
    """
    if finish == "length":
        if length_as_completed:
            return "completed", None
        return "incomplete", {"reason": "max_output_tokens"}
    if finish == "content_filter":
        return "incomplete", {"reason": "content_filter"}
    return "completed", None


def _usage_to_responses(usage: Any) -> dict:
    """Map chat ``usage`` to the local ``ResponseUsage`` shape.

    ``total_tokens`` is DERIVED (never copied from upstream), matching the local
    model's ``model_validator``. ``output_tokens_details`` is intentionally
    omitted (Claude folds thinking into ``output_tokens``); ``cache_creation_
    tokens`` has no chat equivalent (0); ``context_tokens`` is ``None`` -> omitted.
    """
    if not isinstance(usage, dict):
        usage = {}
    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
        "input_tokens_details": {
            "cached_tokens": cached,
            "cache_creation_tokens": 0,
        },
    }


def _response_object(
    *,
    response_id: str,
    model: str,
    status: str,
    output: list[dict],
    usage: dict,
    metadata: Optional[dict],
    created_at: int,
    incomplete_details: Optional[dict] = None,
    error: Optional[dict] = None,
) -> dict:
    """Build the lean local Responses object (no request echo), ``None`` omitted."""
    obj: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "usage": usage,
        "metadata": metadata or {},
    }
    if incomplete_details is not None:
        obj["incomplete_details"] = incomplete_details
    if error is not None:
        obj["error"] = error
    return obj


def _ns_for(name: str, namespace_map: Optional[dict[str, str]]) -> Optional[str]:
    """The namespace a flattened function name belongs to, or ``None``.

    Codex routes a call by ``{namespace, name}`` but a chat tool call carries only
    ``function.name``; PR-1's ``namespace_map_from_tools`` records the mapping so
    the response path can re-stamp it. An unmapped call stays spec-clean.
    """
    if not namespace_map or not name:
        return None
    return namespace_map.get(name) or None


def _function_call_item(
    tc: Any, *, item_status: str, namespace_map: Optional[dict[str, str]]
) -> dict:
    """Translate one chat ``tool_calls`` entry to a Responses ``function_call`` item.

    ``id`` follows the local ``fc_<call_id>`` convention; a missing upstream id is
    minted once (some vLLM/SGLang models omit it) and used for both the item id
    and ``call_id`` so it round-trips within this response. Non-serializable
    arguments are refused rather than dropped.
    """
    if not isinstance(tc, dict):
        raise BridgeCapabilityError("chat tool_call must be an object")
    fn = tc.get("function")
    if not isinstance(fn, dict):
        raise BridgeCapabilityError("chat tool_call is missing its 'function' object")
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise BridgeCapabilityError("chat tool_call function is missing its name")
    call_id = tc.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = _new_call_id()
    args = fn.get("arguments")
    if not isinstance(args, str):
        try:
            args = json.dumps(args if args is not None else {}, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise BridgeCapabilityError(
                "chat tool_call arguments are not JSON-serializable; refusing "
                "rather than emitting a corrupt tool call"
            ) from exc
    item: dict[str, Any] = {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": item_status,
        "call_id": call_id,
        "name": name,
        "arguments": args,
    }
    ns = _ns_for(name, namespace_map)
    if ns:
        item["namespace"] = ns
    return item


def _message_output_items(
    message: Any,
    *,
    item_status: str,
    emit_reasoning: bool,
    namespace_map: Optional[dict[str, str]],
) -> list[dict]:
    """Build the ordered output items for one assistant message: reasoning ->
    message -> function_calls (the order Codex expects)."""
    if not isinstance(message, dict):
        raise BridgeCapabilityError("chat choice 'message' must be an object")
    items: list[dict] = []

    reasoning = message.get("reasoning_content")
    if emit_reasoning and isinstance(reasoning, str) and reasoning:
        items.append(
            {
                "id": _new_reasoning_id(),
                "type": "reasoning",
                "status": item_status,
                "summary": [{"type": "summary_text", "text": reasoning}],
                "content": [{"type": "reasoning_text", "text": reasoning}],
            }
        )

    if message.get("refusal") is not None:
        # The local Responses contract has no refusal content part or refusal
        # event, so a refusal cannot be carried faithfully -- refuse rather than
        # silently dropping the model's refusal (a fail-closed seam).
        raise BridgeCapabilityError(
            "chat message carries a 'refusal', which has no local Responses "
            "representation; refusing rather than dropping it"
        )

    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise BridgeCapabilityError(
            "chat assistant message 'content' must be a string or null, got "
            f"{type(content).__name__}"
        )
    if isinstance(content, str) and content:
        items.append(
            {
                "id": _new_msg_id(),
                "type": "message",
                "role": "assistant",
                "status": item_status,
                "content": [
                    {"type": "output_text", "text": content, "annotations": []}
                ],
            }
        )

    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise BridgeCapabilityError("chat message 'tool_calls' must be a list")
        for tc in tool_calls:
            items.append(
                _function_call_item(
                    tc, item_status=item_status, namespace_map=namespace_map
                )
            )
    return items


def chat_response_to_responses_body(
    chat: dict,
    *,
    response_id: str,
    model: str = "",
    created_at: Optional[int] = None,
    metadata: Optional[dict] = None,
    emit_reasoning: bool = True,
    length_as_completed: bool = False,
    namespace_map: Optional[dict[str, str]] = None,
) -> dict:
    """Translate a non-streaming chat completion response to a Responses object.

    Returns a plain dict (the lean local object shape). Raises
    :class:`BridgeCapabilityError` on an unrepresentable shape (a refusal, an
    ``n>1`` multi-choice response, a malformed tool call).
    """
    if not isinstance(chat, dict):
        raise BridgeCapabilityError("chat completion response must be an object")
    choices = chat.get("choices")
    if not isinstance(choices, list) or not choices:
        raise BridgeCapabilityError("chat completion response has no choices")
    if len(choices) > 1:
        # Responses assumes a single output; dropping alternatives would hide
        # model output the caller paid for.
        raise BridgeCapabilityError(
            "chat completion returned multiple choices (n>1); Responses assumes "
            "one, refusing rather than silently dropping the alternatives"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise BridgeCapabilityError("chat completion choice must be an object")

    status, incomplete = _finish_to_status(
        choice.get("finish_reason"), length_as_completed
    )
    item_status = "incomplete" if status == "incomplete" else "completed"
    output = _message_output_items(
        choice.get("message") or {},
        item_status=item_status,
        emit_reasoning=emit_reasoning,
        namespace_map=namespace_map,
    )
    usage = _usage_to_responses(chat.get("usage"))
    if created_at is None:
        created_at = _as_int(chat.get("created"), default=int(time.time()))
    resolved_model = model or chat.get("model") or ""

    return _response_object(
        response_id=response_id,
        model=resolved_model,
        status=status,
        output=output,
        usage=usage,
        metadata=metadata,
        created_at=created_at,
        incomplete_details=incomplete,
    )
