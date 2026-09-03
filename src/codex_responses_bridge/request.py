"""Responses request -> Chat Completions request translation (#173 checkpoint-2A).

Pure, HTTP-independent. Ported from the reference converter
(``JinY0ung-Shin/UniBridge`` ``llm-converter/app/responses_bridge.py``) but
hardened to the gateway's fail-closed contract:

* an unsupported/unknown tool type is **rejected** (:class:`BridgeCapabilityError`),
  never silently dropped;
* a flattened-function-name **collision** is rejected, never last-wins-collapsed.

**Deliberately unsupported in PR-1 (refused, not degraded), pending pinned-runtime
certification (checkpoint-2):**

* **Reasoning continuation.** A ``reasoning`` input item participates in Codex
  continuation state (``store:false`` + ``include:["reasoning.encrypted_content"]``
  carries encrypted reasoning forward). chat/completions cannot represent it, so
  a request carrying one is refused. A normal multi-turn Codex flow that relies
  on reasoning state therefore does not run through this bridge yet.
* **Active provider controls.** Consumed request fields are accepted only for
  their proven-neutral value (e.g. ``store:false``, empty ``include``, default
  ``service_tier``, absent ``reasoning.summary``/``reasoning.context``,
  ``text.verbosity:"medium"``, no ``access_programs``). A known field carrying a
  non-neutral value is refused rather than silently erased, because certifying
  its faithful translation needs the real backend.

So "request translation" here means the request SHAPE this slice proves it can
carry, not the full current-Codex request surface -- the remaining fields fail
loudly until certified.

The response + streaming halves (chat -> Responses) and real-runtime
certification are separate checkpoint-2 work.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .config import (
    flatten_namespace_tools,
    mid_system_model_regex,
    mid_system_policy,
    reasoning_effort_levels,
)
from .errors import BridgeCapabilityError
from .reasoning_effort import clamp_reasoning_effort
from .system_norm import normalize_system_messages

# --- inbound Responses request contract (fail-closed field boundary) --------
# The boundary derives from the fields the current Codex ``ResponsesApiRequest``
# can emit, so a newly-added control fails loudly HERE rather than disappearing
# one nested field at a time. Every present field is either TRANSLATED into the
# chat body or CONSUMED -- but "consumed" is VALUE-sensitive, not key-only: a
# consumed field is accepted only for the exact value(s) proven to be a semantic
# no-op for this bridge. A known field carrying an ACTIVE (non-neutral) value is
# refused (:class:`BridgeCapabilityError`), never silently erased; its faithful
# translation is deferred to the pinned-runtime certification (checkpoint-2).

# Faithfully translated into the chat/completions body.
_TRANSLATED_BODY_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "text",
        "reasoning",
        "max_output_tokens",
        "temperature",
        "top_p",
        "parallel_tool_calls",
        "metadata",
        "stream",
        "user",
        "safety_identifier",
    }
)

_Neutral = Callable[[Any], bool]

# Consumed top-level fields -> predicate for the value(s) accepted as a proven
# no-op. ``store``/``include`` drive the encrypted-reasoning continuation loop
# this bridge does not carry (reasoning input items are refused), so only their
# neutral form (persistence off / nothing to include) is accepted; a request
# that actively asks to persist state or to receive encrypted reasoning is
# refused. ``service_tier`` accepts only the default routing; a specific tier
# (e.g. flex) is a behavior change. ``access_programs`` selects a Codex access
# program with no certified backend mapping, so any non-empty selection is
# refused. ``prompt_cache_key``/``client_metadata`` are pure optimization/
# telemetry with no output effect. ``previous_response_id`` is key-allowed here
# but its value invariant (history already materialized into ``prior_messages``)
# is enforced in :func:`responses_request_to_chat_body`.
_CONSUMED_BODY_FIELDS: dict[str, _Neutral] = {
    "store": lambda v: not v,
    "include": lambda v: not v,
    "stream_options": lambda v: True,
    "service_tier": lambda v: v in (None, "auto", "default"),
    "prompt_cache_key": lambda v: True,
    "client_metadata": lambda v: True,
    "access_programs": lambda v: not v,
    "previous_response_id": lambda v: True,
}

_TRANSLATED_REASONING_FIELDS = frozenset({"effort"})
# ``summary``/``context``/``generate_summary`` actively change reasoning
# delivery/context; chat/completions cannot honor them, so only their absent/
# empty form is a no-op -- a request that actively asks for a reasoning summary
# or injects reasoning context is refused pending certification.
_CONSUMED_REASONING_FIELDS: dict[str, _Neutral] = {
    "summary": lambda v: not v,
    "context": lambda v: not v,
    "generate_summary": lambda v: not v,
}

_TRANSLATED_TEXT_FIELDS = frozenset({"format"})
# ``verbosity`` changes output length; only the API default ``medium`` (or
# absent) is a no-op. ``low``/``high`` are behavior changes -> refused.
_CONSUMED_TEXT_FIELDS: dict[str, _Neutral] = {
    "verbosity": lambda v: v in (None, "medium"),
}


def _assert_field_contract(
    obj: Any,
    kind: str,
    translated: frozenset[str],
    consumed: dict[str, _Neutral],
) -> None:
    """Fail closed on any field of *obj* outside the known, value-sensitive contract.

    A key the bridge neither translates nor knows how to consume is refused
    (a newly-added Codex control fails loudly). A consumed key whose VALUE is
    not a proven no-op is also refused, so a known field carrying an active
    control cannot be silently erased. A non-dict *obj* is left for its own
    translator to validate.
    """
    if not isinstance(obj, dict):
        return
    for key, value in obj.items():
        if key in translated:
            continue
        predicate = consumed.get(key)
        if predicate is None:
            raise BridgeCapabilityError(
                f"unsupported Responses {kind} field {key!r}; refusing rather "
                "than accepting a request after silently ignoring a control (add "
                "it to the bridge contract to translate or consume it)"
            )
        if not predicate(value):
            raise BridgeCapabilityError(
                f"Responses {kind} field {key!r}={value!r} is not a proven no-op "
                "for this bridge; refusing rather than silently erasing an active "
                "control (its faithful translation is deferred to pinned-runtime "
                "certification)"
            )


def _map_role(role: Any) -> str:
    # chat endpoints accept system/user/assistant/tool; map the Responses-only
    # ``developer`` role to ``system``. An UNKNOWN/privileged role must NOT be
    # reinterpreted as user input (that could silently downgrade a privileged
    # instruction to user text, or hide a new semantic role) -- fail closed.
    if role == "developer":
        return "system"
    if role in ("user", "assistant", "system", "tool"):
        return role
    raise BridgeCapabilityError(
        f"unsupported message role {role!r}; refusing rather than reinterpreting "
        "it as user input"
    )


def _content_to_chat(content: Any, role: str) -> Any:
    """Translate a Responses message ``content`` to chat content.

    Collapses text parts to a plain string; if image parts are present, returns
    a chat multimodal content array. ``input_text``/``output_text``/``text`` ->
    text; ``input_image`` -> ``image_url``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        # A non-list, non-string content value (a number, a dict, ...) has no
        # representable shape; refuse rather than collapse it to "" and lose it.
        raise BridgeCapabilityError(
            f"unsupported message content shape {type(content).__name__}; "
            "refusing rather than dropping it"
        )

    text_parts: list[str] = []
    multimodal: list[dict] = []
    has_image = False
    for part in content:
        if not isinstance(part, dict):
            raise BridgeCapabilityError(
                "unsupported non-object content part; refusing rather than "
                "dropping it"
            )
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            t = part.get("text")
            if not isinstance(t, str):
                # A text part whose payload is absent or non-string is
                # malformed; refuse rather than drop it (a dropped text part
                # silently shortens the message the model sees).
                raise BridgeCapabilityError(
                    f"{ptype!r} content part has a non-string 'text' payload "
                    f"({type(t).__name__}); refusing rather than dropping it"
                )
            text_parts.append(t)
            multimodal.append({"type": "text", "text": t})
        elif ptype == "input_image":
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url:
                # An input_image referenced only by file_id has no chat
                # equivalent here: silently dropping it would run a materially
                # different text-only request while the caller believes an image
                # was supplied. Fail closed (#173 image-input gate) -- the file
                # reference must be resolved to a URL/data URI before this
                # boundary, or the request is refused.
                raise BridgeCapabilityError(
                    "input_image referenced only by file_id has no "
                    "chat/completions representation; resolve the file reference "
                    "to a URL/data URI before this boundary rather than dropping "
                    "the image"
                )
            has_image = True
            img: dict[str, Any] = {"url": url}
            detail = part.get("detail")
            if detail and detail != "original":
                img["detail"] = detail
            multimodal.append({"type": "image_url", "image_url": img})
        elif ptype == "refusal":
            t = part.get("refusal")
            if not isinstance(t, str):
                raise BridgeCapabilityError(
                    "'refusal' content part has a non-string 'refusal' payload "
                    f"({type(t).__name__}); refusing rather than dropping it"
                )
            text_parts.append(t)
        else:
            # An unknown content-part type (a new/privileged part upstream may
            # add) must not silently disappear -- refuse rather than degrade.
            raise BridgeCapabilityError(
                f"unsupported content part type {ptype!r}; refusing rather than "
                "dropping it"
            )

    if has_image:
        return multimodal
    return "".join(text_parts)


def _input_to_messages(input_data: Any) -> list[dict]:
    """Translate the Responses ``input`` (string or item array) to chat messages.

    Consecutive ``function_call`` items (and an immediately-preceding toolless
    assistant ``message``) coalesce into ONE assistant message carrying all their
    ``tool_calls``. The chat contract requires every ``role:"tool"`` result to
    sit adjacent to the single assistant-with-tool_calls block that issued it;
    one assistant message per parallel call would interleave an extra assistant
    turn between a call and its result and get the follow-up rejected (400).
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]
    if not isinstance(input_data, list):
        # ``input`` is either a string or an item array; anything else is
        # malformed. Refuse rather than silently produce an empty message list
        # (which would run a contentless request the caller never intended).
        raise BridgeCapabilityError(
            "Responses 'input' must be a string or a list, got "
            f"{type(input_data).__name__}"
        )

    messages: list[dict] = []
    # The assistant message currently accumulating tool_calls, or None. Reset by
    # any item ending a tool-call run (a message or a tool result), so a later
    # function_call starts a fresh assistant block.
    pending: Optional[dict] = None

    for item in input_data:
        if not isinstance(item, dict):
            # A non-object input item has no representable shape; refuse rather
            # than skip it (a skipped item silently drops conversation/tool
            # state the follow-up turn may depend on).
            raise BridgeCapabilityError(
                "Responses input item must be an object, got " f"{type(item).__name__}"
            )
        itype = item.get("type")
        if itype in (None, "message"):
            if "role" not in item:
                # A message (or typeless) item must carry a role; refuse a
                # malformed one rather than drop it.
                raise BridgeCapabilityError(
                    "Responses 'message' input item is missing 'role'"
                )
            pending = None
            role = _map_role(item.get("role"))
            messages.append(
                {"role": role, "content": _content_to_chat(item.get("content"), role)}
            )
        elif itype == "function_call":
            name = item.get("name")
            if not isinstance(name, str) or not name:
                # A tool call with no function name cannot be routed to a tool;
                # refuse rather than emit an empty name the backend would reject
                # or misroute.
                raise BridgeCapabilityError(
                    "Responses 'function_call' item is missing its function "
                    "'name'; refusing rather than emitting an unnamed tool call"
                )
            # A tool result correlates to the function ``call_id``, which is a
            # DISTINCT identity from the response item's own ``id``. Reusing
            # ``id`` here would mint a chat tool-call id that a later result
            # (keyed by the real call_id) can never match -- so require a real,
            # non-empty ``call_id`` and never substitute ``id`` for it.
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise BridgeCapabilityError(
                    "Responses 'function_call' item is missing its 'call_id'; "
                    "refusing rather than correlating on the distinct item 'id' "
                    "or synthesizing an id no tool result can match"
                )
            # ``arguments`` is a JSON string in the Responses function-call
            # contract. A missing/non-string value must not be coerced (``None``
            # -> ``{}`` would turn a malformed call into an executable
            # zero-argument invocation) -- refuse the unproven shape.
            args = item.get("arguments")
            if not isinstance(args, str):
                raise BridgeCapabilityError(
                    "Responses 'function_call.arguments' must be a JSON string, "
                    f"got {type(args).__name__}; refusing rather than coercing a "
                    "malformed value into a different tool call"
                )
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            if pending is None:
                if (
                    messages
                    and messages[-1].get("role") == "assistant"
                    and "tool_calls" not in messages[-1]
                ):
                    pending = messages[-1]
                    pending["tool_calls"] = [tool_call]
                else:
                    pending = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tool_call],
                    }
                    messages.append(pending)
            else:
                pending["tool_calls"].append(tool_call)
        elif itype == "function_call_output":
            pending = None
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                # A tool result with no correlation id cannot be attached to the
                # assistant tool call that produced it; the chat contract rejects
                # a ``role:"tool"`` message whose ``tool_call_id`` matches no
                # prior call. Refuse rather than emit an empty id.
                raise BridgeCapabilityError(
                    "Responses 'function_call_output' item is missing its "
                    "'call_id'; refusing rather than emitting a tool result that "
                    "correlates to no tool call"
                )
            out = item.get("output", "")
            if isinstance(out, list):
                # A chat ``role:"tool"`` message is text-only. A non-text output
                # part (Codex ``view_image``/image-gen return an ``input_image``
                # data URL, and structured parts carry model-visible payload)
                # has no faithful text-only representation, so refuse rather than
                # drop it behind a placeholder -- the model would otherwise act
                # on a materially emptier tool result than the tool produced.
                texts: list[str] = []
                for p in out:
                    if not isinstance(p, dict):
                        raise BridgeCapabilityError(
                            "'function_call_output' has a non-object output "
                            "part; refusing rather than dropping it"
                        )
                    if p.get("type") in ("output_text", "text", "input_text"):
                        t = p.get("text")
                        if not isinstance(t, str):
                            raise BridgeCapabilityError(
                                "'function_call_output' text part has a "
                                f"non-string 'text' payload ({type(t).__name__})"
                                "; refusing rather than dropping it"
                            )
                        texts.append(t)
                    else:
                        raise BridgeCapabilityError(
                            "'function_call_output' carries a non-text output "
                            f"part {p.get('type')!r} (e.g. an image); it has no "
                            "text-only chat/completions representation and is "
                            "refused rather than dropped -- the tool result would "
                            "otherwise lose model-visible payload"
                        )
                out = "".join(texts)
            elif not isinstance(out, str):
                # The proven output shapes are a string or the content-part array
                # handled above. An arbitrary dict/number/bool must not become
                # JSON text just because chat can carry text -- that invents a
                # tool result the caller never sent. Refuse the unproven shape.
                raise BridgeCapabilityError(
                    "Responses 'function_call_output.output' must be a string or "
                    f"a content-part array, got {type(out).__name__}; refusing "
                    "rather than coercing an unproven shape to JSON text"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": out,
                }
            )
        elif itype == "reasoning":
            # A Responses ``reasoning`` item is NOT safely ignorable: current
            # Codex runs with ``store:false`` + ``include:["reasoning.
            # encrypted_content"]`` and carries encrypted reasoning items forward
            # in the continuation, so they participate in model state. chat/
            # completions has no representation for them, and there is no
            # certified shape here we can prove display-only -- so refuse rather
            # than silently strip reasoning state from an accepted continuation.
            # (A certified display-only allowlist is deferred to the pinned-
            # runtime contract; see the PR discussion.)
            raise BridgeCapabilityError(
                "Responses 'reasoning' input item has no certified chat/"
                "completions representation; refusing rather than silently "
                "dropping reasoning-continuation state (store:false + "
                "include:['reasoning.encrypted_content'] carries it forward)"
            )
        else:
            # An unknown item type may carry conversation/tool semantics that
            # upstream could add later; refuse rather than degrade it invisibly.
            raise BridgeCapabilityError(
                f"unrecognized Responses input item type '{itype}'; refusing "
                "rather than silently dropping a potentially semantic item"
            )
    return messages


def _function_tool_to_chat(t: Any) -> Optional[dict]:
    """Translate one Responses function tool to a chat ``{type, function}`` dict.

    Returns ``None`` for a non-dict entry or one with no name (the caller decides
    whether that is fatal). Accepts both the internally-tagged flat form
    (``{type, name, ...}``) and the nested chat form (``{type, function:{...}}``),
    preserving an explicit ``strict`` flag from either.
    """
    if not isinstance(t, dict):
        return None
    if isinstance(t.get("function"), dict):
        fn = t["function"]
    else:
        fn = {
            "name": t.get("name"),
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {}),
        }
    if not fn.get("name"):
        return None
    chat_fn: dict[str, Any] = {
        "name": fn.get("name"),
        "description": fn.get("description", "") or "",
        "parameters": fn.get("parameters", {}) or {},
    }
    strict = fn.get("strict")
    if strict is None and isinstance(t.get("strict"), bool):
        strict = t.get("strict")
    if strict is not None:
        chat_fn["strict"] = strict
    return {"type": "function", "function": chat_fn}


def _translate_tools(tools: Any) -> tuple[list[dict], dict[str, str]]:
    """Translate Responses tools to chat tools + the namespace re-stamp map.

    Fail-closed hardening over the reference (which silently drops unsupported
    tool types and resolves inner-name collisions last-wins):

    * a ``function`` tool with no usable name, a ``namespace`` tool when
      flattening is off, and any other/unknown tool type all raise
      :class:`BridgeCapabilityError` -- the chat backend cannot represent them,
      so the request is refused rather than run with a capability silently gone;
    * a flattened-function-**name collision** (two inner functions, or an inner
      and a top-level function, sharing a name) raises rather than last-wins,
      because a chat tool call carries only ``function.name`` and an ambiguous
      name cannot be routed back to the right namespace.

    Returns ``(chat_tools, namespace_map)`` where ``namespace_map`` is
    ``{flattened_function_name: namespace_name}`` for the response path.
    """
    chat_tools: list[dict] = []
    namespace_map: dict[str, str] = {}
    seen: set[str] = set()
    if tools is None:
        return chat_tools, namespace_map
    if not isinstance(tools, list):
        raise BridgeCapabilityError("Responses 'tools' must be a list")

    flatten = flatten_namespace_tools()

    def _emit(chat: Optional[dict], *, source: str, namespace: Optional[str]) -> None:
        if chat is None:
            raise BridgeCapabilityError(
                f"{source} is not a representable chat function (missing name)"
            )
        name = chat["function"]["name"]
        if name in seen:
            raise BridgeCapabilityError(
                f"tool name collision on '{name}': the flattened chat/completions "
                "tool namespace cannot disambiguate it; refusing rather than "
                "dropping a capability (last-wins)"
            )
        seen.add(name)
        chat_tools.append(chat)
        if namespace is not None:
            namespace_map[name] = namespace

    for t in tools:
        if not isinstance(t, dict):
            raise BridgeCapabilityError("each Responses tool must be an object")
        ttype = t.get("type")
        if ttype == "function":
            _emit(_function_tool_to_chat(t), source="function tool", namespace=None)
        elif ttype == "namespace":
            if not flatten:
                raise BridgeCapabilityError(
                    "Responses 'namespace' tool cannot be represented in "
                    "chat/completions with namespace flattening disabled; "
                    "refusing rather than silently dropping it"
                )
            ns = t.get("name")
            if not isinstance(ns, str) or not ns:
                raise BridgeCapabilityError("namespace tool is missing its name")
            for inner in t.get("tools") or []:
                _emit(
                    _function_tool_to_chat(inner),
                    source=f"inner function of namespace '{ns}'",
                    namespace=ns,
                )
        else:
            raise BridgeCapabilityError(
                f"unsupported Responses tool type '{ttype}': it has no proven "
                "chat/completions representation and is refused rather than "
                "silently dropped (checkpoint-2 may graduate specific types)"
            )
    return chat_tools, namespace_map


def namespace_map_from_tools(tools: Any) -> dict[str, str]:
    """``{flattened_function_name: namespace_name}`` for the response re-stamp.

    Consistent with :func:`_translate_tools` (same fail-closed collision rule),
    so a request accepted on the way in maps unambiguously on the way back.
    """
    return _translate_tools(tools)[1]


def _tool_choice_to_chat(tc: Any) -> Any:
    """Translate a Responses ``tool_choice`` to chat form, or ``None`` when absent.

    Fail closed: a PRESENT ``tool_choice`` that cannot be represented faithfully
    raises rather than being omitted -- omitting it would drop the caller's
    tool-selection constraint and fall back to default behavior, broadening which
    tool is callable (the opposite of fail closed).
    """
    if tc is None:
        return None
    if isinstance(tc, str):
        if tc in ("auto", "none", "required"):
            return tc
        raise BridgeCapabilityError(
            f"unsupported tool_choice '{tc}'; refusing rather than dropping the "
            "caller's tool-selection constraint"
        )
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "function":
            name = tc.get("name") or (tc.get("function") or {}).get("name")
            if name:
                return {"type": "function", "function": {"name": name}}
            raise BridgeCapabilityError(
                "tool_choice of type 'function' is missing its function name"
            )
        if ttype in ("auto", "none", "required"):
            return ttype
    raise BridgeCapabilityError(
        f"unsupported tool_choice form {tc!r}; refusing rather than dropping the "
        "caller's tool-selection constraint"
    )


def _text_format_to_response_format(text: Any) -> Optional[dict]:
    """Translate ``text.format`` to a chat ``response_format``, or ``None``.

    Fail closed: only an ABSENT format or the explicit default ``type:"text"``
    is omitted. A present-but-unrepresentable ``text.format`` raises rather than
    being dropped, which would turn a constrained (structured-output) request
    into an unconstrained completion.
    """
    if text is None:
        return None
    if not isinstance(text, dict):
        # ``text`` is absent (handled above) or an object; a present non-object
        # value is malformed. Refuse rather than treat it as absent -- a
        # structured-output constraint could be hiding in a shape we do not
        # understand.
        raise BridgeCapabilityError(
            f"Responses 'text' must be an object, got {type(text).__name__}"
        )
    _assert_field_contract(text, "text", _TRANSLATED_TEXT_FIELDS, _CONSUMED_TEXT_FIELDS)
    fmt = text.get("format")
    if fmt is None:
        return None
    if not isinstance(fmt, dict):
        raise BridgeCapabilityError("text.format must be an object")
    ftype = fmt.get("type")
    if ftype == "text":
        # The explicit default carries no constraint -> omit response_format.
        return None
    if ftype == "json_object":
        return {"type": "json_object"}
    if ftype == "json_schema":
        schema: dict[str, Any] = {"name": fmt.get("name")}
        if "schema" in fmt:
            schema["schema"] = fmt.get("schema")
        if "strict" in fmt:
            schema["strict"] = fmt.get("strict")
        return {"type": "json_schema", "json_schema": schema}
    raise BridgeCapabilityError(
        f"unsupported text.format type {ftype!r}; refusing rather than dropping "
        "the structured-output constraint"
    )


def responses_request_to_chat_body(
    body: dict, prior_messages: Optional[list[dict]] = None
) -> dict:
    """Translate a Responses request body to a Chat Completions request body.

    ``prior_messages`` (resolved from ``previous_response_id`` upstream) is
    prepended; a new ``instructions`` on a follow-up applies to the current turn
    as a system message. Raises :class:`BridgeCapabilityError` when a requested
    tool capability cannot be faithfully represented (see :func:`_translate_tools`).
    """
    _assert_field_contract(
        body, "request", _TRANSLATED_BODY_FIELDS, _CONSUMED_BODY_FIELDS
    )
    # ``previous_response_id`` is resolved to ``prior_messages`` upstream; a raw
    # value present here without materialized history means the referenced turns
    # were never carried in, so forwarding only the incremental ``input`` would
    # run a truncated conversation. Enforce the invariant rather than consume it.
    if body.get("previous_response_id") and not prior_messages:
        raise BridgeCapabilityError(
            "Responses 'previous_response_id' is present but no resolved prior "
            "history was supplied; refusing rather than forwarding only the "
            "incremental input as a fresh conversation (resolve it to "
            "prior_messages upstream before this boundary)"
        )
    out: dict[str, Any] = {}
    if body.get("model") is not None:
        out["model"] = body["model"]

    messages: list[dict] = []
    if prior_messages:
        messages.extend(prior_messages)
        if body.get("instructions"):
            messages.append({"role": "system", "content": body["instructions"]})
    elif body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    messages.extend(_input_to_messages(body.get("input", [])))
    out["messages"] = normalize_system_messages(
        messages,
        mid_system_policy(),
        model=out.get("model"),
        model_pattern=mid_system_model_regex(),
    )

    if "max_output_tokens" in body and body["max_output_tokens"] is not None:
        out["max_completion_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p", "parallel_tool_calls", "metadata"):
        if k in body and body[k] is not None:
            out[k] = body[k]
    if "stream" in body:
        out["stream"] = bool(body["stream"])

    reasoning = body.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise BridgeCapabilityError(
            f"Responses 'reasoning' must be an object, got "
            f"{type(reasoning).__name__}"
        )
    if isinstance(reasoning, dict):
        _assert_field_contract(
            reasoning,
            "reasoning",
            _TRANSLATED_REASONING_FIELDS,
            _CONSUMED_REASONING_FIELDS,
        )
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        # Clamp Codex's ladder to the backend vocabulary; ``allowed_openai_params``
        # is LiteLLM's per-request escape hatch so the value is forwarded verbatim
        # instead of being dropped for non-reasoning models. A known ladder value
        # maps deterministically to the nearest allowed level; a present effort
        # with no representable mapping (an unknown/off-ladder name, a non-string
        # value, or a vocabulary that clamps to nothing) is refused rather than
        # dropped -- silently omitting it would run the turn at the backend's
        # default effort while the caller believes they constrained it.
        effort = clamp_reasoning_effort(
            reasoning.get("effort"), reasoning_effort_levels()
        )
        if effort is None:
            raise BridgeCapabilityError(
                f"reasoning.effort {reasoning.get('effort')!r} has no "
                "representable chat/completions mapping (unknown ladder value or "
                "empty backend vocabulary); refusing rather than dropping the "
                "caller's reasoning-effort constraint"
            )
        out["reasoning_effort"] = effort
        out["allowed_openai_params"] = ["reasoning_effort"]

    user = body.get("user") or body.get("safety_identifier")
    if user:
        out["user"] = user

    # Tool-dependent params follow the *translated* tool list, not the client's:
    # Codex hard-codes ``tool_choice:"auto"`` + ``parallel_tool_calls`` and sends
    # ``tools:[]`` on compaction turns, and vLLM/SGLang reject either param
    # without tools -- so both are emitted only alongside a non-empty tool list.
    tools, _namespace_map = _translate_tools(body.get("tools"))
    # Validate tool_choice whenever present (raises on an unrepresentable form),
    # even for an empty tool set -- an unsupported constraint must never be
    # silently dropped. It is EMITTED only alongside a non-empty tool list
    # (vLLM/SGLang reject tool_choice without tools).
    tc = _tool_choice_to_chat(body.get("tool_choice"))
    if tools:
        # A named function choice must reference a tool that actually survived
        # translation; a syntactically valid but missing name is an impossible
        # constraint, not a forwardable one.
        if isinstance(tc, dict):
            chosen = tc["function"]["name"]
            if chosen not in {t["function"]["name"] for t in tools}:
                raise BridgeCapabilityError(
                    f"tool_choice names function {chosen!r}, absent from the "
                    "translated tool set; refusing an unsatisfiable constraint "
                    "rather than forwarding a misbound one"
                )
        out["tools"] = tools
        if tc is not None:
            out["tool_choice"] = tc
    else:
        out.pop("parallel_tool_calls", None)
        # Empty translated tool set: omitting tool_choice is only equivalent for
        # ``auto``/``none`` (nothing to call anyway -- ``auto`` is the Codex
        # compaction case). ``required`` and a named function DEMAND a tool call
        # that zero tools cannot satisfy, so refuse rather than silently relax
        # "must call a tool" into an unconstrained completion.
        if tc is not None and tc not in ("auto", "none"):
            raise BridgeCapabilityError(
                f"tool_choice {tc!r} requires a tool call but the translated "
                "tool set is empty; refusing rather than dropping the constraint "
                "into an unconstrained completion"
            )
    rf = _text_format_to_response_format(body.get("text"))
    if rf is not None:
        out["response_format"] = rf

    if out.get("stream"):
        so = out.get("stream_options") or {}
        if isinstance(so, dict):
            so.setdefault("include_usage", True)
        out["stream_options"] = so

    return out
