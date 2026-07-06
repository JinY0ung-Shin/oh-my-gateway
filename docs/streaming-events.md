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
response.output_text.annotation.added  # zero or more (citations)
response.tool_use_started       # zero or more (liveness; precedes response.tool_use)
response.tool_use               # zero or more
response.tool_result            # zero or more
response.task_started           # zero or more
response.task_progress          # zero or more
response.task_notification      # zero or more
response.task_updated           # zero or more
response.hook_event             # zero or more (liveness)
response.compaction             # zero or more (liveness)
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

Failures emit `response.failed`. Empty SDK output is also surfaced as
`response.failed` so clients receive a definite terminal event.

Reasoning (thinking) blocks are emitted as `reasoning` output items with
`response.reasoning_summary_text.delta` / `response.reasoning_text.delta`
events. A turn that interleaves thinking and text (think → text → think → text)
produces multiple `reasoning` and `message` output items in emission order;
clients that concatenate all `message` items' text reconstruct the full answer.

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

### Citations

When the Claude API emits `citations_delta` stream events (citations are
opt-in, attached to document-grounded text), each one is forwarded as a
`response.output_text.annotation.added` event. The `annotation` field is the
raw Claude citation object (e.g. `char_location`), passed through unchanged:

```json
{
  "type": "response.output_text.annotation.added",
  "item_id": "msg_abc123",
  "output_index": 0,
  "content_index": 0,
  "annotation_index": 0,
  "annotation": {
    "type": "char_location",
    "cited_text": "the answer is 42",
    "document_index": 0,
    "document_title": "guide.pdf",
    "start_char_index": 100,
    "end_char_index": 116
  },
  "sequence_number": 6
}
```

`annotation_index` counts citations within the current message item and resets
when a new message item opens.

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

### Server-side tool events

claude-agent-sdk 0.2.82+ surfaces tools that the Anthropic API executes
server-side (e.g. `web_search`, `web_fetch`, `code_execution`, `advisor`).
These were silently dropped before 0.2.82.

`response.server_tool_use` is emitted when the model invokes a server-side
tool. The full SDK content block (preserving its `type` discriminator) is
passed through under `block`:

```json
{
  "type": "response.server_tool_use",
  "block": {
    "type": "server_tool_use",
    "id": "srv_01ABC",
    "name": "web_search",
    "input": { "query": "claude agent sdk release notes" }
  },
  "sequence_number": 9
}
```

`response.advisor_tool_result` is emitted when the API returns the result
for a server-side tool call. `block.content` is the raw dict from the API
— callers that care about a specific server tool's result schema inspect
`block.content["type"]`:

```json
{
  "type": "response.advisor_tool_result",
  "block": {
    "type": "advisor_tool_result",
    "tool_use_id": "srv_01ABC",
    "content": { "type": "web_search_result", "results": [...] }
  },
  "sequence_number": 10
}
```

Clients are not expected to return anything for server-side tool calls —
the API executes them itself and delivers the result in the same stream.

## Subagent Events

Subagent task system messages are forwarded as structured progress events:

```json
{
  "type": "response.task_started",
  "task_id": "task_abc123",
  "description": "Research API patterns",
  "session_id": "sdk-session-id",
  "task_type": "local_agent",
  "subagent_type": "Explore",
  "sequence_number": 8
}
```

`task_type` distinguishes what kind of task spawned: `local_agent` for a
regular subagent, `in_process_teammate` for an agent-team teammate,
`local_workflow` for a workflow run. `subagent_type` is the agent definition
name when applicable. Both are `null` on CLI versions that don't report them.

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

```json
{
  "type": "response.task_updated",
  "task_id": "task_abc123",
  "status": "killed",
  "patch": { "status": "killed", "end_time": "2026-07-06T00:00:00Z" },
  "session_id": null,
  "tool_use_id": null,
  "parent_tool_use_id": null,
  "sequence_number": 11
}
```

`response.task_updated` mirrors the SDK's task-registry patches. A task's
terminal state can arrive **only** here, with no `response.task_notification`
— e.g. a task killed via `TaskStop`, background tasks, and in-process
teammates. Clients tracking active tasks should clear a task on a terminal
`status` from *either* event. Statuses are passed through raw: `task_updated`
reports `killed` where `task_notification` would report `stopped`; terminal
values across both are `completed`, `failed`, `stopped`, `killed`. Unlike the
other task events, `task_updated` is a registry-level patch not tied to a
spawning `Task` tool call: the CLI reports no `tool_use_id` for it, so it is
forwarded regardless of `SUBAGENT_STREAM_PROGRESS`. The `tool_use_id` /
`parent_tool_use_id` keys are still present in the event JSON — always `null`.

Subagent visibility is controlled by:

| Env var | Default | Effect |
|---------|---------|--------|
| `SUBAGENT_STREAM_TEXT` | `false` | Forward subagent text deltas |
| `SUBAGENT_STREAM_TOOL_BLOCKS` | `true` | Forward subagent tool events |
| `SUBAGENT_STREAM_PROGRESS` | `true` | Forward subagent task progress |

## Liveness / Progress Events

These optional events keep the client informed that the agent is still working
during the gaps between visible text — long tool-call generation, hook
execution, and context compaction — instead of a silent stream punctuated only
by SSE keepalive comments. They are advisory: clients may ignore any event type
they don't recognize.

`response.tool_use_started` is emitted at the *start* of a tool call, before its
JSON arguments finish streaming. The matching `response.tool_use` (same
`tool_use_id`) arrives once the arguments are complete. Use it to show
"preparing <tool>…" during long argument generation.

```json
{
  "type": "response.tool_use_started",
  "tool_use_id": "toolu_01ABC123",
  "name": "Bash",
  "sequence_number": 6
}
```

`response.hook_event` mirrors the SDK's hook lifecycle (PreToolUse, PostToolUse,
Stop, …) so a UI can show "running <tool>…" / "<tool> finished". `phase` is
`hook_started` or `hook_response`; `outcome` is present on `hook_response`.

```json
{
  "type": "response.hook_event",
  "phase": "hook_started",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_use_id": "toolu_01ABC123",
  "outcome": null,
  "session_id": "sdk-session-id",
  "sequence_number": 7
}
```

`response.compaction` is emitted when the SDK compacts the context window (an
otherwise-silent pause). `trigger` is `auto` or `manual` when the SDK reports it.

```json
{
  "type": "response.compaction",
  "subtype": "compact_boundary",
  "trigger": "auto",
  "session_id": "sdk-session-id",
  "sequence_number": 8
}
```

Liveness events are controlled by:

| Env var | Default | Effect |
|---------|---------|--------|
| `STREAM_TOOL_PROGRESS` | `true` | Emit `response.tool_use_started` |
| `STREAM_HOOK_EVENTS` | `true` | Enable SDK `include_hook_events`; forward `response.hook_event` |
| `STREAM_COMPACTION_EVENTS` | `true` | Forward `response.compaction` |

Subagent-originated liveness events (with `parent_tool_use_id`) follow the same
subagent gates as their block type: `response.tool_use_started` respects
`SUBAGENT_STREAM_TOOL_BLOCKS`; `response.hook_event` respects
`SUBAGENT_STREAM_PROGRESS`.

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
