# Repository Guidelines

## Project Scope
This repository is a FastAPI gateway for the Claude Agent SDK. It exposes one primary API surface:
- `/v1/responses` for Responses API flows with `previous_response_id`

## Project Structure
- `src/main.py` wires the app, middleware, endpoints, and startup behavior.
- `src/claude_cli.py` owns SDK option building, working-directory handling, and Claude query execution.
- `src/message_adapter.py` converts OpenAI and Anthropic message shapes into Claude-friendly input.
- `src/streaming_utils.py` maps SDK events into SSE chunks and streaming responses.
- `src/session_manager.py` manages in-memory session history, TTL refresh, and cleanup.
- `src/constants.py` is the shared home for environment-driven defaults and config constants.
- `src/models.py` and `src/response_models.py` define request and response schemas.
- `src/auth.py`, `src/rate_limiter.py`, and `src/mcp_config.py` handle auth, limits, and MCP loading.
- `pipes/open_webui_pipe.py` is a standalone Open WebUI pipe and must stay decoupled from `src/`.
- `tests/` contains unit, API, async, property-based, and shell smoke coverage.

## Working Rules
- Keep new runtime code in `src/` and preserve the existing module boundaries instead of moving logic into `src/main.py`.
- Keep validation and API-compatibility logic out of the main entrypoint and in dedicated modules.
- Keep message-format translation in `src/message_adapter.py`, SDK interaction in `src/claude_cli.py`, and SSE shaping in `src/streaming_utils.py`.
- Preserve response-shape and streaming semantics across the compatible API surfaces.
- Put new shared defaults in `src/constants.py`; when config behavior changes, also update `.env.example` and `README.md`.
- Do not import from `src/` inside `pipes/open_webui_pipe.py`; it communicates with the gateway over HTTP only.
- Preserve the distinction between chat-session history in `src/session_manager.py` and `previous_response_id` chaining in `/v1/responses`.

## Commands
- `uv sync --group dev` installs runtime and development dependencies.
- `uv run uvicorn src.main:app --reload --port 8000` runs the API locally with reload.
- `uv run pytest tests/` runs the full test suite.
- `uv run pytest --cov=src --cov-report=term-missing` checks coverage for touched modules.
- `uv run ruff check --fix . && uv run ruff format .` lints and formats the repository.
- `docker compose up -d` starts the containerized stack for integration checks.

## Coding Conventions
- Use Python 3.10+, type hints on public functions, 4-space indentation, double quotes, and a 100-character line limit.
- Use PascalCase for Pydantic models, snake_case for functions and modules, and UPPER_SNAKE_CASE for constants.
- Prefer extending existing patterns over introducing new abstractions in core request flow code.

## Testing
- Pytest is the primary runner and `pytest-asyncio` is configured with `asyncio_mode = "auto"`.
- Use the shared fixtures in `tests/conftest.py` before adding bespoke mocks or test clients.
- Unit tests should keep SDK calls mocked; do not require real Anthropic credentials.
- Add or update tests whenever behavior changes in auth, streaming, sessions, request validation, MCP config, or API compatibility.
- Run targeted tests for touched modules first, then broader coverage if the change affects shared flow.

## Change Checklist
- Schema changes should update the relevant models plus endpoint tests.
- Streaming changes should verify SSE chunk structure, usage reporting, and stop-reason behavior.
- Session changes should verify TTL refresh, cleanup, and multi-turn history behavior.
- Auth or config changes should verify local env behavior and Docker behavior.
- New environment variables or defaults should be documented in `.env.example` and `README.md`.

## Commit And Review
- Follow Conventional Commit style such as `feat(pipe): ...`, `fix: ...`, `refactor: ...`, `docs: ...`, or `chore: ...`.
- Keep commit subjects imperative and scoped when that adds clarity.
- In review, prioritize correctness, API compatibility, streaming semantics, session continuity, auth/rate-limit regressions, and missing tests.

## Security
- Keep secrets only in local `.env` files or environment variables; never commit API keys or Claude auth tokens.
- Do not log secrets, tokens, API keys, raw authorization headers, or other credential-bearing material.
- Treat `MCP_CONFIG` as sensitive executable configuration that affects runtime execution context.
- Validate changes involving `ANTHROPIC_AUTH_TOKEN`, `API_KEY`, `CLAUDE_CWD`, and `MCP_CONFIG` in both local and containerized setups.

## References
- `README.md` for user-facing setup and endpoint examples
- `docs/plans/2026-03-05-claude-sdk-client-migration-design.md` for SDK migration context
