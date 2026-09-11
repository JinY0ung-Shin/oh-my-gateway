"""Atomic create-only operation for named system prompts.

``system_prompt.save_named_prompt`` intentionally remains an upsert for the legacy
PUT admin API. Creation UIs need a different contract: choosing an existing name
must fail instead of silently replacing that prompt. This module keeps that
create-only guarantee at the filesystem boundary and publishes only complete JSON.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src import system_prompt

logger = logging.getLogger(__name__)


def create_named_prompt(name: str, content: str) -> dict:
    """Create a named prompt, failing atomically when *name* already exists.

    A list/get preflight followed by the legacy PUT has a TOCTOU window. Write a
    complete temporary file first, then hard-link it into the final name. Creating
    the link is atomic and fails with ``FileExistsError`` when another creator won;
    readers therefore observe either no prompt or the complete JSON document.
    """
    name = system_prompt._validate_prompt_name(name)
    content = content.strip()
    if not content:
        raise ValueError("Prompt content cannot be empty")

    prompts_dir = system_prompt._PROMPTS_DIR
    prompts_dir.mkdir(parents=True, exist_ok=True)
    path = system_prompt._prompt_path(name)
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "name": name,
        "content": content,
        "created_at": now,
        "updated_at": now,
    }
    encoded = json.dumps(data, ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(
        dir=prompts_dir,
        prefix=f".{name}.",
        suffix=".tmp",
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        # Same-directory hard-link publish is atomic and never replaces a target.
        os.link(tmp_path, path)
    finally:
        # Cleanup is deliberately best-effort. Once ``os.link`` succeeds, the
        # externally visible mutation is committed; a failure to remove the hidden
        # staging inode must not turn that successful create into a 500. Likewise,
        # on a duplicate-name conflict the original ``FileExistsError`` must reach
        # the route so it remains a 409 instead of being masked by cleanup I/O.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove named-prompt staging file %s: %s", tmp_path, exc)

    logger.info("Named prompt created: %s (%d chars)", name, len(content))
    return data
