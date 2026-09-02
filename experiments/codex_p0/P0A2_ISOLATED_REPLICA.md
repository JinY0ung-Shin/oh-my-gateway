# P0a-2: isolated Responses replica and fault corpus

Canonical decision gate: #163. Decision record: #170. Real-path runner (P0a-1): #168.

`P0A_REAL_PATH.md` states that deterministic 429/5xx/drop/truncated-stream injection must **not**
be aimed at the shared enterprise endpoint. `mock_responses_upstream.py` is that isolated
counterpart: a dependency-free OpenAI Responses upstream that the pinned Codex binary can be
pointed at directly, or that the P0a-1 runner can be pointed at instead of the enterprise path.

It serves three jobs:

1. **Hermetic Responses upstream.** A conformant target with no model, no network and no
   credentials, so Codex-side behavior can be observed in isolation.
2. **Positive control.** The P0a-1 corpus can run end to end offline, so a failure there is
   attributable to the runner or to Codex rather than to the enterprise path. #169 can also use
   this as its inner upstream to self-test its proxy.
3. **Contract capture.** `--log` records each request; that is how
   `fixtures/codex_responses_request_0.147.0.json` was produced. Header *values* are redacted
   unless the name is allowlisted (`SAFE_HEADER_VALUES`), because the runner and the enterprise
   path supply `Authorization`, API-key and deployment-specific `env_http_headers` — a capture log
   must never become a credential dump. Header names are always kept.

## Scope split — #169 owns the canonical fault matrix

There must not be two competing P0a-2 fault matrices drifting apart:

```text
#168  P0a-1 real-path runner
#171  hermetic mock upstream + request-contract capture + positive control   (this file)
#169  canonical isolated-gateway fault proxy + matrix; may use #171 as its inner upstream
```

The fault knobs below stay because they are useful for **low-level Codex control behavior** against
a hermetic upstream — cheap, offline, no proxy or gateway required. They are not the canonical
fault corpus: #169 injects faults into a real isolated LiteLLM/model-gateway path with real
auth/header traffic, and that is the artifact whose matrix results count as P0a-2 evidence.

## A replica pass is never a P0a-1 result

The replica answers protocol-shaped prompts by construction: it echoes the corpus markers
(`CHATDRAGON_P0_*_OK`), emits a reasoning item whenever `reasoning` is present, and reports the
expected image color without looking at a single pixel. That is deliberate — it isolates wire and
plumbing behavior from model capability. Never record a replica run as enterprise-path conformance;
P0a-1 still requires the real deployment.

## Running it

```bash
# standalone, with contract capture
python3 experiments/codex_p0/mock_responses_upstream.py --port 8099 --log /tmp/p0a2-requests.jsonl

# with a fault
python3 experiments/codex_p0/mock_responses_upstream.py --port 8099 --fault drop_mid_stream --fault-after 4
```

Point Codex at it through a provider config in an isolated `CODEX_HOME`:

```toml
model = "replica-model"
model_provider = "replica"
approval_policy = "never"

[model_providers.replica]
name = "P0a-2 replica"
base_url = "http://127.0.0.1:8099/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
```

Disabling both retry counts matters: with retries enabled Codex masks the first fault, which is the
one being measured.

Or reuse the P0a-1 runner unchanged, which is the intended way to prove the runner itself works:

```bash
uv run python experiments/codex_p0/real_path_conformance.py \
  --codex-bin /path/to/pinned/codex \
  --base-url http://127.0.0.1:8099/v1 \
  --model replica-model \
  --artifact-dir /tmp/codex-p0a2
```

## Fault modes

| `--fault` | Behavior |
|---|---|
| `none` | Normal conformant responses (default) |
| `http_429` | Rejects with 429 + `Retry-After: 1` before any stream starts |
| `http_500` | Rejects with a 500 JSON error before any stream starts |
| `mid_stream_500` | Emits an SSE `error` frame mid-stream, then closes |
| `drop_mid_stream` | Closes the connection with no terminal event |
| `truncated_sse` | Writes half a JSON frame, then closes |
| `malformed_json` | Injects one unparsable SSE frame, then continues normally |
| `idle_stall` | Opens the stream and sends nothing (exercises `stream_idle_timeout_ms`) |

`--fault-after N` selects the frame index at which mid-stream faults fire.

## Observed results (codex-cli 0.147.0)

Recorded so the corpus has a known-good baseline; re-verify on any Codex version bump.

**Direct to the replica** — full turn semantics work: text turn with usage; a `reasoning` item when
reasoning is requested; and a complete tool cycle, where the replica returns a streamed
`function_call(exec_command, {"cmd": "printf …"})`, Codex executes it locally, and the follow-up
request carries `function_call` plus `function_call_output` (matching `call_id`) before the final
marker.

**Through the deployment's LiteLLM** (`litellm_serving/litellm_config.yaml`, unmodified, with the
replica standing in for a vLLM upstream) — the turn also completes, and two behaviors are worth
recording:

- **LiteLLM is a passthrough on `/v1/responses`, not a translator.** It forwards the request to the
  upstream's `/v1/responses` path with the body intact; it does not convert to
  `/v1/chat/completions`. For `hosted_vllm/*` models this means the conformance surface is the
  upstream model server itself, and the chat-path settings in that config
  (`drop_params`, `merge_reasoning_content_in_choices`) do not apply to this path.
- **LiteLLM drops `client_metadata`.** Confirmed by diffing two captures from the same binary:
  direct sends `client_metadata`, via-LiteLLM does not; no other key changes. LiteLLM also rewrites
  the response `id` into an opaque routing token, so the upstream id is not visible to the client —
  relevant to `previous_response_id` continuation.

**Fault behavior** — `drop_mid_stream` produces a bounded, deterministic failure rather than a hang:
Codex exits non-zero with `stream disconnected before completion: stream closed before
response.completed`.

**Two non-fatal environment notes** — Codex fetches
`https://chatgpt.com/backend-api/plugins/featured` at startup and merely logs a warning when egress
blocks it; and an unknown model alias produces
`Model metadata for '<model>' not found. Defaulting to fallback metadata`, which Codex itself warns
"can degrade performance and cause issues". Model metadata for the deployment's aliases is a real
work item, not cosmetic.

## Captured contract

`fixtures/codex_responses_request_0.147.0.json` is a capture of what Codex sends, **sanitized**:
every identity UUID (installation / session / thread / turn / window ids and `prompt_cache_key`) is
replaced with a deterministic placeholder, environment-specific absolute paths are replaced with
`/workspace`, and `instructions` is truncated. Only field/schema/value semantics are under test;
identity and path values are not. Keep a raw capture local if an audit trail is needed.

The file carries a `contract_assertions` block holding **only the stable semantics**: required
top-level fields, required tool names, and the load-bearing values (`stream`, `store`, `include`,
`tool_choice`, `parallel_tool_calls`, `reasoning` keys). Any future regression test must assert
against that block, never byte-exact equality of the whole capture — tool descriptions, the model
catalog and `instructions` prose churn with every Codex release, and promoting volatile prose into
the contract would turn routine upgrades into test noise. `prompt_cache_key` and `client_metadata`
are listed as identity fields explicitly *not* under test.

It is the requirement list any Responses upstream must accept:

- top-level `instructions, input, model, stream, store, reasoning, tools, tool_choice,
  parallel_tool_calls, include, prompt_cache_key, client_metadata`
- `store: false`, `stream: true`, `reasoning: {"summary": "auto"}`,
  `include: ["reasoning.encrypted_content"]`, `parallel_tool_calls: false`
- ten tools: `exec_command`, `write_stdin`, `update_plan`, `request_user_input`, `view_image`,
  `multi_agent_v1`, `get_goal`, `create_goal`, `update_goal`, and a built-in `web_search`

One useful negative result: the replica returns no `reasoning.encrypted_content` and Codex tolerates
its absence. That field is OpenAI-proprietary, so a non-OpenAI upstream will not produce it.

## Out of scope

- the actual enterprise path — P0a-1 / #168, which this cannot substitute for
- the canonical fault matrix against a real isolated gateway — #169
- whether the **exact deployed vLLM version plus its model/tool-parser configuration** accepts the
  Codex 0.147.0 payload and stream semantics: the ten tools, `function_call_output` continuation,
  reasoning, image input, streaming. That is the open P0a question the passthrough finding
  relocates onto the model servers. The question is compatibility at that level, **not** mere route
  existence — recent vLLM is reported to implement `/v1/responses` and function tools, a claim this
  environment could not verify independently (vLLM docs are egress-blocked here), so neither
  direction is asserted. Replaying the captured fixture against one real model server settles it
- `strip_thinking.py` / `THINK_OUTPUT_MODE`, which the deployment installs as a LiteLLM worker
  startup hook and which is not loaded here
- app-server writer/`CODEX_HOME` ownership — P0b / #165
- Python SDK B0 parked-reader correctness — P0c / #166
