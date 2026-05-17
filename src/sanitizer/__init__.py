"""Anthropic Messages SSE sanitizer.

Sits as ``POST /v1/messages`` in front of an upstream LiteLLM (or similar) proxy
that emits non-conforming streams (notably LiteLLM #21128, where the first
content block is hardcoded as ``type=text`` regardless of whether the model is
actually thinking). Rewrites the event stream so that ``content_block_start.type``
matches the ``delta.type`` of every contained ``content_block_delta``, which the
Anthropic SDK / Claude Code enforce strictly.

Intentionally isolated from the rest of the gateway: no shared state, no auth,
no session/rate-limit dependencies — it is a pure pass-through proxy that
happens to share the FastAPI app for deployment convenience.
"""
