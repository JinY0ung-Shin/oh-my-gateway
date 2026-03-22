import re
from typing import Optional
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse

from src.constants import RATE_LIMITS
from src.env_utils import parse_bool_env


def get_rate_limit_key(request: Request) -> str:
    """Get the rate limiting key (IP address) from the request."""
    return get_remote_address(request)


def create_rate_limiter() -> Optional[Limiter]:
    """Create and configure the rate limiter based on environment variables."""
    rate_limit_enabled = parse_bool_env("RATE_LIMIT_ENABLED", "true")

    if not rate_limit_enabled:
        return None

    # Create limiter with IP-based identification
    limiter = Limiter(
        key_func=get_rate_limit_key,
        default_limits=[],  # We'll apply limits per endpoint
    )

    return limiter


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Custom rate limit exceeded handler that returns JSON error response."""
    # Try to extract actual retry_after from the exception detail
    retry_after = 60
    detail = getattr(exc, "detail", "") or ""
    if isinstance(detail, str):
        # slowapi detail format: "Rate limit exceeded: N per M unit"
        # N is the request count, M+unit is the window. Parse the window as retry_after.
        unit_seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
        match = re.search(r"(\d+)\s+per\s+(\d+)\s+(second|minute|hour|day)", detail)
        if match:
            period = int(match.group(2))
            unit = match.group(3)
            retry_after = period * unit_seconds.get(unit, 60)

    response = JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": f"Rate limit exceeded. Try again in {retry_after} seconds.",
                "type": "rate_limit_exceeded",
                "code": "too_many_requests",
                "retry_after": retry_after,
            }
        },
        headers={"Retry-After": str(retry_after)},
    )
    return response


def get_rate_limit_for_endpoint(endpoint: str) -> str:
    """Get rate limit string for specific endpoint from constants."""
    rate_per_minute = RATE_LIMITS.get(endpoint, RATE_LIMITS["general"])
    return f"{rate_per_minute}/minute"


def rate_limit_endpoint(endpoint: str):
    """Decorator factory for applying rate limits to endpoints."""

    def decorator(func):
        if limiter:
            return limiter.limit(get_rate_limit_for_endpoint(endpoint))(func)
        return func

    return decorator


# Create the global limiter instance
limiter = create_rate_limiter()
