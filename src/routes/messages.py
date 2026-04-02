"""Anthropic Messages API endpoint (/v1/messages)."""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials

from src.models import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicTextBlock,
    AnthropicUsage,
)
from src.message_adapter import MessageAdapter
from src.auth import verify_api_key, security
from src.backends import BackendRegistry, resolve_model
from src.backends.claude.constants import DEFAULT_ALLOWED_TOOLS
from src.rate_limiter import rate_limit_endpoint
from src.constants import PERMISSION_MODE_BYPASS
from src.runtime_config import get_default_max_turns
from src.mcp_config import get_mcp_servers
from src import streaming_utils
from src.routes.deps import (
    validate_backend_auth_or_raise,
    validate_image_request,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/messages")
@rate_limit_endpoint("chat")
async def anthropic_messages(
    request_body: AnthropicMessagesRequest,
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Anthropic Messages API compatible endpoint (Claude-only).

    This endpoint provides compatibility with the native Anthropic SDK,
    allowing tools like VC to use this wrapper via the VC_API_BASE setting.
    Only Claude models are supported on this endpoint.
    """
    await verify_api_key(request, credentials)

    # Validate model is recognised by a backend
    resolved = resolve_model(request_body.model)
    if resolved is None:
        supported = sorted(BackendRegistry.all_model_ids())
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{request_body.model}' is not supported. "
                f"Supported models: {supported}"
            ),
        )

    # Claude-only guard: reject non-Claude models on Anthropic endpoint
    if resolved.backend != "claude":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model '{request_body.model}' resolves to the {resolved.backend} backend. "
                f"The /v1/messages endpoint only supports Claude models. "
                f"Use /v1/chat/completions for {resolved.backend} models."
            ),
        )

    # Validate Claude authentication
    validate_backend_auth_or_raise("claude")

    claude_backend = BackendRegistry.get("claude")
    validate_image_request(request_body, claude_backend)

    try:
        logger.info(f"Anthropic Messages API request: model={request_body.model}")

        # Convert Anthropic messages to internal format
        messages = request_body.to_openai_messages()

        # Build prompt from messages
        prompt_parts = []
        for msg in messages:
            if msg.role == "user":
                if isinstance(msg.content, list) and hasattr(claude_backend, "image_handler"):
                    prompt_parts.append(
                        MessageAdapter.extract_images_to_prompt(
                            msg.content, claude_backend.image_handler
                        )
                    )
                else:
                    prompt_parts.append(msg.content)
            elif msg.role == "assistant":
                prompt_parts.append(f"Assistant: {msg.content}")

        prompt = "\n\n".join(prompt_parts)
        system_prompt = request_body.system

        # Filter content
        prompt = MessageAdapter.filter_content(prompt)
        if system_prompt:
            system_prompt = MessageAdapter.filter_content(system_prompt)

        # Run Claude Code - tools enabled by default for Anthropic SDK clients
        # (they're typically using this for agentic workflows)
        mcp_servers = get_mcp_servers() or None
        chunks = []
        async for chunk in claude_backend.run_completion(
            prompt=prompt,
            system_prompt=system_prompt,
            model=request_body.model,
            max_turns=get_default_max_turns(),
            allowed_tools=DEFAULT_ALLOWED_TOOLS,
            permission_mode=PERMISSION_MODE_BYPASS,
            stream=False,
            mcp_servers=mcp_servers,
        ):
            chunks.append(chunk)

        # Extract assistant message
        raw_assistant_content = claude_backend.parse_message(chunks)

        if not raw_assistant_content:
            raise HTTPException(status_code=500, detail="No response from Claude Code")

        assistant_content = raw_assistant_content

        # Token usage (prefer real SDK values)
        prompt_tokens, completion_tokens = streaming_utils.resolve_token_usage(
            chunks, prompt, assistant_content
        )

        # Extract stop_reason from SDK messages (use as-is for Anthropic format)
        sdk_stop_reason = streaming_utils.extract_stop_reason(chunks)

        # Create Anthropic-format response
        response = AnthropicMessagesResponse(
            model=request_body.model,
            content=[AnthropicTextBlock(text=assistant_content)],
            stop_reason=sdk_stop_reason or "end_turn",
            usage=AnthropicUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            ),
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Anthropic Messages API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
