"""Atomic create-only operation for named system prompts.

``system_prompt.save_named_prompt`` intentionally remains an upsert for the legacy
PUT admin API. Creation UIs need a different contract: choosing an existing name
must fail instead of silently replacing that prompt. This module keeps that
create-only guarantee at the filesystem boundary and publishes only complete JSON.
"""

import logging

logger = logging.getLogger(__name__)


def create_named_prompt(name: str, content: str, *, author=None) -> dict:
    """Create a named prompt (version 1), failing atomically when *name* exists.

    Delegates to ``prompt_library.create_prompt``, which publishes a complete temp
    file by hard link so readers never see a partial document and a concurrent
    creator is never replaced. ``FileExistsError`` keeps the route's 409 contract.
    """
    from src import prompt_library

    try:
        return prompt_library.create_prompt(name, content, author=author)
    except prompt_library.PromptExists as exc:
        raise FileExistsError(name) from exc
