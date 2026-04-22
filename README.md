# Claude Code Gateway

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://github.com/JinY0ung-Shin/claude-code-gateway)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

FastAPI gateway that exposes the Claude Agent SDK through the Responses API. Use Claude Code with `previous_response_id` chaining for multi-turn conversations.

## Quick Start

```bash
git clone https://github.com/JinY0ung-Shin/claude-code-gateway
cd claude-code-gateway
uv sync

export ANTHROPIC_AUTH_TOKEN=your-api-key

uv run uvicorn src.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'
```

## Features

- **Responses API** — `/v1/responses` with `previous_response_id` chaining
- **Session Management** — Multi-turn conversations via `session_id`
- **Auth Support** — API key or CLI auth
- **MCP Server Integration** — Connect external tool servers at startup
- **Subagent Control** — Block specific subagent types per deployment
- **Adaptive Thinking** — Configurable thinking modes and budget
- **Token-Level Streaming** — Real-time token delivery via SDK partial messages
- **Rate Limiting** — Per-endpoint configurable limits
- **Docker Ready** — Dockerfile and docker-compose included

## Installation

**Prerequisites:** Python 3.10+ and [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/JinY0ung-Shin/claude-code-gateway
cd claude-code-gateway
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
| `DISALLOWED_SUBAGENT_TYPES` | `statusline-setup` | Comma-separated subagent types to block |
| `CLAUDE_SANDBOX_ENABLED` | unset | Bash sandbox: unset = project settings, `true` = force on, `false` = force off |
| `MCP_CONFIG` | — | MCP server config (JSON string or file path) |
| `API_KEY` | — | Optional Bearer token for access control |
| `SESSION_MAX_AGE_MINUTES` | `60` | Session TTL |

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
| `GET` | `/admin/api/backends` | Backend health, auth status, model availability |
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
docker build -t claude-code-gateway .

# With API key auth
docker run -d -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=your-key \
  claude-code-gateway

# With CLI auth
docker run -d -p 8000:8000 \
  -v ~/.claude:/root/.claude \
  claude-code-gateway

# With workspace
docker run -d -p 8000:8000 \
  -e ANTHROPIC_AUTH_TOKEN=your-key \
  -v /path/to/project:/workspace \
  -e CLAUDE_CWD=/workspace \
  claude-code-gateway
```

Or with docker-compose: `docker compose up -d`

## Development

```bash
uv sync --group dev
uv run pytest tests/                                # Run tests
uv run pytest --cov=src --cov-report=term-missing    # With coverage
uv run ruff check --fix . && uv run ruff format .   # Lint & format
```

## Terms Compliance

You must use your own Claude access (API key or CLI auth) to use this gateway.

This project is a gateway layer on top of the official Claude Agent SDK. It does not pool credentials, resell access, or bypass Anthropic authentication.

- [Usage Policy](https://www.anthropic.com/legal/aup)
- [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
- [Commercial Terms](https://www.anthropic.com/legal/commercial-terms)

This is an independent open-source project, not affiliated with or endorsed by Anthropic.

## License

MIT
