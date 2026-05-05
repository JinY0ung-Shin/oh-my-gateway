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

## Supported Behavior

- Text prompts and text responses are supported.
- Codex app-server command, file-change, and permission approval requests are
  exposed as Responses `requires_action` entries with the existing
  `AskUserQuestion` function-call shape. Send a matching
  `function_call_output` with the previous response id to continue the paused
  Codex turn.
- Command/file approvals accept Codex decision strings such as `accept`,
  `acceptForSession`, `decline`, and `cancel`. Short aliases like `yes`,
  `no`, and `always` are normalized by the gateway.

## Current Limits

- Codex turns are serialized through one shared app-server process; concurrent
  Codex request multiplexing is not implemented yet.
- Image input is not exposed through the gateway Codex backend yet, even though
  Codex app-server models may support images.
