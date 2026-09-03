"""Fail-closed errors for the Codex Responses -> chat/completions bridge (#173 checkpoint-2A).

The gateway's compatibility contract is stricter than a best-effort translator:
a capability the target chat backend cannot faithfully represent must be
**rejected explicitly**, never silently dropped or last-wins-collapsed. Callers
map :class:`BridgeCapabilityError` to an explicit 4xx rather than forwarding a
weakened request.
"""

from __future__ import annotations


class BridgeCapabilityError(ValueError):
    """A Responses request asks for something the chat backend cannot represent.

    Raised (fail closed) instead of silently degrading, e.g. an unsupported
    built-in/unknown tool type, or a tool-name collision the flattened
    chat/completions tool namespace cannot disambiguate.
    """
