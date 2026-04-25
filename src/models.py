from typing import Literal
from pydantic import BaseModel, model_validator
from datetime import datetime


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list
    name: str | None = None

    @model_validator(mode="after")
    def normalize_content(self):
        """Convert array content to string for Claude Code compatibility."""
        if isinstance(self.content, list):
            text_parts = []
            for part in self.content:
                if hasattr(part, "type") and part.type == "text":
                    text_parts.append(part.text)
                elif isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))

            self.content = "\n".join(text_parts) if text_parts else ""

        return self


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    last_accessed: datetime
    message_count: int
    expires_at: datetime


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]
    total: int
