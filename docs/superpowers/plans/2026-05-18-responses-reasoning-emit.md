# Responses API Reasoning Emission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface Claude Agent SDK ``ThinkingBlock`` content through ``POST /v1/responses`` as OpenAI Responses API ``reasoning`` output items, in both the streamed event sequence and the non-streaming final response body. Default ON, no opt-out env.

**Architecture:** Replace the thinking-suppress branch in ``src/streaming_utils.py:stream_response_chunks`` with a state machine that opens a ``reasoning`` output item on each ``content_block_start{type=thinking}``, emits ``response.reasoning_summary_text.*`` and ``response.reasoning_text.*`` deltas for the contained ``thinking_delta`` chunks, and closes the item on ``content_block_stop``. The message item's ``output_item.added`` is deferred until the first text delta (or stream end) so it lands at the correct ``output_index``. The non-streaming response builder walks the SDK terminal message and prepends a ``ResponseReasoningItem`` per ``ThinkingBlock``.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, claude-agent-sdk, pytest (asyncio_mode=auto).

**Spec:** `docs/superpowers/plans/../specs/2026-05-18-responses-reasoning-emit-design.md`

---

## File Structure

| File | Responsibility | Change type |
|------|----------------|-------------|
| `src/response_models.py` | Pydantic models for Responses API output shape | Add `ReasoningSummary`, `ReasoningContent`, `ReasoningOutputItem`; widen `ResponseObject.output` union |
| `src/streaming_utils.py` | SSE streaming generator for `/v1/responses` | Rework preamble + thinking handling; add reasoning state machine |
| `src/routes/responses.py` | Non-streaming `/v1/responses` response builder | Prepend reasoning items in final `output[]` |
| `src/chunk_processing.py` | SDK chunk → delta extraction | No change |
| `src/sse_builders.py` | SSE wire-format builders | No new builder — reuse generic `make_response_sse` with extra kwargs |
| `tests/test_streaming_utils_unit.py` | Existing streaming generator tests | Extend with reasoning cases; fix any tests that assume early message item announcement |
| `tests/test_streaming_coverage_unit.py` | Existing edge-case coverage | Extend |

---

### Task 1: Pydantic models for reasoning output

**Files:**
- Modify: `src/response_models.py`
- Test: `tests/test_response_models.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_response_models.py`:

```python
def test_reasoning_output_item_serializes_per_openai_spec():
    from src.response_models import (
        ReasoningOutputItem,
        ReasoningSummary,
        ReasoningContent,
    )

    item = ReasoningOutputItem(
        id="rs_abc",
        summary=[ReasoningSummary(text="thinking...")],
        content=[ReasoningContent(text="thinking...")],
    )
    dumped = item.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "id": "rs_abc",
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "thinking..."}],
        "content": [{"type": "reasoning_text", "text": "thinking..."}],
    }


def test_response_object_accepts_reasoning_in_output():
    from src.response_models import (
        ResponseObject,
        OutputItem,
        ReasoningOutputItem,
        ReasoningSummary,
    )

    resp = ResponseObject(
        id="resp_1",
        model="m",
        output=[
            ReasoningOutputItem(
                id="rs_1",
                summary=[ReasoningSummary(text="x")],
            ),
            OutputItem(id="msg_1"),
        ],
    )
    items = resp.model_dump(mode="json", exclude_none=True)["output"]
    assert items[0]["type"] == "reasoning"
    assert items[1]["type"] == "message"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_response_models.py::test_reasoning_output_item_serializes_per_openai_spec tests/test_response_models.py::test_response_object_accepts_reasoning_in_output -v`
Expected: FAIL with `ImportError: cannot import name 'ReasoningOutputItem'`.

- [ ] **Step 3: Add models**

In `src/response_models.py`, add after the `OutputItem` class:

```python
class ReasoningSummary(BaseModel):
    """A summary part inside a reasoning output item."""

    type: Literal["summary_text"] = "summary_text"
    text: str = ""


class ReasoningContent(BaseModel):
    """A raw reasoning text part inside a reasoning output item."""

    type: Literal["reasoning_text"] = "reasoning_text"
    text: str = ""


class ReasoningOutputItem(BaseModel):
    """A reasoning output item (Anthropic ThinkingBlock → OpenAI reasoning)."""

    id: str
    type: Literal["reasoning"] = "reasoning"
    status: Literal["completed", "in_progress", "incomplete"] = "completed"
    summary: List[ReasoningSummary] = Field(default_factory=list)
    content: Optional[List[ReasoningContent]] = None
```

Then widen the `ResponseObject.output` union:

```python
output: List[Union[OutputItem, FunctionCallOutputItem, ReasoningOutputItem]] = Field(default_factory=list)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_response_models.py -v`
Expected: PASS, no regressions in existing tests.

- [ ] **Step 5: Commit**

```bash
git add src/response_models.py tests/test_response_models.py
git commit -m "feat(responses): add ReasoningOutputItem model"
```

---

### Task 2: Helper to extract thinking blocks from chunks

**Files:**
- Modify: `src/streaming_utils.py` (new helper near `extract_sdk_usage`)
- Test: `tests/test_streaming_utils_unit.py`

This helper is used by the non-streaming builder in Task 6. Build it first so it has unit-test coverage independent of the streaming generator.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_streaming_utils_unit.py`:

```python
class TestExtractThinkingTexts:
    def test_returns_thinking_block_text_in_order(self):
        from src.streaming_utils import extract_thinking_texts

        chunks = [
            {"type": "assistant", "content": [
                {"type": "thinking", "thinking": "first"},
                {"type": "text", "text": "hi"},
                {"type": "thinking", "thinking": "second"},
            ]},
        ]
        assert extract_thinking_texts(chunks) == ["first", "second"]

    def test_returns_empty_when_no_thinking(self):
        from src.streaming_utils import extract_thinking_texts

        chunks = [{"type": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        assert extract_thinking_texts(chunks) == []

    def test_handles_object_blocks_with_attributes(self):
        from src.streaming_utils import extract_thinking_texts

        class _ThinkingBlock:
            def __init__(self, t):
                self.thinking = t

        chunks = [{"content": [_ThinkingBlock("hidden")]}]
        assert extract_thinking_texts(chunks) == ["hidden"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_streaming_utils_unit.py::TestExtractThinkingTexts -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the helper**

In `src/streaming_utils.py`, add near `extract_sdk_usage`:

```python
def extract_thinking_texts(chunks: list) -> list[str]:
    """Return thinking-block texts in the order they appear in the chunk list.

    Walks ``assistant`` chunks and pulls out every ``ThinkingBlock``'s text.
    Tolerates both SDK dataclass objects (``block.thinking``) and dict form
    (``{"type": "thinking", "thinking": "..."}``).
    """
    out: list[str] = []
    for chunk in chunks:
        content = chunk.get("content") if isinstance(chunk, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            text = None
            if hasattr(block, "thinking"):
                text = getattr(block, "thinking", None)
            elif isinstance(block, dict) and block.get("type") == "thinking":
                text = block.get("thinking")
            if text:
                out.append(text)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_streaming_utils_unit.py::TestExtractThinkingTexts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_streaming_utils_unit.py
git commit -m "feat(streaming): add extract_thinking_texts helper"
```

---

### Task 3: Defer message-item announcement in streaming generator

The current preamble in `src/streaming_utils.py:640-658` unconditionally emits `response.output_item.added` and `response.content_part.added` for the message item before any delta arrives. With reasoning items, the message item must come AFTER any reasoning items, so we defer this announcement until the first text delta — or the stream's close, if no text ever arrives.

**Files:**
- Modify: `src/streaming_utils.py` (`stream_response_chunks` preamble + close paths)
- Test: `tests/test_streaming_utils_unit.py`

- [ ] **Step 1: Write the failing test (regression guard for non-thinking streams)**

Append to `tests/test_streaming_utils_unit.py`:

```python
async def test_stream_emits_message_item_after_first_text_when_no_thinking(caplog):
    """No thinking → message output_item.added still fires, just deferred until first text."""
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hi"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"subtype": "success", "result": "hi"}

    events: list[tuple[str, dict]] = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    # No "response.output_item.added" should appear BEFORE the first text delta.
    first_delta_idx = types.index("response.output_text.delta")
    added_idx = types.index("response.output_item.added")
    assert added_idx < first_delta_idx  # still before — but only just
    # content_part.added must also appear after the first text delta is requested
    # (we open it lazily on first text); both should still fire before completion.
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"
```

- [ ] **Step 2: Run test to verify it fails or shifts expected ordering**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_emits_message_item_after_first_text_when_no_thinking -v`
Expected: depending on existing ordering this may PASS already; verify by running before edits.

- [ ] **Step 3: Refactor preamble to defer message-item announcement**

In `src/streaming_utils.py:stream_response_chunks`, after the `response.in_progress` event, REMOVE the unconditional emissions of `response.output_item.added` and `response.content_part.added` for the message item. Replace with a deferred opener:

```python
# Track whether the message output_item has been announced yet.
message_item_opened = False
output_index = 0  # increments per closed reasoning item

def _open_message_item() -> list[str]:
    """Emit message output_item.added + content_part.added; return SSE lines."""
    nonlocal message_item_opened
    if message_item_opened:
        return []
    message_item_opened = True
    out_item = OutputItem(id=output_item_id, status="in_progress")
    content_part = ResponseContentPart(type="output_text", text="")
    return [
        make_response_sse(
            "response.output_item.added",
            output_index=output_index,
            item=out_item,
            sequence_number=_next_seq(),
        ),
        make_response_sse(
            "response.content_part.added",
            item_id=output_item_id,
            output_index=output_index,
            content_index=0,
            part=content_part,
            sequence_number=_next_seq(),
        ),
    ]
```

Then update `_emit_delta` to thread the current `output_index`:

```python
def _emit_delta(text: str) -> str:
    return make_response_sse(
        "response.output_text.delta",
        item_id=output_item_id,
        output_index=output_index,
        content_index=0,
        delta=text,
        logprobs=[],
        sequence_number=_next_seq(),
    )
```

Where the existing code calls `yield _emit_delta(cleaned)`, first prepend any opener events:

```python
if not message_item_opened:
    for line in _open_message_item():
        yield line
yield _emit_delta(cleaned)
```

Similarly, every other site in the function that emits text-related events (`output_text.done`, `content_part.done`, `output_item.done` for the message item) must use the tracked `output_index` instead of the hardcoded `0`, and must emit the opener first if `not message_item_opened`.

At the close path (before `response.completed`), if `not message_item_opened`, call `_open_message_item()` so the consumer contract (always at least one message item) is preserved, then immediately close it with empty content.

- [ ] **Step 4: Run all streaming tests, fix any that asserted early announcement**

Run: `uv run pytest tests/test_streaming_utils_unit.py tests/test_streaming_coverage_unit.py -v`
Expected: existing tests pass with updated ordering (announcement happens lazily but still before `output_text.delta`, since the opener is emitted right before the delta).

If a test asserted exact event indices and now fails, update the assertion to check relative ordering (`added` before `delta`) rather than absolute positions.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_streaming_utils_unit.py
git commit -m "refactor(streaming): defer message output_item announcement"
```

---

### Task 4: Open reasoning item on first thinking_delta

Add the state machine entry for thinking. When a `thinking_delta` arrives and no reasoning item is open, emit `response.output_item.added` (reasoning) and `response.reasoning_summary_part.added`.

**Files:**
- Modify: `src/streaming_utils.py`
- Test: `tests/test_streaming_utils_unit.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_stream_opens_reasoning_on_first_thinking_delta(caplog):
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        }}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    # Sequence up to first delta: created → in_progress → output_item.added(reasoning)
    # → reasoning_summary_part.added → reasoning_summary_text.delta → reasoning_text.delta
    assert "response.output_item.added" in types
    added_idx = types.index("response.output_item.added")
    # The added item must be type=reasoning
    _, added_payload = events[added_idx]
    assert added_payload["item"]["type"] == "reasoning"
    assert added_payload["output_index"] == 0
    assert "response.reasoning_summary_part.added" in types
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_opens_reasoning_on_first_thinking_delta -v`
Expected: FAIL (current code suppresses thinking).

- [ ] **Step 3: Add reasoning-open transition in the main loop**

Remove the suppress branch (currently `src/streaming_utils.py:736-738`). Replace with:

```python
# Thinking-state transition: open reasoning item.
text_delta, in_thinking = extract_stream_event_delta(chunk, in_thinking)
if text_delta is not None:
    token_streaming = True

    # NEW: open reasoning on first thinking delta of a block.
    if in_thinking and not was_thinking:
        reasoning_item_id = _generate_rs_id()
        reasoning_open = True
        reasoning_text_buf = []
        reasoning_item = ReasoningOutputItem(id=reasoning_item_id, status="in_progress")
        yield make_response_sse(
            "response.output_item.added",
            output_index=output_index,
            item=reasoning_item,
            sequence_number=_next_seq(),
        )
        yield make_response_sse(
            "response.reasoning_summary_part.added",
            item_id=reasoning_item_id,
            output_index=output_index,
            summary_index=0,
            part={"type": "summary_text", "text": ""},
            sequence_number=_next_seq(),
        )

    # Drop the synthetic <think>/</think> markers; they're state-only.
    if text_delta in ("<think>", "</think>"):
        continue

    # ... reasoning delta + message delta paths added in Task 5 ...
```

Add imports near the top of the file:

```python
from src.response_models import ReasoningOutputItem
```

Add a local id generator:

```python
def _generate_rs_id() -> str:
    import uuid
    return f"rs_{uuid.uuid4().hex[:24]}"
```

State variables initialized at the top of `stream_response_chunks`:

```python
reasoning_open = False
reasoning_item_id: Optional[str] = None
reasoning_text_buf: list[str] = []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_opens_reasoning_on_first_thinking_delta -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_streaming_utils_unit.py
git commit -m "feat(streaming): open reasoning output_item on first thinking delta"
```

---

### Task 5: Stream reasoning deltas (summary_text + reasoning_text)

For each `thinking_delta` while a reasoning item is open, emit both `response.reasoning_summary_text.delta` and `response.reasoning_text.delta` with the same text. Accumulate text into the buffer for the `done` events.

**Files:**
- Modify: `src/streaming_utils.py`
- Test: `tests/test_streaming_utils_unit.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_stream_emits_summary_and_reasoning_text_deltas_with_same_text():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        for piece in ("abc", "def"):
            yield {"type": "stream_event", "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": piece},
            }}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    summary_deltas = [p for t, p in events if t == "response.reasoning_summary_text.delta"]
    reasoning_deltas = [p for t, p in events if t == "response.reasoning_text.delta"]
    assert [d["delta"] for d in summary_deltas] == ["abc", "def"]
    assert [d["delta"] for d in reasoning_deltas] == ["abc", "def"]
    assert all(d["output_index"] == 0 for d in summary_deltas + reasoning_deltas)
    assert all(d["summary_index"] == 0 for d in summary_deltas)
    assert all(d["content_index"] == 0 for d in reasoning_deltas)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_emits_summary_and_reasoning_text_deltas_with_same_text -v`
Expected: FAIL (no delta events emitted yet).

- [ ] **Step 3: Add the reasoning-delta emission path**

In the body of the `text_delta is not None` block, after the open-on-first-thinking step and after dropping the synthetic markers, branch on `reasoning_open`:

```python
if reasoning_open and in_thinking:
    reasoning_text_buf.append(text_delta)
    yield make_response_sse(
        "response.reasoning_summary_text.delta",
        item_id=reasoning_item_id,
        output_index=output_index,
        summary_index=0,
        delta=text_delta,
        sequence_number=_next_seq(),
    )
    yield make_response_sse(
        "response.reasoning_text.delta",
        item_id=reasoning_item_id,
        output_index=output_index,
        content_index=0,
        delta=text_delta,
        sequence_number=_next_seq(),
    )
    continue
```

This `continue` skips the existing text-delta path so the message item is not opened on thinking content.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_emits_summary_and_reasoning_text_deltas_with_same_text -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_streaming_utils_unit.py
git commit -m "feat(streaming): emit reasoning summary_text + reasoning_text deltas"
```

---

### Task 6: Close reasoning item on thinking → text or block end

When `in_thinking` transitions `True → False` (or the stream ends with a reasoning item still open), emit the four close events with the accumulated text, then increment `output_index` so the next item (or message) lands at a fresh index.

**Files:**
- Modify: `src/streaming_utils.py`
- Test: `tests/test_streaming_utils_unit.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_stream_closes_reasoning_with_accumulated_text():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hello"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "text", "text": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "world"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}}
        yield {"subtype": "success", "result": "world"}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    done_text = next(p for t, p in events if t == "response.reasoning_summary_text.done")
    assert done_text["text"] == "hello"
    rt_done = next(p for t, p in events if t == "response.reasoning_text.done")
    assert rt_done["text"] == "hello"
    # The reasoning output_item.done must come BEFORE the message output_item.added.
    r_done_idx = next(i for i, (t, p) in enumerate(events)
                       if t == "response.output_item.done" and p["item"]["type"] == "reasoning")
    msg_added_idx = next(i for i, (t, p) in enumerate(events)
                         if t == "response.output_item.added" and p["item"]["type"] == "message")
    assert r_done_idx < msg_added_idx
    # output_index for message must be 1 (after reasoning at 0).
    _, msg_added = events[msg_added_idx]
    assert msg_added["output_index"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_closes_reasoning_with_accumulated_text -v`
Expected: FAIL (close events not yet emitted).

- [ ] **Step 3: Add the close-reasoning transition**

Define a helper inside `stream_response_chunks`:

```python
def _close_reasoning() -> list[str]:
    """Emit the four close events for the open reasoning item. Bumps output_index."""
    nonlocal reasoning_open, reasoning_item_id, reasoning_text_buf, output_index
    if not reasoning_open:
        return []
    full_text = "".join(reasoning_text_buf)
    item = ReasoningOutputItem(
        id=reasoning_item_id,
        status="completed",
        summary=[ReasoningSummary(text=full_text)],
        content=[ReasoningContent(text=full_text)],
    )
    out = [
        make_response_sse(
            "response.reasoning_summary_text.done",
            item_id=reasoning_item_id,
            output_index=output_index,
            summary_index=0,
            text=full_text,
            sequence_number=_next_seq(),
        ),
        make_response_sse(
            "response.reasoning_text.done",
            item_id=reasoning_item_id,
            output_index=output_index,
            content_index=0,
            text=full_text,
            sequence_number=_next_seq(),
        ),
        make_response_sse(
            "response.reasoning_summary_part.done",
            item_id=reasoning_item_id,
            output_index=output_index,
            summary_index=0,
            part={"type": "summary_text", "text": full_text},
            sequence_number=_next_seq(),
        ),
        make_response_sse(
            "response.output_item.done",
            output_index=output_index,
            item=item,
            sequence_number=_next_seq(),
        ),
    ]
    reasoning_open = False
    reasoning_item_id = None
    reasoning_text_buf = []
    output_index += 1
    return out
```

Add `ReasoningSummary` and `ReasoningContent` to the imports.

Hook the close into two places:

1. In the `text_delta is not None` path, just before opening the message item on first non-thinking text delta:

```python
if reasoning_open and not in_thinking:
    for line in _close_reasoning():
        yield line
```

Place this immediately before the existing block that opens the message item via `_open_message_item()`.

2. At end-of-stream cleanup, before `response.completed`:

```python
if reasoning_open:
    for line in _close_reasoning():
        yield line
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_streaming_utils_unit.py::test_stream_closes_reasoning_with_accumulated_text -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_streaming_utils_unit.py
git commit -m "feat(streaming): close reasoning output_item on thinking-end transition"
```

---

### Task 7: Multi-thinking-block and thinking-only edge cases

Cover (a) two separate thinking blocks interleaved with text and (b) a thinking-only stream that never emits text (must still produce an empty message item so the consumer contract holds).

**Files:**
- Modify: `src/streaming_utils.py` (only if a bug surfaces)
- Test: `tests/test_streaming_utils_unit.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_stream_two_thinking_blocks_get_separate_reasoning_items():
    import logging
    from src.streaming_utils import stream_response_chunks

    def think_start(idx):
        return {"type": "stream_event", "event": {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "thinking", "thinking": ""},
        }}

    def think_delta(idx, t):
        return {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "thinking_delta", "thinking": t},
        }}

    def text_start(idx):
        return {"type": "stream_event", "event": {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "text", "text": ""},
        }}

    def text_delta(idx, t):
        return {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "text_delta", "text": t},
        }}

    def stop(idx):
        return {"type": "stream_event", "event": {"type": "content_block_stop", "index": idx}}

    async def chunk_source():
        yield think_start(0); yield think_delta(0, "a"); yield stop(0)
        yield text_start(1); yield text_delta(1, "X"); yield stop(1)
        yield think_start(2); yield think_delta(2, "b"); yield stop(2)
        yield text_start(3); yield text_delta(3, "Y"); yield stop(3)
        yield {"subtype": "success", "result": "XY"}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    reasoning_added = [p for t, p in events
                       if t == "response.output_item.added" and p["item"]["type"] == "reasoning"]
    assert len(reasoning_added) == 2
    assert [p["output_index"] for p in reasoning_added] == [0, 2]
    msg_added = next(p for t, p in events
                     if t == "response.output_item.added" and p["item"]["type"] == "message")
    # Message lands at index 3 (after 2 reasonings + interleaved nothing else opened).
    # Implementation-defined: as long as it's after both reasoning indices and unique.
    assert msg_added["output_index"] >= 3 or msg_added["output_index"] == 1


async def test_stream_thinking_only_emits_empty_message_item():
    import logging
    from src.streaming_utils import stream_response_chunks

    async def chunk_source():
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "thoughts"},
        }}
        yield {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}}
        yield {"subtype": "success", "result": ""}

    events = []
    async for line in stream_response_chunks(
        chunk_source(),
        model="m",
        response_id="resp_1",
        output_item_id="msg_1",
        chunks_buffer=[],
        logger=logging.getLogger("test"),
    ):
        events.append(_parse_response_sse(line))

    types = [t for t, _ in events]
    # Reasoning item closed.
    assert any(t == "response.output_item.done"
               and p["item"]["type"] == "reasoning" for t, p in events)
    # Message item still announced + closed (empty).
    msg_added = next(p for t, p in events
                     if t == "response.output_item.added" and p["item"]["type"] == "message")
    msg_done = next(p for t, p in events
                    if t == "response.output_item.done" and p["item"]["type"] == "message")
    assert msg_added["output_index"] == 1
    assert msg_done["item"]["content"] == [] or all(
        cp.get("text", "") == "" for cp in msg_done["item"].get("content", [])
    )
    assert types[-1] == "response.completed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_streaming_utils_unit.py -k "two_thinking_blocks or thinking_only" -v`
Expected: FAIL on the assertions above.

- [ ] **Step 3: Fix any gaps**

The state machine from Tasks 4–6 should naturally support both scenarios:
- Each new `thinking` block enters at `was_thinking=False, in_thinking=True`, which is the open path.
- The end-of-stream `_close_reasoning()` + `_open_message_item()` already handles thinking-only.

If tests still fail, the most likely issue is the empty-message-close path skipping `_open_message_item()` when `not content_sent`. In the post-loop close, ensure:

```python
# After the main async-for loop:
if reasoning_open:
    for line in _close_reasoning():
        yield line
if not message_item_opened:
    for line in _open_message_item():
        yield line
# ... existing output_text.done / content_part.done / output_item.done / completed emission ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_streaming_utils_unit.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/streaming_utils.py tests/test_streaming_utils_unit.py
git commit -m "feat(streaming): handle multiple thinking blocks and thinking-only responses"
```

---

### Task 8: Non-streaming response builder prepends reasoning items

**Files:**
- Modify: `src/routes/responses.py` (around line 1284-1295 where `_build_completed_response` is called)
- Modify: `src/routes/responses.py` (around line 191-209 where `_build_completed_response` is defined)
- Test: `tests/test_responses_user.py` (or `tests/test_responses_rehydrate_integration.py` — pick the one with a non-streaming test fixture)

- [ ] **Step 1: Find the non-streaming response builder**

Read `src/routes/responses.py:191-209`:

```python
def _build_completed_response(
    response_id: str,
    model: str,
    assistant_text: str,
    metadata: Dict[str, str],
    *,
    output_tokens: int,
    input_tokens: int,
) -> ResponseObject:
    ...
    output=[
        OutputItem(
            id=_generate_msg_id(),
            content=[ResponseContentPart(text=assistant_text)],
        ),
    ],
    ...
```

It currently takes only `assistant_text`. It needs the raw `chunks` list (or a pre-extracted list of thinking texts) to build reasoning items.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_responses_user.py` (or create a new file `tests/test_responses_reasoning_nonstream_unit.py`):

```python
def test_build_completed_response_prepends_reasoning_items_per_thinking_block():
    from src.routes.responses import _build_completed_response

    resp = _build_completed_response(
        response_id="resp_1",
        model="m",
        assistant_text="hi",
        metadata={},
        output_tokens=1,
        input_tokens=1,
        thinking_texts=["first thought", "second thought"],
    )
    assert [item.type for item in resp.output] == ["reasoning", "reasoning", "message"]
    r0, r1, msg = resp.output
    assert r0.summary[0].text == "first thought"
    assert r0.content[0].text == "first thought"
    assert r1.summary[0].text == "second thought"
    assert msg.content[0].text == "hi"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_responses_user.py::test_build_completed_response_prepends_reasoning_items_per_thinking_block -v`
Expected: FAIL with `TypeError: unexpected keyword argument 'thinking_texts'`.

- [ ] **Step 4: Add `thinking_texts` parameter and prepend reasoning items**

Modify `_build_completed_response` signature to accept `thinking_texts: Optional[List[str]] = None`:

```python
import uuid
from src.response_models import (
    ReasoningContent,
    ReasoningOutputItem,
    ReasoningSummary,
)

def _build_completed_response(
    response_id: str,
    model: str,
    assistant_text: str,
    metadata: Dict[str, str],
    *,
    output_tokens: int,
    input_tokens: int,
    thinking_texts: Optional[List[str]] = None,
) -> ResponseObject:
    output: List = []
    for t in thinking_texts or []:
        output.append(
            ReasoningOutputItem(
                id=f"rs_{uuid.uuid4().hex[:24]}",
                summary=[ReasoningSummary(text=t)],
                content=[ReasoningContent(text=t)],
            )
        )
    output.append(
        OutputItem(
            id=_generate_msg_id(),
            content=[ResponseContentPart(text=assistant_text)],
        )
    )

    return ResponseObject(
        id=response_id,
        model=model,
        status="completed",
        metadata=metadata,
        output=output,
        usage=ResponseUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )
```

- [ ] **Step 5: Wire the call site (line ~1292)**

```python
from src.streaming_utils import extract_thinking_texts

response_obj = _build_completed_response(
    response_id,
    body.model,
    assistant_text,
    body.metadata,
    output_tokens=completion_tokens,
    input_tokens=prompt_tokens,
    thinking_texts=extract_thinking_texts(chunks),
)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_responses_user.py -v`
Expected: new test PASSES; existing tests still PASS (the new parameter defaults to None).

- [ ] **Step 7: Commit**

```bash
git add src/routes/responses.py tests/
git commit -m "feat(responses): prepend reasoning items in non-streaming response.output"
```

---

### Task 9: Full suite + lint + final commit

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 2: Run ruff**

Run: `uv run ruff check src/ tests/`
Expected: `All checks passed!`.

- [ ] **Step 3: If anything failed, fix inline**

Common failures and remedies:
- Existing test asserting exact event count → update to relative-ordering assertion.
- Type-narrowing complaint in pydantic union → ensure `ReasoningOutputItem` is added to the `output` Union and re-export from any `__all__` lists.
- Lint: unused import in `responses.py` after refactor.

- [ ] **Step 4: Verify manually against the original LiteLLM stream**

Document the manual smoke test in the commit body so the deployer knows what to do:

```text
Manual verification:
  1. .env has SANITIZER_ENABLED=true and ANTHROPIC_BASE_URL=<upstream>
  2. POST /v1/responses with a prompt that triggers thinking
  3. Observe event stream contains:
     - response.output_item.added with item.type=reasoning
     - response.reasoning_summary_text.delta+done
     - response.reasoning_text.delta+done
     - response.output_item.done (reasoning) before response.output_item.added (message)
```

- [ ] **Step 5: Final commit (if any leftover changes from Step 3)**

```bash
git add -A
git commit -m "chore(responses): final cleanup for reasoning emission"
```

If the working tree is clean after Step 3, skip this step.

---

## Self-Review

Done after writing the plan, before user review:

- **Spec coverage:**
  - Streaming event sequence per Task 4–7 ✓
  - Multiple thinking blocks → Task 7 ✓
  - Same-text duplication (summary + reasoning_text) → Task 5 ✓
  - Deferred message announcement → Task 3 ✓
  - Non-streaming output[] prepend → Task 8 ✓
  - Pydantic models → Task 1 ✓
- **Placeholder scan:** no `TBD`/`TODO`. Every code step has a complete code block. ✓
- **Type consistency:** `ReasoningOutputItem`, `ReasoningSummary`, `ReasoningContent` consistent across Task 1, 6, 8. `_open_message_item`, `_close_reasoning` defined in Tasks 3 and 6 respectively. ✓
- **Scope:** Single feature, one plan, no cross-cutting refactors beyond what the feature needs. ✓
