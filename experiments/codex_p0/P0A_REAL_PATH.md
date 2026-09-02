# P0a-1: real internal Responses path

Canonical decision gate: #163. This is **not** a chaos/fault-injection test and must not be used to inject 429/5xx/dropped frames into a shared production endpoint. Deterministic failure injection belongs to P0a-2 on an isolated replica/proxy.

`real_path_conformance.py` answers one question:

> Can the exact pinned Codex binary, using its public custom-provider configuration, run the required Codex workloads through the actual enterprise OpenAI-compatible Responses path?

It bypasses the stale oh-my-gateway Codex backend entirely.

## Public Codex surface used

The runner creates an isolated `$CODEX_HOME/config.toml` with a provider shaped like:

```toml
model = "<deployment model alias>"
model_provider = "chatdragon_p0"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"

[model_providers.chatdragon_p0]
name = "ChatDRAGON P0 enterprise Responses"
base_url = "<enterprise provider base URL>"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 120000
env_key = "P0_API_KEY"

[model_providers.chatdragon_p0.env_http_headers]
"X-Example-Enterprise-Header" = "P0_ENTERPRISE_HEADER"
```

The right-hand values in `env_http_headers` are **environment-variable names**, not header secrets. The API key is also referenced by environment-variable name. Secret values never enter the summary JSON.

`request_max_retries=0` and `stream_max_retries=0` are deliberate: P0a-1 should expose the first real-path failure instead of hiding it behind Codex retries.

## Example

Use an artifact directory outside the repository because raw JSONL/stderr can contain model output or internal diagnostic text:

```bash
export P0_API_KEY='...'
export P0_ENTERPRISE_HEADER='...'

uv run python experiments/codex_p0/real_path_conformance.py \
  --codex-bin /path/to/the/exact/pinned/codex \
  --base-url 'https://internal.example/v1' \
  --model '<deployment-model-alias>' \
  --api-key-env P0_API_KEY \
  --header-env 'X-Example-Enterprise-Header=P0_ENTERPRISE_HEADER' \
  --artifact-dir /tmp/codex-p0-real-path
```

For multiple enterprise headers, repeat `--header-env`.

The runner records the base URL only as a SHA-256 digest in the summary. The generated temporary config is deleted after the run.

## Cases

The default real-path corpus runs:

1. **text** — exact marker, clean `turn.completed`, usage present.
2. **reasoning** — explicit reasoning effort + detailed summary, `reasoning` item required.
3. **tool** — safe local `printf`, `command_execution` item required.
4. **image** — runner generates a red PNG with stdlib; model must identify red without the prompt revealing the color.
5. **long_turn** — safe local sleep + marker, exercises a sustained stream without writes/network tools.
6. **cancel** — starts a long sleep command and sends SIGINT after a bounded delay. If the turn finishes before the signal, the case is `inconclusive`; if SIGINT itself wedges until hard-kill, it fails.
7. **MCP** — deployment-specific and therefore only runs when both `--extra-config-toml` and `--mcp-prompt` are supplied. Without it, overall P0a-1 is `incomplete`, never `pass`.

The tool/long-turn cases use read-only sandboxing and `approval_policy=never`; they do not intentionally modify files or access the network.

## Deployment MCP case

The isolated CODEX_HOME means deployment MCP configuration must be supplied explicitly. Put only the required deployment-specific TOML sections in a local file, for example the relevant `[mcp_servers.*]` entries, then run:

```bash
uv run python experiments/codex_p0/real_path_conformance.py \
  ... \
  --extra-config-toml /secure/local/path/p0-mcp.toml \
  --mcp-prompt 'Use <known deployment MCP tool> to perform <safe read-only action>, then answer.'
```

The extra TOML contents are not copied into the summary; only their SHA-256 digest is recorded. Treat both the TOML and raw case artifacts as local sensitive material.

## Output

The shareable artifact is:

```text
p0a-real-path-summary.json
```

It records:

- exact Codex path/version/binary hash
- model alias
- base-URL hash, not raw URL
- API-key env-var name, not value
- enterprise header names + env-var names, not values
- generated config hash
- event/item counts per case
- usage from `turn.completed`
- raw artifact filenames + hashes
- case `pass | fail | inconclusive | not_run`
- overall `pass | fail | incomplete`

Raw files remain alongside it:

```text
<case>.stdout.jsonl
<case>.stderr.txt
```

Do **not** commit those raw files without reviewing/redacting them.

## Failure ownership

The runner intentionally leaves `failure_owner: null`. After inspecting the local raw artifacts and, where available, corresponding LiteLLM/provider logs, classify each failure into one of:

```text
Codex
LiteLLM/model-gateway
backend/provider
configuration
```

Do not repair a P0a failure by adding transport hacks to oh-my-gateway. A model-gateway defect/configuration failure blocks the Codex rewrite until fixed and rerun; a demonstrated hard Responses incompatibility is a stop/defer result.

## What this does not test

- deterministic 429/5xx/drop/truncated/reordered-stream behavior — P0a-2 isolated fault corpus
- app-server writer/CODEX_HOME ownership — P0b / PR #165
- Python SDK B0 parked-reader/HITL correctness — P0c
- direct app-server C0 transport — only build if the #163 sequential tree reaches it
- production oh-my-gateway response conversion — intentionally out of scope until Codex itself passes the model path
