"""Pydantic models for OpenAI Responses API compatibility."""

import time
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class ResponseInputItem(BaseModel):
    """A single item in the input array (message format)."""

    role: str
    content: Union[str, List[Dict[str, Any]]] = ""


class ResponseCreateRequest(BaseModel):
    """POST /v1/responses request body."""

    model: str
    input: Union[str, List[ResponseInputItem]] = Field(
        description="User input as a plain string or array of input items"
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


class ResponseObject(BaseModel):
    """The response object returned by POST /v1/responses."""

    id: str
    object: Literal["response"] = "response"
    created_at: int = Field(default_factory=lambda: int(time.time()))
    status: Literal["completed", "in_progress", "failed"] = "completed"
    model: str = ""
    output: List[OutputItem] = Field(default_factory=list)
    usage: ResponseUsage = Field(default_factory=ResponseUsage)
    metadata: Dict[str, str] = Field(default_factory=dict)
    error: Optional[ResponseErrorDetail] = None
