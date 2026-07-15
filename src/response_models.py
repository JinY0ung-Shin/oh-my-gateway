"""Pydantic models for OpenAI Responses API compatibility."""

import time
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from src.runtime_config import get_default_model

PermissionMode = Literal["default", "acceptEdits", "bypassPermissions", "plan"]


class ResponseInputTextPart(BaseModel):
    """A text content part within a Responses API input item."""

    type: Literal["input_text"] = "input_text"
    text: str = ""


class ResponseInputImagePart(BaseModel):
    """An image content part within a Responses API input item."""

    type: Literal["input_image"] = "input_image"
    image_url: str
    detail: Optional[str] = None


def _response_input_part_discriminator(v: Any) -> str:
    """Route content parts by ``type``; unknown types fall through to ``unknown``.

    Known types (``input_text`` / ``input_image``) get strict pydantic validation.
    Unknown types are accepted as raw dicts so the Responses API surface stays
    forward-compatible with evolving OpenAI part types (e.g., ``input_file``,
    ``input_audio``). Downstream code already tolerates both pydantic models
    and dicts via ``isinstance`` / ``getattr`` forks.
    """
    if isinstance(v, dict):
        t = v.get("type")
    else:
        t = getattr(v, "type", None)
    if t == "input_text":
        return "input_text"
    if t == "input_image":
        return "input_image"
    return "unknown"


ResponseInputContentPart = Annotated[
    Union[
        Annotated[ResponseInputTextPart, Tag("input_text")],
        Annotated[ResponseInputImagePart, Tag("input_image")],
        Annotated[Dict[str, Any], Tag("unknown")],
    ],
    Discriminator(_response_input_part_discriminator),
]


class ResponseInputItem(BaseModel):
    """A single item in the input array (message format)."""

    role: Literal["user", "assistant", "system", "developer"]
    content: Union[str, List[ResponseInputContentPart]] = ""


class FunctionCallOutputInput(BaseModel):
    """A function_call_output input item from the client."""

    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str


class TextFormatText(BaseModel):
    """The default ``text`` response format (no-op)."""

    type: Literal["text"] = "text"


class TextFormatJSONSchema(BaseModel):
    """Structured Outputs response format (``text.format.type == "json_schema"``).

    Mirrors the OpenAI Responses API shape. The ``schema`` payload is handed
    to the backend SDK as-is (gateway pass-through philosophy — no rewriting).
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["json_schema"] = "json_schema"
    name: Optional[str] = None
    description: Optional[str] = None
    json_schema: Dict[str, Any] = Field(
        alias="schema",
        description="JSON Schema the model output must conform to",
    )
    strict: Optional[bool] = None


class ResponseTextConfig(BaseModel):
    """The ``text`` request option (response format configuration)."""

    format: Optional[
        Annotated[
            Union[TextFormatText, TextFormatJSONSchema],
            Field(discriminator="type"),
        ]
    ] = None


class ResponseCreateRequest(BaseModel):
    """POST /v1/responses request body."""

    model: str = Field(default_factory=get_default_model)
    input: Union[str, List[Union[ResponseInputItem, FunctionCallOutputInput]]] = Field(
        description="User input as a plain string, array of input items, "
        "or function_call_output for tool continuations"
    )
    instructions: Optional[str] = Field(
        default=None, description="System prompt (cannot be used with previous_response_id)"
    )
    previous_response_id: Optional[str] = Field(
        default=None, description="Chain to a previous response for conversation continuity"
    )
    stream: Optional[bool] = False
    metadata: Optional[Dict[str, str]] = None
    store: Optional[bool] = True
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    allowed_tools: Optional[List[str]] = Field(
        default=None,
        description="Explicit list of allowed tools. Overrides default tool list.",
    )
    disallowed_tools: Optional[List[str]] = Field(
        default=None,
        description=(
            "Tools that must be blocked for this request. Hard-blocked even when "
            "permission_mode bypasses checks. Merged with the DISALLOWED_TOOLS env var."
        ),
    )
    permission_mode: Optional[PermissionMode] = Field(
        default=None,
        description=(
            "Session permission mode override. One of: default, acceptEdits, "
            "bypassPermissions, plan. Continuation requests that omit this field "
            "keep the current session mode."
        ),
    )
    user: Optional[str] = Field(
        default=None,
        description="Unique user identifier for workspace isolation",
    )
    text: Optional[ResponseTextConfig] = Field(
        default=None,
        description=(
            "Text response configuration. format.type 'json_schema' enables "
            "Structured Outputs (Claude backend only); 'text' is the default."
        ),
    )


class ResponseContentPart(BaseModel):
    """A content part within a Responses API output item."""

    type: Literal["output_text"] = "output_text"
    text: str = ""
    annotations: List[Any] = Field(default_factory=list)


class OutputItem(BaseModel):
    """An output item (message) in the response."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    status: Literal["completed", "in_progress", "incomplete", "failed"] = "completed"
    content: List[ResponseContentPart] = Field(default_factory=list)


class ReasoningSummary(BaseModel):
    """A summary part inside a reasoning output item."""

    type: Literal["summary_text"] = "summary_text"
    text: str = ""


class ReasoningContent(BaseModel):
    """A raw reasoning text part inside a reasoning output item."""

    type: Literal["reasoning_text"] = "reasoning_text"
    text: str = ""


class ReasoningOutputItem(BaseModel):
    """A reasoning output item (Anthropic ThinkingBlock → OpenAI reasoning)."""

    id: str
    type: Literal["reasoning"] = "reasoning"
    status: Literal["completed", "in_progress", "incomplete"] = "completed"
    summary: List[ReasoningSummary] = Field(default_factory=list)
    content: Optional[List[ReasoningContent]] = None


class ResponseUsage(BaseModel):
    """Token usage for a response.

    ``input_tokens``/``output_tokens`` are run-cumulative (an agentic turn
    spanning many model calls sums each call's tokens, cache reads included).
    ``context_tokens`` is different: the context-window occupancy of the
    *last* model call in the turn — what a "context fullness" gauge should
    show. Extension field; OpenAI clients ignore it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: Optional[int] = None


class ResponseErrorDetail(BaseModel):
    """Error detail when status is 'failed'."""

    code: str
    message: str


class ResponseIncompleteDetails(BaseModel):
    """Why a response stopped before normal completion."""

    reason: str


class FunctionCallOutputItem(BaseModel):
    """A function_call output item in the response (e.g. AskUserQuestion)."""

    id: str
    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str
    status: str = "completed"


class ResponseDeletedObject(BaseModel):
    """DELETE /v1/responses/{response_id} acknowledgment (OpenAI shape)."""

    id: str
    object: Literal["response"] = "response"
    deleted: bool = True


class ResponseObject(BaseModel):
    """The response object returned by POST /v1/responses."""

    id: str
    object: Literal["response"] = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    status: Literal["completed", "in_progress", "incomplete", "failed", "requires_action"] = (
        "completed"
    )
    model: str = ""
    output: List[Union[OutputItem, FunctionCallOutputItem, ReasoningOutputItem]] = Field(
        default_factory=list
    )
    usage: ResponseUsage = Field(default_factory=ResponseUsage)
    metadata: Dict[str, str] = Field(default_factory=dict)
    error: Optional[ResponseErrorDetail] = None
    incomplete_details: Optional[ResponseIncompleteDetails] = None
    structured_output: Any = Field(
        default=None,
        description=(
            "Parsed Structured Outputs payload from the backend (ResultMessage"
            ".structured_output) when the request set text.format json_schema."
        ),
    )
