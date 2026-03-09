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

    Each backend (Claude, Codex, etc.) implements this to manage its own
    API key validation and environment variable injection.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend identifier (e.g. 'claude', 'codex')."""

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

        Prevents cross-backend key leakage. For example, Claude's provider
        returns ["OPENAI_API_KEY"] so that Codex credentials are never
        visible to the Claude SDK process.
        """


class ClaudeAuthProvider(BackendAuthProvider):
    """Claude backend auth — delegates to ANTHROPIC_AUTH_TOKEN or CLI auth."""

    @property
    def name(self) -> str:
        return "claude"

    def __init__(self):
        self.auth_method = self._detect_method()

    def _detect_method(self) -> str:
        explicit = os.getenv("CLAUDE_AUTH_METHOD", "").lower()
        if explicit:
            method_map = {
                "cli": "claude_cli",
                "claude_cli": "claude_cli",
                "api_key": "anthropic",
                "anthropic": "anthropic",
            }
            if explicit in method_map:
                return method_map[explicit]
            raise ValueError(
                f"Unsupported CLAUDE_AUTH_METHOD '{explicit}'. "
                f"Supported values: {', '.join(method_map.keys())}"
            )
        if os.getenv("ANTHROPIC_AUTH_TOKEN"):
            return "anthropic"
        return "claude_cli"

    def validate(self) -> Dict[str, Any]:
        if self.auth_method == "anthropic":
            key = os.getenv("ANTHROPIC_AUTH_TOKEN")
            if not key:
                return {
                    "valid": False,
                    "errors": ["ANTHROPIC_AUTH_TOKEN environment variable not set"],
                    "config": {},
                }
            if len(key) < 10:
                logger.warning("ANTHROPIC_AUTH_TOKEN is shorter than 10 characters")
            return {
                "valid": True,
                "errors": [],
                "config": {"api_key_present": True, "api_key_length": len(key)},
            }
        # claude_cli — assume valid, actual check happens at SDK call time
        return {
            "valid": True,
            "errors": [],
            "config": {
                "method": "Claude Code CLI authentication",
                "note": "Using existing Claude Code CLI authentication",
            },
        }

    def build_env(self) -> Dict[str, str]:
        if self.auth_method == "anthropic":
            key = os.getenv("ANTHROPIC_AUTH_TOKEN")
            if key:
                return {"ANTHROPIC_AUTH_TOKEN": key}
        return {}

    def get_isolation_vars(self) -> List[str]:
        return ["OPENAI_API_KEY"]


class CodexAuthProvider(BackendAuthProvider):
    """Codex backend auth — uses upstream-standard OPENAI_API_KEY."""

    @property
    def name(self) -> str:
        return "codex"

    def validate(self) -> Dict[str, Any]:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            return {
                "valid": False,
                "errors": ["OPENAI_API_KEY environment variable not set"],
                "config": {},
            }
        if not key.startswith("sk-"):
            logger.warning("OPENAI_API_KEY doesn't match expected sk-... format")
        return {
            "valid": True,
            "errors": [],
            "config": {"api_key_present": True, "api_key_length": len(key)},
        }

    def build_env(self) -> Dict[str, str]:
        key = os.getenv("OPENAI_API_KEY")
        return {"OPENAI_API_KEY": key} if key else {}

    def get_isolation_vars(self) -> List[str]:
        return ["ANTHROPIC_AUTH_TOKEN"]


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

        # Delegate Claude auth to the provider
        self._claude_provider = ClaudeAuthProvider()
        self.auth_method = self._claude_provider.auth_method
        self.auth_status = self._validate_auth_method()

        # Codex provider (lazy — only used when explicitly requested)
        self._codex_provider: Optional[CodexAuthProvider] = None

    # ------------------------------------------------------------------
    # Backend provider access
    # ------------------------------------------------------------------

    def get_provider(self, backend: str) -> BackendAuthProvider:
        """Return the auth provider for the given backend name."""
        if backend == "claude":
            return self._claude_provider
        if backend == "codex":
            if self._codex_provider is None:
                self._codex_provider = CodexAuthProvider()
            return self._codex_provider
        raise ValueError(f"Unknown backend: {backend!r}")

    # ------------------------------------------------------------------
    # Gateway API key (client → server authentication)
    # ------------------------------------------------------------------

    def get_api_key(self):
        """Get the active API key (environment or runtime-generated)."""
        # Try to import runtime_api_key from main module
        try:
            from src import main

            if hasattr(main, "runtime_api_key") and main.runtime_api_key:
                return main.runtime_api_key
        except ImportError:
            pass

        # Fall back to environment variable
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
    """Get authentication info for all backends (for /v1/auth/status)."""
    result = {}
    for backend_name in ("claude", "codex"):
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
