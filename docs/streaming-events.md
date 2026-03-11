# Streaming Events Reference

This document describes all SSE (Server-Sent Events) emitted by the gateway during streaming responses. It is intended for UI developers who want to render tool calls, subagent activity, and other structured events.

## Table of Contents

- [Event Delivery by Endpoint](#event-delivery-by-endpoint)
- [Chat Completions Events (`/v1/chat/completions`)](#chat-completions-events)
  - [Text Delta](#cc-text-delta)
  - [system_event: tool_use](#cc-tool-use)
  - [system_event: tool_result](#cc-tool-result)
  - [system_event: task_started](#cc-task-started)
  - [system_event: task_progress](#cc-task-progress)
  - [system_event: task_notification](#cc-task-notification)
  - [Final Chunk (finish_reason + usage)](#cc-final)
- [Responses API Events (`/v1/responses`)](#responses-api-events)
  - [Lifecycle Events](#ra-lifecycle)
  - [response.output_text.delta](#ra-text-delta)
  - [response.tool_use](#ra-tool-use)
  - [response.tool_result](#ra-tool-result)
  - [response.task_started](#ra-task-started)
  - [response.task_progress](#ra-task-progress)
  - [response.task_notification](#ra-task-notification)
  - [Closing Events](#ra-closing)
- [Tool Names](#tool-names)
  - [Claude Backend Tools](#claude-tools)
  - [Codex Backend Tools](#codex-tools)
  - [MCP Tools](#mcp-tools)
- [Tool Input Schemas](#tool-input-schemas)
- [Subagent (Nested) Tool Calls](#subagent-tool-calls)
- [Non-Streaming Responses](#non-streaming-responses)
- [Usage & Stop Reason](#usage-and-stop-reason)

---

## Event Delivery by Endpoint

| Feature | `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
|---------|----------------------|----------------|----------------|
| Text streaming | `delta.content` in chunk | `response.output_text.delta` event | Not streamed |
| Tool calls | `system_event` field (custom) | Separate SSE event types | N/A (text only) |
| Subagent events | `system_event` field | Separate SSE event types | N/A |
| Usage reporting | Final chunk with `usage` | `response.completed` event | Response body |

---

## Chat Completions Events

Wire format: `data: <json>\n\n`

All chunks share this base shape:

```jsonc
{
  "id": "chatcmpl-a1b2c3d4",
  "object": "chat.completion.chunk",
  "created": 1741654800,
  "model": "sonnet",
  "choices": [{ "index": 0, "delta": { ... }, "finish_reason": null }]
  // optional: "system_event": { ... }
  // optional: "usage": { ... }
}
```

<a id="cc-text-delta"></a>
### Text Delta

Standard OpenAI-compatible text streaming.

```jsonc
// First chunk (role announcement)
{ "choices": [{ "delta": { "role": "assistant", "content": "" }, "finish_reason": null }] }

// Subsequent chunks
{ "choices": [{ "delta": { "content": "Hello, " }, "finish_reason": null }] }
{ "choices": [{ "delta": { "content": "world!" }, "finish_reason": null }] }
```

<a id="cc-tool-use"></a>
### system_event: tool_use

Emitted when the agent invokes a tool. The `delta` is empty `{}`.

```jsonc
{
  "choices": [{ "delta": {}, "finish_reason": null }],
  "system_event": {
    "type": "tool_use",
    "id": "toolu_01ABC123",          // unique tool invocation ID
    "name": "Read",                  // tool name (see Tool Names section)
    "input": {                       // tool-specific parameters
      "file_path": "/home/user/project/src/main.py"
    }
    // optional:
    // "parent_tool_use_id": "toolu_01XYZ789"  // present if called by a subagent
  }
}
```

<a id="cc-tool-result"></a>
### system_event: tool_result

Emitted when a tool execution completes. The `delta` is empty `{}`.

```jsonc
{
  "choices": [{ "delta": {}, "finish_reason": null }],
  "system_event": {
    "type": "tool_result",
    "tool_use_id": "toolu_01ABC123", // references the tool_use.id
    "content": "file contents here...",
    "is_error": false
    // optional:
    // "parent_tool_use_id": "toolu_01XYZ789"  // present if result from a subagent
  }
}
```

<a id="cc-task-started"></a>
### system_event: task_started

Emitted when a subagent task begins.

```jsonc
{
  "choices": [{ "delta": {}, "finish_reason": null }],
  "system_event": {
    "type": "task_started",
    "task_id": "task_abc123",
    "description": "Research API patterns",
    "session_id": "sess_xyz"
  }
}
```

<a id="cc-task-progress"></a>
### system_event: task_progress

Periodic progress updates from a running subagent.

```jsonc
{
  "choices": [{ "delta": {}, "finish_reason": null }],
  "system_event": {
    "type": "task_progress",
    "task_id": "task_abc123",
    "description": "Reading source files...",
    "last_tool_name": "Read",        // last tool the subagent used (nullable)
    "usage": { ... }                 // token usage so far (nullable)
  }
}
```

<a id="cc-task-notification"></a>
### system_event: task_notification

Emitted when a subagent task completes or fails.

```jsonc
{
  "choices": [{ "delta": {}, "finish_reason": null }],
  "system_event": {
    "type": "task_notification",
    "task_id": "task_abc123",
    "status": "completed",           // "completed" or "failed"
    "summary": "Found 3 relevant patterns in the codebase",
    "usage": { ... }                 // final token usage (nullable)
  }
}
```

<a id="cc-final"></a>
### Final Chunk

```jsonc
{
  "choices": [{ "delta": {}, "finish_reason": "stop" }],
  // present when request had stream_options.include_usage = true:
  "usage": {
    "prompt_tokens": 150,
    "completion_tokens": 42,
    "total_tokens": 192
  }
}
```

Terminated by `data: [DONE]\n\n`.

---

## Responses API Events

Wire format: `event: <type>\ndata: <json>\n\n`

Each event has a monotonically increasing `sequence_number` for ordering.

<a id="ra-lifecycle"></a>
### Lifecycle Events

These events frame the response. Emitted in this order:

```
event: response.created
event: response.in_progress
event: response.output_item.added
event: response.content_part.added
  ... content / tool events ...
event: response.output_text.done
event: response.content_part.done
event: response.output_item.done
event: response.completed          (or response.failed)
```

<a id="ra-text-delta"></a>
### response.output_text.delta

```jsonc
{
  "type": "response.output_text.delta",
  "item_id": "item_abc123",
  "output_index": 0,
  "content_index": 0,
  "delta": "Hello, world!",
  "logprobs": [],
  "sequence_number": 5
}
```

<a id="ra-tool-use"></a>
### response.tool_use

```jsonc
{
  "type": "response.tool_use",
  "tool_use_id": "toolu_01ABC123",
  "name": "Bash",
  "input": {
    "command": "ls -la"
  },
  "sequence_number": 6
  // optional:
  // "parent_tool_use_id": "toolu_01XYZ789"
}
```

<a id="ra-tool-result"></a>
### response.tool_result

```jsonc
{
  "type": "response.tool_result",
  "tool_use_id": "toolu_01ABC123",
  "content": "total 42\ndrwxr-xr-x ...",
  "is_error": false,
  "sequence_number": 7
  // optional:
  // "parent_tool_use_id": "toolu_01XYZ789"
}
```

<a id="ra-task-started"></a>
### response.task_started

```jsonc
{
  "type": "response.task_started",
  "task_id": "task_abc123",
  "description": "Research API patterns",
  "session_id": "sess_xyz",
  "sequence_number": 8
}
```

<a id="ra-task-progress"></a>
### response.task_progress

```jsonc
{
  "type": "response.task_progress",
  "task_id": "task_abc123",
  "description": "Reading source files...",
  "last_tool_name": "Read",
  "usage": { ... },
  "sequence_number": 9
}
```

<a id="ra-task-notification"></a>
### response.task_notification

```jsonc
{
  "type": "response.task_notification",
  "task_id": "task_abc123",
  "status": "completed",
  "summary": "Found 3 relevant patterns",
  "usage": { ... },
  "sequence_number": 10
}
```

<a id="ra-closing"></a>
### Closing Events

On success: `response.output_text.done` → `response.content_part.done` → `response.output_item.done` → `response.completed`

On failure: `response.failed`

```jsonc
// response.completed
{
  "type": "response.completed",
  "response": {
    "id": "resp_session1_3",
    "object": "response",
    "status": "completed",
    "model": "sonnet",
    "output": [{ "id": "item_abc", "type": "message", "status": "completed", "content": [...] }],
    "usage": { "input_tokens": 150, "output_tokens": 42 }
  },
  "sequence_number": 15
}

// response.failed
{
  "type": "response.failed",
  "response": {
    "id": "resp_session1_3",
    "status": "failed",
    "error": { "code": "sdk_error", "message": "Authentication failed" }
  },
  "sequence_number": 8
}
```

---

## Tool Names

Tool names come directly from the Claude Agent SDK or Codex backend. The `name` field in `tool_use` events will be one of the following.

### Claude Backend Tools

These are the built-in tools available to the Claude agent:

| Name | Description | Key Input Fields |
|------|-------------|------------------|
| `Read` | Read a file from disk | `file_path`, `offset?`, `limit?` |
| `Write` | Create or overwrite a file | `file_path`, `content` |
| `Edit` | Apply targeted string replacements | `file_path`, `old_string`, `new_string` |
| `Bash` | Execute a shell command | `command`, `timeout?` |
| `Glob` | Find files by glob pattern | `pattern`, `path?` |
| `Grep` | Search file contents with regex | `pattern`, `path?`, `glob?` |
| `Agent` | Spawn a subagent for a subtask | `prompt`, `description?` |
| `WebFetch` | Fetch a URL | `url` |
| `WebSearch` | Search the web | `query` |
| `NotebookEdit` | Edit Jupyter notebook cells | `notebook_path`, `cell_number`, `new_source` |
| `TodoRead` | Read the task list | _(no params)_ |
| `TodoWrite` | Update the task list | `todos` |

### Codex Backend Tools

When using the Codex backend, tool names follow the same conventions. Codex may also produce:

| Name | Description | Key Input Fields |
|------|-------------|------------------|
| `Agent` | Codex subagent (with `codex_agent_*` ID prefix) | `prompt` |

### MCP Tools

MCP (Model Context Protocol) server tools use a namespaced format:

| Pattern | Example |
|---------|---------|
| `mcp__<server>__<tool>` | `mcp__github__create_issue` |
| `mcp__<server>__<tool>` | `mcp__docs__search` |

The available MCP tools depend on what servers are configured via `MCP_CONFIG`. Check `GET /v1/mcp/servers` for loaded servers.

---

## Tool Input Schemas

### Read

```jsonc
{
  "file_path": "/absolute/path/to/file.py",
  "offset": 10,    // optional: start line
  "limit": 50      // optional: number of lines
}
```

### Write

```jsonc
{
  "file_path": "/absolute/path/to/file.py",
  "content": "file content here..."
}
```

### Edit

```jsonc
{
  "file_path": "/absolute/path/to/file.py",
  "old_string": "text to find",
  "new_string": "replacement text",
  "replace_all": false  // optional
}
```

### Bash

```jsonc
{
  "command": "npm test",
  "timeout": 30000  // optional: ms
}
```

### Glob

```jsonc
{
  "pattern": "**/*.ts",
  "path": "/project/src"  // optional: search root
}
```

### Grep

```jsonc
{
  "pattern": "function\\s+\\w+",
  "path": "/project/src",     // optional
  "glob": "*.py"              // optional: file filter
}
```

### Agent

```jsonc
{
  "prompt": "Find all API endpoints in the codebase and list them",
  "description": "API endpoint discovery"  // optional
}
```

---

## Subagent (Nested) Tool Calls

When the agent spawns a subagent (via the `Agent` tool), subsequent tool calls and results from that subagent include a `parent_tool_use_id` field that references the original `Agent` tool_use ID. This enables UI nesting.

**Event flow for a subagent call:**

```
1. tool_use   { name: "Agent", id: "toolu_parent" }
   └─ 2. task_started   { task_id: "task_1" }
   └─ 3. task_progress  { task_id: "task_1", last_tool_name: "Read" }
   └─ 4. task_notification { task_id: "task_1", status: "completed" }
5. tool_result { tool_use_id: "toolu_parent", content: "subagent output..." }
```

Tool calls made *by* the subagent will have `parent_tool_use_id: "toolu_parent"`. Use this to render nested tool activity under the parent Agent call.

---

## Non-Streaming Responses

### `/v1/chat/completions` (stream=false)

Tool calls are **not** returned as structured objects. They are flattened into the text content. The response is a standard OpenAI `chat.completion` object:

```jsonc
{
  "id": "chatcmpl-a1b2c3d4",
  "object": "chat.completion",
  "model": "sonnet",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "The answer is 42." },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60 }
}
```

### `/v1/responses` (stream=false)

```jsonc
{
  "id": "resp_session1_3",
  "object": "response",
  "status": "completed",
  "model": "sonnet",
  "output": [{
    "id": "item_abc",
    "type": "message",
    "role": "assistant",
    "status": "completed",
    "content": [{ "type": "output_text", "text": "The answer is 42.", "annotations": [] }]
  }],
  "usage": { "input_tokens": 50, "output_tokens": 10 }
}
```

### `/v1/messages` (non-streaming only)

```jsonc
{
  "id": "msg_abc123def456",
  "type": "message",
  "role": "assistant",
  "content": [{ "type": "text", "text": "The answer is 42." }],
  "model": "sonnet",
  "stop_reason": "end_turn",
  "usage": { "input_tokens": 50, "output_tokens": 10 }
}
```

---

## Usage & Stop Reason

### Token Usage

Usage values prefer real SDK-reported tokens. If unavailable, they are estimated (~4 characters per token).

**Chat Completions format:**
```jsonc
{ "prompt_tokens": 150, "completion_tokens": 42, "total_tokens": 192 }
```

**Responses API / Messages format:**
```jsonc
{ "input_tokens": 150, "output_tokens": 42 }
```

### finish_reason / stop_reason Mapping

| Agent SDK Value | Chat Completions `finish_reason` | Messages `stop_reason` |
|-----------------|--------------------------------|----------------------|
| `"max_tokens"` | `"length"` | `"max_tokens"` |
| `"tool_use"` | `"tool_calls"` | _(not exposed)_ |
| `null` / other | `"stop"` | `"end_turn"` |

---

## UI Implementation Tips

1. **Detect tool events**: In Chat Completions, check for `system_event` field on every chunk. In Responses API, dispatch on the SSE event type.
2. **Match tool_use → tool_result**: Use `tool_use.id` and `tool_result.tool_use_id` to pair invocations with their results.
3. **Render nesting**: Use `parent_tool_use_id` to show subagent tool calls as children of the parent `Agent` call.
4. **Show progress**: `task_started` → `task_progress` → `task_notification` events let you show a subagent activity indicator with live tool names.
5. **Handle errors**: `tool_result.is_error = true` indicates the tool failed. Display the `content` as an error message.
6. **Thinking blocks**: Claude's thinking/reasoning is wrapped in `<think>...</think>` tags within text deltas (Chat Completions only; suppressed in Responses API).
