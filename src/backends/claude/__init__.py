"""Claude backend subpackage.

Re-exports the Claude backend client, auth provider, and registration helpers.

NOTE: Heavy imports (ClaudeCodeCLI, ClaudeAuthProvider) are lazy to avoid
circular imports.  ``src.constants`` imports ``src.backends.claude.constants``
which triggers this ``__init__.py``.  If we eagerly import ``auth.py`` here,
it loops back to ``src.auth`` → ``src.backends.claude.auth`` (circular).
"""

import logging
from typing import Optional

from src.backends.claude.constants import CLAUDE_MODELS
from src.backends.base import BackendDescriptor, BackendRegistry, ResolvedModel

logger = logging.getLogger(__name__)


def _claude_resolve(model: str) -> Optional[ResolvedModel]:
    """Resolve function for the Claude descriptor."""
    if "/" in model:
        prefix, sub_model = model.split("/", 1)
        if prefix == "claude":
            return ResolvedModel(public_model=model, backend="claude", provider_model=sub_model)
        return None
    if model in CLAUDE_MODELS:
        return ResolvedModel(public_model=model, backend="claude", provider_model=model)
    return None


CLAUDE_DESCRIPTOR = BackendDescriptor(
    name="claude",
    owned_by="anthropic",
    models=list(CLAUDE_MODELS),
    resolve_fn=_claude_resolve,
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


def register(registry_cls=None, cwd: Optional[str] = None, timeout: Optional[int] = None) -> None:
    """Register Claude descriptor and client into the BackendRegistry.

    Always registers the descriptor (static metadata).
    Attempts to create a ClaudeCodeCLI instance and register it as a live client.
    """
    import os
    from src.constants import DEFAULT_TIMEOUT_MS
    from src.backends.claude.client import ClaudeCodeCLI

    if registry_cls is None:
        registry_cls = BackendRegistry

    # Always register descriptor
    registry_cls.register_descriptor(CLAUDE_DESCRIPTOR)

    # Create and register client
    try:
        cli = ClaudeCodeCLI(
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT_MS,
            cwd=cwd or os.getenv("CLAUDE_CWD"),
        )
        registry_cls.register("claude", cli)
        logger.info("Registered backend: claude")
    except Exception as e:
        logger.error("Claude backend client creation failed: %s", e)
        raise
