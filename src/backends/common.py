"""Shared helpers for backend implementations."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional


def parse_csv(value: str) -> list[str]:
    """Parse comma-separated environment values, preserving order."""
    items: list[str] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if item and item not in items:
            items.append(item)
    return items


def combine_system_prompt(
    custom_base: Optional[str],
    system_prompt: Optional[str],
) -> Optional[str]:
    """Combine a backend base prompt with the per-request system prompt."""
    if custom_base and system_prompt:
        return f"{custom_base}\n\n{system_prompt}"
    return custom_base or system_prompt


def error_chunk(message: str) -> Dict[str, Any]:
    """Build the shared error chunk emitted by streaming backends."""
    return {"type": "error", "is_error": True, "error_message": message}


def completion_chunks(
    text: str,
    usage: Optional[Dict[str, int]] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield the terminal assistant + result chunks for a completed turn."""
    assistant: Dict[str, Any] = {
        "type": "assistant",
        "content": [{"type": "text", "text": text}],
    }
    result: Dict[str, Any] = {"type": "result", "subtype": "success", "result": text}
    if usage:
        assistant["usage"] = {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        }
        result["usage"] = {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        }
    yield assistant
    yield result


def estimate_token_usage(prompt: str, completion: str) -> Dict[str, int]:
    """Estimate backend token usage using the existing rough length heuristic."""
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(completion) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


class TokenEstimateMixin:
    """Provides the shared ``estimate_token_usage`` implementation.

    Every backend client delegates token estimation to the module-level
    :func:`estimate_token_usage` heuristic; inheriting this mixin keeps that
    single implementation in one place instead of three identical wrappers.
    """

    def estimate_token_usage(
        self, prompt: str, completion: str, model: Optional[str] = None
    ) -> Dict[str, int]:
        _ = model  # signature parity with the BackendClient protocol; unused
        return estimate_token_usage(prompt, completion)
