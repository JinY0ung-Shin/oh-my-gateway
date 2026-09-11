"""Atomic create-only operation for named system prompts.

``system_prompt.save_named_prompt`` intentionally remains an upsert for the legacy
PUT admin API.  Creation UIs need a different contract: choosing an existing name
must fail instead of silently replacing that prompt.  This module keeps that
create-only guarantee at the filesystem boundary with exclusive file creation.
"""

import json
import logging
from datetime import datetime, timezone

from src import system_prompt

logger = logging.getLogger(__name__)


def create_named_prompt(name: str, content: str) -> dict:
    """Create a named prompt, failing atomically when *name* already exists.

    ``FileExistsError`` is the intentional conflict signal.  The exclusive
    ``open('x')`` is important: a list/get preflight followed by the legacy PUT
    would still have a TOCTOU window between checking and writing.
    """
    name = system_prompt._validate_prompt_name(name)
    content = content.strip()
    if not content:
        raise ValueError("Prompt content cannot be empty")

    system_prompt._PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = system_prompt._prompt_path(name)
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "content": content,
        "created_at": now,
        "updated_at": now,
    }
    encoded = json.dumps(data, ensure_ascii=False, indent=2)

    # 'x' maps to O_CREAT|O_EXCL: exactly one concurrent creator can win.
    with path.open("x", encoding="utf-8") as f:
        f.write(encoded)

    logger.info("Named prompt created: %s (%d chars)", name, len(content))
    return data
