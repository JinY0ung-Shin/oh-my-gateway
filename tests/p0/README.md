# Codex P0 transport probes

These are evidence-gathering artifacts for issue #163. They do **not** reactivate or extend the
frozen `src/backends/codex/` backend.

## Always-on fixture self-test

The protocol adversary is intentionally schema-light. It can emit JSON-RPC responses,
notifications/server requests, malformed lines, delays, stderr, and process exits in a deterministic
order.

```bash
uv run pytest tests/p0/test_protocol_adversary_fixture.py -q
```

This test has no Codex SDK dependency and should stay in the normal suite.

## Optional official Python SDK probe

The repository intentionally does not add `openai-codex` to production or dev dependencies for this
spike. Run the probe with an ephemeral dependency instead:

```bash
uv run --with openai-codex pytest tests/p0/test_python_sdk_transport.py -q -rxX
```

Known upstream failure classes are strict `xfail`s. A fixed SDK therefore becomes `XPASS(strict)` and
forces the P0 decision record to be revisited instead of silently leaving stale assumptions in place.

Initial fixtures cover:

- terminal `turn/completed` arriving before the matching `turn/start` response (`openai/codex#41078`)
- a second global read after terminal transport failure (`openai/codex#40399`)
- child-process death while a synchronous human-approval handler owns the SDK reader

The potentially blocking calls run on daemon probe threads with explicit deadlines so reproducing a
hang cannot hang the pytest interpreter itself.

## Rules

- Test semantic outcomes, not a preferred implementation shape.
- Use the same adversary scenarios for the official SDK candidate and any future direct stdio client.
- Do not reach into private SDK `_proc`, `_router`, `_reader_loop`, or raw-message state to make a
  candidate pass.
- Keep model-gateway conformance (P0a) and real Codex writer/storage experiments (P0b) separate from
  these transport fixtures.
- Stop building more transport candidates once the cheapest candidate satisfies every hard invariant
  and the deployment resource budget.
