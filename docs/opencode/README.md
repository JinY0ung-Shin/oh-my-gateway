# OpenCode Backend

[OpenCode](https://opencode.ai) is a coding-agent runtime similar to Claude Code, but it can talk to any provider behind an OpenAI-compatible API (LiteLLM, vLLM, OpenAI, Anthropic, Bedrock, etc.). This gateway treats OpenCode as a second backend, sitting alongside Claude.

## When to use OpenCode vs Claude

| | Claude backend | OpenCode backend |
|---|----------------|------------------|
| **Routing** | `model: "sonnet" / "opus" / "haiku"` | `model: "opencode/<provider>/<model>"` |
| **Auth** | Anthropic API key or `claude auth login` | Provider-specific keys (passed through OpenCode) |
| **Provider variety** | Anthropic only | Any OpenAI-compatible endpoint |
| **MCP tools** | Native | Native (per OpenCode); wrapper `MCP_CONFIG` can be forwarded |
| **Reasoning models** | First-class via SDK | Works through any reasoning model on the provider side (e.g. via LiteLLM `merge_reasoning_content_in_choices: true`) |
| **Best for** | Anthropic Claude usage | Multi-provider routing, on-prem models, vendor isolation |

You can have **both backends enabled at once** — `BACKENDS=claude,opencode`. Each request is routed by its `model` id.

## Two operating modes

OpenCode runs in one of two modes. Pick based on **where the agent process needs to live**:

|  | **Managed** (default) | **External** |
|---|----------------------|---------------|
| Where OpenCode runs | Subprocess inside the gateway container | Separate process / host |
| Who owns the config | Gateway (built from `OPENCODE_CONFIG_CONTENT` + wrapper `MCP_CONFIG`) | The external server itself |
| Filesystem access | Whatever the gateway container can see | Whatever the external host can see |
| Setup complexity | Low | Medium |
| Use when… | Single deployment, gateway container is the trust boundary | Gateway is sandboxed but OpenCode needs broader filesystem access; or several gateway replicas share one OpenCode |

**Switch is automatic, controlled by `OPENCODE_BASE_URL`:**

- Unset → managed mode
- Set → external mode (gateway forwards to that URL)

Detailed setup:

- **[managed.md](managed.md)** — managed-mode setup with LiteLLM provider and MCP server walkthrough
- **[external.md](external.md)** — external-mode setup, security model, and what's a no-op
- **[litellm.md](litellm.md)** — LiteLLM-specific recipes (reasoning_content rendering, virtual keys, upstream patterns)

## Quick start (managed mode)

```bash
# .env
BACKENDS=claude,opencode
LITELLM_API_KEY=sk-1234
OPENCODE_CONFIG_CONTENT={"provider":{"litellm":{"npm":"@ai-sdk/openai-compatible","name":"LiteLLM","options":{"baseURL":"http://litellm:4000/v1","apiKey":"{env:LITELLM_API_KEY}"},"models":{"claude-sonnet-4-5":{},"gpt-4o":{}}}}}
OPENCODE_MODELS=litellm/claude-sonnet-4-5,litellm/gpt-4o
OPENCODE_DEFAULT_MODEL=litellm/claude-sonnet-4-5
```

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/litellm/claude-sonnet-4-5","input":"Hello"}'
```

Full walkthrough: [managed.md](managed.md).

## Quick start (external mode)

You already have `opencode serve` running on a trusted host:

```bash
# .env
BACKENDS=claude,opencode
OPENCODE_BASE_URL=http://opencode-host:7891
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=...
OPENCODE_MODELS=litellm/claude-sonnet-4-5
```

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/litellm/claude-sonnet-4-5","input":"Hello"}'
```

Full walkthrough: [external.md](external.md).

## Model id format

The gateway recognises any `model` field starting with `opencode/`:

```
opencode/<provider>/<model>
        ^^^^^^^^^^^^^^^^^^^
        passed verbatim to OpenCode
```

`<provider>` matches a key under `provider` in OpenCode's config (managed mode: `OPENCODE_CONFIG_CONTENT`; external mode: whatever config the external server loads). `<model>` matches a key under that provider's `models` block.

The gateway's **`OPENCODE_MODELS` env var is an allowlist** that controls which `opencode/<provider>/<model>` ids `/v1/models` returns and which the `/v1/responses` endpoint accepts. Anything not in the list is rejected at the wrapper layer, even if the underlying OpenCode server would happily run it.

```bash
OPENCODE_MODELS=litellm/claude-sonnet-4-5,litellm/gpt-4o,openai/gpt-5.5
```

Wire-format example (what the request body sends):

```json
{ "model": "opencode/litellm/claude-sonnet-4-5", "input": "..." }
```

## Settings that apply in both modes

These work regardless of mode:

| Variable | Description |
|----------|-------------|
| `OPENCODE_MODELS` | Public allowlist exposed via `/v1/models` |
| `OPENCODE_DEFAULT_MODEL` | Used when an `opencode/...` request omits the provider|
| `OPENCODE_AGENT` | OpenCode agent profile (`general` by default) |
| `OPENCODE_QUESTION_PERMISSION` | `ask` / `allow` / `deny` for OpenCode's `question` tool |
| `OPENCODE_SERVER_USERNAME` | Basic-auth username (default `opencode`) |
| `OPENCODE_SERVER_PASSWORD` | Basic-auth password; absent = no auth header sent |

## Settings that apply in managed mode only

These are silently ignored in external mode:

| Variable | Description |
|----------|-------------|
| `OPENCODE_BIN` | Binary path (default `opencode`) |
| `OPENCODE_HOST`, `OPENCODE_PORT` | Bind address for the spawned subprocess |
| `OPENCODE_START_TIMEOUT_MS` | Startup wait timeout (default 5000) |
| `OPENCODE_CONFIG_CONTENT` | Provider/MCP config injected into the subprocess |
| `OPENCODE_USE_WRAPPER_MCP_CONFIG` | Forward wrapper `MCP_CONFIG` to OpenCode |

## Switching modes

To move from managed → external:

1. Stand up `opencode serve` on the target host with the same config you previously fed to the gateway via `OPENCODE_CONFIG_CONTENT`.
2. Set `OPENCODE_BASE_URL` (and basic-auth env vars if applicable) on the gateway.
3. Restart the gateway. Verify `GET /admin/api/backends` shows `opencode.config.mode = external`.
4. Optional: clear `OPENCODE_CONFIG_CONTENT` and `OPENCODE_USE_WRAPPER_MCP_CONFIG` from the gateway env. They're no-ops in external mode but removing them avoids confusion.

External → managed is the reverse: unset `OPENCODE_BASE_URL`, restore the config envs, restart.

## Verification

```bash
# Mode + base url
curl -s http://localhost:8000/admin/api/backends | jq '.opencode.config'
# {"mode": "managed", "binary": "/usr/local/bin/opencode"} OR
# {"mode": "external", "base_url": "http://opencode-host:7891"}

# Models actually exposed
curl -s http://localhost:8000/v1/models | jq '.data[].id'

# End-to-end smoke test
curl -N http://localhost:8000/v1/responses \
  -d '{"model":"opencode/litellm/claude-sonnet-4-5","input":"ping","stream":true}'
```

For mode-specific troubleshooting see [managed.md](managed.md#troubleshooting) and [external.md](external.md#troubleshooting).
