"""Backend registry for multi-backend model dispatch.

Provides model resolution, a BackendClient protocol, and a singleton registry
so endpoint code dispatches by interface rather than concrete backend type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """Result of resolving a user-facing model string.

    Attributes:
        public_model: The original model string from the request.
        backend: Backend name (e.g. "claude").
        provider_model: Model identifier passed to the backend, or None for
            the backend's default.
    """

    public_model: str
    backend: str
    provider_model: Optional[str]


# ---------------------------------------------------------------------------
# BackendDescriptor — static metadata for known backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendDescriptor:
    """Static metadata for a known backend.

    Separates "known backends" from "live clients" so that model resolution
    and auth status work even if a backend failed to start.
    """

    name: str
    owned_by: str
    models: List[str]
    resolve_fn: Callable[[str], Optional[ResolvedModel]]


# ---------------------------------------------------------------------------
# BackendClient protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BackendClient(Protocol):
    """Interface that every backend must satisfy.

    Method names intentionally match ``ClaudeCodeCLI`` so the existing
    implementation is already structurally compatible.
    """

    @property
    def name(self) -> str: ...

    def supported_models(self) -> List[str]: ...

    def get_auth_provider(self) -> Any: ...

    def run_completion(
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
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        **_extra: Any,
    ) -> AsyncIterator[Dict[str, Any]]: ...

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
    """Singleton registry that owns backend client instances and descriptors.

    Usage in ``main.py``::

        BackendRegistry.register("claude", claude_cli)
        BackendRegistry.register("my_backend", my_client)

        resolved = resolve_model(request.model)
        backend  = BackendRegistry.get(resolved.backend)
        async for chunk in backend.run_completion(...):
            ...
    """

    _backends: Dict[str, BackendClient] = {}
    _descriptors: Dict[str, BackendDescriptor] = {}

    # -- mutation ----------------------------------------------------------

    @classmethod
    def register(cls, name: str, client: BackendClient) -> None:
        """Register a backend client under *name*."""
        cls._backends[name] = client

    @classmethod
    def register_descriptor(cls, descriptor: BackendDescriptor) -> None:
        """Register a static backend descriptor (model metadata)."""
        cls._descriptors[descriptor.name] = descriptor

    @classmethod
    def unregister(cls, name: str) -> None:
        """Remove a backend (mainly useful in tests)."""
        cls._backends.pop(name, None)

    @classmethod
    def clear(cls) -> None:
        """Remove all registered backends and descriptors (test helper)."""
        cls._backends.clear()
        cls._descriptors.clear()

    # -- queries -----------------------------------------------------------

    @classmethod
    def get(cls, name: str) -> BackendClient:
        """Return the backend registered under *name*, or raise."""
        if name not in cls._backends:
            if name in cls._descriptors:
                raise ValueError(
                    f"Backend '{name}' is known but not available (failed to start). "
                    f"Check server logs for details."
                )
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
    def all_descriptors(cls) -> Dict[str, BackendDescriptor]:
        """Return a snapshot of all registered descriptors."""
        return dict(cls._descriptors)

    @classmethod
    def all_model_ids(cls) -> set:
        """Return a set of all model IDs across all descriptors."""
        ids: set = set()
        for desc in cls._descriptors.values():
            ids.update(desc.models)
        return ids

    @classmethod
    def available_models(cls) -> List[Dict[str, str]]:
        """Build the ``/v1/models`` data list from registered backends."""
        data: List[Dict[str, str]] = []

        for desc in cls._descriptors.values():
            if cls.is_registered(desc.name):
                for model_id in desc.models:
                    data.append({"id": model_id, "object": "model", "owned_by": desc.owned_by})

        return data
