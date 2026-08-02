"""Request models for the stateless agent Messages endpoint."""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentTextBlock(BaseModel):
    """Anthropic-style text content block accepted in message history."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str


class AgentMessage(BaseModel):
    """One caller-owned turn in the complete stateless transcript."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: Union[str, List[AgentTextBlock]]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(block.text for block in self.content)


class AgentMessagesRequest(BaseModel):
    """POST /v1/agents/messages request body.

    The endpoint intentionally supports a small Anthropic-Messages-like subset:
    callers send the complete text transcript and receive raw SDK envelopes over
    SSE. It never accepts a continuation/session identifier.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str = "claude"
    model: str = "sonnet"
    system: Optional[Union[str, List[AgentTextBlock]]] = None
    messages: List[AgentMessage] = Field(min_length=1)
    stream: bool = True
    effort: Optional[Literal["none", "low", "medium", "high", "xhigh", "max"]] = Field(
        default=None,
        description=(
            "Per-request reasoning control: 'none' disables extended "
            "thinking, the five SDK levels select adaptive-thinking effort. "
            "Omitted = the gateway's global THINKING_MODE default."
        ),
    )

    @model_validator(mode="after")
    def validate_final_user_turn(self):
        final = self.messages[-1]
        if final.role != "user":
            raise ValueError("messages must end with a user turn")
        if not final.text().strip():
            raise ValueError("the final user message must not be empty")
        return self

    def system_text(self) -> Optional[str]:
        if self.system is None or isinstance(self.system, str):
            return self.system
        return "\n".join(block.text for block in self.system)
