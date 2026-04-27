# Streaming Events Reference

This document describes the SSE events emitted by `POST /v1/responses` when
`"stream": true` is set. It is intended for UI clients that need to render text,
tool calls, subagent progress, AskUserQuestion pauses, and terminal failures.

## Wire Format

Each event uses standard Server-Sent Events framing:

```text
event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"Hello","sequence_number":5}
```

The `sequence_number` field is monotonically increasing within a response stream
for events produced by the main streaming loop. AskUserQuestion function-call
events are emitted by the route after the SDK hook pauses and may omit it.

Keepalive comments may appear during long tool execution:

```text
: keepalive
```

SSE clients should ignore comment lines.

## Event Order

Successful text responses are framed like this:

```text
response.created
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta      # zero or more
response.tool_use               # zero or more
response.tool_result            # zero or more
response.task_started           # zero or more
response.task_progress          # zero or more
response.task_notification      # zero or more
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

Failures emit `response.failed`. Empty SDK output is also surfaced as
`response.failed` so clients receive a definite terminal event.

## Lifecycle Events

`response.created` and `response.in_progress` include a `response` object with
`status: "in_progress"`.

```json
{
  "type": "response.created",
  "response": {
    "id": "resp_00000000-0000-0000-0000-000000000000_1",
    "object": "response",
    "status": "in_progress",
    "model": "sonnet",
    "output": [],
    "usage": { "input_tokens": 0, "output_tokens": 0 },
    "metadata": {}
  },
  "sequence_number": 0
}
```

`response.output_item.added` and `response.content_part.added` open the assistant
message and its first text part.

## Text Events

`response.output_text.delta` carries visible text increments:

```json
{
  "type": "response.output_text.delta",
  "item_id": "msg_abc123",
  "output_index": 0,
  "content_index": 0,
  "delta": "Hello, world!",
  "logprobs": [],
  "sequence_number": 5
}
```

Final text is repeated in `response.output_text.done`,
`response.content_part.done`, and `response.output_item.done` so clients can
reconcile their buffered content.

## Tool Events

`response.tool_use` is emitted when Claude invokes a tool:

```json
{
  "type": "response.tool_use",
  "tool_use_id": "toolu_01ABC123",
  "name": "Bash",
  "input": { "command": "ls -la" },
  "sequence_number": 6
}
```

`response.tool_result` is emitted when a tool result returns:

```json
{
  "type": "response.tool_result",
  "tool_use_id": "toolu_01ABC123",
  "content": "total 42\n-rw-r--r-- ...",
  "is_error": false,
  "sequence_number": 7
}
```

If the tool call/result comes from a subagent, the event includes
`parent_tool_use_id`.

## Subagent Events

Subagent task system messages are forwarded as structured progress events:

```json
{
  "type": "response.task_started",
  "task_id": "task_abc123",
  "description": "Research API patterns",
  "session_id": "sdk-session-id",
  "sequence_number": 8
}
```

```json
{
  "type": "response.task_progress",
  "task_id": "task_abc123",
  "description": "Reading source files...",
  "last_tool_name": "Read",
  "usage": null,
  "sequence_number": 9
}
```

```json
{
  "type": "response.task_notification",
  "task_id": "task_abc123",
  "status": "completed",
  "summary": "Found relevant patterns",
  "usage": null,
  "sequence_number": 10
}
```

Subagent visibility is controlled by:

| Env var | Default | Effect |
|---------|---------|--------|
| `SUBAGENT_STREAM_TEXT` | `false` | Forward subagent text deltas |
| `SUBAGENT_STREAM_TOOL_BLOCKS` | `true` | Forward subagent tool events |
| `SUBAGENT_STREAM_PROGRESS` | `true` | Forward subagent task progress |

## AskUserQuestion Pauses

When the Claude SDK hook intercepts `AskUserQuestion`, the stream ends with a
function-call output item and a `response.completed` event whose response status
is `requires_action`.

```json
{
  "type": "response.output_item.added",
  "response_id": "resp_00000000-0000-0000-0000-000000000000_1",
  "item": {
    "type": "function_call",
    "id": "fc_toolu_question",
    "call_id": "toolu_question",
    "name": "AskUserQuestion",
    "arguments": "{\"question\":\"Continue?\"}",
    "status": "completed"
  }
}
```

The client continues by sending a new `POST /v1/responses` request with the
latest response id and a `function_call_output` input item:

```json
{
  "model": "sonnet",
  "previous_response_id": "resp_00000000-0000-0000-0000-000000000000_1",
  "input": [
    {
      "type": "function_call_output",
      "call_id": "toolu_question",
      "output": "Yes, continue."
    }
  ]
}
```

## Terminal Events

`response.completed` contains the final `response` object and token usage:

```json
{
  "type": "response.completed",
  "response": {
    "id": "resp_00000000-0000-0000-0000-000000000000_1",
    "object": "response",
    "status": "completed",
    "model": "sonnet",
    "output": [
      {
        "id": "msg_abc123",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
          { "type": "output_text", "text": "The answer is 42.", "annotations": [] }
        ]
      }
    ],
    "usage": { "input_tokens": 50, "output_tokens": 10 },
    "metadata": {}
  },
  "sequence_number": 15
}
```

`response.failed` contains a compact error detail:

```json
{
  "type": "response.failed",
  "response": {
    "id": "resp_00000000-0000-0000-0000-000000000000_1",
    "object": "response",
    "status": "failed",
    "model": "sonnet",
    "output": [],
    "usage": { "input_tokens": 0, "output_tokens": 0 },
    "metadata": {},
    "error": { "code": "sdk_error", "message": "Authentication failed" }
  },
  "sequence_number": 8
}
```

## Tool Names

Tool names come from the Claude Agent SDK. Common built-in tools include:

| Name | Description |
|------|-------------|
| `Read` | Read files |
| `Write` | Create or overwrite files |
| `Edit` | Apply targeted string replacements |
| `Bash` | Execute shell commands |
| `Glob` | Find files by glob pattern |
| `Grep` | Search file contents |
| `Task` | Launch a subagent |
| `WebFetch` | Fetch web content |
| `WebSearch` | Search the web |
| `NotebookEdit` | Edit notebook cells |
| `TodoWrite` | Update task lists |

MCP tool names use `mcp__<server>__<tool>`. The available MCP servers are exposed
through `GET /v1/mcp/servers`.

## Client Tips

1. Dispatch on the SSE `event:` value or the JSON `type`.
2. Pair tool results by `tool_use_id`.
3. Use `parent_tool_use_id` to render nested subagent tool activity.
4. Treat `response.completed` and `response.failed` as terminal events.
5. Ignore keepalive comments.
