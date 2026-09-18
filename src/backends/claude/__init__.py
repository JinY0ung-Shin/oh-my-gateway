"""Claude backend subpackage.

Re-exports the Claude backend client, auth provider, and registration helpers.

NOTE: Heavy imports (ClaudeCodeCLI, ClaudeAuthProvider) are lazy to avoid
circular imports. ``src.constants`` imports ``src.backends.claude.constants``
which triggers this ``__init__.py``. If we eagerly import ``auth.py`` here,
it loops back to ``src.auth`` → ``src.backends.claude.auth`` (circular).
"""

import logging
import os
from pathlib import Path
from typing import Optional

from src.backends.claude.constants import (
    CLAUDE_MODELS,
    CUSTOM_UPSTREAM_EFFORT_ENV,
    CUSTOM_UPSTREAM_EFFORT_WILDCARD,
    EFFORT_LEVELS,
    configured_model_aliases,
    configured_public_models,
    parse_custom_upstream_effort_certification,
    tier_applies_effort,
)
from src.backends.claude.model_discovery import (
    discover_models,
    discovered_effort_levels,
    discovered_model_ids,
)
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


def _claude_model_entry_meta(model: str) -> dict:
    """Per-entry metadata: alias bookkeeping plus the effort ladder.

    ``effort_levels`` is present exactly when ``capabilities.reasoning_effort``
    is true and lists the levels the model applies, weakest → strongest. On a
    first-party upstream that is the whole ladder; on a certified custom
    upstream it is what the operator declared (a served model's chat template
    decides the subset — see ``CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS``). A client
    offers exactly these levels; the request path rejects any other with a 400
    instead of letting the upstream fail the turn (``validate_model_effort_support``).
    """
    meta = _claude_model_meta(model)
    levels = claude_effort_levels(model)
    if levels is not None:
        meta["effort_levels"] = list(levels)
    return meta


def _custom_upstream_effort_levels(model: str) -> tuple[str, ...] | None:
    """Levels a custom upstream applies for *model*, from the best source available.

    1. ``CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS`` — the operator's word (exact id or
       ``*``), kept as the override for an upstream that cannot state its own.
    2. ``effort_levels`` the upstream itself put on its ``/v1/models`` row
       (``MODEL_DISCOVERY_ENABLED=true``): the litellm_serving sanitizer learns
       each served model's set from the model's own 400s / a probe and publishes
       it there, so nothing has to be copied by hand.

    A nonblank but malformed manual override suppresses discovery fallback
    entirely. Invalid operator input is a global fail-closed state, not the same
    thing as "no override for this model": otherwise a typo in a narrowing
    override could silently widen back to the upstream-discovered ladder.
    """
    raw = os.getenv(CUSTOM_UPSTREAM_EFFORT_ENV)
    if raw is not None and raw.strip():
        try:
            certified = parse_custom_upstream_effort_certification(raw)
        except ValueError as exc:
            logger.warning(
                "%s; certifying nothing and suppressing discovery fallback",
                exc,
            )
            return None
        if model in certified:
            return certified[model]
        wildcard = certified.get(CUSTOM_UPSTREAM_EFFORT_WILDCARD)
        if wildcard is not None:
            return wildcard
    return discovered_effort_levels().get(model)


def claude_effort_levels(model: str) -> tuple[str, ...] | None:
    """The effort levels *model* applies, or ``None`` when effort is not guaranteed.

    Mirrors ``_claude_model_capabilities``: the answer is ``None`` wherever that
    reports ``reasoning_effort: False``.
    """
    custom_upstream = bool((os.getenv("ANTHROPIC_BASE_URL") or "").strip())
    if custom_upstream:
        return _custom_upstream_effort_levels(model)
    if _claude_model_capabilities(model).get("reasoning_effort"):
        return EFFORT_LEVELS
    return None


def _claude_model_capabilities(model: str) -> dict:
    """Narrow ``reasoning_effort`` to the ids where effort is actually applied.

    Accepting ``reasoning.effort`` and having it reach the model are different
    claims, and this backend advertises ids for which only the first holds.

    The CLI decides whether a model supports effort from its own model registry
    **and** whether the base URL is first-party. Against a custom
    ``ANTHROPIC_BASE_URL`` (sanitizer / LiteLLM) with an id it does not know,
    that judgment is false and it would send no effort at all;
    ``CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1`` forces it to send anyway, and **if the
    upstream answers 400 the CLI retries without effort** (see
    ``create_client`` in ``client.py``). The turn then succeeds with the
    requested effort silently dropped.

    So a descriptor-level ``True`` would advertise a guarantee for every id,
    including the arbitrary names configured through ``ANTHROPIC_DEFAULT_*_MODEL``
    and every id discovered from a custom upstream. A client that hides a
    no-op control would then still show one.

    ``reasoning_effort`` is therefore true only where the guarantee holds:

    - no custom ``ANTHROPIC_BASE_URL`` (first-party upstream),
    - a bare tier alias the CLI's own registry resolves, with no
      ``ANTHROPIC_DEFAULT_*_MODEL`` override redirecting that tier to an id we
      cannot vouch for, **and**
    - the concrete model that tier resolves to actually takes an effort.

    That last condition is not the tier's name. ``output_config.effort`` is a
    per-model parameter: Claude Haiku 4.5 — what bare ``haiku`` resolves to on a
    first-party upstream — does not have it and errors on one, while the Opus and
    Sonnet generations do. A tier is therefore asked about its resolved concrete
    model (``tier_applies_effort``), so the answer moves with the generation
    instead of being frozen into the string ``"haiku"``.

    Everything else fails closed. A custom-upstream deployment can explicitly
    certify model ids with ``CLAUDE_CUSTOM_UPSTREAM_EFFORT_MODELS`` — optionally
    with the level subset that upstream accepts, surfaced as ``effort_levels``
    on the ``/v1/models`` entry; this is an operator assertion that the upstream
    applies the forwarded effort value, not a guess made by the gateway. An
    invalid declaration certifies nothing (see ``custom_upstream_effort_certification``).
    ``reasoning_effort_accepted`` stays true for the whole backend — the request
    is still accepted and still forwarded, so a client that wants to offer effort
    as best-effort can read that instead.

    Measured with CLI 2.1.276: against a custom base URL and an unknown model id
    the CLI sends ``output_config.effort`` (default ``high``) and
    ``thinking: {"type": "adaptive"}`` on every request, with or without
    ``CLAUDE_CODE_ALWAYS_ENABLE_EFFORT``. The env stays set for older CLIs; what
    this flag certifies is the UPSTREAM side of that wire.
    """
    custom_upstream = bool((os.getenv("ANTHROPIC_BASE_URL") or "").strip())
    if custom_upstream and _custom_upstream_effort_levels(model) is not None:
        # The upstream stated this model's levels (discovery), or the operator
        # certified it — see ``_custom_upstream_effort_levels``.
        return {"reasoning_effort": True}
    if model not in CLAUDE_MODELS:
        # A configured override name or an id discovered from the upstream:
        # an arbitrary string we cannot match against the CLI's registry.
        return {"reasoning_effort": False}
    if custom_upstream:
        # Custom upstream without an explicit operator certification — effort
        # may be dropped on a 400 retry, so the strong guarantee stays false.
        return {"reasoning_effort": False}
    if model in configured_model_aliases().values():
        # This tier is redirected to a configured concrete id; the CLI resolves
        # the alias to that id, so the guarantee is the id's, not the alias's.
        return {"reasoning_effort": False}
    # First-party tier: the guarantee is the resolved model's to give.
    return {"reasoning_effort": tier_applies_effort(model)}


CLAUDE_DESCRIPTOR = BackendDescriptor(
    name="claude",
    owned_by="anthropic",
    models=configured_public_models(),
    resolve_fn=_claude_resolve,
    # Image input is supported via the client's image_handler (see
    # validate_image_request in src/routes/deps.py).
    # ``reasoning_effort_accepted``: this backend accepts ``reasoning.effort``
    # on the session-creating turn (``_configure_thinking``); every other
    # backend is rejected with 400 by ``_validate_reasoning_backend`` in
    # src/routes/responses.py. Clients read this flag instead of hard-coding
    # "claude".
    #
    # ``reasoning_effort`` is the stronger claim — the effort actually reaches
    # the model — and it is **not** a property of the backend, so it is left to
    # fail closed here and computed per model below. See
    # ``_claude_model_capabilities``.
    capabilities={"image_input": True, "reasoning_effort_accepted": True},
    model_meta_fn=_claude_model_entry_meta,
    model_capabilities_fn=_claude_model_capabilities,
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


def _install_workspace_resource_materializer(cli) -> None:
    """Refresh backend-neutral project resources before SDK client creation.

    WorkspaceManager deliberately avoids recursive mirror scans because file-manager
    polling also calls ``resolve()``. Production Claude turns enter through the
    registered client's ``create_client`` method, so this thin instance wrapper is
    the narrow point where the native `.claude` view must be fresh.
    """
    original_create_client = cli.create_client

    async def create_client_with_workspace_resources(*args, **kwargs):
        cwd = kwargs.get("cwd")
        if cwd:
            from src.backends.claude.workspace_resources import materialize_workspace_resources

            materialize_workspace_resources(Path(cwd))
        return await original_create_client(*args, **kwargs)

    cli.create_client = create_client_with_workspace_resources


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
        _install_workspace_resource_materializer(cli)
        registry_cls.register("claude", cli)
        logger.info("Registered backend: claude")
    except Exception as e:
        logger.error("Claude backend client creation failed: %s", e)
        raise
