---
name: ui-state
description: >
  Read the current frontend UI state and send commands to modify it via the
  orchestrator's REST endpoints. Use when your agent needs context-aware behavior
  (e.g. what view, row, or filters the user has active) or wants to drive UI
  navigation programmatically.
compatibility: Designed for the A2A multi-agent platform orchestrator (default http://localhost:9000).
metadata:
  author: a2a-agent
  version: "1.0"
---

# UI State — Read & Command

This skill teaches you how to read the current frontend UI state and send commands to change it. Both endpoints live on the **orchestrator** (default `http://localhost:9000`) and do not require authentication.

**`THREAD_ID` is pre-set as an environment variable.** Use `${THREAD_ID}` directly in commands — do NOT hardcode or ask for the thread ID value.

## How It Works

```
Frontend                         Orchestrator                    Your Agent
────────                         ────────────                    ──────────
useCopilotReadable()
  → sends UI state on every      caches ui_state        ──►    GET /api/ui-state
    user message (AG-UI context)  per thread_id                 (read current state)

useUICommands()            ◄──   StateSnapshotEvent     ◄──    POST /api/ui-commands
  (executes the action)           drains command queue          (queue a command)
```

**Important**: UI state is updated only when the user sends a chat message. If the user changes the view but doesn't send a message, the cached state is stale.

## Read UI State

```bash
curl -s "http://localhost:9000/api/ui-state?thread_id=${THREAD_ID}" | python3 -m json.tool
```

### Response

```json
{
  "thread_id": "abc-123",
  "ui_state": {
    "_key": "ui_state",
    "_version": 1,
    "view": "detail",
    "selectedRow": {
      "lot_id": "LOT-2024-001",
      "product": "Widget-A"
    },
    "filters": [
      { "type": "column", "column": "product", "value": "Widget-A" }
    ],
    "keyword": null,
    "selectedDate": "2024-12-01"
  }
}
```

### State Fields

| Field | Type | Description |
|-------|------|-------------|
| `view` | `"dashboard"` \| `"detail"` | Current view |
| `selectedRow` | `object \| null` | Selected row's join columns (when in detail view) |
| `filters` | `array` | Active column filters |
| `keyword` | `string \| null` | Active search keyword |
| `selectedDate` | `string \| null` | Selected date filter |

`ui_state` is `null` if the user hasn't sent any message yet in the session.

## Send UI Command

```bash
curl -s -X POST "http://localhost:9000/api/ui-commands?thread_id=${THREAD_ID}" \
  -H "Content-Type: application/json" \
  -d '{"action": "<action_name>", "params": {}}'
```

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `action` | Yes | string | Command action (see table below) |
| `params` | No | object | Action-specific parameters (default `{}`) |
| `request_id` | No | string | Optional correlation ID |

### Response

```json
{
  "status": "queued",
  "command_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

### Available Actions

| Action | Params | Description |
|--------|--------|-------------|
| `go_back` | — | Navigate from detail view back to dashboard |
| `clear_filters` | — | Remove all active column filters |
| `set_keyword` | `{ "keyword": "term" }` | Set search keyword filter |

### Delivery

Commands are **not delivered immediately**. They are queued and delivered to the frontend via the next AG-UI `StateSnapshotEvent` (during an active agent response stream). If no stream is active, commands wait until the next user message.

## Examples

### Check what user is viewing, then navigate back

```bash
# 1. Read current state
curl -s "http://localhost:9000/api/ui-state?thread_id=${THREAD_ID}" | python3 -m json.tool

# 2. Send go_back command
curl -s -X POST "http://localhost:9000/api/ui-commands?thread_id=${THREAD_ID}" \
  -H "Content-Type: application/json" \
  -d '{"action": "go_back"}'

# 3. Filter by keyword
curl -s -X POST "http://localhost:9000/api/ui-commands?thread_id=${THREAD_ID}" \
  -H "Content-Type: application/json" \
  -d '{"action": "set_keyword", "params": {"keyword": "Widget-A"}}'
```
