# OpenAI Responses API Reasoning Emission Design

Date: 2026-05-18
Status: Draft

## Goal

Surface Claude Agent SDK ``ThinkingBlock`` content through ``POST /v1/responses``
as OpenAI Responses API ``reasoning`` output items, both in the streamed event
sequence and in non-streaming final responses. Default ON, no opt-out env.

## Non-Goals

- Backend changes outside Claude (``codex``, ``opencode``).
- Modifying how thinking is **requested** from the SDK (``options.thinking``
  configuration stays as-is in ``src/backends/claude/client.py:176``).
- Encrypted reasoning (``ResponseReasoningItem.encrypted_content``).
- ``ChatCompletion`` (``/v1/chat/completions``) reasoning — only Responses API.
- Removing or reusing the ``<think>...</think>`` text wrapper in
  ``src/message_adapter.py:120`` (used by non-Responses code paths).

## Current Behavior to Replace

``src/streaming_utils.py:736-738`` unconditionally drops every thinking delta
and the ``<think>``/``</think>`` markers from the Responses streaming generator:

```python
if was_thinking or in_thinking or text_delta in ("<think>", "</think>"):
    continue
```

This branch is removed. Thinking deltas are routed into reasoning events
instead.

The non-streaming path in ``src/routes/responses.py`` (around the final
``response.output[]`` assembly, lines ~1186 onward) returns only ``message``
items today. ``ThinkingBlock`` content is invisible to the caller.

## Target Event Sequence (Streaming)

Per ``openai-python`` SDK schemas (verified against
``openai/types/responses/response_reasoning_*.py``):

When the SDK emits at least one ``content_block_start{type=thinking}`` followed
by one or more ``thinking_delta`` events, the gateway emits a full
``reasoning`` output item **before** the message output item:

```
response.created
response.in_progress
response.output_item.added            item={type:"reasoning", id:rs_…, status:"in_progress", summary:[], content:[]}, output_index=0
response.reasoning_summary_part.added  item_id, output_index=0, summary_index=0, part={type:"summary_text", text:""}
response.reasoning_summary_text.delta  item_id, output_index=0, summary_index=0, delta="…"   (repeated)
response.reasoning_text.delta          item_id, output_index=0, content_index=0, delta="…"   (repeated, SAME text)
response.reasoning_summary_text.done   item_id, output_index=0, summary_index=0, text="<full thinking>"
response.reasoning_text.done           item_id, output_index=0, content_index=0, text="<full thinking>"
response.reasoning_summary_part.done   item_id, output_index=0, summary_index=0, part={type:"summary_text", text:"<full thinking>"}
response.output_item.done              item={type:"reasoning", id:rs_…, status:"completed",
                                              summary:[{type:"summary_text", text:"<full thinking>"}],
                                              content:[{type:"reasoning_text", text:"<full thinking>"}]},
                                       output_index=0
response.output_item.added             item={type:"message", id:msg_…, …}, output_index=1
…existing message event sequence (content_part.added → output_text.delta* → output_text.done → content_part.done → output_item.done)…
response.completed
```

If there is no thinking content, the stream stays identical to today's output
(reasoning item is never opened).

**Same-text duplication policy.** Anthropic exposes raw thinking text, not a
summary. We emit it under both event families so consumers that expect either
schema work. Doubles outbound bytes during thinking phases; acceptable for
parity with OpenAI's published schemas.

## Multiple Thinking Blocks

Claude may emit interleaved ``thinking → tool_use → thinking → text`` patterns.
Each ``content_block_start{type=thinking}`` opens a **new** reasoning output
item with the next ``output_index``. The message item's ``output_index`` is
``(number of reasoning items emitted)``.

Reasoning items are flushed as soon as their ``content_block_stop`` arrives —
they are never held open across other blocks.

## Non-Streaming Response Shape

``src/routes/responses.py`` builds the final response from the SDK's terminal
``AssistantMessage``. The output assembler now walks ``message.content`` in
order and, for every ``ThinkingBlock``, inserts a ``ResponseReasoningItem``
into ``response.output[]`` ahead of the message item.

Each reasoning item:

```json
{
  "id": "rs_<generated>",
  "type": "reasoning",
  "status": "completed",
  "summary": [{"type": "summary_text", "text": "<thinking>"}],
  "content": [{"type": "reasoning_text", "text": "<thinking>"}]
}
```

``encrypted_content`` is omitted. Order is preserved (thinking → message →
thinking → message becomes ``reasoning → message → reasoning → message`` —
though in practice Claude collapses to one trailing message).

## State Machine (Streaming Generator)

Tracked variables inside the existing generator in ``src/streaming_utils.py``:

- ``in_thinking: bool`` — already tracked via ``extract_stream_event_delta``.
- ``output_index: int`` — new. Incremented per emitted output item.
- ``reasoning_item_id: Optional[str]`` — id of currently open reasoning item.
- ``reasoning_text: list[str]`` — accumulating thinking text for the open item.
- ``message_item_opened: bool`` — whether the message ``output_item.added`` has
  been emitted yet. Today this is emitted unconditionally at start; we **defer
  it** until either (a) the first text delta arrives, or (b) the stream is
  closing without text (we still emit an empty message item so the existing
  consumer contract holds).

Transitions:

| Event                                              | Action                                                                                          |
|----------------------------------------------------|--------------------------------------------------------------------------------------------------|
| Stream start                                       | Emit ``response.created``, ``response.in_progress``. Do **not** emit message ``output_item.added``. |
| ``in_thinking`` transitions ``False → True``       | Generate ``rs_<uuid>``, emit reasoning ``output_item.added`` + ``reasoning_summary_part.added``. |
| Thinking delta while ``in_thinking``               | Append to ``reasoning_text``. Emit ``reasoning_summary_text.delta`` + ``reasoning_text.delta``.  |
| ``in_thinking`` transitions ``True → False``       | Emit ``reasoning_summary_text.done`` + ``reasoning_text.done`` + ``reasoning_summary_part.done`` + reasoning ``output_item.done`` with full ``summary``/``content``. Increment ``output_index``. Reset reasoning state. |
| First text delta arrives                           | If ``message_item_opened`` is False: emit message ``output_item.added`` at current ``output_index`` and ``content_part.added``, set flag. Then emit ``output_text.delta`` as today. |
| Subsequent text deltas / tool events / stop events | Unchanged from current code path (just operate on the deferred message item).                   |
| Stream closing without any text delta              | If ``message_item_opened`` is False: emit empty message ``output_item.added`` + close sequence so downstream contract is preserved. |

The ``<think>``/``</think>`` synthetic markers from
``chunk_processing.extract_stream_event_delta`` are still consumed by the state
transition logic but never reach the wire (they were already filtered).

## File-Level Changes

### ``src/streaming_utils.py``
- Add reasoning event emission helpers (``_emit_reasoning_*``) near
  ``_emit_delta``.
- Replace the suppress branch (line 736-738) with the state machine above.
- Track ``output_index`` and ``message_item_opened`` across the generator.
- Defer the initial ``response.output_item.added`` for the message item.
- Add an ``rs_…`` id generator (analogous to existing ``msg_…`` /
  ``output_item_…`` generators — reuse ``_generate_msg_id`` style).

### ``src/sse_builders.py``
- New ``make_reasoning_summary_part_added/done``,
  ``make_reasoning_summary_text_delta/done``,
  ``make_reasoning_text_delta/done`` builders following the existing
  ``make_response_sse`` shape (frame is ``event: <type>\ndata: <json>\n\n``).

### ``src/routes/responses.py``
- In the non-streaming response builder, walk ``AssistantMessage.content`` and
  prepend a ``ResponseReasoningItem`` for each ``ThinkingBlock`` before the
  ``message`` item in ``response.output``.

### ``src/chunk_processing.py``
- No change. ``extract_stream_event_delta`` already returns ``in_thinking``
  transitions.

## Tests

New tests in ``tests/`` (no new files needed; extend the closest existing
module). Required coverage:

1. **Streaming, single thinking block + text** — verifies full reasoning event
   sequence emitted before message, with correct indices and accumulated
   ``done`` text.
2. **Streaming, multiple thinking blocks interleaved with tool use** — each
   thinking block becomes its own reasoning item with increasing
   ``output_index``.
3. **Streaming, thinking-only response (no text)** — reasoning item emitted,
   message item emitted empty, sequence still terminates with
   ``response.completed``.
4. **Streaming, no thinking** — output identical to current behavior (regression
   guard for non-thinking flows).
5. **Non-streaming, thinking + text** — final ``response.output`` contains
   ``[reasoning, message]`` in that order with correct ``summary``/``content``.
6. **Sequence numbers monotonic** across reasoning + message events.

All tests are unit tests against the generator with a fake SDK event stream;
no live SDK or upstream is hit.

## Risks

- **Doubled outbound traffic during thinking** — emitting the same text under
  both summary and reasoning_text event families. Acceptable for spec parity.
- **Consumers that already ignore unknown events** — fine; OpenAI Python SDK
  drops unknown events silently. Custom consumers that strictly validate the
  event union may need an update. No env flag is provided per the user's
  decision; if a consumer breaks, the fix is consumer-side.
- **Deferred message item announcement** — clients that expect
  ``output_item.added`` for the message before any other event will see it
  later (after the reasoning item closes). This is consistent with OpenAI's
  own event ordering.
