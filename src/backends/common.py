"""Shared helpers for backend implementations."""

from __future__ import annotations

from typing import Dict, Optional


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


def estimate_token_usage(prompt: str, completion: str) -> Dict[str, int]:
    """Estimate backend token usage using the existing rough length heuristic."""
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(completion) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
