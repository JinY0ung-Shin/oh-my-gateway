"""Claude backend subpackage.

Re-exports the Claude backend client, auth provider, and registration helpers.

NOTE: Heavy imports (ClaudeCodeCLI, ClaudeAuthProvider) are lazy to avoid
circular imports.  ``src.constants`` imports ``src.backends.claude.constants``
which triggers this ``__init__.py``.  If we eagerly import ``auth.py`` here,
it loops back to ``src.auth`` → ``src.backends.claude.auth`` (circular).
"""

import logging
from typing import Optional

from src.backends.claude.constants import (
    CLAUDE_MODELS,
    configured_model_aliases,
    configured_public_models,
)
from src.backends.claude.model_discovery import discover_models, discovered_model_ids
from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel

logger = logging.getLogger(__name__)


def _claude_resolve(model: str) -> Optional[ResolvedModel]:
    """Resolve function for the Claude descriptor."""
    # Names configured via ANTHROPIC_DEFAULT_*_MODEL resolve back to their bare
    # alias so the Claude CLI performs the real alias->model resolution. Checked
    # first so a configured name containing "/" is not swallowed by the
    # claude/<sub-model> heuristic below.
    alias = configured_model_aliases().get(model)
    if alias is not None:
        return ResolvedModel(public_model=model, backend="claude", provider_model=alias)

    # ``claude/<model>`` is a RESERVED namespace meaning "route <model> to this
    # backend", so it is resolved before the discovery allowlist below. An
    # upstream that happens to advertise an id spelled ``claude/foo`` must not
    # reinterpret an explicit request as a literal model name.
    if model.startswith("claude/"):
        _, sub_model = model.split("/", 1)
        return ResolvedModel(public_model=model, backend="claude", provider_model=sub_model)

    # A bare/provider-qualified ID learned from the upstream /v1/models endpoint
    # is safe to pass through exactly. Do not accept every unknown bare string:
    # discovery acts as the allowlist so typos and another backend's IDs are not
    # silently claimed by Claude.
    if model in discovered_model_ids():
        return ResolvedModel(public_model=model, backend="claude", provider_model=model)

    if "/" in model:
        # Another backend's namespace, and discovery never learned it.
        return None
    if model in CLAUDE_MODELS:
        return ResolvedModel(public_model=model, backend="claude", provider_model=model)
    return None


def _claude_model_meta(model: str) -> dict:
    """Alias bookkeeping for ``/v1/models`` entries.

    Clients need to tell the bare ``opus``/``sonnet``/``haiku`` aliases apart
    from the concrete ids configured via ``ANTHROPIC_DEFAULT_*_MODEL``, so they
    can offer the deployment's actual model names instead of both:

    - configured name → ``{"alias_of": "sonnet"}``
    - bare alias with an override set → ``{"configured_as": "<name>"}``

    No override configured means no extra fields (unchanged default surface).
    """
    aliases = configured_model_aliases()  # configured name -> bare alias
    alias = aliases.get(model)
    if alias is not None:
        return {"alias_of": alias}
    if model in CLAUDE_MODELS:
        # Bare tier alias. Clients that want to show only real deployment model
        # names filter on ``alias`` alone; ``configured_as`` says which name
        # supersedes this one when an override is set.
        meta: dict = {"alias": True}
        for name, bare in aliases.items():
            if bare == model:
                meta["configured_as"] = name
        return meta
    return {}


CLAUDE_DESCRIPTOR = BackendDescriptor(
    name="claude",
    owned_by="anthropic",
    models=configured_public_models(),
    resolve_fn=_claude_resolve,
    # Image input is supported via the client's image_handler (see
    # validate_image_request in src/routes/deps.py).
    capabilities={"image_input": True},
    model_meta_fn=_claude_model_meta,
    model_discovery_fn=discover_models,
)


# Lazy re-exports — deferred to avoid circular imports at module load time.
def __getattr__(name):
    if name == "ClaudeCodeCLI":
        from src.backends.claude.client import ClaudeCodeCLI

        return ClaudeCodeCLI
    if name == "ClaudeAuthProvider":
        from src.backends.claude.auth import ClaudeAuthProvider

        return ClaudeAuthProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def register(registry_cls=None, cwd: Optional[str] = None) -> None:
    """Register Claude descriptor and client into the BackendRegistry.

    Always registers the descriptor (static metadata).
    Attempts to create a ClaudeCodeCLI instance and register it as a live client.
    """
    from src.backends.claude.client import ClaudeCodeCLI

    if registry_cls is None:
        registry_cls = BackendRegistry

    # Always register descriptor
    registry_cls.register_descriptor(CLAUDE_DESCRIPTOR)

    # Create and register client. With no explicit cwd the client falls back to
    # a private temp dir; live requests always override cwd per request from the
    # resolved per-user workspace.
    try:
        cli = ClaudeCodeCLI(cwd=cwd)
        registry_cls.register("claude", cli)
        logger.info("Registered backend: claude")
    except Exception as e:
        logger.error("Claude backend client creation failed: %s", e)
        raise
