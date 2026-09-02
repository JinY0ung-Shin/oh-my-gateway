# P0a-2: isolated deterministic Responses fault corpus

Canonical gate: #163. This corpus exists to answer transport/error-handling questions that cannot be safely or deterministically tested against a shared enterprise endpoint.

**Never point the fault proxy at shared production.** Use an isolated LiteLLM/model-gateway replica or a dedicated test route with no unrelated traffic. Both the proxy and matrix runner require `--i-understand-isolated-test-only`.

## Components

- `fault_proxy.py` — local reverse proxy that forwards the real Responses request/stream while injecting one deterministic failure mode.
- `fault_matrix.py` — starts the proxy mode-by-mode, points the exact pinned Codex binary at it, runs one safe text turn, and mechanically checks whether Codex succeeds, fails terminally, or hangs.
- `real_path_conformance.py` — shared P0a-1 Codex exec/provider/event runner used by the matrix.

The proxy does not log request bodies or header values. Raw proxy/Codex diagnostics are written only to the local artifact directory by the matrix runner.

## Provider URL handling

Pass the exact provider base URL used by Codex, for example:

```text
https://isolated-litellm.example/v1
```

The matrix decomposes this into:

```text
upstream origin: https://isolated-litellm.example
local Codex base: http://127.0.0.1:<ephemeral-port>/v1
```

so the original path prefix is preserved exactly through the proxy.

## Run

```bash
export P0_API_KEY='...'
export P0_ENTERPRISE_HEADER='...'

uv run python experiments/codex_p0/fault_matrix.py \
  --codex-bin /path/to/the/exact/pinned/codex \
  --upstream-base-url 'https://isolated-litellm.example/v1' \
  --model '<deployment-model-alias>' \
  --api-key-env P0_API_KEY \
  --header-env 'X-Enterprise-Header=P0_ENTERPRISE_HEADER' \
  --artifact-dir /tmp/codex-p0-faults \
  --i-understand-isolated-test-only
```

The default Codex provider settings used by the matrix set both request and stream retries to **0**. This is deliberate: P0a-2 needs to see the first failure and its terminalization behavior rather than an SDK/runtime retry policy hiding it.

## Gating corpus

The default matrix contains:

| Case | Injection | Required observation |
|---|---|---|
| `control` | byte-equivalent proxy pass-through | clean successful turn |
| `http_429` | deterministic HTTP 429 + Retry-After | bounded terminal failure |
| `http_500` | deterministic pre-stream HTTP 500 | bounded terminal failure |
| `drop_before_body` | response connection dies before first body byte | bounded terminal failure |
| `truncate_after_first_event` | clean EOF after first SSE event | bounded terminal failure |
| `abort_after_first_event` | abnormal stream abort after first SSE event | bounded terminal failure |
| `malformed_after_first_event` | invalid SSE JSON frame | bounded terminal failure |
| `delay_within_idle` | delay first event less than Codex idle timeout | successful turn |
| `delay_beyond_idle` | delay first event beyond Codex idle timeout | bounded terminal failure |

A **hang is never acceptable** for these cases.

## Observational corpus

Two cases are deliberately weaker gates:

- `duplicate_event_observation`
- `reorder_event_observation`

The matrix requires only **bounded terminal behavior** (success or explicit failure), because current protocol behavior may legitimately reject these streams. Review their raw artifacts manually for duplicate side effects, corrupted state, or silent mis-ordering before deciding whether a stricter invariant is warranted.

## What remains intentionally separate

This matrix does **not** yet claim the #163 case "connection loss after side-effect-capable work". A normal text request cannot prove that boundary. That test needs a deterministic tool-call fixture where the model/tool cycle reaches a known side-effect-capable point and the proxy then drops the subsequent provider exchange at a controlled protocol boundary.

Do not fake that evidence by choosing an arbitrary SSE event number from a nondeterministic model response.

Likewise, P0a-2 does not replace:

- real-path P0a-1 conformance (#168)
- app-server writer/storage/resource facts (#165)
- B0 Python SDK server-request/liveness tests
- C0 direct app-server transport if the sequential decision tree reaches it

## Output

The shareable artifact is:

```text
p0a-fault-matrix-summary.json
```

It contains only:

- exact Codex binary/version/hash
- model alias
- upstream base URL hash, not raw URL
- auth/header **environment-variable names**, never values
- per-case injection, expected class, observed class, status
- secret-safe Codex event/item/usage summary
- local artifact filenames

Per-case directories contain raw Codex JSONL/stderr and proxy stderr/lifecycle data. Treat those files as local potentially sensitive diagnostics; inspect/redact before sharing.

## Interpretation

A P0a-2 failure means one of:

```text
Codex failed to terminalize a provider/stream fault correctly
fault proxy/harness bug
isolated LiteLLM/model-gateway behavior differs from the assumed Responses stream contract
```

Classify ownership from raw artifacts and corresponding isolated gateway logs. Do not compensate in oh-my-gateway's future Codex transport until the model-path behavior itself is understood.
