"""Docker sandbox routing for ``/v1/responses``.

When Docker per-user sandbox is enabled and the gateway runs in
*orchestrator* mode, this router **replaces** the normal responses
router.  Every request is forwarded to the user's dedicated sandbox
container, which runs the same gateway image in *worker* mode.

The orchestrator never touches the Claude SDK directly — all SDK
interaction happens inside the isolated container.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials

from src.auth import verify_api_key, security
from src.response_models import ResponseCreateRequest
from src.rate_limiter import rate_limit_endpoint

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/responses")
@rate_limit_endpoint("responses")
async def create_sandboxed_response(
    request: Request,
    body: ResponseCreateRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Route ``/v1/responses`` to the user's sandbox container.

    Validates the gateway API key, resolves (or creates) the user's
    sandbox container, and proxies the full request — including SSE
    streams — to that container.
    """
    await verify_api_key(request, credentials)

    if not body.user:
        raise HTTPException(
            status_code=400,
            detail=(
                "Docker per-user sandbox is enabled.  "
                "A 'user' field is required in the request body "
                "to identify which sandbox container to route to."
            ),
        )

    from src.docker_sandbox import sandbox_manager, sandbox_proxy

    if sandbox_manager is None or sandbox_proxy is None:
        raise HTTPException(
            status_code=503,
            detail="Docker sandbox is enabled but not yet initialised",
        )

    # Resolve or create the user's sandbox container
    try:
        container = await sandbox_manager.get_or_create(body.user)
    except RuntimeError as e:
        logger.error("Sandbox creation failed for user=%s: %s", body.user, e)
        raise HTTPException(status_code=503, detail=str(e))

    # Serialise request body for forwarding.
    # exclude_none avoids sending null optional fields that confuse
    # the worker's validation.
    body_dict = body.model_dump(exclude_none=True, mode="json")

    logger.info(
        "Routing request to sandbox: user=%s url=%s stream=%s",
        body.user,
        container.internal_url,
        body.stream,
    )

    if body.stream:
        return await sandbox_proxy.forward_stream(
            container.internal_url, body_dict
        )
    else:
        return await sandbox_proxy.forward_json(
            container.internal_url, body_dict
        )
