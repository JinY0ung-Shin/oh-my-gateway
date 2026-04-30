# OpenCode — Managed Mode

In managed mode the gateway spawns `opencode serve` as a subprocess on startup, feeds it a generated config, and proxies HTTP traffic to it. This is the **default** when `BACKENDS=claude,opencode` and `OPENCODE_BASE_URL` is unset.

```
┌──────────────────────────────────────────┐
│ gateway container                        │
│                                          │
│   FastAPI    ───── HTTP ─────►  opencode │
│   (port 8000)                  serve     │
│                                (port N)  │
│                                          │
│   spawns/owns  ──────────────────►       │
│   reads stdout for "listening on" line   │
└──────────────────────────────────────────┘
```

## When to use

- Single deployment where the gateway container is the trust boundary
- You want one source of truth for provider config and MCP servers (the gateway env)
- You don't need OpenCode to access files outside the gateway container

If you need OpenCode to run somewhere else, see [external.md](external.md).

## Prerequisites

- `opencode` binary on `PATH` inside the gateway process / container
- The Docker image bundles it; for local dev: `npm i -g opencode-ai` (or whatever the official install method is)

Verify the gateway sees the binary:

```bash
curl -s http://localhost:8000/admin/api/backends \
  | jq '.backends[] | select(.name == "opencode") | {healthy, auth, metadata}'
```

```json
{
  "healthy": true,
  "auth": {"valid": true, "errors": []},
  "metadata": {"mode": "managed", "base_url": "http://127.0.0.1:4096"}
}
```

## Step 1 — enable the backend

```bash
BACKENDS=claude,opencode
```

Without `opencode` in `BACKENDS`, the OpenCode-prefixed model ids return `404 unknown model`.

## Step 2 — declare your providers

OpenCode loads a config that defines providers (LLM endpoints) and which model ids to expose for each. The gateway accepts this config as a JSON string in `OPENCODE_CONFIG_CONTENT`. The string is parsed, merged with safe defaults, and serialised back into the OpenCode config file.

### LiteLLM provider example

LiteLLM exposes an OpenAI-compatible endpoint, so register it as an `openai-compatible` provider:

```json
{
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM",
      "options": {
        "baseURL": "http://litellm:4000/v1",
        "apiKey": "{env:LITELLM_API_KEY}"
      },
      "models": {
        "claude-sonnet-4-5": {},
        "gpt-4o": {},
        "gpt-4o-mini": {}
      }
    }
  }
}
```

Notes:

- `baseURL` is your LiteLLM `/v1` endpoint (network-reachable from the gateway container).
- `{env:VAR}` is OpenCode's env-var interpolation; the variable must exist in the gateway process env so OpenCode (a child process) inherits it.
- Each key under `models` is a **LiteLLM model name** — the same string you'd send as `model` directly to LiteLLM.

In `.env`:

```bash
LITELLM_API_KEY=sk-1234
OPENCODE_CONFIG_CONTENT={"provider":{"litellm":{"npm":"@ai-sdk/openai-compatible","name":"LiteLLM","options":{"baseURL":"http://litellm:4000/v1","apiKey":"{env:LITELLM_API_KEY}"},"models":{"claude-sonnet-4-5":{},"gpt-4o":{}}}}}
```

### Mixing providers

`provider` is a dict — add as many entries as you want:

```json
{
  "provider": {
    "litellm": { "npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://litellm:4000/v1", "apiKey": "{env:LITELLM_API_KEY}"}, "models": {"gpt-4o": {}} },
    "openai":  { "options": {"apiKey": "{env:OPENAI_API_KEY}"}, "models": {"gpt-5.5": {}} }
  }
}
```

## Step 3 — allowlist models

`OPENCODE_MODELS` controls what `/v1/models` returns and what the `/v1/responses` endpoint accepts. Each entry must match a `<provider>/<model-key>` pair from your config:

```bash
OPENCODE_MODELS=litellm/claude-sonnet-4-5,litellm/gpt-4o,openai/gpt-5.5
OPENCODE_DEFAULT_MODEL=litellm/claude-sonnet-4-5
```

Anything not in the allowlist is rejected by the gateway, even if the underlying OpenCode server would happily run it.

## Step 4 — call it

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/litellm/claude-sonnet-4-5",
    "input": "ping"
  }'
```

Streaming, `previous_response_id` chaining, `user` workspace isolation work the same as for Claude.

## MCP servers

OpenCode has its own MCP config schema (separate from the wrapper's `MCP_CONFIG`). You can configure it two ways, and they can be mixed.

### Option A — inline in `OPENCODE_CONFIG_CONTENT`

OpenCode's native MCP block uses `type: "local"` (stdio) or `type: "remote"` (HTTP/SSE/streamable-HTTP):

```json
{
  "provider": { "...": "..." },
  "mcp": {
    "filesystem": {
      "type": "local",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
      "environment": {"FOO": "bar"},
      "enabled": true,
      "timeout": 30
    },
    "internal-search": {
      "type": "remote",
      "url": "https://mcp.example.com/sse",
      "headers": {"Authorization": "Bearer {env:INTERNAL_MCP_TOKEN}"},
      "enabled": true
    }
  }
}
```

`{env:VAR}` interpolation works in `headers` values and inside `environment`. Variables must exist in the gateway env for OpenCode to inherit.

### Option B — reuse the wrapper's `MCP_CONFIG`

If you already have `MCP_CONFIG` set up for Claude, forward it to OpenCode automatically:

```bash
OPENCODE_USE_WRAPPER_MCP_CONFIG=true
```

The converter at `src/backends/opencode/config.py:_convert_mcp_server` translates wrapper transports into OpenCode types:

| Wrapper `type` | OpenCode `type` | Notes |
|----------------|-----------------|-------|
| `stdio` | `local` | `command` + `args` flattened into one list; `env`/`environment` copied across |
| `http`, `sse`, `streamable-http` | `remote` | `url`, `headers`, `oauth`, `enabled`, `timeout` preserved |

Wrapper `MCP_CONFIG` example that works on both backends:

```json
{
  "mcpServers": {
    "fs": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    },
    "search": {
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer {env:SEARCH_TOKEN}"}
    }
  }
}
```

### Precedence (Option A + B combined)

- Servers explicitly defined in `OPENCODE_CONFIG_CONTENT.mcp` are kept as-is
- Wrapper `MCP_CONFIG` entries are added **only** for names not already present (the merger uses `setdefault`)

So `MCP_CONFIG` is the shared baseline; override per-OpenCode by adding the same name to `OPENCODE_CONFIG_CONTENT.mcp`.

## Reasoning content (`<think>` rendering)

If you call a reasoning model (e.g. via LiteLLM), the model's reasoning_content can either be returned as a separate field or merged into the response text. The cleanest path for Open WebUI / clients that recognise `<think>` tags is to enable LiteLLM's tag-merge mode:

```yaml
# litellm config.yaml
litellm_settings:
  merge_reasoning_content_in_choices: true
```

LiteLLM then prefixes reasoning into `content` as `<think>...</think>`, and the gateway/client renders it as a collapsed block. No gateway-side changes needed.

## docker-compose example

```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-stable
    ports: ["4000:4000"]
    volumes:
      - ./litellm.config.yaml:/app/config.yaml
    command: ["--config", "/app/config.yaml"]

  gateway:
    build: .
    ports: ["8000:8000"]
    environment:
      ANTHROPIC_AUTH_TOKEN: ${ANTHROPIC_AUTH_TOKEN}
      BACKENDS: claude,opencode
      LITELLM_API_KEY: ${LITELLM_API_KEY}
      OPENCODE_MODELS: litellm/claude-sonnet-4-5,litellm/gpt-4o
      OPENCODE_DEFAULT_MODEL: litellm/claude-sonnet-4-5
      OPENCODE_USE_WRAPPER_MCP_CONFIG: "true"
      MCP_CONFIG: |
        {"mcpServers":{"fs":{"type":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/workspace"]}}}
      OPENCODE_CONFIG_CONTENT: |
        {"provider":{"litellm":{"npm":"@ai-sdk/openai-compatible","name":"LiteLLM","options":{"baseURL":"http://litellm:4000/v1","apiKey":"{env:LITELLM_API_KEY}"},"models":{"claude-sonnet-4-5":{},"gpt-4o":{}}}}}
    depends_on: [litellm]
```

## Verification

```bash
# Backend mode + base URL
curl -s http://localhost:8000/admin/api/backends \
  | jq '.backends[] | select(.name == "opencode") | .metadata'

# Models exposed
curl -s http://localhost:8000/v1/models | jq '.data[].id'

# MCP servers each backend sees
curl -s http://localhost:8000/admin/api/mcp-servers | jq

# Live smoke test
OPENCODE_SMOKE_ENABLED=1 \
OPENCODE_SMOKE_MODEL=litellm/claude-sonnet-4-5 \
uv run pytest tests/integration/test_opencode_smoke.py -q
```

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `opencode binary not found on PATH` | Install OpenCode in the gateway image / dev environment |
| `Timeout waiting for OpenCode server after 5000ms` | `opencode serve` failed to start; check gateway logs for stderr from the subprocess. Common: invalid `OPENCODE_CONFIG_CONTENT` JSON, port conflict |
| `400 unknown model: opencode/litellm/foo` | `litellm/foo` missing from `OPENCODE_MODELS` |
| `provider not found` from OpenCode | provider key not in `OPENCODE_CONFIG_CONTENT.provider`, or model key not under it |
| MCP tools never appear | server failed to start; check gateway logs for `[opencode]` MCP load errors and verify command/url is reachable |
| Reasoning text leaks into the answer body | enable `merge_reasoning_content_in_choices` on LiteLLM, or use a non-reasoning model |
| 401 from OpenCode | `OPENCODE_SERVER_PASSWORD` set on gateway but not on the (auto-spawned) subprocess — managed mode shouldn't need basic auth, so simply unset both username/password env vars |

Useful checks:

- `GET /admin/api/backends` — backend health, auth status, mode, and base URL
- `GET /admin/api/mcp-servers` — what MCP each backend sees
- `GET /admin/api/config` — redacted env (verify variables made it in)
- gateway stdout — captures the OpenCode subprocess's stdout/stderr

## Configuration reference (managed mode)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCODE_BIN` | `opencode` | Binary on `PATH` |
| `OPENCODE_HOST` | `127.0.0.1` | Subprocess bind host |
| `OPENCODE_PORT` | `0` (auto) | Subprocess bind port |
| `OPENCODE_START_TIMEOUT_MS` | `5000` | How long to wait for "listening on" line |
| `OPENCODE_AGENT` | `general` | Agent profile id |
| `OPENCODE_DEFAULT_MODEL` | unset | Used when request omits provider/model |
| `OPENCODE_QUESTION_PERMISSION` | `ask` | `ask` / `allow` / `deny` for the `question` tool |
| `OPENCODE_CONFIG_CONTENT` | `{}` | Provider + MCP config injected into the subprocess |
| `OPENCODE_USE_WRAPPER_MCP_CONFIG` | `false` | Forward wrapper `MCP_CONFIG` to OpenCode |
| `OPENCODE_MODELS` | unset | Public allowlist of `<provider>/<model>` ids |
