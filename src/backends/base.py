"""Backend registry for multi-backend model dispatch.

Provides model resolution, a BackendClient protocol, and a singleton registry
so endpoint code dispatches by interface rather than concrete backend type.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Union,
)

logger = logging.getLogger(__name__)


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
    """Metadata and optional discovery hooks for a known backend.

    Separates "known backends" from "live clients" so that model resolution
    and auth status work even if a backend failed to start.

    ``capabilities`` carries feature flags surfaced in ``/v1/models``
    (e.g. ``{"image_input": True}``).

    ``model_meta_fn`` optionally adds per-model fields to the ``/v1/models``
    entry (e.g. alias bookkeeping so clients can tell a bare ``sonnet`` from
    the concrete id configured via ``ANTHROPIC_DEFAULT_SONNET_MODEL``).

    ``model_discovery_fn`` optionally returns additional model IDs from a live
    upstream. Discovery is best-effort: the registry preserves the static
    model list if a hook fails, so ``/v1/models`` never depends on upstream
    availability.
    """

    name: str
    owned_by: str
    models: List[str]
    resolve_fn: Callable[[str], Optional[ResolvedModel]]
    capabilities: Dict[str, bool] = field(default_factory=dict)
    model_meta_fn: Optional[Callable[[str], Dict[str, Any]]] = None
    model_discovery_fn: Optional[Callable[[], Awaitable[List[str]]]] = None


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
        """Register a backend descriptor (model metadata/discovery)."""
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
        """Return a set of all statically declared model IDs."""
        ids: set[str] = set()
        for desc in cls._descriptors.values():
            ids.update(desc.models)
        return ids

    @staticmethod
    def _model_entry(desc: BackendDescriptor, model_id: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "id": model_id,
            "object": "model",
            "owned_by": desc.owned_by,
            "backend": desc.name,
            "capabilities": {"image_input": False, **desc.capabilities},
        }
        if desc.model_meta_fn is not None:
            entry.update(desc.model_meta_fn(model_id))
        return entry

    @classmethod
    def available_models(cls) -> List[Dict[str, Any]]:
        """Build the static ``/v1/models`` data list from registered backends.

        Keeps the original ``id``/``object``/``owned_by`` fields for
        compatibility and adds ``backend`` plus a ``capabilities`` map
        (``image_input`` is always present). A descriptor's ``model_meta_fn``
        may contribute extra per-model fields (alias bookkeeping).
        """
        data: List[Dict[str, Any]] = []

        for desc in cls._descriptors.values():
            if cls.is_registered(desc.name):
                data.extend(cls._model_entry(desc, model_id) for model_id in desc.models)

        return data

    @classmethod
    async def warm_model_discovery(cls) -> None:
        """Prime every registered backend's discovery cache before serving.

        Model resolution is synchronous, so dynamic IDs can only resolve after
        their backend's discovery hook has populated its cache. Startup calls
        this method before readiness, removing any dependency on a client first
        calling ``GET /v1/models``. Failures remain isolated per backend.
        """
        for name, desc in cls._descriptors.items():
            if desc.model_discovery_fn is None or not cls.is_registered(name):
                continue
            try:
                await desc.model_discovery_fn()
            except Exception:
                logger.warning(
                    "model discovery warm-up failed for backend %s",
                    name,
                    exc_info=True,
                )

    @classmethod
    async def available_models_async(cls) -> List[Dict[str, Any]]:
        """Build ``/v1/models`` including best-effort live discovery.

        Static descriptor models are always returned first. Dynamic IDs are
        appended per backend, preserving upstream order and skipping duplicates.
        A broken/slow discovery hook is isolated to that backend and never makes
        the model-list endpoint fail.
        """
        data: List[Dict[str, Any]] = []

        for desc in cls._descriptors.values():
            if not cls.is_registered(desc.name):
                continue

            model_ids = list(desc.models)
            if desc.model_discovery_fn is not None:
                try:
                    discovered = await desc.model_discovery_fn()
                except Exception:
                    logger.warning(
                        "model discovery failed for backend %s; using static models",
                        desc.name,
                        exc_info=True,
                    )
                    discovered = []
                seen = set(model_ids)
                for model_id in discovered:
                    if isinstance(model_id, str) and model_id and model_id not in seen:
                        seen.add(model_id)
                        model_ids.append(model_id)

            data.extend(cls._model_entry(desc, model_id) for model_id in model_ids)

        return data
