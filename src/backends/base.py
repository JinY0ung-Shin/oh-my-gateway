"""Backend registry for multi-backend model dispatch.

Provides model resolution, a BackendClient protocol, and a singleton registry
so endpoint code dispatches by interface rather than concrete backend type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Union,
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

    ``capabilities`` carries feature flags surfaced in ``/v1/models``
    (e.g. ``{"image_input": True}``).
    """

    name: str
    owned_by: str
    models: List[str]
    resolve_fn: Callable[[str], Optional[ResolvedModel]]
    capabilities: Dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Session handle protocol
# ---------------------------------------------------------------------------


class SessionHandle(Protocol):
    """Per-session handle returned by ``BackendClient.create_client``.

    Each backend keeps its own concrete handle (e.g. ``OpenCodeSessionClient``,
    ``CodexSessionClient``); the only shared contract is that the gateway can
    release it on cleanup via ``disconnect()``.
    """

    async def disconnect(self) -> None: ...


# ---------------------------------------------------------------------------
# BackendClient protocol
# ---------------------------------------------------------------------------


class BackendClient(Protocol):
    """Interface that every backend must satisfy.

    Method names intentionally match ``ClaudeCodeCLI`` so the existing
    implementation is already structurally compatible.
    """

    @property
    def name(self) -> str: ...

    def supported_models(self) -> List[str]: ...

    def get_auth_provider(self) -> Any: ...

    async def create_client(
        self,
        *,
        session: Any,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: Optional[str] = None,
        mcp_servers: Optional[Dict[str, Any]] = None,
        task_budget: Optional[int] = None,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        model_params: Optional[Dict[str, Any]] = None,
        _custom_base: Any = None,
    ) -> Any: ...

    def run_completion_with_client(
        self,
        client: Any,
        prompt: Union[str, List[Dict[str, Any]]],
        session: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Execute a turn against an existing backend client.

        ``prompt`` may be either a plain string or a list of backend-native
        input items, emitted only when the request body carried natively
        supported multimodal parts: Codex receives Codex turn-input items,
        Claude receives Anthropic content blocks (inline images, issue #140).
        Backends that don't carry multimodal payloads natively (OpenCode)
        never see the list shape.
        """
        ...

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
        client   = await backend.create_client(session=session, ...)
        async for chunk in backend.run_completion_with_client(client, prompt, session):
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
    def all_model_ids(cls) -> set[str]:
        """Return a set of all model IDs across all descriptors."""
        ids: set[str] = set()
        for desc in cls._descriptors.values():
            ids.update(desc.models)
        return ids

    @classmethod
    def available_models(cls) -> List[Dict[str, Any]]:
        """Build the ``/v1/models`` data list from registered backends.

        Keeps the original ``id``/``object``/``owned_by`` fields for
        compatibility and adds ``backend`` plus a ``capabilities`` map
        (``image_input`` is always present).
        """
        data: List[Dict[str, Any]] = []

        for desc in cls._descriptors.values():
            if cls.is_registered(desc.name):
                for model_id in desc.models:
                    data.append(
                        {
                            "id": model_id,
                            "object": "model",
                            "owned_by": desc.owned_by,
                            "backend": desc.name,
                            "capabilities": {"image_input": False, **desc.capabilities},
                        }
                    )

        return data
