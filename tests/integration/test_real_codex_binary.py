"""Opt-in smoke tests that exercise the real Codex CLI binary.

These tests are skipped by default and only run when:
  - RUN_CODEX_BINARY_TESTS=1 is set
  - ``codex`` is available on PATH
  - (for completion tests) OPENAI_API_KEY is set
"""

import pytest

from src.codex_cli import CodexCLI
from tests.integration.conftest import requires_codex_binary, requires_openai_key

pytestmark = [pytest.mark.integration, pytest.mark.codex_binary]


@requires_codex_binary
async def test_verify_real_binary():
    """Verify that the real Codex binary responds to --version."""
    cli = CodexCLI(cwd="/tmp")
    result = await cli.verify()
    assert result is True


@requires_codex_binary
@requires_openai_key
async def test_basic_real_completion():
    """Run a trivial prompt through the real Codex binary and check chunk types."""
    cli = CodexCLI(cwd="/tmp")

    chunks = []
    async for chunk in cli.run_completion(prompt="Say hello in one word"):
        chunks.append(chunk)

    chunk_types = [c.get("type") for c in chunks]
    assert "assistant" in chunk_types, f"Expected an assistant chunk, got: {chunk_types}"
    assert "result" in chunk_types, f"Expected a result chunk, got: {chunk_types}"
