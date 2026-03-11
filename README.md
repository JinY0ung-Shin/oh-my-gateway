# Claude Code Gateway

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://github.com/JinY0ung-Shin/claude-code-gateway)
[![MIT License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

FastAPI gateway that exposes the Claude Agent SDK through OpenAI-compatible APIs. Use Claude Code with any OpenAI client library, Open WebUI, or custom integrations. Some OpenAI parameters (e.g., `temperature`, `top_p`) are accepted for compatibility but silently ignored — see `/v1/compatibility` for details.

## Quick Start

```bash
git clone https://github.com/JinY0ung-Shin/claude-code-gateway
cd claude-code-gateway
uv sync

export ANTHROPIC_AUTH_TOKEN=your-api-key

uv run uvicorn src.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Features

- **OpenAI API Compatible** — `/v1/chat/completions` with streaming
- **Anthropic API Compatible** — `/v1/messages` endpoint
- **Responses API** — `/v1/responses` with `previous_response_id` chaining
- **Session Management** — Multi-turn conversations via `session_id`
- **Auth Support** — API key or CLI auth
- **MCP Server Integration** — Connect external tool servers at startup
- **Subagent Control** — Block specific subagent types per deployment
- **Adaptive Thinking** — Configurable thinking modes and budget
- **Token-Level Streaming** — Real-time token delivery via SDK partial messages
- **Rate Limiting** — Per-endpoint configurable limits
- **Docker Ready** — Dockerfile and docker-compose included
- **Open WebUI Pipe** — Drop-in integration via `open_webui_pipe.py`

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
| `TOKEN_STREAMING` | `true` | Token-level partial streaming |
| `MAX_TIMEOUT` | `600000` | Request timeout (ms) |
| `DEFAULT_MAX_TURNS` | `10` | Max agent turns per request |
| `DISALLOWED_SUBAGENT_TYPES` | `statusline-setup` | Comma-separated subagent types to block |
| `CLAUDE_SANDBOX_ENABLED` | unset | Bash sandbox: unset = project settings, `true` = force on, `false` = force off |
| `MCP_CONFIG` | — | MCP server config (JSON string or file path) |
| `API_KEY` | — | Optional Bearer token for access control |
| `SESSION_MAX_AGE_MINUTES` | `60` | Session TTL |
| `RATE_LIMIT_CHAT_PER_MINUTE` | `10` | Chat endpoint rate limit |

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

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="any")

# Basic
response = client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "Explain quantum computing"}]
)
print(response.choices[0].message.content)

# Streaming
for chunk in client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "Write a haiku"}],
    stream=True
):
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")

# Session continuity
response = client.chat.completions.create(
    model="sonnet",
    messages=[{"role": "user", "content": "My name is Alice"}],
    extra_body={"session_id": "my-session"}
)
```

### curl

```bash
# Streaming
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'

# With session
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Remember: x=42"}], "session_id": "s1"}'

# Anthropic-style messages
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "max_tokens": 512, "messages": [{"role": "user", "content": "Hello"}]}'
```

### Open WebUI

`open_webui_pipe.py` is included for Open WebUI integration. Configure in the pipe:

- `BASE_URL` — Gateway URL (e.g., `http://localhost:8000`)
- `API_KEY` — Gateway Bearer token, if enabled
- `MODEL` — `sonnet`, `opus`, or `haiku`

The pipe uses `/v1/responses` with `previous_response_id` chaining, maintaining real multi-turn continuity without replaying the full transcript.

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions |
| `POST` | `/v1/messages` | Anthropic-style messages |
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
| `POST` | `/v1/compatibility` | OpenAI parameter compatibility report |
| `POST` | `/v1/debug/request` | Debug raw request inspection |
| `GET` | `/` | Interactive API explorer |

Streaming (`"stream": true`) is supported on `/v1/chat/completions`, `/v1/messages`, and `/v1/responses`.

For detailed SSE event formats including tool call rendering, subagent events, and tool name/input schemas, see **[docs/streaming-events.md](docs/streaming-events.md)**.

### Responses API Deviations

The `/v1/responses` endpoint intentionally deviates from the OpenAI Responses API in the following ways:

| Behavior | This Gateway | OpenAI API |
|----------|-------------|------------|
| `instructions` + `previous_response_id` | Returns **400** — system prompt cannot change mid-session | Allowed (prior instructions don't carry over) |
| Stale `previous_response_id` | Returns **409** with the latest valid response ID for client recovery | May allow branching from earlier IDs |
| Backend mismatch in session | Returns **400** — mixing Claude/Codex models within a session is not supported | N/A |
| Codex resume without thread_id | Returns **409** — previous turn must have returned a thread_id to continue | N/A |
| Failure-path `provider_session_id` | Captured internally but not externally recoverable — no `response_id` is committed on failed turns | N/A |

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
