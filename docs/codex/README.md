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
| `CODEX_SANDBOX` | Thread-level Codex sandbox mode. Default: `workspace-write` |
| `CODEX_CONFIG_OVERRIDES` | Comma-separated `codex --config key=value` overrides |

## MVP Limits

- Text prompts and text responses are supported.
- OpenAI Responses API function-call continuations are not mapped to Codex
  approval flows yet. Codex app-server approval requests are cancelled until
  the gateway has an explicit approval UI.
- Image input is not exposed through the gateway Codex backend yet, even though
  Codex app-server models may support images.
