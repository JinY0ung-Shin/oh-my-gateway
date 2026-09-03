"""Env-driven knobs for the Codex Responses -> chat/completions bridge.

Plain getters (no pydantic), matching ``src/sanitizer/config.py``. Each is read
at call time so tests can monkeypatch the environment. Defaults mirror the
reference converter's field-tested values, adjusted where our contract is
stricter (see ``request.py`` for the fail-closed tool handling).
"""

from __future__ import annotations

import os
import re
from typing import Optional

from src.backends.common import parse_csv

# Default backend reasoning vocabulary: vLLM/SGLang deployments typically accept
# only these, while Codex's ladder reaches xhigh/max/ultra. Capability-driven,
# not a free-standing downgrade: an off-vocabulary effort is clamped to the
# nearest listed level (see reasoning_effort.py).
_DEFAULT_REASONING_EFFORT_LEVELS = "low,medium,high"

# Strict Qwen chat templates 400 on any system message past index 0; only those
# generations need the mid-system rewrite, so it is gated on the model name.
_DEFAULT_MID_SYSTEM_MODEL_PATTERN = r"qwen3\.\d"


def flatten_namespace_tools() -> bool:
    """Whether Responses ``namespace`` tools are flattened to top-level chat
    functions (default on). When off, a namespace tool is unrepresentable and
    the request is rejected fail-closed rather than silently dropped."""
    return os.getenv(
        "CODEX_BRIDGE_FLATTEN_NAMESPACE_TOOLS", "true"
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def reasoning_effort_levels() -> Optional[frozenset[str]]:
    """Backend reasoning-effort vocabulary, or ``None`` for passthrough.

    ``CODEX_BRIDGE_REASONING_EFFORT_LEVELS`` is a comma-separated list; ``*``
    disables clamping (passthrough).
    """
    raw = os.getenv(
        "CODEX_BRIDGE_REASONING_EFFORT_LEVELS", _DEFAULT_REASONING_EFFORT_LEVELS
    ).strip()
    if raw == "*":
        return None
    levels = parse_csv(raw)
    return frozenset(level.lower() for level in levels) if levels else None


def mid_system_policy() -> str:
    """``reject`` | ``user`` | ``hoist`` | ``asis`` (default ``reject``).

    The default is fail-closed: for a gated model whose system messages would
    have to be rewritten, the request is refused rather than silently lowering
    a later ``system`` instruction's authority to ``user``. ``user``/``hoist``/
    ``asis`` are explicit operator opt-ins (see system_norm.py). An unrecognized
    value takes the ``reject`` path, never a silent demote.
    """
    return (
        os.getenv("CODEX_BRIDGE_MID_SYSTEM_POLICY", "reject").strip().lower()
        or "reject"
    )


def mid_system_model_regex() -> Optional[re.Pattern[str]]:
    """Compiled gate for the mid-system rewrite; ``None`` never rewrites.

    Empty ``CODEX_BRIDGE_MID_SYSTEM_MODEL_PATTERN`` disables the rewrite; an
    invalid pattern also disables it (fail safe, never raise on config).
    """
    raw = os.getenv(
        "CODEX_BRIDGE_MID_SYSTEM_MODEL_PATTERN", _DEFAULT_MID_SYSTEM_MODEL_PATTERN
    )
    if not raw:
        return None
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        return None
