# Claude Code Backend

The Claude Code backend wraps Anthropic's [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) and exposes it through the gateway's `/v1/responses` endpoint. This is the **default** backend — sessions for `sonnet`, `opus`, or `haiku` model ids route here automatically.

## Quick Start

```bash
export ANTHROPIC_AUTH_TOKEN=sk-ant-...
uv run uvicorn src.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "input": "Hello"}'
```

Multi-turn:

```bash
# First turn
RESP=$(curl -s http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","input":"My name is Alice"}')
RESP_ID=$(echo "$RESP" | jq -r .id)

# Continue
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"sonnet\",\"input\":\"What is my name?\",\"previous_response_id\":\"$RESP_ID\"}"
```

## Authentication

| Method | Setup | When to use |
|--------|-------|-------------|
| **API key** | `export ANTHROPIC_AUTH_TOKEN=sk-ant-...` | Production, CI, headless |
| **CLI auth** | `claude auth login` then mount `~/.claude` | Local dev, sharing your subscription |

Override which is used (auto-detection by default):

```bash
CLAUDE_AUTH_METHOD=api_key   # or cli
```

For corporate proxies or alternate Anthropic endpoints:

```bash
ANTHROPIC_BASE_URL=https://your-proxy.example.com
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7
ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5-20251001
```

Verify:

```bash
curl http://localhost:8000/v1/auth/status
```

## Working Directory

Claude operates inside a working directory. Configure it once globally, or per-user.

### Single global workspace

```bash
CLAUDE_CWD=./working_dir
```

If unset, the gateway creates a fresh temp directory per session and cleans it up on expiry.

### Per-user workspace isolation

Each `/v1/responses` request can include a `user` field. The gateway routes that user's requests to a permanent directory under `USER_WORKSPACES_DIR`:

```bash
USER_WORKSPACES_DIR=/data/workspaces
```

```bash
curl http://localhost:8000/v1/responses \
  -d '{"model":"sonnet","input":"Create a Python script","user":"alice"}'
```

- `user` specified → workspace at `/data/workspaces/alice/`, survives restarts
- `user` omitted → temp workspace per session, cleaned on TTL expiry
- New sessions copy `.claude/` config from `CLAUDE_CWD` into the user workspace
- User identifiers must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$`

`USER_WORKSPACES_DIR` falls back to `CLAUDE_CWD` when unset; if both are unset, temp dirs are used.

## Thinking Mode

Claude can show internal reasoning ("thinking") before producing a final answer. Three modes:

| Mode | Behavior |
|------|----------|
| `adaptive` (default) | SDK decides per-request whether to allocate budget |
| `enabled` | Always thinking with fixed `THINKING_BUDGET_TOKENS` |
| `disabled` | No thinking blocks emitted |

```bash
THINKING_MODE=adaptive
THINKING_BUDGET_TOKENS=10000   # only used in 'enabled' mode
```

The gateway forwards thinking blocks via streaming events as a separate part type (see [streaming-events.md](../streaming-events.md)). Most clients render them as a collapsed "thought" UI.

## Bash Sandbox (OS-level isolation)

The Claude Agent SDK's `SandboxSettings` can isolate Bash tool execution using macOS Seatbelt or Linux bubblewrap.

```bash
CLAUDE_SANDBOX_ENABLED=true              # tri-state: unset / true / false
CLAUDE_SANDBOX_AUTO_ALLOW_BASH=true      # auto-approve bash when sandboxed
CLAUDE_SANDBOX_EXCLUDED_COMMANDS=        # comma-separated bypass list
CLAUDE_SANDBOX_ALLOW_UNSANDBOXED=false   # let model request unsandboxed via dangerouslyDisableSandbox
CLAUDE_SANDBOX_NETWORK_ALLOW_LOCAL=false # allow local-port binds inside sandbox
CLAUDE_SANDBOX_WEAKER_NESTED=false       # for unprivileged Linux containers
```

Tri-state semantics:

- **unset** — gateway does not configure sandbox; respects `.claude/settings.json`
- **`true`** — forces sandbox on with strict defaults
- **`false`** — forces sandbox off, overriding project settings

> Sandbox isolates Bash commands only. File tool access (Read/Edit/Write) is governed by SDK permission rules, not the sandbox. Linux/macOS only — not Windows.

## Custom System Prompt

By default the gateway uses the bundled `claude_code` preset. To override:

```bash
SYSTEM_PROMPT_FILE=docs/claude-code-system-prompt-reference.md
```

The file may contain `{{PLACEHOLDER}}` tokens; some are auto-resolved per request:

| Placeholder | Resolved from |
|-------------|---------------|
| `{{WORKING_DIRECTORY}}` | per-request workspace cwd |
| `{{MEMORY_PATH}}` | `<cwd>/.memory` (auto-created) |
| `{{PLATFORM}}` | `sys.platform` |
| `{{SHELL}}` | `$SHELL` |
| `{{OS_VERSION}}` | `platform.platform()` |
| `{{PROMPT_LANGUAGE}}` | `PROMPT_LANGUAGE` env var |

You can also manage prompts at runtime via the admin UI (`/admin`).

Three reference prompts ship with the repo:

- `docs/claude-code-system-prompt-reference.md` — the full Claude Code prompt
- `docs/compact-system-prompt.md` — leaner variant for less verbose models
- `docs/minimal-system-prompt.md` — bare-bones for narrow agent tasks

## Subagent Control

Claude exposes built-in subagent types (`general-purpose`, `Explore`, `Plan`, `statusline-setup`). To block specific ones per deployment:

```bash
DISALLOWED_SUBAGENT_TYPES=statusline-setup,general-purpose
```

When the model invokes a blocked subagent, the SDK returns an error to the model and the parent agent continues.

Streaming visibility for subagent output:

```bash
SUBAGENT_STREAM_TEXT=false           # default: only final summary, not interim deltas
SUBAGENT_STREAM_TOOL_BLOCKS=true     # default: forward tool_use/tool_result blocks
SUBAGENT_STREAM_PROGRESS=true        # default: forward task_started/progress events
```

## MCP Server Integration

The gateway forwards configured MCP servers to Claude. Configure once with the gateway's shared `MCP_CONFIG`:

```bash
MCP_CONFIG=/path/to/mcp-config.json
# or inline JSON
MCP_CONFIG='{"mcpServers":{"fs":{"type":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/workspace"]}}}'
```

Supported transports: `stdio`, `sse`, `http`, `streamable-http`. The same config can also be forwarded to OpenCode via `OPENCODE_USE_WRAPPER_MCP_CONFIG=true` — see [opencode/managed.md](../opencode/managed.md#mcp-servers).

Verify what the gateway loaded:

```bash
curl http://localhost:8000/v1/mcp/servers
```

## Task Budget

Cap the total tokens a single agent run can spend on tool use:

```bash
TASK_BUDGET=100000
```

Per-request `task_budget` in the API body overrides the global default. The model paces tool use and wraps up before exceeding the limit.

## Sessions and Multi-turn

The gateway tracks sessions internally so `previous_response_id` chains work across multiple turns:

```bash
SESSION_MAX_AGE_MINUTES=60
SESSION_CLEANUP_INTERVAL_MINUTES=5
```

Quirks worth knowing:

- `instructions` + `previous_response_id` together → **400** (system prompt cannot change mid-session)
- Stale `previous_response_id` → **409** with the latest valid id in the body, e.g.:
  ```
  Stale previous_response_id: only the latest response (resp_<uuid>_<turn>) can be continued
  ```
  Clients should retry with that id.
- Mixing backends (Claude → OpenCode) within one session → **400**

## Streaming

Set `"stream": true` for SSE delivery:

```bash
curl -N http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","input":"Hello","stream":true}'
```

For full event reference (tool calls, subagent events, thinking blocks, etc.) see [streaming-events.md](../streaming-events.md).

## Examples

End-to-end examples live in `examples/`:

- `examples/curl_example.sh` — basic + streaming + chaining
- `examples/openai_sdk.py` — using the OpenAI Python SDK against the gateway
- `examples/streaming.py` — streaming SSE in Python
- `examples/session_continuity.py` — multi-turn with `previous_response_id`

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_AUTH_TOKEN` | — | API key auth |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Override Anthropic endpoint |
| `CLAUDE_AUTH_METHOD` | auto | `cli` or `api_key` |
| `DEFAULT_MODEL` | `sonnet` | Used when request omits `model` |
| `CLAUDE_CWD` | temp dir | Global working directory |
| `USER_WORKSPACES_DIR` | `CLAUDE_CWD` | Per-user workspace base |
| `THINKING_MODE` | `adaptive` | `adaptive` / `enabled` / `disabled` |
| `THINKING_BUDGET_TOKENS` | `10000` | Budget for `enabled` mode |
| `TASK_BUDGET` | unset | Global tool-use token budget |
| `DEFAULT_MAX_TURNS` | `10` | Max agent turns per request |
| `TOKEN_STREAMING` | `true` | Per-token vs per-message streaming |
| `MAX_TIMEOUT` | `600000` | Request timeout (ms) |
| `CLAUDE_SANDBOX_ENABLED` | unset | Tri-state Bash sandbox |
| `SYSTEM_PROMPT_FILE` | unset | Custom system prompt path |
| `DISALLOWED_SUBAGENT_TYPES` | `statusline-setup` | Blocked subagent types |
| `MCP_CONFIG` | — | MCP server config (shared with OpenCode optionally) |

See `.env.example` for the full list.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `401` from Anthropic | `ANTHROPIC_AUTH_TOKEN` missing or invalid; check `/v1/auth/status` |
| `409 Stale previous_response_id` | Concurrent writer advanced the session; retry with the id from the error body |
| Sandbox errors on Linux | bubblewrap not installed, or unprivileged container without `CLAUDE_SANDBOX_WEAKER_NESTED=true` |
| MCP tools missing | check `/v1/mcp/servers` and gateway logs for `[mcp]` load errors |
| Model produces empty answer | look for blocked subagent or task budget exhaustion in admin logs |

Useful checks:

- `GET /admin/api/backends` — backend health, auth method, model availability
- `GET /admin/api/mcp-servers` — MCP server load status per backend
- `GET /admin/api/logs` — request log with filtering
- `GET /admin/api/config` — redacted runtime env (verify variables made it in)
