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
RUN_STALE_BACKEND_TESTS=1 uv run --with 'openai-codex==0.147.0' \
  pytest tests/p0/test_codex_p0_python_sdk_transport.py -q -rxX
```

Pin the SDK **exactly**: the probes assert `openai-codex==0.147.0` at collection and refuse to
certify any other version, so a result can never be misattributed. The bundled `openai-codex-cli-bin`
is never executed (`launch_args_override` fully replaces argv), so the ~114 MiB download is
dependency baggage — cache it in CI.

The probe file is named `test_codex_*` on purpose: this repo deselects Codex tests by default
(`collect_ignore_glob` + the node-id deselect hook), so the probes are opt-in through the same
`RUN_STALE_BACKEND_TESTS=1` switch as the rest of the Codex corpus and can never run by accident
when `openai_codex` happens to be installed. The adversary self-test keeps its neutral name and stays
in the normal suite.

Known upstream failure classes are strict `xfail`s **with `raises=TimeoutError`**, so an unrelated
failure (a signature change, a model-validation rejection, a scenario desync) fails the test instead
of masquerading as the upstream bug. A fixed SDK becomes `XPASS(strict)` and forces the P0 decision
record to be revisited instead of silently leaving stale assumptions in place. Every known-hang probe
has a **positive control** on the same scenario machinery that must pass; if a control fails, the
harness is broken and the neighbouring xfail carries no information.

The probes exercise the transport **pre-`initialize`** deliberately: `CodexClient.start()` does not
send `initialize`, the fixture is strictly step-sequential, and none of the failure classes under test
depend on handshake state. A probe that adds `initialize()` must script that step into its scenario.

Initial fixtures cover:

- terminal `turn/completed` arriving before the matching `turn/start` response (`openai/codex#41078`),
  probed through the **public `turn_start()` helper** (where the registration ordering lives) and
  through raw `request()` + manual registration, each paired with a safe-ordering control
- a second global read after terminal transport failure (`openai/codex#40399`)
- child-process death while a synchronous human-approval handler owns the SDK reader, paired with a
  non-parking control that proves the park — not the SDK in general — is what delays detection
- `turn/interrupt` routing while the handler is parked, paired with a non-parking control in which the
  same interrupt is routed within the runtime-health bound

Every potentially blocking SDK read — including the first post-death read — runs on a daemon probe
thread behind `_await_call`, so reproducing a hang cannot hang the pytest interpreter itself. Teardown
goes through `_close_quietly`: `CodexClient.close()` flushes stdin and raises `BrokenPipeError` once the
child has died, which is teardown noise, not the property under test (every assertion precedes it). The
healthy-arrival deadline is 8 s: a fixed SDK answers in milliseconds, and only the genuinely-buggy
path pays the wait, so CI load cannot turn a fix into a false xfail.

## Rules

- Test semantic outcomes, not a preferred implementation shape.
- Use the same adversary scenarios for the official SDK candidate and any future direct stdio client.
- Do not reach into private SDK `_proc`, `_router`, `_reader_loop`, or raw-message state to make a
  candidate pass.
- Keep model-gateway conformance (P0a) and real Codex writer/storage experiments (P0b) separate from
  these transport fixtures.
- Stop building more transport candidates once the cheapest candidate satisfies every hard invariant
  and the deployment resource budget.
