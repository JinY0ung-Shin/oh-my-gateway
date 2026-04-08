from typing import List, Optional, Dict, Any, Union, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
import uuid


# Import DEFAULT_MODEL to avoid circular imports
def get_default_model():
    """Get default model — checks runtime overrides first, then startup constant."""
    from src.runtime_config import get_default_model as _get

    return _get()


class ContentPart(BaseModel):
    """Content part for multimodal messages (OpenAI format)."""

    type: Literal["text", "image_url"]
    text: Optional[str] = None
    image_url: Optional[dict] = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: Union[str, List[ContentPart]]
    name: Optional[str] = None

    @model_validator(mode="after")
    def normalize_content(self):
        """Convert array content to string for Claude Code compatibility.

        If the list contains any image_url parts, keep it as a list to preserve
        image data for downstream image handlers. Text-only lists are collapsed
        to a single string as before.
        """
        if isinstance(self.content, list):
            # Check if any part is an image_url type
            has_image = any(
                (isinstance(part, ContentPart) and part.type == "image_url")
                or (isinstance(part, dict) and part.get("type") == "image_url")
                for part in self.content
            )

            if has_image:
                # Keep content as list when images are present
                return self

            # Text-only: extract and concatenate as before
            text_parts = []
            for part in self.content:
                if isinstance(part, ContentPart) and part.type == "text":
                    text_parts.append(part.text)
                elif isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

            # Join all text parts with newlines
            self.content = "\n".join(text_parts) if text_parts else ""

        return self


class StreamOptions(BaseModel):
    """Options for streaming responses."""

    include_usage: bool = Field(
        default=False, description="Include usage information in the final streaming chunk"
    )


class ChatCompletionRequest(BaseModel):
    model: str = Field(default_factory=get_default_model)
    messages: List[Message]
    temperature: Optional[float] = Field(default=1.0, ge=0, le=2)
    top_p: Optional[float] = Field(default=1.0, ge=0, le=1)
    n: Optional[int] = Field(default=1, ge=1)
    stream: Optional[bool] = True
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = Field(
        default=None, description="Maximum tokens to generate in the completion (OpenAI standard)"
    )
    presence_penalty: Optional[float] = Field(default=0, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(default=0, ge=-2, le=2)
    logit_bias: Optional[Dict[str, float]] = None
    user: Optional[str] = None
    session_id: Optional[str] = Field(
        default=None, description="Optional session ID for conversation continuity"
    )
    enable_tools: Optional[bool] = Field(
        default=True,
        description="Enable Claude Code tools (Read, Write, Bash, etc.)",
    )
    allowed_tools: Optional[List[str]] = Field(
        default=None,
        description="Explicit list of allowed tools. When set, overrides the default tool list. "
        "Supports exact names (Read, Bash) and MCP patterns (mcp__server__*).",
    )
    stream_options: Optional[StreamOptions] = Field(
        default=None, description="Options for streaming responses"
    )
    response_format: Optional[Dict[str, Any]] = Field(
        default=None, description="Response format (supports json_schema type)"
    )
    task_budget: Optional[int] = Field(
        default=None,
        gt=0,
        description="Token budget for the task — the model paces tool use and wraps up before exceeding this limit",
    )
    metadata: Optional[Dict[str, str]] = Field(
        default=None,
        description="Key-value metadata forwarded as env vars to the backend subprocess (filtered by METADATA_ENV_ALLOWLIST)",
    )

    @field_validator("n")
    @classmethod
    def validate_n(cls, v):
        if v > 1:
            raise ValueError(
                "Claude Code SDK does not support multiple choices (n > 1). Only single response generation is supported."
            )
        return v

    def to_claude_options(self) -> Dict[str, Any]:
        """Convert OpenAI request parameters to Claude Code SDK options.

        Unsupported parameters (temperature, top_p, max_tokens, penalties, etc.)
        are accepted for OpenAI compatibility but not passed to the SDK.
        """
        options = {}

        if self.model:
            options["model"] = self.model

        # Map response_format json_schema to output_format
        if self.response_format:
            fmt_type = self.response_format.get("type")
            if fmt_type == "json_schema":
                json_schema = self.response_format.get("json_schema", {})
                schema = json_schema.get("schema") if isinstance(json_schema, dict) else None
                if schema:
                    options["output_format"] = {"type": "json_schema", "schema": schema}

        if self.task_budget is not None:
            options["task_budget"] = self.task_budget

        return options


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: Optional[Literal["stop", "length", "content_filter", "tool_calls"]] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:8]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str
    choices: List[Choice]
    usage: Optional[Usage] = None
    system_fingerprint: Optional[str] = None


class StreamChoice(BaseModel):
    index: int
    delta: Dict[str, Any]
    finish_reason: Optional[Literal["stop", "length", "content_filter", "tool_calls"]] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:8]}")
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    model: str
    choices: List[StreamChoice]
    usage: Optional[Usage] = Field(
        default=None,
        description="Usage information (only in final chunk when stream_options.include_usage=true)",
    )
    system_fingerprint: Optional[str] = None


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    last_accessed: datetime
    message_count: int
    expires_at: datetime


class SessionListResponse(BaseModel):
    sessions: List[SessionInfo]
    total: int


# ============================================================================
# Anthropic API Compatible Models (for /v1/messages endpoint)
# ============================================================================


class AnthropicContentBlock(BaseModel):
    """Anthropic content block (text or image)."""

    type: Literal["text", "image"] = "text"
    text: Optional[str] = None
    source: Optional[dict] = None


# Backward-compatible alias
AnthropicTextBlock = AnthropicContentBlock


class AnthropicMessage(BaseModel):
    """Anthropic message format."""

    role: Literal["user", "assistant"]
    content: Union[str, List[AnthropicContentBlock]]


class AnthropicMessagesRequest(BaseModel):
    """Anthropic Messages API request format."""

    model: str
    messages: List[AnthropicMessage]
    max_tokens: int = Field(default=4096, description="Maximum tokens to generate")
    system: Optional[str] = Field(default=None, description="System prompt")
    temperature: Optional[float] = Field(default=1.0, ge=0, le=1)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    top_k: Optional[int] = Field(default=None, ge=0)
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = True
    metadata: Optional[Dict[str, Any]] = None

    def to_openai_messages(self) -> List[Message]:
        """Convert Anthropic messages to OpenAI format."""
        result = []
        for msg in self.messages:
            content = msg.content
            if isinstance(content, list):
                # Extract text from content blocks, skip image blocks
                # (image blocks are handled separately by the image handler)
                text_parts = [
                    block.text
                    for block in content
                    if isinstance(block, AnthropicContentBlock)
                    and block.type == "text"
                    and block.text is not None
                ]
                content = "\n".join(text_parts)
            result.append(Message(role=msg.role, content=content))
        return result


class AnthropicUsage(BaseModel):
    """Anthropic usage information."""

    input_tokens: int
    output_tokens: int


class AnthropicMessagesResponse(BaseModel):
    """Anthropic Messages API response format."""

    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: List[AnthropicContentBlock]
    model: str
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence"]] = "end_turn"
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage
