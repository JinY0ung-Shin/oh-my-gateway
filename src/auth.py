import hmac
import os
import logging
from typing import Optional, Dict, Any, Tuple
from fastapi import HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)
# Note: load_dotenv() is called in constants.py at import time


class ClaudeCodeAuthManager:
    """Manages authentication for Claude Code SDK integration."""

    def __init__(self):
        self.env_api_key = os.getenv("API_KEY")  # Environment API key
        self.auth_method = self._detect_auth_method()
        self.auth_status = self._validate_auth_method()

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

    def _detect_auth_method(self) -> str:
        """Detect which Claude Code authentication method is configured.

        Priority:
        1. Explicit CLAUDE_AUTH_METHOD env var (cli, api_key)
        2. Auto-detect based on ANTHROPIC_AUTH_TOKEN presence
        3. Default to claude_cli
        """
        # Check for explicit auth method first
        explicit_method = os.getenv("CLAUDE_AUTH_METHOD", "").lower()
        if explicit_method:
            method_map = {
                "cli": "claude_cli",
                "claude_cli": "claude_cli",
                "api_key": "anthropic",
                "anthropic": "anthropic",
            }
            if explicit_method in method_map:
                logger.info(f"Using explicit auth method: {method_map[explicit_method]}")
                return method_map[explicit_method]
            else:
                raise ValueError(
                    f"Unsupported CLAUDE_AUTH_METHOD '{explicit_method}'. "
                    f"Supported values: {', '.join(method_map.keys())}"
                )

        # Auto-detect based on environment
        if os.getenv("ANTHROPIC_AUTH_TOKEN"):
            return "anthropic"
        else:
            # If no explicit method, assume Claude Code CLI is already authenticated
            return "claude_cli"

    def _validate_auth_method(self) -> Dict[str, Any]:
        """Validate the detected authentication method."""
        method = self.auth_method
        status = {"method": method, "valid": False, "errors": [], "config": {}}

        if method == "anthropic":
            status.update(self._validate_anthropic_auth())
        elif method == "claude_cli":
            status.update(self._validate_claude_cli_auth())
        else:
            status["errors"].append("No Claude Code authentication method configured")

        return status

    def _validate_anthropic_auth(self) -> Dict[str, Any]:
        """Validate Anthropic API key authentication."""
        api_key = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            return {
                "valid": False,
                "errors": ["ANTHROPIC_AUTH_TOKEN environment variable not set"],
                "config": {},
            }

        if len(api_key) < 10:
            logger.warning("ANTHROPIC_AUTH_TOKEN is shorter than 10 characters")

        return {
            "valid": True,
            "errors": [],
            "config": {"api_key_present": True, "api_key_length": len(api_key)},
        }

    def _validate_claude_cli_auth(self) -> Dict[str, Any]:
        """Validate that Claude Code CLI is already authenticated."""
        # For CLI authentication, we assume it's valid and let the SDK handle auth
        # The actual validation will happen when we try to use the SDK
        return {
            "valid": True,
            "errors": [],
            "config": {
                "method": "Claude Code CLI authentication",
                "note": "Using existing Claude Code CLI authentication",
            },
        }

    # Stale env vars that should never leak to the SDK process
    _STALE_ENV_VARS = [
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
    ]

    def get_claude_code_env_vars(self) -> Dict[str, str]:
        """Get environment variables needed for Claude Code SDK."""
        env_vars = {}

        if self.auth_method == "anthropic":
            if os.getenv("ANTHROPIC_AUTH_TOKEN"):
                env_vars["ANTHROPIC_AUTH_TOKEN"] = os.getenv("ANTHROPIC_AUTH_TOKEN")

        elif self.auth_method == "claude_cli":
            # For CLI auth, don't set any environment variables
            # Let Claude Code SDK use the existing CLI authentication
            pass

        return env_vars

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


def get_claude_code_auth_info() -> Dict[str, Any]:
    """Get Claude Code authentication information for diagnostics."""
    return {
        "method": auth_manager.auth_method,
        "status": auth_manager.auth_status,
        "environment_variables": list(auth_manager.get_claude_code_env_vars().keys()),
    }
