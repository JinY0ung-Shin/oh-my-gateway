# Codex Backend

The Codex backend is an opt-in gateway backend for the local Codex harness.
It uses `codex app-server --listen stdio://` and drives the app-server JSON-RPC
protocol directly.

## Enable

```bash
BACKENDS=claude,codex
CODEX_MODELS=gpt-5.5
DEFAULT_MODEL=codex/gpt-5.5
```

Requests use `codex/<model>`:

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "codex/gpt-5.5", "input": "Summarize this repository"}'
```

## Runtime Requirements

- Codex CLI must be installed and available on `PATH`, or set `CODEX_BIN`.
- Codex auth is owned by the Codex CLI/app-server. Use your existing ChatGPT
  Codex login or Codex-supported API-key setup.
- Each gateway session maps to one Codex app-server thread. The gateway stores
  the Codex thread id on the server-side session for subsequent turns.
- The gateway keeps one Codex app-server subprocess per backend instance and
  serializes Codex turns through that shared process. This avoids per-session
  process startup cost while keeping the MVP transport simple.

## Python SDK Status

OpenAI documents a Python SDK package named `openai-codex-app-server-sdk`
with import package `codex_app_server`. It is experimental and controls the
same local `codex app-server` JSON-RPC protocol used here.

At implementation time, the documented package and paired
`openai-codex-cli-bin` runtime package were not resolvable from PyPI in this
environment, while the installed Codex CLI 0.128.0 app-server responded to
`initialize` and `model/list`. For that reason, the MVP keeps a small in-tree
JSON-RPC client instead of adding an unavailable package dependency.

## Configuration

| Variable | Purpose |
|----------|---------|
| `CODEX_BIN` | Codex CLI binary name/path. Default: `codex` |
| `CODEX_MODELS` | Comma-separated model allowlist exposed as `codex/<model>`. Default: `gpt-5.5` |
| `CODEX_APPROVAL_POLICY` | `approvalPolicy` sent to Codex threads and turns. Default: `never` |
| `CODEX_SANDBOX` | Thread-level Codex sandbox mode. Default: `danger-full-access` for local experimental use |
| `CODEX_CONFIG_OVERRIDES` | Comma-separated `codex --config key=value` overrides |

## Docker Compose

Use the Codex-specific Compose file when the gateway container should own the
Codex backend runtime:

```bash
cp .env.example .env
# set ADMIN_API_KEY and either OPENAI_API_KEY/CODEX_API_KEY, or run codex login
docker compose -f docker-compose.codex.yml up -d --build
```

`docker-compose.codex.yml` builds `Dockerfile.codex`, installs
`@openai/codex@${CODEX_VERSION:-0.130.0}`, forces `BACKENDS=codex`, and stores
Codex CLI state in the `codex_home` named volume mounted at `/home/app/.codex`.

For ChatGPT/Codex CLI login instead of API-key auth:

```bash
docker compose -f docker-compose.codex.yml run --rm gateway codex login
docker compose -f docker-compose.codex.yml up -d
```

Use `CODEX_DEFAULT_GATEWAY_MODEL` to change the container's gateway default
model without reusing a Claude-oriented `DEFAULT_MODEL` from `.env`.

## Supported Behavior

- Text prompts and text responses are supported.
- Responses `input_image` parts are forwarded to Codex as native image input
  items when the request uses a Codex model.
- `temperature` and `max_output_tokens` are forwarded to Codex `turn/start`
  params as generation controls.
- Gateway `allowed_tools`, `disallowed_tools`, `permission_mode`, and the
  global `DISALLOWED_TOOLS` setting are enforced at Codex approval time.
- Codex app-server command, file-change, and permission approval requests are
  exposed as Responses `requires_action` entries with the existing
  `AskUserQuestion` function-call shape. Send a matching
  `function_call_output` with the previous response id to continue the paused
  Codex turn.
- Command/file approvals accept Codex decision strings such as `accept`,
  `acceptForSession`, `decline`, and `cancel`. Short aliases like `yes`,
  `no`, and `always` are normalized by the gateway.
- MCP server configuration is forwarded to Codex thread params; MCP approvals
  honor both the `mcpToolCall` bucket and Claude-style
  `mcp__<server>__*` / `mcp__<server>__<tool>` policy names.

## Current Limits

- Codex turns are serialized through one shared app-server process; concurrent
  Codex request multiplexing is not implemented yet.
- The shared Codex app-server is restarted when the metadata-derived allowlisted
  environment changes between requests.
- A Codex turn or approval transport error closes the shared app-server so the
  next request starts from a fresh process.
- Codex app-server payload field names follow the current in-tree fixtures and
  local CLI conventions; they should be rechecked when the Codex app-server
  protocol changes.
