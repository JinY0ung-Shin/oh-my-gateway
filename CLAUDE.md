# CLAUDE.md

Agent gateway exposing Claude Agent SDK, OpenCode, and Codex backends through the OpenAI-compatible
`/v1/responses` API, plus a stateless Claude SDK event stream at `/v1/agents/messages` (FastAPI,
Python 3.10+, uv).

## Commands

```bash
uv sync                                            # install deps (incl. dev group)
uv run uvicorn src.main:app --reload --port 8000   # dev server
uv run pytest                                      # tests (e2e excluded via addopts)
uv run pytest -m integration                       # subprocess integration tests
uv run pytest --cov=src                            # with coverage
```

- `ADMIN_API_KEY` must be set or the server fails fast at startup (`src/main.py`). `ANTHROPIC_AUTH_TOKEN` for the Claude backend; optional `API_KEY` bearer-protects public endpoints. See `.env.example` for the full list.
- Backends are enabled via `BACKENDS=claude,opencode,codex` (claude is the default).

## Architecture

- `src/main.py` — FastAPI app assembly, startup validation.
- `src/routes/` — `responses.py` (OpenAI-compatible streaming + non-streaming), `agent_messages.py`
  (stateless Claude SDK event stream), `admin.py`, `sessions.py`, `general.py`.
- `src/agent_message_models.py` — strict caller-owned full-history request contract for
  `/v1/agents/messages`.
- `src/backends/` — `base.py` defines the `BackendClient`/`SessionHandle` protocols and `BackendRegistry`; `claude/`, `codex/`, `opencode/` implement them. Codex talks JSON-RPC to a local `codex app-server`; OpenCode has managed (subprocess) and external (HTTP) modes.
- `src/sanitizer/` — stream sanitization + OpenAI-format bridge.
- `src/session_manager.py` / `src/workspace_manager.py` — `/v1/responses` continuation state and
  workspace isolation. Stateless agent-message runs use request-scoped temporary workspaces and never
  enter the session manager.
- Admin dashboard is server-rendered from `src/admin_*.py` modules (no frontend build).

## Code Style

- Code is **black-88 formatted**, even though `pyproject.toml` carries a `[tool.ruff]` section with line-length 100. Do not run repo-wide `ruff format`; match surrounding style.
- Gateway philosophy: pass SDK behavior through rather than hiding upstream breaking changes. The one
  deliberate adapter is the versioned, endpoint-local mapper in `src/routes/agent_messages.py`, because
  the Python SDK objects are not wire-compatible with Noah's JavaScript SDK handlers. Never move that
  mapper into the shared Responses conversion path.

## API Compatibility Boundaries

- `/v1/responses` owns OpenAI response semantics, `previous_response_id`, cancellation, and stored
  continuation state. Preserve its existing conversion, streaming, and session behavior.
- `/v1/agents/messages` is Claude-only and stateless: the caller sends complete text history, every call
  creates a fresh SDK client/workspace, and the stream declares `claude-agent-sdk-message-v1` before
  normalized `sdk_message` events. Do not accept or expose continuation/session IDs.
- Noah consumes these envelopes through the same `dispatchSdkMessage` used for its local SDK runs. A
  schema or SDK-event change must be coordinated with the Noah `avatar-chat` repository; do not fork a
  second Noah-side event handler.
- Keep endpoint-specific partial messages, secret/path redaction, tool-result projection,
  `AskUserQuestion` denial, disconnect, and transcript/artifact cleanup isolated from `/v1/responses`.

## Testing

- `pytest-asyncio` uses `asyncio_mode = "auto"`; do not add `@pytest.mark.asyncio` unless a test specifically needs it.
- Mock SDK calls in tests and prefer the shared fixtures in `tests/conftest.py`.
- Markers: `integration` (real subprocess with mock binary), `slow`, `e2e` (needs live server + credentials; excluded by default).
- `claude-agent-sdk` is pinned exactly (`==0.2.108`); upgrades are deliberate, gap-analyzed events — do not bump casually.
- Changes to the stateless mapper must pass `uv run pytest tests/test_agent_messages.py -q` and the full
  gateway suite. If the schema/event shape changes, also run Noah's `tests/external-agent.test.ts`.
