"""Anthropic ``/v1/messages`` ↔ OpenAI ``/v1/chat/completions`` bridge.

Background
----------
The default sanitizer path forwards Anthropic SSE through an upstream LiteLLM
proxy that converts the upstream model's native OpenAI-format stream into
Anthropic SSE. That conversion has confirmed bugs in the field:

* ``hosted_vllm`` provider: when ``tools`` are defined and the model emits
  ``delta.reasoning_content`` followed by ``delta.content``, the content is
  serialized as zero-payload ``input_json_delta`` events — the actual text
  reply never reaches the client.
* ``openai`` provider: tool calls are emitted as a ``content_block_start``
  with the proper ``name``/``id``, but the trailing ``input_json_delta``
  stream is truncated — the SDK sees a tool_use block with no arguments.

The underlying OpenAI route (``/v1/chat/completions``) on the same LiteLLM
instance is correct and matches what the upstream vLLM emits verbatim. This
module bypasses LiteLLM's broken Anthropic adapter by translating in-process
between the two formats so the rest of the gateway (and any downstream
``claude_agent_sdk`` consumer) keeps speaking Anthropic.

Public surface
--------------
* :func:`anthropic_request_to_openai_body` — Anthropic request body →
  OpenAI request body.
* :func:`openai_stream_to_anthropic_events` — async iterator of parsed
  OpenAI SSE chunk dicts → async iterator of Anthropic SSE event dicts.
* :func:`openai_response_to_anthropic_body` — non-streaming OpenAI response
  body → non-streaming Anthropic response body.

The functions are independent of HTTP/SSE encoding; the route layer is
responsible for parsing and serialization.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional


# ---------------------------------------------------------------------------
# Request: Anthropic → OpenAI
# ---------------------------------------------------------------------------


_FINISH_REASON_TO_STOP_REASON: Dict[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "stop_sequence",
}


def _flatten_text_blocks(content: Any) -> str:
    """Concatenate the ``text`` of every ``{type: 'text'}`` block.

    Used to turn Anthropic-style structured content arrays into the single
    string OpenAI expects. ``thinking`` blocks are intentionally dropped:
    OpenAI's wire format has no slot for reasoning in historical assistant
    turns, and the model will recompute its own chain-of-thought on the next
    pass anyway.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                out.append(text)
    return "".join(out)


def _convert_assistant_message(content: Any) -> Dict[str, Any]:
    """Translate one Anthropic assistant turn to one OpenAI assistant message.

    Anthropic allows a single assistant turn to mix ``text``, ``thinking``,
    and ``tool_use`` blocks. OpenAI represents the same turn as a message
    with optional ``content`` (the text) and optional ``tool_calls`` (the
    tool invocations) — ``thinking`` is dropped, as it has no equivalent.
    """
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{uuid.uuid4().hex[:16]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(
                                block.get("input", {}) or {}, ensure_ascii=False
                            ),
                        },
                    }
                )
            # ``thinking`` and any unknown block types are dropped.

    msg: Dict[str, Any] = {"role": "assistant"}
    # OpenAI requires ``content`` to be present even when ``tool_calls`` is
    # set. ``null`` is allowed in spec but the LiteLLM ``hosted_vllm`` adapter
    # serializes assistant messages through a Pydantic model that drops the
    # key entirely when the value is ``None``; vLLM then rejects the request
    # with a 422 "content field required". An empty string survives that
    # round-trip and is equally valid under the OpenAI schema.
    msg["content"] = "".join(text_parts) if text_parts else ""
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _convert_user_message(content: Any) -> List[Dict[str, Any]]:
    """Translate one Anthropic user turn into one or more OpenAI messages.

    An Anthropic user turn can hold both plain text and ``tool_result``
    blocks; OpenAI expresses those as separate messages — text stays a
    ``user`` message, each ``tool_result`` becomes a ``tool`` message keyed
    by ``tool_call_id``.
    """
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return []

    out: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str):
                text_parts.append(t)
        elif btype == "tool_result":
            tr_content = block.get("content", "")
            # Anthropic allows tool_result.content to be a structured list of
            # text blocks; collapse to a plain string for OpenAI.
            if isinstance(tr_content, list):
                tr_content = _flatten_text_blocks(tr_content)
            elif not isinstance(tr_content, str):
                tr_content = json.dumps(tr_content, ensure_ascii=False)
            tool_msg: Dict[str, Any] = {
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or "",
                "content": tr_content,
            }
            out.append(tool_msg)

    if text_parts:
        # Place user text *before* tool results so the assistant sees the
        # human prompt in the natural conversational order. In practice
        # tool_result-only turns never carry user text, so the order rarely
        # matters — but keeping text first matches Anthropic's own ordering
        # when it round-trips history.
        out.insert(0, {"role": "user", "content": "".join(text_parts)})

    return out


def _convert_tool_choice(tc: Any) -> Any:
    """Map Anthropic ``tool_choice`` to OpenAI form, or ``None`` if absent."""
    if not isinstance(tc, dict):
        return None
    ttype = tc.get("type")
    if ttype == "auto":
        return "auto"
    if ttype == "any":
        return "required"
    if ttype == "tool":
        name = tc.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    if ttype == "none":
        return "none"
    return None


def anthropic_request_to_openai_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an Anthropic ``/v1/messages`` body to an OpenAI one.

    Fields without an OpenAI equivalent (``thinking``, ``metadata``,
    ``anthropic_*`` namespacing) are dropped — the upstream vLLM enables
    reasoning via its chat template, not via a request flag. Fields with
    a 1:1 analogue (``max_tokens``, ``temperature``, ``top_p``, ``stop``)
    are forwarded as-is.
    """
    out: Dict[str, Any] = {}

    model = body.get("model")
    if model is not None:
        out["model"] = model

    if "max_tokens" in body:
        out["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "top_p" in body:
        out["top_p"] = body["top_p"]
    if "stream" in body:
        out["stream"] = bool(body["stream"])
    if "stop_sequences" in body:
        out["stop"] = body["stop_sequences"]

    messages: List[Dict[str, Any]] = []

    system = body.get("system")
    if system:
        system_text = system if isinstance(system, str) else _flatten_text_blocks(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            messages.extend(_convert_user_message(content))
        elif role == "assistant":
            messages.append(_convert_assistant_message(content))
        elif role == "system":
            # Some clients place additional ``system`` messages mid-history
            # (Anthropic disallows this, but be liberal in what we accept).
            messages.append({"role": "system", "content": _flatten_text_blocks(content)})

    out["messages"] = messages

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}) or {},
                },
            }
            for t in tools
            if isinstance(t, dict) and t.get("name")
        ]

    tc = _convert_tool_choice(body.get("tool_choice"))
    if tc is not None:
        out["tool_choice"] = tc

    if "stream_options" in body:
        out["stream_options"] = body["stream_options"]
    # Always request usage in stream chunks when streaming, so we can populate
    # Anthropic's ``message_delta.usage`` accurately.
    if out.get("stream"):
        out.setdefault("stream_options", {})
        if isinstance(out["stream_options"], dict):
            out["stream_options"].setdefault("include_usage", True)

    return out


# ---------------------------------------------------------------------------
# Streaming response: OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------------------


class _StreamState:
    """Mutable cursor tracking the in-progress Anthropic block structure.

    Each OpenAI delta field type (``reasoning_content`` / ``content`` /
    ``tool_calls[i]``) maps to its own Anthropic content block. When the
    field type changes mid-stream we close the previous block and open a
    new one, keeping ``index`` monotonically increasing.
    """

    __slots__ = (
        "open_kind",
        "open_index",
        "pending_tool_calls",
        "model",
        "message_id",
        "input_tokens",
        "output_tokens",
        "finish_reason",
        "started",
    )

    def __init__(self, model: str) -> None:
        self.open_kind: Optional[str] = None  # "thinking" | "text" | tool_call_index (str)
        self.open_index: int = -1
        # OpenAI may stream multiple tool calls in parallel, with argument
        # fragments interleaved by ``index``. Anthropic content blocks are
        # contiguous, so we buffer each OpenAI tool call and flush it as one
        # complete Anthropic block before ``message_delta``.
        self.pending_tool_calls: Dict[int, _PendingToolCall] = {}
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason: Optional[str] = None
        self.started = False


class _PendingToolCall:
    __slots__ = ("id", "name", "argument_parts")

    def __init__(self, call_id: Optional[str], name: Optional[str]) -> None:
        self.id = call_id or f"toolu_{uuid.uuid4().hex[:24]}"
        self.name = name or ""
        self.argument_parts: List[str] = []

    def update_metadata(self, call_id: Optional[str], name: Optional[str]) -> None:
        if call_id:
            self.id = call_id
        if name:
            self.name = name


def _message_start_event(state: _StreamState) -> Dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": state.message_id,
            "type": "message",
            "role": "assistant",
            "model": state.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": state.input_tokens,
                "output_tokens": 0,
            },
        },
    }


def _close_block(state: _StreamState) -> Dict[str, Any]:
    idx = state.open_index
    state.open_kind = None
    return {"type": "content_block_stop", "index": idx}


def _open_block(state: _StreamState, kind: str, content_block: Dict[str, Any]) -> Dict[str, Any]:
    state.open_index += 1
    state.open_kind = kind
    return {
        "type": "content_block_start",
        "index": state.open_index,
        "content_block": content_block,
    }


def _record_tool_call_delta(
    state: _StreamState,
    tc_index: int,
    call_id: Optional[str],
    name: Optional[str],
    arguments: Optional[str],
) -> None:
    pending = state.pending_tool_calls.get(tc_index)
    if pending is None:
        pending = _PendingToolCall(call_id, name)
        state.pending_tool_calls[tc_index] = pending
    else:
        pending.update_metadata(call_id, name)

    if isinstance(arguments, str) and arguments:
        pending.argument_parts.append(arguments)


def _flush_tool_calls(state: _StreamState) -> List[Dict[str, Any]]:
    """Emit buffered OpenAI tool calls as contiguous Anthropic blocks."""
    if not state.pending_tool_calls:
        return []

    events: List[Dict[str, Any]] = []
    if state.open_kind is not None:
        events.append(_close_block(state))

    for tc_index in sorted(state.pending_tool_calls):
        pending = state.pending_tool_calls[tc_index]
        events.append(
            _open_block(
                state,
                f"tool:{tc_index}",
                {
                    "type": "tool_use",
                    "id": pending.id,
                    "name": pending.name,
                    "input": {},
                },
            )
        )
        for arguments in pending.argument_parts:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": state.open_index,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                }
            )
        events.append(_close_block(state))

    state.pending_tool_calls.clear()
    return events


async def openai_stream_to_anthropic_events(
    chunks: AsyncIterator[Dict[str, Any]],
    model: str,
) -> AsyncIterator[Dict[str, Any]]:
    """Convert parsed OpenAI SSE chunks into Anthropic SSE event dicts.

    *chunks* must yield dicts (the parsed JSON bodies of ``data:`` lines;
    the ``[DONE]`` sentinel is the caller's responsibility to skip). The
    output sequence is structurally valid Anthropic SSE — every
    ``content_block_start`` is closed before the next start or
    ``message_delta``, indices are 0-based and monotonic, and exactly one
    ``message_start``/``message_stop`` pair wraps the content.
    """
    state = _StreamState(model=model)

    async for chunk in chunks:
        # ``usage`` may arrive on its own terminal chunk (with an empty
        # ``choices`` array, when ``stream_options.include_usage=true``) or
        # alongside a regular delta — handle both.
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            pt = usage.get("prompt_tokens")
            if isinstance(pt, int):
                state.input_tokens = pt
            ct = usage.get("completion_tokens")
            if isinstance(ct, int):
                state.output_tokens = ct

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}

        if not state.started:
            yield _message_start_event(state)
            state.started = True

        # Reasoning (thinking) — vLLM uses ``reasoning_content``; some
        # variants additionally mirror to ``reasoning``. Treat the former
        # as canonical and ignore the duplicate to avoid double-emitting.
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            # Flush any buffered tool_calls before opening a new reasoning
            # block. This is intentional: tool_use blocks always precede the
            # text/reasoning that follows them in the converted stream, even
            # when that text arrives after a tool-call delta in the same turn.
            for event in _flush_tool_calls(state):
                yield event
            if state.open_kind != "thinking":
                if state.open_kind is not None:
                    yield _close_block(state)
                yield _open_block(
                    state, "thinking", {"type": "thinking", "thinking": ""}
                )
            yield {
                "type": "content_block_delta",
                "index": state.open_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            }

        # Plain assistant text.
        content = delta.get("content")
        if isinstance(content, str) and content:
            # Flush any buffered tool_calls before opening a new text block.
            # This is intentional: tool_use blocks always precede the
            # text/reasoning that follows them in the converted stream, even
            # when that text arrives after a tool-call delta in the same turn.
            for event in _flush_tool_calls(state):
                yield event
            if state.open_kind != "text":
                if state.open_kind is not None:
                    yield _close_block(state)
                yield _open_block(state, "text", {"type": "text", "text": ""})
            yield {
                "type": "content_block_delta",
                "index": state.open_index,
                "delta": {"type": "text_delta", "text": content},
            }

        # Tool calls. The OpenAI streaming protocol delivers each tool call
        # as a series of deltas keyed by the call's own ``index`` field —
        # the first occurrence carries ``id``/``name``, subsequent ones
        # only append to ``arguments``. Buffer by index because parallel
        # calls can interleave argument chunks, while Anthropic blocks cannot.
        tool_calls = delta.get("tool_calls") or []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_index = tc.get("index")
            if not isinstance(tc_index, int):
                continue
            fn = tc.get("function") or {}
            tc_id = tc.get("id")
            tc_name = fn.get("name") if isinstance(fn, dict) else None
            tc_args = fn.get("arguments") if isinstance(fn, dict) else None

            _record_tool_call_delta(state, tc_index, tc_id, tc_name, tc_args)

        fr = choice.get("finish_reason")
        if isinstance(fr, str) and fr:
            state.finish_reason = fr

    # Close trailing block, if any.
    if not state.started:
        # Empty upstream stream — still emit a minimal valid message frame
        # so the SDK doesn't get a dangling response.
        yield _message_start_event(state)
        state.started = True
    for event in _flush_tool_calls(state):
        yield event
    if state.open_kind is not None:
        yield _close_block(state)

    stop_reason = _FINISH_REASON_TO_STOP_REASON.get(
        state.finish_reason or "stop", "end_turn"
    )
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": state.output_tokens},
    }
    yield {"type": "message_stop"}


# ---------------------------------------------------------------------------
# Non-streaming response: OpenAI → Anthropic
# ---------------------------------------------------------------------------


def openai_response_to_anthropic_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a non-streaming OpenAI chat-completion to Anthropic shape.

    Mirrors :func:`openai_stream_to_anthropic_events` for the one-shot case:
    reasoning → thinking block, content → text block, each tool call →
    tool_use block.
    """
    message = body.get("choices", [{}])[0].get("message", {}) if body.get("choices") else {}
    content_blocks: List[Dict[str, Any]] = []

    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args_str = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            args = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": (fn.get("name") if isinstance(fn, dict) else "") or "",
                "input": args,
            }
        )

    finish = body.get("choices", [{}])[0].get("finish_reason") if body.get("choices") else None
    stop_reason = _FINISH_REASON_TO_STOP_REASON.get(finish or "stop", "end_turn")

    usage = body.get("usage") or {}
    return {
        "id": body.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model") or "",
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
