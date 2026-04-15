"""Pydantic models for OpenAI Responses API compatibility."""

import time
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Discriminator, Field, Tag


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


class ResponseCreateRequest(BaseModel):
    """POST /v1/responses request body."""

    model: str
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
    user: Optional[str] = Field(
        default=None,
        description="Unique user identifier for workspace isolation",
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
    status: Literal["completed", "in_progress", "failed"] = "completed"
    content: List[ResponseContentPart] = Field(default_factory=list)


class ResponseUsage(BaseModel):
    """Token usage for a response."""

    input_tokens: int = 0
    output_tokens: int = 0


class ResponseErrorDetail(BaseModel):
    """Error detail when status is 'failed'."""

    code: str
    message: str


class FunctionCallOutputItem(BaseModel):
    """A function_call output item in the response (e.g. AskUserQuestion)."""

    id: str
    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str
    status: str = "completed"


class ResponseObject(BaseModel):
    """The response object returned by POST /v1/responses."""

    id: str
    object: Literal["response"] = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    status: Literal["completed", "in_progress", "failed", "requires_action"] = "completed"
    model: str = ""
    output: List[Union[OutputItem, FunctionCallOutputItem]] = Field(default_factory=list)
    usage: ResponseUsage = Field(default_factory=ResponseUsage)
    metadata: Dict[str, str] = Field(default_factory=dict)
    error: Optional[ResponseErrorDetail] = None
