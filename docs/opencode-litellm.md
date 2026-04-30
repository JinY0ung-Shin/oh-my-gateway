# OpenCode + LiteLLM Setup

How to use the OpenCode backend through this gateway, and how to wire it to a
LiteLLM proxy so OpenCode talks to whatever models LiteLLM exposes (OpenAI,
Anthropic, Bedrock, local vLLM, etc.) instead of going to OpenAI directly.

## TL;DR

- `BACKENDS=claude,opencode` turns OpenCode on (Claude is the default).
- `OPENCODE_MODELS` is the **public allowlist** — what the gateway exposes as
  `opencode/<provider>/<model>`.
- `OPENCODE_CONFIG_CONTENT` is the **OpenCode config JSON** — what defines the
  actual provider (URL, key, models) **and MCP servers**. LiteLLM and MCP go
  here.
- Call the gateway with `model: "opencode/<provider>/<model>"`. The
  `<provider>/<model>` must match an entry in both `OPENCODE_MODELS` and the
  `provider` block of `OPENCODE_CONFIG_CONTENT`.

## How model routing works

```
client request                gateway                        OpenCode
────────────────              ───────────────────             ─────────────────
model: opencode/X/Y    ──►    strip "opencode/" prefix  ──►   provider X / model Y
                              check OPENCODE_MODELS           resolve via opencode
                              allowlist                       config (provider URL,
                                                              key, model id)
```

The `opencode/` prefix tells the gateway to use the OpenCode backend (see
`src/backends/opencode/`). The remaining `<provider>/<model>` is passed to the
managed `opencode serve` instance, which uses the merged config to decide
which HTTP endpoint and key to use.

## Step 1 — Enable OpenCode

```bash
BACKENDS=claude,opencode
```

The gateway starts `opencode serve` on its own (the Docker image bundles the
`opencode` binary; for local dev install it and make sure it is on `PATH`).
Verify: `GET /admin/api/backends` should show `opencode: valid`.

## Step 2 — Point OpenCode at LiteLLM

LiteLLM exposes an OpenAI-compatible endpoint, so you register it as an
"openai-compatible" provider inside OpenCode's config. The gateway accepts the
config as a JSON string in `OPENCODE_CONFIG_CONTENT` and merges it through
`build_opencode_config` (`src/backends/opencode/config.py`).

Minimal config:

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

- `baseURL` is your LiteLLM proxy's `/v1` endpoint.
- `{env:VAR}` is OpenCode's env-var interpolation; the variable must be set in
  the gateway process environment.
- Each key under `models` must be a **LiteLLM model name** (the same string you
  call LiteLLM with — `model` in LiteLLM's `model_list`).

In `.env`, pass it as a single-line JSON string:

```bash
BACKENDS=claude,opencode
LITELLM_API_KEY=sk-your-litellm-key
OPENCODE_CONFIG_CONTENT={"provider":{"litellm":{"npm":"@ai-sdk/openai-compatible","name":"LiteLLM","options":{"baseURL":"http://litellm:4000/v1","apiKey":"{env:LITELLM_API_KEY}"},"models":{"claude-sonnet-4-5":{},"gpt-4o":{},"gpt-4o-mini":{}}}}}
```

## Step 3 — Allowlist models

`OPENCODE_MODELS` is what `/v1/models` returns and what the gateway accepts on
the wire. Each entry must match a `<provider>/<model-key>` pair from your
config above:

```bash
OPENCODE_MODELS=litellm/claude-sonnet-4-5,litellm/gpt-4o,litellm/gpt-4o-mini
OPENCODE_DEFAULT_MODEL=litellm/claude-sonnet-4-5
```

Drop `openai/gpt-5.5` etc. from `OPENCODE_MODELS` if you don't want OpenAI
direct access — only what's listed here is reachable.

## Step 4 — Call it

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opencode/litellm/claude-sonnet-4-5",
    "input": "ping"
  }'
```

Streaming, `previous_response_id` chaining, `user` workspace isolation all work
the same as for Claude.

## Mixing LiteLLM with other providers

`provider` is a dict — add as many entries as you want. They share the same
config:

```json
{
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://litellm:4000/v1",
        "apiKey": "{env:LITELLM_API_KEY}"
      },
      "models": { "claude-sonnet-4-5": {}, "gpt-4o": {} }
    },
    "openai": {
      "options": { "apiKey": "{env:OPENAI_API_KEY}" },
      "models": { "gpt-5.5": {} }
    }
  }
}
```

Then expose both:

```bash
OPENCODE_MODELS=litellm/claude-sonnet-4-5,litellm/gpt-4o,openai/gpt-5.5
```

## MCP servers with OpenCode

OpenCode has its **own** MCP config schema (separate from the wrapper's
`MCP_CONFIG`). You can configure it two ways, and they can be mixed.

### Option A — Inline MCP servers in `OPENCODE_CONFIG_CONTENT`

OpenCode's native format uses a top-level `mcp` block with two server types:

- **`local`** — stdio server (subprocess command + args + env)
- **`remote`** — HTTP / SSE / streamable-HTTP server

```json
{
  "provider": { "...": "..." },
  "mcp": {
    "filesystem": {
      "type": "local",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
      "environment": { "FOO": "bar" },
      "enabled": true,
      "timeout": 30
    },
    "internal-search": {
      "type": "remote",
      "url": "https://mcp.example.com/sse",
      "headers": { "Authorization": "Bearer {env:INTERNAL_MCP_TOKEN}" },
      "enabled": true
    }
  }
}
```

`{env:VAR}` interpolation works in `headers` values and inside `environment`.
The variable must be exported to the gateway process so OpenCode (started as
a child of the gateway) inherits it.

### Option B — Reuse the wrapper's `MCP_CONFIG`

If you already have `MCP_CONFIG` set up for Claude (gateway-wide MCP), have
the gateway forward it to OpenCode automatically:

```bash
OPENCODE_USE_WRAPPER_MCP_CONFIG=true
```

The converter at `src/backends/opencode/config.py:_convert_mcp_server` maps
wrapper transports to OpenCode types:

| Wrapper `type` | OpenCode `type` | Notes |
|----------------|-----------------|-------|
| `stdio` | `local` | `command` + `args` flattened into a single list; `env`/`environment` copied across |
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
      "headers": { "Authorization": "Bearer {env:SEARCH_TOKEN}" }
    }
  }
}
```

With `OPENCODE_USE_WRAPPER_MCP_CONFIG=true`, OpenCode will see `fs` as `local`
and `search` as `remote`.

### Precedence (when you mix A + B)

- Servers explicitly defined in `OPENCODE_CONFIG_CONTENT.mcp` are kept as-is.
- Wrapper `MCP_CONFIG` entries are added **only** for names not already
  present (the merger uses `setdefault`, see `build_opencode_config` →
  `mcp_config.setdefault(name, ...)`).

So `MCP_CONFIG` is your shared baseline; override per-OpenCode by adding the
same name to `OPENCODE_CONFIG_CONTENT.mcp`.

### Where it ends up / how to verify

- Managed `opencode serve` is started with the merged config inlined as a
  temporary `opencode.json`.
- `GET /admin/api/mcp-servers` — what each backend sees.
- `GET /admin/api/backends` — `opencode.config.mode = managed` plus the
  resolved binary path.
- At runtime, ask OpenCode "what tools do you have" — MCP tool names appear
  as `mcp__<server>__<tool>`.

### Common pitfalls

- **Stdio command in Docker** — the binary (`npx`, `uvx`, `python`, …) must
  exist *inside the gateway container*, not just on the host. Add it to
  the image.
- **Remote `url`** — must be reachable from the gateway container's
  network, not from your laptop.
- **`{env:VAR}`** — OpenCode interpolates this at startup; if the variable
  is unset OpenCode fails the server load. Confirm with
  `GET /admin/api/config` (redacted) that the var made it into the gateway
  env.
- **stdio `env` vs `environment`** — wrapper `MCP_CONFIG` accepts either
  field name, OpenCode only writes `environment`. The converter handles
  both directions automatically.

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

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `400 unknown model: opencode/litellm/foo` | `litellm/foo` missing from `OPENCODE_MODELS` |
| OpenCode replies but model errors out | model key not in `OPENCODE_CONFIG_CONTENT.provider.litellm.models`, or LiteLLM doesn't know that model |
| `opencode binary not found on PATH` | install OpenCode (`npm i -g opencode-ai`) or use the Docker image |
| 401 from OpenCode | `LITELLM_API_KEY` unset or `{env:...}` interpolation typo |
| Wrapper MCP tools missing in OpenCode sessions | set `OPENCODE_USE_WRAPPER_MCP_CONFIG=true` |
| OpenCode MCP tools listed but never called | server failed to start; check gateway logs for `[opencode]` MCP load errors and verify command/url is reachable |
| `mcp__<x>__<y>` shows for Claude but not OpenCode (or vice versa) | server defined only in one config; either add it to the other side or use Option B |

Useful checks:

- `GET /v1/models` — what the gateway is exposing
- `GET /admin/api/backends` — backend health and resolved config
- `GET /admin/api/mcp-servers` — MCP servers loaded per backend
- `GET /admin/api/config` — redacted runtime env (confirms `OPENCODE_*`,
  `MCP_CONFIG`, and provider keys got picked up)

## Related code

- `src/backends/opencode/constants.py` — parses `OPENCODE_MODELS`
- `src/backends/opencode/config.py` — merges `OPENCODE_CONFIG_CONTENT` with
  defaults and wrapper MCP servers; `_convert_mcp_server` does the
  stdio→local / http→remote translation
- `src/backends/opencode/auth.py` — env vars forwarded to managed
  `opencode serve`
- `src/mcp_config.py` — wrapper `MCP_CONFIG` parser (shared with Claude)
- `.env.example` — full list of `OPENCODE_*` env vars
