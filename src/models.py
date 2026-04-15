from typing import List, Optional, Union, Literal
from pydantic import BaseModel, model_validator
from datetime import datetime


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


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    last_accessed: datetime
    message_count: int
    expires_at: datetime


class SessionListResponse(BaseModel):
    sessions: List[SessionInfo]
    total: int
