"""Route modules for Oh My Gateway."""

from src.routes.responses import router as responses_router
from src.routes.agent_messages import router as agent_messages_router
from src.routes.sessions import router as sessions_router
from src.routes.general import router as general_router
from src.routes.admin import router as admin_router

__all__ = [
    "responses_router",
    "agent_messages_router",
    "sessions_router",
    "general_router",
    "admin_router",
]
