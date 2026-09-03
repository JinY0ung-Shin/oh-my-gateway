"""System-message placement normalization for chat/completions bodies.

Strict Qwen-generation chat templates hard-error (HTTP 400) on a ``system``
message anywhere but index 0, or on more than one. Codex/Claude Code routinely
plant mid-history ``role:"system"`` reminders, so the request would 400 before
the model sees a token. This rewrites *placement* only -- every instruction the
client sent still reaches the model, just from a position the template accepts.

Pure: it never reads config; the caller passes the policy + the model gate. The
rewrite is gated on the outbound model name because only the strict templates
need it. Idempotent under every policy (``f(f(x)) == f(x)``).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .errors import BridgeCapabilityError

# Separator between two MERGED system messages: each is an independent
# instruction block, unlike the fragments within one message's content.
_MERGE_SEPARATOR = "\n\n"


def _content_text(content: Any) -> str:
    """Plain text of one chat message's ``content`` (junk -> "")."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _merge_texts(texts: list[str]) -> str:
    """Join system texts with a blank line, skipping empty ones."""
    return _MERGE_SEPARATOR.join(text for text in texts if text)


def _is_system(message: Any) -> bool:
    return isinstance(message, dict) and message.get("role") == "system"


def _needs_system_rewrite(messages: list[dict]) -> bool:
    """Whether a strict template would 400 on this system-message placement.

    True iff any ``system`` message sits past index 0 (which also covers the
    >1-system case, since a second system message cannot also be at index 0).
    A single leading system message, or none, is accepted as-is.
    """
    return any(_is_system(message) for message in messages[1:])


def _hoist(messages: list[dict]) -> list[dict]:
    """Collect every system message into one leading system message."""
    texts: list[str] = []
    rest: list[dict] = []
    for message in messages:
        if _is_system(message):
            texts.append(_content_text(message.get("content")))
        else:
            rest.append(message)
    if not texts:
        return messages
    return [{"role": "system", "content": _merge_texts(texts)}, *rest]


def _demote_to_user(messages: list[dict]) -> list[dict]:
    """Merge the leading system run; role-swap every later system to ``user``."""
    head_run = 0
    for message in messages:
        if not _is_system(message):
            break
        head_run += 1
    tail = messages[head_run:]

    if head_run <= 1 and not any(_is_system(message) for message in tail):
        return messages

    out: list[dict] = []
    if head_run:
        head = dict(messages[0])
        head["content"] = _merge_texts(
            [_content_text(message.get("content")) for message in messages[:head_run]]
        )
        out.append(head)
    for message in tail:
        if _is_system(message):
            demoted = dict(message)
            demoted["role"] = "user"
            out.append(demoted)
        else:
            out.append(message)
    return out


def normalize_system_messages(
    messages: list[dict],
    policy: str,
    model: Any = None,
    model_pattern: Optional[re.Pattern[str]] = None,
) -> list[dict]:
    """Rewrite *messages* so no system turn lands where a strict template 400s.

    *model_pattern* gates the whole rewrite: given a pattern, *messages* returns
    untouched unless *model* is a string the pattern searches successfully (a
    missing/non-string model therefore passes through). ``None`` normalizes
    unconditionally (how the unit tests exercise the policies).

    Policies: ``reject`` (default) refuses (:class:`BridgeCapabilityError`) a
    gated request whose system placement would 400, rather than lowering a
    later ``system`` instruction's authority; ``user`` merges the leading
    system run + role-swaps later system messages to ``user`` in place;
    ``hoist`` merges ALL system text into one leading system message; ``asis``
    passes through. Any UNRECOGNIZED policy takes the ``reject`` path -- it
    never silently demotes. Never mutates the input.
    """
    if model_pattern is not None and not (
        isinstance(model, str) and model_pattern.search(model)
    ):
        return messages
    if policy == "asis" or not isinstance(messages, list) or not messages:
        return messages
    if policy == "hoist":
        return _hoist(messages)
    if policy == "user":
        return _demote_to_user(messages)
    # ``reject`` and any unrecognized policy: fail closed. Passing through a
    # system message that a strict template rejects would 400, and demoting it
    # to ``user`` would lower its authority -- so refuse only when a rewrite is
    # actually required, leaving already-valid placement untouched.
    if _needs_system_rewrite(messages):
        raise BridgeCapabilityError(
            "a later 'system' message cannot be represented for this model "
            "without lowering its authority; the default fail-closed policy "
            "refuses it (set CODEX_BRIDGE_MID_SYSTEM_POLICY=user|hoist to opt "
            "into an explicit rewrite, or asis to pass through unchanged)"
        )
    return messages
