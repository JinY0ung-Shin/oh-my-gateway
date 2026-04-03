"""Route modules for the Claude Code OpenAI API Wrapper."""

from src.routes.chat import router as chat_router
from src.routes.messages import router as messages_router
from src.routes.responses import router as responses_router
from src.routes.sessions import router as sessions_router
from src.routes.general import router as general_router
from src.routes.admin import router as admin_router
from src.routes.image_proxy import router as image_proxy_router

__all__ = [
    "chat_router",
    "messages_router",
    "responses_router",
    "sessions_router",
    "general_router",
    "admin_router",
    "image_proxy_router",
]
