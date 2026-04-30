# Oh My Gateway

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://github.com/JinY0ung-Shin/oh-my-gateway)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

OpenAI-compatible gateway for coding agent backends. It exposes Claude Agent SDK and OpenCode through one `/v1/responses` API with streaming, multi-turn `previous_response_id` chaining, MCP integration, workspace isolation, and an admin dashboard.

> Previously published as **Claude Code Gateway**. The repository was renamed because the gateway now fronts multiple agent backends, not just Claude.

## Quick Start

```bash
git clone https://github.com/JinY0ung-Shin/oh-my-gateway
cd oh-my-gateway
uv sync
cp .env.example .env

export ANTHROPIC_AUTH_TOKEN=your-api-key
uv run uvicorn src.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'
```

## What It Provides

- **Responses API**: `/v1/responses` with non-streaming and SSE streaming responses.
- **Multiple backends**: Claude (`sonnet`, `opus`, `haiku`) and OpenCode (`opencode/<provider>/<model>`).
- **Session continuity**: `previous_response_id` and server-side session tracking.
- **Workspace isolation**: temporary sessions by default, or per-user directories with `USER_WORKSPACES_DIR`.
- **MCP support**: shared gateway `MCP_CONFIG`, with optional OpenCode managed-mode config generation.
- **Admin tools**: `/admin` dashboard, `/admin/chat`, runtime config, sessions, logs, prompts, skills, plugins, and diagnostics.
- **Docker support**: Dockerfile and Compose setup with optional usage-log MySQL sidecar.

## Documentation

| Topic | Doc |
|-------|-----|
| Claude backend setup, auth, workspaces, sandbox, MCP, subagents | [docs/claude-code/](docs/claude-code/) |
| OpenCode backend overview and mode selection | [docs/opencode/](docs/opencode/) |
| OpenCode managed mode | [docs/opencode/managed.md](docs/opencode/managed.md) |
| OpenCode external mode | [docs/opencode/external.md](docs/opencode/external.md) |
| OpenCode + LiteLLM recipes | [docs/opencode/litellm.md](docs/opencode/litellm.md) |
| Streaming event reference | [docs/streaming-events.md](docs/streaming-events.md) |
| System prompt presets | [docs/](docs/) |

## Backend Modes

Claude is enabled by default:

```bash
BACKENDS=claude
DEFAULT_MODEL=sonnet
```

Enable OpenCode alongside Claude:

```bash
BACKENDS=claude,opencode
OPENCODE_MODELS=openai/gpt-5.5
```

OpenCode has two modes:

| Mode | How it works | Use when |
|------|--------------|----------|
| Managed | Gateway starts `opencode serve` as a subprocess and generates its config | Gateway container is the trust boundary |
| External | Gateway forwards to an existing `opencode serve` via `OPENCODE_BASE_URL` | OpenCode needs broader filesystem access or a separate lifecycle |

External mode example:

```bash
OPENCODE_BASE_URL=http://opencode-host:7891
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=...
OPENCODE_MODELS=litellm/claude-sonnet-4-5
```

Check active backend mode:

```bash
curl -s http://localhost:8000/admin/api/backends \
  | jq '.backends[] | select(.name == "opencode") | .metadata'
```

## Configuration

Most settings are environment variables. Start with `.env.example`.

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_AUTH_TOKEN` | Claude API key auth |
| `CLAUDE_AUTH_METHOD` | Force `api_key` or `cli` auth |
| `BACKENDS` | Backend allowlist, for example `claude,opencode` |
| `DEFAULT_MODEL` | Default model for requests without `model` |
| `GATEWAY_HOST` | Host bind address; falls back to legacy `CLAUDE_WRAPPER_HOST` |
| `CLAUDE_CWD` | Global Claude working directory |
| `USER_WORKSPACES_DIR` | Per-user workspace root |
| `MCP_CONFIG` | Shared MCP server config |
| `OPENCODE_BASE_URL` | Enables OpenCode external mode |
| `OPENCODE_MODELS` | Gateway allowlist for OpenCode models |
| `API_KEY` | Optional public API bearer token |
| `ADMIN_API_KEY` | Required admin dashboard key |

## Docker

```bash
docker build -t oh-my-gateway .

docker run -d -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=your-key \
  -e ADMIN_API_KEY=admin-secret \
  oh-my-gateway
```

With Compose:

```bash
cp .env.example .env
docker compose up -d
```

The Compose service is named `gateway`. Optional usage logging is available through the `logging` profile:

```bash
docker compose --profile logging up -d
```

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check .
```

Refresh Docker dependency pins after dependency changes:

```bash
uv export --format requirements.txt --no-dev --no-hashes --no-emit-project --locked -o requirements.txt
```

Run a live OpenCode smoke test only when a real provider is configured:

```bash
OPENCODE_SMOKE_ENABLED=1 \
OPENCODE_SMOKE_MODEL=openai/gpt-5.5 \
uv run pytest tests/integration/test_opencode_smoke.py -q
```

## API Surface

Primary endpoints:

- `POST /v1/responses`
- `GET /v1/models`
- `GET /v1/sessions`
- `GET /v1/auth/status`
- `GET /v1/mcp/servers`
- `GET /health`
- `GET /version`

Admin endpoints live under `/admin` and `/admin/api/*`.

## Terms

You must use your own upstream credentials. This project does not pool credentials, resell access, or bypass upstream authentication.

Oh My Gateway is an independent open-source project and is not affiliated with or endorsed by Anthropic or the OpenCode authors.

## License

MIT
