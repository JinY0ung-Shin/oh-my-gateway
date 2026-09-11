"""Create-only named prompt admin endpoint.

The legacy PUT route in ``routes.admin`` intentionally stays an upsert.  POST on
the same resource path is the create contract used by admin UIs that must never
silently replace an existing saved prompt.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.admin_auth import require_admin
from src.named_prompt_create import create_named_prompt
from src.routes.admin import NamedPromptWrite

router = APIRouter()


@router.post("/api/prompts/{name}")
def create_prompt_endpoint(
    name: str,
    body: NamedPromptWrite,
    _=Depends(require_admin),
):
    """Create a named prompt, returning 409 when the name already exists."""
    try:
        return create_named_prompt(name, body.content)
    except FileExistsError:
        return JSONResponse(
            status_code=409,
            content={"error": f"Prompt already exists: {name}"},
        )
    except ValueError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to create: {e}"},
        )
