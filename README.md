# Oh My Gateway

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://github.com/JinY0ung-Shin/oh-my-gateway)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

OpenAI-compatible gateway for coding agent backends. It exposes Claude Agent SDK, OpenCode, and Codex through one `/v1/responses` API with streaming, multi-turn `previous_response_id` chaining, MCP integration, workspace isolation, and an admin dashboard.

> Previously published as **Claude Code Gateway**. The repository was renamed because the gateway now fronts multiple agent backends, not just Claude. The Docker Compose service is now `gateway`; update commands such as `docker compose logs claude-wrapper` to use `gateway`.

## Quick Start

```bash
git clone https://github.com/JinY0ung-Shin/oh-my-gateway
cd oh-my-gateway
uv sync
cp .env.example .env

export ANTHROPIC_AUTH_TOKEN=your-api-key
export ADMIN_API_KEY=change-this-admin-key
uv run uvicorn src.main:app --reload --port 8000
```

`ADMIN_API_KEY` is required at startup because the admin surface can inspect files and change runtime settings. Use `API_KEY` separately when public gateway endpoints need bearer-token protection.

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'
```

## What It Provides

- **Responses API**: `/v1/responses` with non-streaming and SSE streaming responses.
- **Multiple backends**: Claude (`sonnet`, `opus`, `haiku`), OpenCode (`opencode/<provider>/<model>`), and experimental Codex (`codex/<model>`).
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
| Experimental Codex backend setup and SDK status | [docs/codex/](docs/codex/) |
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

Enable the experimental Codex backend alongside Claude:

```bash
BACKENDS=claude,codex
CODEX_MODELS=gpt-5.5
```

The Codex backend is experimental. It is intended for local evaluation while the Codex CLI and SDK integration surface are still changing, so request behavior and configuration may change between releases. It uses the local `codex app-server` harness through JSON-RPC, not the OpenAI Responses API. The official Python SDK exists but is experimental and may not be installable from PyPI; see [docs/codex/](docs/codex/) for the current integration notes.

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
| `BACKENDS` | Backend allowlist, for example `claude,opencode,codex` |
| `DEFAULT_MODEL` | Default model for requests without `model` |
| `DEFAULT_MAX_TURNS` | Maximum agent turns per request |
| `MAX_TIMEOUT` | Backend timeout in milliseconds |
| `MAX_REQUEST_SIZE` | Maximum request body size in bytes |
| `SSE_KEEPALIVE_INTERVAL` | SSE keepalive comment interval; `0` disables it |
| `GATEWAY_HOST` | Host bind address; falls back to legacy `CLAUDE_WRAPPER_HOST` |
| `CLAUDE_CWD` | Global Claude working directory |
| `USER_WORKSPACES_DIR` | Per-user workspace root |
| `MCP_CONFIG` | Shared MCP server config |
| `METADATA_ENV_ALLOWLIST` | Request metadata keys forwarded as env vars to Claude |
| `ASK_USER_TIMEOUT_SECONDS` | AskUserQuestion wait time before denying the tool call |
| `OPENCODE_BASE_URL` | Enables OpenCode external mode |
| `OPENCODE_MODELS` | Gateway allowlist for OpenCode models |
| `CODEX_BIN` | Experimental Codex CLI binary name/path; default `codex` |
| `CODEX_MODELS` | Gateway allowlist for Codex models; default `gpt-5.5` |
| `CODEX_APPROVAL_POLICY` | Codex approval policy; default `never` |
| `CODEX_SANDBOX` | Codex thread sandbox mode; default `danger-full-access` for local experimental use |
| `CODEX_CONFIG_OVERRIDES` | Comma-separated `codex --config key=value` overrides |
| `API_KEY` | Optional public API bearer token |
| `ADMIN_API_KEY` | Required admin dashboard key |
| `USAGE_LOG_DB_URL` | Optional SQLAlchemy URL for usage logging |

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

For corporate networks, Compose forwards build-time mirror settings from the
host environment or `.env`:

```bash
APT_MIRROR_URL=http://apt-mirror.example.com/debian \
APT_SECURITY_MIRROR_URL=http://apt-mirror.example.com/debian-security \
NPM_CONFIG_REGISTRY=https://npm.example.com/repository/npm/ \
PIP_INDEX_URL=https://pypi.example.com/simple/ \
docker compose build
```

The Docker image is pinned to Debian trixie. OpenCode is installed from npm as
`opencode-ai@${OPENCODE_VERSION:-1.14.29}`; mirror that package plus the
matching platform package such as `opencode-linux-x64` or
`opencode-linux-arm64`.

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

When `USAGE_LOG_DB_URL` is configured, usage analytics are available under `/admin/api/usage/*`: `summary`, `users`, `tools`, `series`, `tools-series`, and `turns`.

## Response Compatibility

Effective `/v1/responses` request fields:

- `model`: `sonnet`, `opus`, `haiku`, `opencode/<provider>/<model>`, or `codex/<model>`.
- `input`: string, message array, `input_text` or `input_image` parts, or `function_call_output`.
- `instructions`: system/developer prompt for a new session only.
- `previous_response_id`: continue the latest turn of an existing session.
- `stream`: emit Responses-style SSE events.
- `metadata`: stored on responses; allowlisted keys can be forwarded to Claude with `METADATA_ENV_ALLOWLIST`.
- `allowed_tools`: explicit Claude tool allowlist.
- `user`: per-user workspace key.

The request model also accepts `store`, `temperature`, and `max_output_tokens` for client compatibility, but those fields are not currently forwarded as generation controls.

Notable deviations from OpenAI Responses API behavior:

- `instructions`, `system`, or `developer` input items cannot be used with `previous_response_id`; the session prompt is fixed after turn one.
- A stale `previous_response_id` returns `409` with the latest valid response ID for client recovery.
- Mixing backends inside one session is rejected.

## Terms

You must use your own upstream credentials. This project does not pool credentials, resell access, or bypass upstream authentication.

Oh My Gateway is an independent open-source project and is not affiliated with or endorsed by Anthropic, OpenAI, or the OpenCode authors.

## License

MIT
