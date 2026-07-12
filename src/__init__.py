"""Oh My Gateway - OpenAI-compatible gateway for coding agent backends."""

import os

from dotenv import dotenv_values, load_dotenv

# .env must load here: this package __init__ is the only module guaranteed to
# run before every src.* submodule, and several of them (e.g.
# src/backends/claude/constants.py) freeze os.getenv values at import time.
# Outside Docker (which injects .env via compose env_file) a load_dotenv()
# placed any lower in the import graph runs after those values are frozen.
# GATEWAY_SKIP_DOTENV blocks the load entirely; tests/conftest.py sets it so
# the suite stays hermetic against the developer's local .env.
if not os.getenv("GATEWAY_SKIP_DOTENV"):
    load_dotenv()

    # Selected Anthropic-related keys: .env wins over pre-existing shell env.
    # Why: shell-injected values (e.g. corp defaults) silently override .env,
    # making local routing/model overrides ineffective without this opt-in.
    _DOTENV_OVERRIDE_KEYS = (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    )
    _dotenv_file_values = dotenv_values()
    for _k in _DOTENV_OVERRIDE_KEYS:
        _v = _dotenv_file_values.get(_k)
        if _v is not None:
            os.environ[_k] = _v

__version__ = "2.3.0"
