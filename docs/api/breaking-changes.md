# Breaking Changes for API Consumers

## 2026-07-27 — `/v1/responses` usage gains a token breakdown (additive)

`response.usage` now carries two more keys. Existing readers of
`input_tokens` / `output_tokens` are unaffected — no field changed meaning or
was removed.

```jsonc
"usage": {
  "input_tokens": 600,
  "output_tokens": 50,
  "total_tokens": 650,              // new: always input + output
  "input_tokens_details": {          // new
    "cached_tokens": 300,            // OpenAI-standard; SDK cache_read_input_tokens
    "cache_creation_tokens": 200     // gateway extension, no OpenAI equivalent
  }
}
```

- `cached_tokens` follows OpenAI semantics: it is a **subset** of
  `input_tokens`, not an addition to it.
- `cache_creation_tokens` is separated out because cache writes bill at a
  premium; clients that only know the OpenAI shape can ignore it.
- Turns with no SDK usage (the character-estimation fallback) report zeros
  here rather than guessing.

**`output_tokens_details.reasoning_tokens` is deliberately absent.** Claude's
usage payload folds thinking tokens into `output_tokens` and never reports
them separately, so the gateway has no honest value to emit. A hard-coded `0`
would misreport billed thinking tokens as zero, which is worse for cost
tracking than the field being missing. Clients should not infer that a
response used zero thinking tokens.

Scope: `/v1/responses` only. `/v1/agents/messages` and the `/v1/messages`
sanitizer keep their existing usage shapes.

## 2026-05-16 — Claude Agent SDK 0.2.82 upgrade

### Task tools are now available (opt-in)

claude-agent-sdk 0.2.82 ships a new task-tracking tool family (`TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList`) alongside the legacy `TodoWrite`. The bundled Claude CLI gates these behind the `CLAUDE_CODE_ENABLE_TASKS` env var:

- **Env unset (default)** — `TodoWrite` remains the only task-tracking tool emitted. No `response.tool_use` payload changes for existing clients.
- **`CLAUDE_CODE_ENABLE_TASKS=1`** — the SDK emits `TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList` instead. The gateway does not set this env automatically; operators choose per deployment.

Schemas observed in this SDK version (from CLI tool definitions and a smoke-test run):

| Tool | Input fields | id source |
|---|---|---|
| `TaskCreate` | `subject`, `description`, `activeForm?` (status is auto-`pending`) | returned in the matching `tool_result.content` as the created task record (for example, `{ "task": { "id": "...", "subject": "..." } }`), not in the `input` |
| `TaskUpdate` | `taskId`, plus any of `status`, `subject`, `description`, `activeForm`, `owner`, `addBlocks`, `addBlockedBy`, `metadata` | n/a (caller supplies `taskId`) |
| `TaskGet` | `taskId` | n/a |
| `TaskList` | (no required input) | n/a |

`Task*` events are per-id deltas (the CLI maintains task state on disk); `TodoWrite` events are full snapshots. Clients that want to render Task* should accumulate by `taskId`. Clients that only handle `TodoWrite` keep working as long as `CLAUDE_CODE_ENABLE_TASKS` stays unset.

### MCP server `init` may include pending servers

Sessions now start before MCP servers finish connecting. The `init` system message may list servers with `status: "pending"`. Clients that surface MCP server state should reflect this transitional state rather than treating non-`"ready"` as an error.

To force the previous behavior (block on MCP connect), set `MCP_CONNECTION_NONBLOCKING=0` in the gateway environment, or mark individual servers `alwaysLoad: true` in your MCP config.

### New block types may appear in assistant content

The SDK now emits `server_tool_use` and `advisor_tool_result` blocks (previously silently dropped). Streaming responses expose these as `response.server_tool_use` and `response.advisor_tool_result` SSE events with the original content block under `block`. Clients that exhaustively switch on `block.type` should add cases for these (or use a default fallback).

### `api_error_status` field available on error results

The underlying `ResultMessage` exposes an `api_error_status: int | None` field surfacing the HTTP status (429, 500, 529) when the API call failed. The gateway does not yet propagate this to its downstream payload, but a follow-up may do so; clients planning to distinguish rate-limit from server errors should request it.

---

## Why the gateway propagates these changes

This gateway is intentionally a thin pass-through over `claude-agent-sdk`. We do not insert a compatibility shim because:

- Shims hide useful new fields (e.g., `pending` MCP status).
- Each shim adds maintenance cost that scales with upstream changes.
- Downstream consumers tend to want the latest SDK semantics.

If you need a compatibility layer in your own client, build it client-side.
