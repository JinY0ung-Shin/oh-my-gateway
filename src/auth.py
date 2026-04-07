import hmac
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Tuple
from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)
# Note: load_dotenv() is called in constants.py at import time


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
        """Return env var names that must be REMOVED when calling this backend.

        Prevents cross-backend key leakage.
        """


# Concrete auth providers are imported lazily to avoid circular imports.
# The cycle is: auth.py -> backends/claude/auth.py -> auth.py (for BackendAuthProvider).
# Direct imports from the .auth submodules (not __init__.py) are safe ONLY AFTER
# this module has finished defining BackendAuthProvider.
# We use a helper function to defer the import to first use.


def _get_claude_auth_provider_class():
    from src.backends.claude.auth import ClaudeAuthProvider

    return ClaudeAuthProvider


# ============================================================================
# ClaudeCodeAuthManager — backward-compatible gateway auth manager
# ============================================================================


class ClaudeCodeAuthManager:
    """Manages authentication for Claude Code SDK integration.

    Backward-compatible wrapper that now delegates to BackendAuthProvider
    instances internally.  All existing public attributes and methods are
    preserved so that callers (main.py, claude_cli.py, tests) continue
    to work without changes.
    """

    def __init__(self):
        self.env_api_key = os.getenv("API_KEY")  # Environment API key
        self.runtime_api_key = None  # Set at startup by run_server()

        # Delegate Claude auth to the provider (lazy import to break circular dep)
        _ClaudeAuthProvider = _get_claude_auth_provider_class()
        self._claude_provider = _ClaudeAuthProvider()
        self.auth_method = self._claude_provider.auth_method
        self.auth_status = self._validate_auth_method()

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
            pass

        # Pre-startup / fallback: direct instantiation for known backends
        if backend == "claude":
            return self._claude_provider
        raise ValueError(f"Unknown backend: {backend!r}")

    # ------------------------------------------------------------------
    # Gateway API key (client → server authentication)
    # ------------------------------------------------------------------

    def get_api_key(self):
        """Get the active API key (environment or runtime-generated)."""
        if self.runtime_api_key:
            return self.runtime_api_key
        return self.env_api_key

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
    """
    # Get the active API key (environment or runtime-generated)
    active_api_key = auth_manager.get_api_key()

    # If no API key is configured, allow all requests
    if not active_api_key:
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

    # Verify the API key (timing-safe comparison to prevent timing attacks)
    if not hmac.compare_digest(credentials.credentials, active_api_key):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return True


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
            result[backend_name] = {"status": {"valid": False, "errors": [str(e)]}}
    return result


# ---------------------------------------------------------------------------
# Backward-compat lazy re-exports for ClaudeAuthProvider / CodexAuthProvider
# so that ``from src.auth import ClaudeAuthProvider`` still works.
# ---------------------------------------------------------------------------
def __getattr__(name):
    if name == "ClaudeAuthProvider":
        return _get_claude_auth_provider_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
