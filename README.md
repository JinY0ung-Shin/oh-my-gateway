# Oh My Gateway

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://github.com/JinY0ung-Shin/oh-my-gateway)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

FastAPI gateway that exposes coding agent backends (Claude Agent SDK, OpenCode) through a single OpenAI Responses-compatible API. Supports `previous_response_id` chaining for multi-turn conversations, MCP server integration, per-user workspace isolation, and a built-in admin dashboard.

> Previously published as **Claude Code Gateway**. The repository was renamed because the gateway now fronts multiple agent backends, not just Claude.

## Quick Start

```bash
git clone https://github.com/JinY0ung-Shin/oh-my-gateway
cd oh-my-gateway
uv sync

export ANTHROPIC_AUTH_TOKEN=your-api-key

uv run uvicorn src.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'
```

## Documentation

| Topic | Doc |
|-------|-----|
| Claude Code backend (auth, workspaces, sandbox, thinking, MCP, subagents) | [docs/claude-code/](docs/claude-code/) |
| OpenCode backend overview + when to use which mode | [docs/opencode/](docs/opencode/) |
| OpenCode managed mode (gateway spawns `opencode serve`) | [docs/opencode/managed.md](docs/opencode/managed.md) |
| OpenCode external mode (point at an existing `opencode serve`) | [docs/opencode/external.md](docs/opencode/external.md) |
| OpenCode + LiteLLM recipes (reasoning content, multi-provider routing) | [docs/opencode/litellm.md](docs/opencode/litellm.md) |
| SSE streaming event reference | [docs/streaming-events.md](docs/streaming-events.md) |
| System prompt presets | [docs/](docs/) — `claude-code-system-prompt-reference.md`, `compact-system-prompt.md`, `minimal-system-prompt.md` |

## Features

- **Responses API** — `/v1/responses` with `previous_response_id` chaining
- **Multiple Backends** — Claude (Agent SDK) and OpenCode in one gateway, selected by model id (`sonnet`, `opus`, `haiku`, or `opencode/<provider>/<model>`)
- **Session Management** — Multi-turn conversations via `session_id`
- **Auth Support** — API key or CLI auth
- **MCP Server Integration** — Connect external tool servers at startup; shared between Claude and OpenCode
- **Subagent Control** — Block specific subagent types per deployment
- **Adaptive Thinking** — Configurable thinking modes and budget
- **Token-Level Streaming** — Real-time token delivery via SDK partial messages
- **Rate Limiting** — Per-endpoint configurable limits
- **Docker Ready** — Dockerfile and docker-compose included

## Installation

**Prerequisites:** Python 3.10+ and [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/JinY0ung-Shin/oh-my-gateway
cd oh-my-gateway
uv sync
cp .env.example .env
```

The Claude Code CLI is bundled with `claude-agent-sdk` — no separate Node.js or npm required.

### Authentication

| Method | Setup |
|--------|-------|
| API Key (recommended) | `export ANTHROPIC_AUTH_TOKEN=your-key` |
| CLI Auth | `claude auth login` |

## Configuration

Key environment variables (see `.env.example` for full list):

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Server port |
| `DEFAULT_MODEL` | `sonnet` | Default model (`opus`, `sonnet`, `haiku`) |
| `CLAUDE_CWD` | temp dir | Working directory for Claude Code |
| `THINKING_MODE` | `adaptive` | `adaptive`, `enabled`, or `disabled` |
| `THINKING_BUDGET_TOKENS` | `10000` | Budget for `enabled` mode |
| `TASK_BUDGET` | _(unset)_ | Global task token budget (per-request `task_budget` overrides) |
| `TOKEN_STREAMING` | `true` | Token-level partial streaming |
| `MAX_TIMEOUT` | `600000` | Request timeout (ms) |
| `DEFAULT_MAX_TURNS` | `10` | Max agent turns per request |
| `BACKENDS` | `claude` | Backend allowlist, for example `claude,opencode` |
| `OPENCODE_BASE_URL` | _(unset)_ | When set, switches OpenCode to external mode (skip subprocess, point at this URL) |
| `OPENCODE_MODELS` | _(unset)_ | Comma-separated OpenCode `provider/model` IDs exposed as `opencode/...` |
| `OPENCODE_USE_WRAPPER_MCP_CONFIG` | `false` | Copy validated wrapper `MCP_CONFIG` into OpenCode config (managed mode only) |
| `DISALLOWED_SUBAGENT_TYPES` | `statusline-setup` | Comma-separated subagent types to block |
| `CLAUDE_SANDBOX_ENABLED` | unset | Bash sandbox: unset = project settings, `true` = force on, `false` = force off |
| `MCP_CONFIG` | — | MCP server config (JSON string or file path) |
| `API_KEY` | — | Optional Bearer token for access control |
| `SESSION_MAX_AGE_MINUTES` | `60` | Session TTL |

### OpenCode Backend

OpenCode is opt-in. Claude remains the default backend when `BACKENDS` is unset.

```bash
export BACKENDS=claude,opencode
export OPENCODE_MODELS=openai/gpt-5.5
```

OpenCode runs in one of two modes:

#### Managed mode (default)

The gateway starts `opencode serve` automatically and requires the `opencode` binary on `PATH`. The Docker image installs OpenCode during build. OpenCode's `question` tool is exposed by default with `OPENCODE_QUESTION_PERMISSION=ask`; set it to `deny` to hide the tool.

Set `OPENCODE_USE_WRAPPER_MCP_CONFIG=true` to copy the validated wrapper `MCP_CONFIG` into the generated OpenCode config. The wrapper converts `stdio` servers to OpenCode `local` MCP entries and `http`, `sse`, or `streamable-http` servers to OpenCode `remote` entries.

When `OPENCODE_CONFIG_CONTENT` is set, the wrapper parses it as JSON, preserves explicit values, fills missing safe defaults, and then serializes the generated config passed to `opencode serve`.

#### External mode

Set `OPENCODE_BASE_URL` to point at an externally-managed `opencode serve` instance. Use this when OpenCode must run on a trusted host with broader filesystem access while the gateway itself is sandboxed (or when several gateway replicas share a single OpenCode server).

```bash
export OPENCODE_BASE_URL=http://opencode-host:7891
export OPENCODE_SERVER_USERNAME=opencode
export OPENCODE_SERVER_PASSWORD=...
```

In external mode the gateway does **not** start a subprocess and does **not** generate a config. The external server owns its own MCP and provider definitions, so the following variables become **no-ops**:

- `OPENCODE_CONFIG_CONTENT`
- `OPENCODE_USE_WRAPPER_MCP_CONFIG`
- `OPENCODE_BIN`, `OPENCODE_HOST`, `OPENCODE_PORT`, `OPENCODE_START_TIMEOUT_MS`

Request-time options (`OPENCODE_AGENT`, `OPENCODE_DEFAULT_MODEL`, `OPENCODE_QUESTION_PERMISSION`, `OPENCODE_MODELS`) and basic-auth credentials still apply. Verify the active mode with `GET /admin/api/backends` — the OpenCode backend item's `metadata.mode` field reports `managed` or `external`.

Full setup walkthroughs (LiteLLM provider, MCP servers, docker-compose, troubleshooting) live under **[docs/opencode/](docs/opencode/)** — see [managed.md](docs/opencode/managed.md) and [external.md](docs/opencode/external.md). The Claude backend has its own detailed guide at **[docs/claude-code/](docs/claude-code/)**.

### Bash Sandbox

The gateway can enable OS-level process isolation for Bash tool execution using the Claude Agent SDK's `SandboxSettings`. This uses macOS Seatbelt or Linux bubblewrap to restrict what Bash commands can access.

`CLAUDE_SANDBOX_ENABLED` is a tri-state setting:
- **Unset** (default) — does not configure sandbox, respects project-level Claude settings
- **`true`** — forces sandbox on with strict defaults (`allowUnsandboxedCommands=false`, no excluded commands)
- **`false`** — forces sandbox off, overriding project settings

> **Note:** Sandbox only isolates Bash commands. File tool access (Read/Edit/Write) is controlled separately by SDK permission rules. For Docker deployments, set `CLAUDE_SANDBOX_WEAKER_NESTED=true` if running in unprivileged containers on Linux.

See `.env.example` for the full list of sandbox environment variables.

### MCP Server Config Example

```json
{
  "mcpServers": {
    "docs": {
      "type": "stdio",
      "command": "uvx",
      "args": ["your-mcp-server"]
    }
  }
}
```

## Usage

### curl

```bash
# Basic request
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'

# Streaming
curl -N http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello", "stream": true}'

# Multi-turn chaining
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "What did I just say?", "previous_response_id": "<response_id_from_previous_turn>"}'
```

### OpenCode Smoke Test

Live OpenCode smoke test:

```bash
OPENCODE_SMOKE_ENABLED=1 \
OPENCODE_SMOKE_MODEL=openai/gpt-5.5 \
uv run pytest tests/integration/test_opencode_smoke.py -q
```

### Per-User Workspace Isolation

Each `/v1/responses` request can include a `user` field to isolate working directories:

```json
{
  "model": "sonnet",
  "input": "Create a Python script",
  "user": "alice"
}
```

**Behavior:**
- `user` specified: Permanent workspace at `{base_path}/{user}/` (survives server restarts)
- `user` omitted: Temporary workspace created per session, cleaned up on expiry
- On new sessions, `.claude/` config is copied from `CLAUDE_CWD` to the workspace

**Configuration:**
- `USER_WORKSPACES_DIR`: Base directory for workspaces (defaults to `CLAUDE_CWD`)
- User identifiers must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$`

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/responses` | Responses API with `previous_response_id` chaining |
| `GET` | `/v1/models` | Available models |
| `GET` | `/v1/sessions` | List active sessions |
| `GET` | `/v1/sessions/{id}` | Session details |
| `DELETE` | `/v1/sessions/{id}` | Delete session |
| `GET` | `/v1/sessions/stats` | Session stats |
| `GET` | `/v1/auth/status` | Authentication status |
| `GET` | `/v1/mcp/servers` | Loaded MCP servers |
| `GET` | `/health` | Health check |
| `GET` | `/version` | Version info |
| `GET` | `/` | Interactive API explorer |

Streaming (`"stream": true`) is supported on `/v1/responses`.

For detailed SSE event formats including tool call rendering, subagent events, and tool name/input schemas, see **[docs/streaming-events.md](docs/streaming-events.md)**.

### Admin Panel

The gateway includes a built-in admin dashboard at `/admin` (requires `ADMIN_API_KEY`).

**UI**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin` | Admin dashboard HTML |
| `GET` | `/admin/chat` | Admin chat interface HTML |

**Auth**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/admin/api/login` | Authenticate with admin API key |
| `POST` | `/admin/api/logout` | Invalidate admin session |
| `GET` | `/admin/api/status` | Admin auth/session status |

**Summary & diagnostics**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/summary` | Dashboard summary (models, sessions, backends) |
| `GET` | `/admin/api/server-info` | Server version and runtime info |
| `GET` | `/admin/api/backends` | Backend health, auth status, model availability (reports `metadata.mode: managed` or `metadata.mode: external` for OpenCode) |
| `GET` | `/admin/api/mcp-servers` | MCP server config and tool patterns |
| `GET` | `/admin/api/tools` | Tool registry per backend and MCP patterns |
| `GET` | `/admin/api/sandbox` | Sandbox and permission mode config |
| `GET` | `/admin/api/metrics` | Performance metrics (latency percentiles, error rate) |
| `GET` | `/admin/api/rate-limits` | Rate limit usage snapshot |
| `GET` | `/admin/api/logs` | Request logs with filtering |
| `GET` | `/admin/api/config` | Redacted runtime configuration |

**Runtime config**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET/PATCH` | `/admin/api/runtime-config` | Hot-reloadable settings |
| `POST` | `/admin/api/runtime-config/reset` | Reset runtime config to defaults |

**Sessions**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/sessions/stats` | Aggregate session stats |
| `POST` | `/admin/api/sessions/cleanup` | Purge expired sessions |
| `POST` | `/admin/api/sessions/bulk-delete` | Delete multiple sessions by ID |
| `GET` | `/admin/api/sessions/{id}/detail` | Session metadata (backend, turns, TTL) |
| `GET` | `/admin/api/sessions/{id}/export` | Export session as JSON |
| `GET` | `/admin/api/sessions/{id}/messages` | Session message history |
| `DELETE` | `/admin/api/sessions/{id}` | Delete session |

**Workspace files**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/files` | List workspace files |
| `GET/PUT` | `/admin/api/files/{path}` | Read/write workspace files |

**Skills**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/skills` | List skills with metadata |
| `GET/PUT/DELETE` | `/admin/api/skills/{name}` | Skill CRUD with ETag concurrency |

**System prompt**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/system-prompt/templates` | Built-in system prompt templates |
| `GET/PUT/DELETE` | `/admin/api/system-prompt` | System prompt management |

**Prompts (named)**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/prompts` | List saved prompts |
| `GET/PUT/DELETE` | `/admin/api/prompts/{name}` | Prompt CRUD |
| `POST` | `/admin/api/prompts/{name}/activate` | Activate a saved prompt |

**Plugins & marketplaces**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/admin/api/plugins` | List installed plugins |
| `GET` | `/admin/api/plugins/blocklist` | Plugin blocklist config |
| `GET` | `/admin/api/plugins/{id}` | Plugin details |
| `GET` | `/admin/api/plugins/{id}/skills/{name}` | Plugin skill details |
| `GET` | `/admin/api/marketplaces` | Available plugin marketplaces |

### Responses API Deviations

The `/v1/responses` endpoint intentionally deviates from the OpenAI Responses API in the following ways:

| Behavior | This Gateway | OpenAI API |
|----------|-------------|------------|
| `instructions` + `previous_response_id` | Returns **400** — system prompt cannot change mid-session | Allowed (prior instructions don't carry over) |
| Stale `previous_response_id` | Returns **409** with the latest valid response ID for client recovery | May allow branching from earlier IDs |
| Backend mismatch in session | Returns **400** — mixing backends within a session is not supported | N/A |

**Stale ID recovery:** When a `409` is returned for a stale `previous_response_id`, the error message includes the current latest response ID (e.g., `resp_{session_id}_{turn}`), allowing clients to retry with the correct value.

## Docker

```bash
docker build -t oh-my-gateway .

# With a private PyPI mirror
docker build \
  --build-arg PIP_INDEX_URL=https://pypi.example.com/simple \
  -t oh-my-gateway .

# Pin or override the OpenCode version
docker build \
  --build-arg OPENCODE_VERSION=1.14.29 \
  -t oh-my-gateway .

# With API key auth
docker run -d -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=your-key \
  oh-my-gateway

# With CLI auth
docker run -d -p 8000:8000 \
  -v ~/.claude:/root/.claude \
  oh-my-gateway

# With workspace
docker run -d -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=your-key \
  -v /path/to/project:/workspace \
  -e CLAUDE_CWD=/workspace \
  oh-my-gateway

# With OpenCode in managed mode (gateway spawns `opencode serve`)
docker run -d -p 8000:8000 \
  -e BACKENDS=claude,opencode \
  -e OPENCODE_MODELS=openai/gpt-5.5 \
  -e OPENAI_API_KEY=your-key \
  oh-my-gateway

# With OpenCode in external mode (gateway is sandboxed; OpenCode runs on a trusted host)
docker run -d -p 8000:8000 \
  -e BACKENDS=claude,opencode \
  -e OPENCODE_BASE_URL=http://opencode-host:7891 \
  -e OPENCODE_SERVER_PASSWORD=... \
  -e OPENCODE_MODELS=openai/gpt-5.5 \
  oh-my-gateway
```

Or with docker-compose: set `BACKENDS=claude,opencode`, `OPENCODE_MODELS`, and provider keys (or `OPENCODE_BASE_URL` for external mode) in `.env`, then run `docker compose up -d`. The image includes OpenCode and the gateway starts it automatically when running in managed mode.

## Development

```bash
uv sync --group dev
uv export --format requirements.txt --no-dev --no-hashes --no-emit-project --locked -o requirements.txt  # Refresh Docker deps
uv run pytest tests/                                # Run tests
uv run pytest --cov=src --cov-report=term-missing    # With coverage
uv run ruff check --fix . && uv run ruff format .   # Lint & format
```

## Terms Compliance

You must use your own Claude access (API key or CLI auth) to use this gateway.

This project is a gateway layer on top of the official Claude Agent SDK and OpenCode. It does not pool credentials, resell access, or bypass upstream authentication.

- [Anthropic Usage Policy](https://www.anthropic.com/legal/aup)
- [Anthropic Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
- [Anthropic Commercial Terms](https://www.anthropic.com/legal/commercial-terms)

This is an independent open-source project, not affiliated with or endorsed by Anthropic or the OpenCode authors.

## License

MIT
