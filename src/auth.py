import hmac
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Tuple
from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from src.env_utils import parse_bool_env_strict, parse_user_api_keys

logger = logging.getLogger(__name__)
# Note: load_dotenv() runs in src/__init__.py, before any src submodule import

# Strict-identity mode (#251): when on, a request to a per-user surface
# (/v1/responses + its continuation/retrieval routes, /v1/sessions/* and the
# workspace file API) is refused unless its identity is bound to the credential
# — i.e. a USER_API_KEYS token. It closes the #249 impersonation vector
# (caller-chosen body/query/header `user`) for operators who opt in, instead of
# only warning. A legacy unscoped API_KEY carries no principal, so a request
# authenticated with it is refused here; no-auth is refused too (and already
# default-open-warned). Startup (src/config_check) fails fast when this is on
# without USER_API_KEYS, so the mode can never refuse *every* request silently.
REQUIRE_SCOPED_IDENTITY_ENV = "GATEWAY_REQUIRE_SCOPED_IDENTITY"


def scoped_identity_required() -> bool:
    """Whether strict per-user identity binding is enforced on identity routes.

    Parsed strictly: an unrecognized value (a typo, a stray word) is rejected at
    startup by ``config_check._check_scoped_identity``. Should one slip past that
    (e.g. ``SKIP_CONFIG_CHECK=true``), this fails **closed** — a security switch
    must never silently fall open on a misread — and enforces.
    """
    try:
        return parse_bool_env_strict(REQUIRE_SCOPED_IDENTITY_ENV, False)
    except ValueError:
        logger.warning(
            "%s has an unrecognized value; enforcing scoped identity "
            "(fail-closed). Set a valid boolean (startup normally rejects this).",
            REQUIRE_SCOPED_IDENTITY_ENV,
        )
        return True


# ============================================================================
# Backend Auth Provider — abstract base for per-backend authentication
# ============================================================================


class BackendAuthProvider(ABC):
    """Abstract base for backend-specific authentication.

    Each backend implements this to manage its own API key validation
    and environment variable injection.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier (e.g. 'claude')."""

    @abstractmethod
    def validate(self) -> Dict[str, Any]:
        """Validate backend authentication.

        Returns dict with keys: valid (bool), errors (list[str]), config (dict).
        """

    @abstractmethod
    def build_env(self) -> Dict[str, str]:
        """Return env vars to inject for this backend's subprocess/SDK calls."""

    @abstractmethod
    def get_isolation_vars(self) -> List[str]:
        """Return env var names that must not reach this backend's child process.

        Removed where the transport can drop variables (app-server
        ``env_remove``), blanked where it can only add them (Claude
        ``options.env``). Prevents cross-backend key leakage.
        """


# Concrete auth providers are imported lazily to avoid circular imports.
# The cycle is: auth.py -> backends/claude/auth.py -> auth.py (for BackendAuthProvider).
# Direct imports from the .auth submodules (not __init__.py) are safe ONLY AFTER
# this module has finished defining BackendAuthProvider.
# We use a helper function to defer the import to first use.


def _get_claude_auth_provider_class():
    from src.backends.claude.auth import ClaudeAuthProvider

    return ClaudeAuthProvider


def _get_opencode_auth_provider_class():
    from src.backends.opencode.auth import OpenCodeAuthProvider

    return OpenCodeAuthProvider


def _get_codex_auth_provider_class():
    from src.backends.codex.auth import CodexAuthProvider

    return CodexAuthProvider


# ============================================================================
# ClaudeCodeAuthManager — backward-compatible gateway auth manager
# ============================================================================


class ClaudeCodeAuthManager:
    """Manages authentication for Claude Code SDK integration.

    Backward-compatible wrapper that now delegates to BackendAuthProvider
    instances internally.  All existing public attributes and methods are
    preserved so that callers (main.py, claude_cli.py, tests) continue
    to work without changes.

    Gateway client authentication supports two modes that may coexist:

    * ``API_KEY`` — the legacy unscoped/service key. Holders may act across
      users, preserving the historical single-user behavior.
    * ``USER_API_KEYS`` — a JSON object mapping a workspace/user id to a
      bearer token, e.g. ``{"alice":"secret-a","bob":"secret-b"}``.
      A matching token yields an authenticated user principal that middleware
      binds to the request so caller-supplied ``user`` values cannot select a
      different tenant/workspace.
    """

    def __init__(self):
        self.env_api_key = os.getenv("API_KEY")  # Legacy unscoped/service API key
        self.runtime_api_key = None  # Set at startup by run_server()
        self.user_api_keys = self._load_user_api_keys()

        # Delegate Claude auth to the provider (lazy import to break circular dep)
        _ClaudeAuthProvider = _get_claude_auth_provider_class()
        self._claude_provider = _ClaudeAuthProvider()
        self.auth_method = self._claude_provider.auth_method
        self.auth_status = self._validate_auth_method()

    @staticmethod
    def _load_user_api_keys() -> Dict[str, str]:
        """Parse and validate ``USER_API_KEYS`` (user -> bearer token).

        Invalid configuration fails fast at import/startup rather than silently
        falling back to an unscoped identity boundary. The parse itself lives in
        ``src.env_utils.parse_user_api_keys`` — a single side-effect-free source
        the startup config check (``src.config_check``) shares, so the check's
        notion of "authenticated" can never drift from this one.
        """
        return parse_user_api_keys(os.getenv("USER_API_KEYS"))

    # ------------------------------------------------------------------
    # Backend provider access
    # ------------------------------------------------------------------

    def get_provider(self, backend: str) -> BackendAuthProvider:
        """Return the auth provider for the given backend name.

        Tries registry first (post-startup path), then falls back to
        direct instantiation for known backends (pre-startup path).
        """
        # Post-startup: try to get auth provider from live backend client
        try:
            from src.backends.base import BackendRegistry

            if BackendRegistry.is_registered(backend):
                client = BackendRegistry.get(backend)
                if hasattr(client, "get_auth_provider"):
                    return client.get_auth_provider()
        except Exception:
            logger.debug(
                "live auth provider lookup failed for %s", backend, exc_info=True
            )

        # Pre-startup / fallback: direct instantiation for known backends
        if backend == "claude":
            return self._claude_provider
        if backend == "opencode":
            return _get_opencode_auth_provider_class()()
        if backend == "codex":
            return _get_codex_auth_provider_class()()
        raise ValueError(f"Unknown backend: {backend!r}")

    # ------------------------------------------------------------------
    # Gateway API key (client → server authentication)
    # ------------------------------------------------------------------

    def get_api_key(self):
        """Get the active legacy unscoped API key (environment or runtime-generated)."""
        if self.runtime_api_key:
            return self.runtime_api_key
        return self.env_api_key

    def has_api_auth(self) -> bool:
        """True when either legacy or user-scoped bearer authentication is configured."""
        return bool(self.get_api_key() or self.user_api_keys)

    def authenticate_gateway_key(self, provided_key: str) -> Tuple[bool, Optional[str]]:
        """Authenticate a gateway bearer key and return ``(valid, principal)``.

        ``principal`` is the authenticated user for a ``USER_API_KEYS`` match.
        A legacy ``API_KEY`` match returns ``None`` to preserve its historical
        unscoped/service-key behavior.
        """
        matched_user: Optional[str] = None
        # Compare every configured user key so the position of a matching entry
        # does not leak through a short-circuit timing difference.
        for user, configured_key in self.user_api_keys.items():
            if hmac.compare_digest(provided_key, configured_key):
                matched_user = user

        legacy_key = self.get_api_key()
        legacy_match = bool(
            legacy_key and hmac.compare_digest(provided_key, legacy_key)
        )

        if matched_user is not None:
            return True, matched_user
        if legacy_match:
            return True, None
        return False, None

    # ------------------------------------------------------------------
    # Claude-specific validation (backward compat)
    # ------------------------------------------------------------------

    def _validate_auth_method(self) -> Dict[str, Any]:
        """Validate the detected authentication method."""
        status = {"method": self.auth_method, "valid": False, "errors": [], "config": {}}
        status.update(self._claude_provider.validate())
        return status

    # Stale env vars that should never leak to the SDK process
    _STALE_ENV_VARS = [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
    ]

    def get_claude_code_env_vars(self) -> Dict[str, str]:
        """Get environment variables needed for Claude Code SDK."""
        return self._claude_provider.build_env()

    def clean_stale_env_vars(self):
        """Remove stale Bedrock/Vertex env vars from the process environment."""
        for var in self._STALE_ENV_VARS:
            if var in os.environ:
                logger.warning(f"Removing stale environment variable: {var}")
                del os.environ[var]


# Initialize the auth manager
auth_manager = ClaudeCodeAuthManager()

# HTTP Bearer security scheme (for FastAPI endpoint protection)
security = HTTPBearer(auto_error=False)


async def verify_api_key(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials] = None
):
    """
    Verify API key if one is configured for FastAPI endpoint protection.
    This is separate from Claude Code authentication.

    When a ``USER_API_KEYS`` token matches, the credential-derived user is
    published on ``request.state.auth_user`` for route-level tenant checks.
    """
    if not auth_manager.has_api_auth():
        return True

    # Get credentials from Authorization header
    if credentials is None:
        credentials = await security(request)

    # Check if credentials were provided
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    valid, principal = auth_manager.authenticate_gateway_key(credentials.credentials)
    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if principal is not None:
        request.state.auth_user = principal

    return True


def get_authenticated_user(request: Request) -> Optional[str]:
    """Return the credential-derived user principal, if this request has one."""
    state = getattr(request, "state", None)
    principal = getattr(state, "auth_user", None) if state is not None else None
    return principal if isinstance(principal, str) and principal else None


def enforce_scoped_identity(request: Request) -> None:
    """Refuse a per-user request whose identity is not credential-bound.

    No-op unless ``GATEWAY_REQUIRE_SCOPED_IDENTITY`` is on. When on, the request
    must carry a ``USER_API_KEYS`` principal (the only credential
    ``ConcurrencyLimitMiddleware`` binds to the request's ``user``); a no-auth or
    legacy-``API_KEY`` request — where ``user`` is caller-chosen — is refused with
    403. Call it on every route that resolves a tenant from caller input, after
    ``verify_api_key`` has run.
    """
    if not scoped_identity_required():
        return
    if get_authenticated_user(request) is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "This gateway requires a per-user credential: set "
                "GATEWAY_REQUIRE_SCOPED_IDENTITY only with USER_API_KEYS, and "
                "authenticate with a user token (the unscoped API_KEY cannot "
                "bind a caller's identity)."
            ),
            headers={"x-should-retry": "false"},
        )


def validate_claude_code_auth() -> Tuple[bool, Dict[str, Any]]:
    """
    Validate Claude Code authentication and return status.
    Returns (is_valid, status_info)
    """
    status = auth_manager.auth_status

    if not status["valid"]:
        logger.error(f"Claude Code authentication failed: {status['errors']}")
        return False, status

    logger.info(f"Claude Code authentication validated: {status['method']}")
    return True, status


def validate_backend_auth(backend: str) -> Tuple[bool, Dict[str, Any]]:
    """Validate authentication for any registered backend.

    Returns (is_valid, status_info).
    """
    provider = auth_manager.get_provider(backend)
    status = {"method": backend, **provider.validate()}
    if not status["valid"]:
        logger.error(f"{backend} authentication failed: {status['errors']}")
    return status["valid"], status


def get_claude_code_auth_info() -> Dict[str, Any]:
    """Get Claude Code authentication information for diagnostics."""
    return {
        "method": auth_manager.auth_method,
        "status": auth_manager.auth_status,
        "environment_variables": list(auth_manager.get_claude_code_env_vars().keys()),
    }


def get_all_backends_auth_info() -> Dict[str, Any]:
    """Get authentication info for all backends (for /v1/auth/status).

    Iterates descriptors from BackendRegistry so all known backends
    (including unavailable ones) are reported.
    """
    from src.backends.base import BackendRegistry

    result = {}

    # Use descriptors to discover all known backends
    descriptors = BackendRegistry.all_descriptors()
    backend_names = set(descriptors.keys())
    # Also include hard-coded fallback for pre-startup
    backend_names.add("claude")

    for backend_name in sorted(backend_names):
        try:
            provider = auth_manager.get_provider(backend_name)
            status = provider.validate()
            result[backend_name] = {
                "status": status,
                "environment_variables": list(provider.build_env().keys()),
            }
        except Exception as e:
            logger.warning(
                "auth validate failed for %s", backend_name, exc_info=True
            )
            result[backend_name] = {"status": {"valid": False, "errors": [str(e)]}}
    return result


# ---------------------------------------------------------------------------
# Backward-compat lazy re-export for ClaudeAuthProvider
# so that ``from src.auth import ClaudeAuthProvider`` still works.
# ---------------------------------------------------------------------------
def __getattr__(name):
    if name == "ClaudeAuthProvider":
        return _get_claude_auth_provider_class()
    if name == "OpenCodeAuthProvider":
        return _get_opencode_auth_provider_class()
    if name == "CodexAuthProvider":
        return _get_codex_auth_provider_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
