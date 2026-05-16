# Breaking Changes for API Consumers

## 2026-05-16 — Claude Agent SDK 0.2.82 upgrade

### Tool name changes in `response.tool_use` events

The Claude backend now emits the following tool names in `response.tool_use` SSE events instead of `TodoWrite`:

- `TaskCreate` — create a task (input: `{title, description, status}`)
- `TaskUpdate` — update task status (input: `{task_id, status, ...}`)
- `TaskGet` — fetch a single task by id
- `TaskList` — list all tasks

**Required client changes:**

1. **Recognize the new tool names.** Clients that previously branched on `tool_use.name == "TodoWrite"` must also handle `TaskCreate` / `TaskUpdate` / `TaskGet` / `TaskList`.
2. **Switch from snapshot-replace to per-id accumulation.** `TodoWrite` emitted a full snapshot of all todos on every call; the new events are deltas keyed by task id. Maintain a `Map<task_id, task>` and apply each event:
   - `TaskCreate`: insert
   - `TaskUpdate`: update by id
   - (others: as semantically appropriate)
3. **`TodoWrite` may still appear from older sessions or back-compat paths.** Treat it as a snapshot for legacy data.

### MCP server `init` may include pending servers

Sessions now start before MCP servers finish connecting. The `init` system message may list servers with `status: "pending"`. Clients that surface MCP server state should reflect this transitional state rather than treating non-`"ready"` as an error.

To force the previous behavior (block on MCP connect), set `MCP_CONNECTION_NONBLOCKING=0` in the gateway environment, or mark individual servers `alwaysLoad: true` in your MCP config.

### New block types may appear in assistant content

The SDK now emits `server_tool_use` and `advisor_tool_result` blocks (previously silently dropped). These pass through to clients unchanged. Clients that exhaustively switch on block `type` should add cases for these (or use a default fallback).

### `api_error_status` field available on error results

The underlying `ResultMessage` exposes an `api_error_status: int | None` field surfacing the HTTP status (429, 500, 529) when the API call failed. The gateway does not yet propagate this to its downstream payload, but a follow-up may do so; clients planning to distinguish rate-limit from server errors should request it.

---

## Why the gateway propagates these changes

This gateway is intentionally a thin pass-through over `claude-agent-sdk`. We do not insert a compatibility shim because:

- Shims hide useful new fields (e.g., `pending` MCP status).
- Each shim adds maintenance cost that scales with upstream changes.
- Downstream consumers tend to want the latest SDK semantics.

If you need a compatibility layer in your own client, build it client-side.
