"""Route modules for Oh My Gateway."""

from src.routes.responses import router as responses_router
from src.routes.agent_messages import router as agent_messages_router
from src.routes.sessions import router as sessions_router
from src.routes.general import router as general_router
from src.routes.admin import router as admin_router
from src.routes.admin_prompt_create import router as admin_prompt_create_router

# Keep the compatibility admin router as the single surface registered by main,
# while extending it with create-only semantics for named prompts.
admin_router.include_router(admin_prompt_create_router)

__all__ = [
    "responses_router",
    "agent_messages_router",
    "sessions_router",
    "general_router",
    "admin_router",
]
