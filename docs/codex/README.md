# Codex Backend

The Codex backend is an opt-in gateway backend built on the **official
`openai-codex` Python SDK** (Codex-pinned versioning; the paired
`openai-codex-cli-bin` package bundles the Codex CLI binary, so nothing needs
to be installed on `PATH`). The SDK drives a local
`codex app-server --listen stdio://` process with generated v2 protocol types.

> History: the backend originally kept a hand-rolled app-server JSON-RPC
> client in-tree (frozen 2026-07) because no official Python SDK was
> installable at the time. It was rebuilt on `openai-codex` in 2026-09 and is
> maintained again.

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

- No separate Codex CLI install: the `openai-codex` dependency bundles the
  binary. Set `CODEX_BIN` only to override it with a specific build.
- Codex auth is owned by the Codex CLI/app-server (`CODEX_HOME`). Use your
  existing ChatGPT Codex login, an API key, or a custom model provider (see
  below) that needs no OpenAI auth at all.
- **One app-server process per gateway session.** Each session maps to one
  Codex thread; the gateway stores the thread id on the session and resumes it
  on continuation turns. Sessions are isolated — there is no shared process or
  cross-session head-of-line blocking anymore. The process is released when
  the session ends (TTL cleanup, stream failure, or cancellation).

## Serving self-hosted models through LiteLLM (or any Responses-API provider)

Codex only speaks the **OpenAI Responses API** to model providers
(`wire_api = "chat"` was removed upstream), so any provider you point it at
must serve `POST /v1/responses`. LiteLLM's proxy does, including translating
to chat-completions for backends that lack it — which makes the chain

```
client → gateway /v1/responses (codex backend) → codex app-server → LiteLLM /v1/responses → your model
```

work end-to-end with self-hosted models. Configure it purely through config
overrides (no code changes):

```bash
CODEX_MODELS=qwen3.6-27b
CODEX_CONFIG_OVERRIDES=model_providers.litellm.name=LiteLLM,model_providers.litellm.base_url=http://litellm-host:4000/v1,model_providers.litellm.wire_api=responses,model_providers.litellm.env_key=LITELLM_API_KEY,model_provider=litellm
LITELLM_API_KEY=sk-...   # whatever env var you named in env_key
```

## Configuration

| Variable | Purpose |
|----------|---------|
| `CODEX_BIN` | Optional override for the Codex binary. Default: the SDK-bundled CLI |
| `CODEX_HOME` | Codex state dir (auth, sessions). Default: `~/.codex` |
| `CODEX_MODELS` | Comma-separated model allowlist exposed as `codex/<model>`. Default: `gpt-5.5` |
| `CODEX_APPROVAL_POLICY` | `approvalPolicy` used when the request has no `permission_mode`. Default: `never` |
| `CODEX_SANDBOX` | Thread-level Codex sandbox mode (`read-only`, `workspace-write`, `danger-full-access`). Default: `danger-full-access` for local experimental use |
| `CODEX_CONFIG_OVERRIDES` | Comma-separated `codex --config key=value` overrides (model providers, MCP defaults, ...) |
| `CODEX_READ_IDLE_TIMEOUT_MS` | Per-event idle cap on a turn's stream. Default: 60000 |
| `CODEX_APPROVAL_TIMEOUT_MS` | How long an interactive approval may stay pending before it auto-cancels. Default: 600000 |
| `CODEX_MODEL_DISCOVERY_ENABLED` | Opt-in live `model/list` discovery for `/v1/models`. Default: false |
| `CODEX_MODEL_DISCOVERY_TTL_SECONDS` | Discovery cache TTL. Default: 300 |

## Docker Compose

```bash
cp .env.example .env
# set ADMIN_API_KEY and either OPENAI_API_KEY/CODEX_API_KEY, run codex login,
# or configure a model provider via CODEX_CONFIG_OVERRIDES
docker compose -f docker-compose.codex.yml up -d --build
```

`docker-compose.codex.yml` builds `Dockerfile.codex` (the CLI comes from the
`openai-codex` pip dependency, version-locked by `uv.lock`), forces
`BACKENDS=codex`, and stores Codex CLI state in the `codex_home` named volume
mounted at `/home/app/.codex`.

For ChatGPT/Codex CLI login instead of API-key auth:

```bash
docker compose -f docker-compose.codex.yml run --rm gateway \
  python -c "from openai_codex import Codex; c = Codex(); h = c.login_chatgpt(); print(h.auth_url); h.wait()"
docker compose -f docker-compose.codex.yml up -d
```

Use `CODEX_DEFAULT_GATEWAY_MODEL` to change the container's gateway default
model without reusing a Claude-oriented `DEFAULT_MODEL` from `.env`.

## Supported Behavior

- Text prompts and streaming text responses.
- Responses `input_image` parts are forwarded to Codex as native image input
  items when the request uses a Codex model.
- `reasoning.effort` is forwarded to Codex `turn/start` (`none`/`minimal`/
  `low`/`medium`/`high`/`xhigh`). Raw sampling knobs (`temperature`,
  `max_output_tokens`) have no Codex turn-API equivalent anymore and are
  dropped with a debug log instead of being rejected.
- Token usage (including reasoning tokens rolled into `output_tokens`) is
  reported per turn.
- Gateway `allowed_tools`, `disallowed_tools`, `permission_mode`, and the
  global `DISALLOWED_TOOLS` setting are enforced at Codex approval time
  through the SDK's approval handler (`bypassPermissions` maps to `never`;
  a tool policy upgrades it back to `on-request` so enforcement still runs;
  `acceptEdits` auto-accepts only `fileChange` approvals).
- Codex command, file-change, and permission approval requests are exposed as
  Responses `requires_action` entries with the existing `AskUserQuestion`
  function-call shape. Send a matching `function_call_output` with the
  previous response id to continue the paused Codex turn. Decision strings
  (`accept`, `acceptForSession`, `decline`, `cancel`) and short aliases
  (`yes`, `no`, `always`) are normalized by the gateway. A pending approval
  that is never answered cancels after `CODEX_APPROVAL_TIMEOUT_MS`.
- MCP server configuration is forwarded through the thread-scoped Codex config
  tree (`mcp_servers.<name>`; stdio `command/args/env` and streamable-HTTP
  `url` + `http_headers`), with the gateway's per-request MCP context headers
  injected like the Claude backend. MCP approvals honor both the
  `mcpToolCall` bucket and Claude-style `mcp__<server>__*` /
  `mcp__<server>__<tool>` policy names.
- Live model discovery (`/v1/models`) via the app-server `model/list`, opt-in
  and TTL-cached; failures fall back to the static `CODEX_MODELS` list.

## Current Limits

- Approval continuations must land on the same gateway process that surfaced
  the approval (the decision unblocks a waiting SDK reader thread); a restart
  in between cancels the pending approval.
- Structured output (`output_schema`) and turn steering exist in the SDK but
  are not yet wired to gateway request fields.
- `openai-codex` is pinned exactly (like `claude-agent-sdk`); upgrades are
  deliberate, gap-analyzed events. The SDK-internal seam the gateway uses to
  install its approval handler is covered by a canary test in
  `tests/test_codex_backend.py`.
