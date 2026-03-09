"""Backend registry for multi-backend model dispatch.

Provides model resolution, a BackendClient protocol, and a singleton registry
so endpoint code dispatches by interface rather than concrete backend type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """Result of resolving a user-facing model string.

    Attributes:
        public_model: The original model string from the request.
        backend: Backend name ("claude" or "codex").
        provider_model: Model identifier passed to the backend, or None for
            the backend's default.
    """

    public_model: str
    backend: str
    provider_model: Optional[str]


def resolve_model(model: str) -> ResolvedModel:
    """Parse a model string into backend + provider model.

    Resolution rules:
    - ``codex``         -> backend="codex", provider_model=None
    - ``codex/o3``      -> backend="codex", provider_model="o3"
    - ``codex/o4-mini`` -> backend="codex", provider_model="o4-mini"
    - ``sonnet``        -> backend="claude", provider_model="sonnet"
    - ``opus``          -> backend="claude", provider_model="opus"

    The function imports ``CODEX_MODELS`` lazily to avoid circular imports
    with ``constants.py``.
    """
    from src.constants import CODEX_MODELS

    # Slash-delimited pattern: "backend/sub-model"
    if "/" in model:
        prefix, sub_model = model.split("/", 1)
        if prefix in ("codex", *CODEX_MODELS):
            return ResolvedModel(
                public_model=model,
                backend="codex",
                provider_model=sub_model or None,
            )
        # Future: "claude/opus" could be handled here.
        # For now, treat unknown prefixes as Claude with full model string.
        return ResolvedModel(public_model=model, backend="claude", provider_model=model)

    # Exact match against known Codex model names
    if model in CODEX_MODELS:
        return ResolvedModel(public_model=model, backend="codex", provider_model=None)

    # Default: Claude backend
    return ResolvedModel(public_model=model, backend="claude", provider_model=model)


# ---------------------------------------------------------------------------
# BackendClient protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BackendClient(Protocol):
    """Interface that every backend must satisfy.

    Method names intentionally match ``ClaudeCodeCLI`` so the existing
    implementation is already structurally compatible.
    """

    async def run_completion(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = True,
        max_turns: int = 10,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        resume: Optional[str] = None,
        permission_mode: Optional[str] = None,
        output_format: Optional[Dict[str, Any]] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]: ...

    def parse_message(self, messages: List[Dict[str, Any]]) -> Optional[str]: ...

    def estimate_token_usage(
        self,
        prompt: str,
        completion: str,
        model: Optional[str] = None,
    ) -> Dict[str, int]: ...

    async def verify(self) -> bool: ...


# ---------------------------------------------------------------------------
# Backend registry (singleton)
# ---------------------------------------------------------------------------


class BackendRegistry:
    """Singleton registry that owns backend client instances.

    Usage in ``main.py``::

        BackendRegistry.register("claude", claude_cli)
        BackendRegistry.register("codex", codex_cli)

        resolved = resolve_model(request.model)
        backend  = BackendRegistry.get(resolved.backend)
        async for chunk in backend.run_completion(...):
            ...
    """

    _backends: Dict[str, BackendClient] = {}

    # -- mutation ----------------------------------------------------------

    @classmethod
    def register(cls, name: str, client: BackendClient) -> None:
        """Register a backend client under *name*."""
        cls._backends[name] = client

    @classmethod
    def unregister(cls, name: str) -> None:
        """Remove a backend (mainly useful in tests)."""
        cls._backends.pop(name, None)

    @classmethod
    def clear(cls) -> None:
        """Remove all registered backends (test helper)."""
        cls._backends.clear()

    # -- queries -----------------------------------------------------------

    @classmethod
    def get(cls, name: str) -> BackendClient:
        """Return the backend registered under *name*, or raise."""
        if name not in cls._backends:
            available = ", ".join(sorted(cls._backends)) or "(none)"
            raise ValueError(f"Backend '{name}' is not registered. Available backends: {available}")
        return cls._backends[name]

    @classmethod
    def is_registered(cls, name: str) -> bool:
        return name in cls._backends

    @classmethod
    def all_backends(cls) -> Dict[str, BackendClient]:
        """Return a snapshot of all registered backends."""
        return dict(cls._backends)

    @classmethod
    def available_models(cls) -> List[Dict[str, str]]:
        """Build the ``/v1/models`` data list from registered backends."""
        from src.constants import CLAUDE_MODELS, CODEX_MODELS

        data: List[Dict[str, str]] = []

        for model_id in CLAUDE_MODELS:
            if cls.is_registered("claude"):
                data.append({"id": model_id, "object": "model", "owned_by": "anthropic"})

        for model_id in CODEX_MODELS:
            if cls.is_registered("codex"):
                data.append({"id": model_id, "object": "model", "owned_by": "openai"})

        return data
