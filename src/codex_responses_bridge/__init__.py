"""Codex Responses API <-> Chat Completions data-plane bridge (#173 checkpoint-2A).

The model-provider data plane: a Codex runtime issues Responses-API requests, but
the org serves models via LiteLLM -> vLLM/SGLang which speak chat/completions.
This package translates between the two, hardened to the gateway's fail-closed
contract (an unrepresentable capability is refused, never silently degraded).

This is distinct from ``src/routes/responses.py`` (the client-facing ChatDRAGON
``/v1/responses`` endpoint) and does not share its state.

PR-1 lands the request half (Responses request -> chat request); the response +
streaming halves and real-runtime certification are separate checkpoint-2 work.
"""

from __future__ import annotations

from .errors import BridgeCapabilityError
from .reasoning_effort import clamp_reasoning_effort
from .request import namespace_map_from_tools, responses_request_to_chat_body

__all__ = [
    "BridgeCapabilityError",
    "clamp_reasoning_effort",
    "namespace_map_from_tools",
    "responses_request_to_chat_body",
]
