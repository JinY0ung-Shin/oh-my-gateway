"""Clamp a client reasoning-effort level to the backend's vocabulary.

Capability-driven adaptation of Codex's provider-side reasoning hint to what the
inference backend actually accepts (vLLM/SGLang deployments may accept only a
subset). This is the DATA-plane provider hint, not the gateway's Harness policy
(reasoning as a *policy* constraint is enforced in the control-plane adapter);
here an off-vocabulary value is clamped to the nearest level rather than sent
raw, which the backend would 400 on.
"""

from __future__ import annotations

from typing import Optional

# OpenAI-style ladder, cheapest first. Codex sends any of these.
EFFORT_LADDER: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)


def clamp_reasoning_effort(
    value: object, allowed: Optional[frozenset[str]]
) -> Optional[str]:
    """Return the effort level to send upstream, or ``None`` to drop the param.

    ``allowed`` is the backend vocabulary (``None`` = passthrough, no clamping).
    A value already in ``allowed`` is kept; a known ladder value outside it is
    moved to the nearest allowed ladder level (ties -> the cheaper one); an
    unknown string is dropped so the backend never sees a value it would 400 on.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if allowed is None:
        return normalized
    if normalized in allowed:
        return normalized
    if normalized not in EFFORT_LADDER:
        # An unknown name has no place on the ladder, so there is no "nearest"
        # level to fall back to -- dropping the param beats guessing.
        return None
    idx = EFFORT_LADDER.index(normalized)
    # Only allowed levels that are ladder members are candidates; a vocabulary
    # of pure unknowns leaves nothing to clamp to.
    candidates = [lvl for lvl in EFFORT_LADDER if lvl in allowed]
    if not candidates:
        return None
    # ``candidates`` is cheapest-first and ``min`` keeps the first minimum, so an
    # equidistant pair resolves to the cheaper level.
    return min(candidates, key=lambda lvl: abs(EFFORT_LADDER.index(lvl) - idx))
