# CLAUDE.md

OpenAI-compatible gateway exposing Claude Agent SDK, OpenCode, and Codex backends through one `/v1/responses` API (FastAPI, Python 3.10+, uv).

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
- `src/routes/` — `responses.py` (main `/v1/responses` API, streaming + non-streaming), `admin.py`, `sessions.py`, `general.py`.
- `src/backends/` — `base.py` defines the `BackendClient`/`SessionHandle` protocols and `BackendRegistry`; `claude/`, `codex/`, `opencode/` implement them. Codex talks JSON-RPC to a local `codex app-server`; OpenCode has managed (subprocess) and external (HTTP) modes.
- `src/sanitizer/` — stream sanitization + OpenAI-format bridge.
- `src/session_manager.py` / `src/workspace_manager.py` — `previous_response_id` chaining and per-user workspace isolation.
- Admin dashboard is server-rendered from `src/admin_*.py` modules (no frontend build).

## Code Style

- Code is **black-88 formatted**, even though `pyproject.toml` carries a `[tool.ruff]` section with line-length 100. Do not run repo-wide `ruff format`; match surrounding style.
- Gateway philosophy: pass SDK behavior through to clients; do not add compatibility adapters that paper over upstream SDK breaking changes.

## Testing

- `pytest-asyncio` uses `asyncio_mode = "auto"`; do not add `@pytest.mark.asyncio` unless a test specifically needs it.
- Mock SDK calls in tests and prefer the shared fixtures in `tests/conftest.py`.
- Markers: `integration` (real subprocess with mock binary), `slow`, `e2e` (needs live server + credentials; excluded by default).
- `claude-agent-sdk`  is pinned exactly (`==0.2.90`); upgrades are deliberate, gap-analyzed events — do not bump casually.
