# OpenCode + LiteLLM Recipes

[LiteLLM](https://docs.litellm.ai/) is a unified proxy that exposes 100+ LLM providers behind a single OpenAI-compatible endpoint. Pairing it with OpenCode means you write **one** OpenCode provider definition and reach every model LiteLLM routes to (Anthropic, OpenAI, Bedrock, on-prem vLLM/SGLang, GLM, Qwen, etc.).

This page covers LiteLLM-specific concerns. For general OpenCode setup see:

- **[managed.md](managed.md)** — gateway spawns `opencode serve` itself
- **[external.md](external.md)** — gateway forwards to your own `opencode serve`

## Why LiteLLM?

| | Direct provider in OpenCode | LiteLLM proxy |
|---|----------------------------|---------------|
| One config per provider | yes — `provider.openai`, `provider.anthropic`, … | one provider entry → many upstream models |
| Per-team API keys | provider-level | **per-virtual-key, plus rate limits & budgets** |
| On-prem / self-hosted endpoints | one provider per endpoint | one LiteLLM, many backends |
| Reasoning-content normalization | provider-dependent | **`merge_reasoning_content_in_choices: true` works everywhere** |
| Logs / observability | provider-dependent | unified callbacks (Langfuse, OTEL, …) |

If you only target Anthropic, you might not need LiteLLM. If you mix providers or run any self-hosted models, it's the right abstraction.

## OpenCode provider definition

LiteLLM speaks OpenAI Chat-Completions, so you register it in OpenCode as the `openai-compatible` provider type:

```json
{
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM",
      "options": {
        "baseURL": "http://litellm:4000/v1",
        "apiKey": "{env:LITELLM_API_KEY}"
      },
      "models": {
        "claude-sonnet-4-5": {},
        "gpt-4o": {},
        "GLM-5.1-FP8": {},
        "Qwen3-235B": {}
      }
    }
  }
}
```

Each key under `models` is a **LiteLLM model name** — the same string you pass as `model` directly to LiteLLM's `/chat/completions`. The gateway exposes it as `opencode/litellm/<key>` to clients.

In **managed mode** put this in `OPENCODE_CONFIG_CONTENT` (single-line JSON). In **external mode** put it in the external server's own opencode config file (e.g. `~/.config/opencode/opencode.json`).

## Reasoning-content rendering (`<think>` tags)

When you call a reasoning model via LiteLLM, the API returns `reasoning_content` as a separate field on each chunk:

```json
{
  "choices": [{
    "message": {
      "content": "Hello!",
      "reasoning_content": "User asked for a greeting..."
    }
  }]
}
```

OpenCode's `@ai-sdk/openai-compatible` provider does **not** automatically split `reasoning_content` into a distinct UI track. By default the reasoning either disappears (if the adapter ignores the field) or leaks into the visible answer (if some intermediate step concatenates fields).

**Cleanest fix: let LiteLLM merge reasoning into content with `<think>` tags.**

```yaml
# litellm config.yaml
litellm_settings:
  merge_reasoning_content_in_choices: true
```

Or per model:

```yaml
model_list:
  - model_name: GLM-5.1-FP8
    litellm_params:
      model: openai/GLM-5.1-FP8
      api_base: http://glm-server:8000/v1
      api_key: dummy
      merge_reasoning_content_in_choices: true
```

After this, LiteLLM returns:

```json
{
  "choices": [{
    "message": {
      "content": "<think>User asked for a greeting...</think>\nHello!"
    }
  }]
}
```

OpenCode forwards the content verbatim, the gateway streams it through, and Open WebUI / any `<think>`-aware client renders it as a collapsible reasoning block. **No gateway-side changes required.**

## Per-tenant or per-team isolation via virtual keys

LiteLLM supports **virtual keys** — short-lived API keys with their own rate limits, budget caps, allowed models, and metadata. Pair this with the gateway's `user` field for end-to-end isolation:

```bash
# 1. Mint a virtual key on LiteLLM with a budget
curl -X POST http://litellm:4000/key/generate \
  -H "Authorization: Bearer sk-master" \
  -d '{"models": ["claude-sonnet-4-5"], "max_budget": 5.0, "metadata": {"team": "research"}}'
```

The OpenCode provider config stays the same; rotate `LITELLM_API_KEY` when you rotate the virtual key. For multi-tenant setups, run **one gateway per tenant** and give each its own virtual key, rather than trying to multiplex tenants through one OpenCode session.

## Common LiteLLM upstream patterns

### Self-hosted vLLM / SGLang

```yaml
model_list:
  - model_name: Qwen3-235B
    litellm_params:
      model: openai/Qwen3-235B   # forwards to OpenAI-compatible vLLM
      api_base: http://vllm:8000/v1
      api_key: dummy             # vLLM ignores it
```

OpenCode side:

```json
{"provider":{"litellm":{"...": "...", "models": {"Qwen3-235B": {}}}}}
```

### Anthropic via LiteLLM (instead of native)

```yaml
model_list:
  - model_name: claude-sonnet-4-5
    litellm_params:
      model: anthropic/claude-sonnet-4-5-20250929
      api_key: os.environ/ANTHROPIC_API_KEY
      thinking: {"type": "enabled", "budget_tokens": 5000}
```

Why? Lets you put Anthropic, OpenAI, and on-prem models behind one auth surface (LiteLLM virtual keys) with unified logging and budgets.

### Bedrock

```yaml
model_list:
  - model_name: claude-via-bedrock
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-5
      aws_region_name: us-east-1
```

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `provider not found: litellm` from OpenCode | provider key missing in OpenCode config |
| `model not found` from LiteLLM | the LiteLLM `model_name` doesn't match what OpenCode sent. Compare LiteLLM logs against OpenCode's request body. |
| Reasoning text leaks into the answer body | `merge_reasoning_content_in_choices` not enabled on LiteLLM, or the model isn't a reasoning model and is just narrating |
| 401 from LiteLLM (not OpenCode!) | `LITELLM_API_KEY` env var unset or wrong; `{env:LITELLM_API_KEY}` interpolation will pass an empty string |
| LiteLLM logs show calls but model errors | upstream provider issue — call LiteLLM directly with the same payload to isolate |
| Streaming stalls partway | LiteLLM's request timeout < the gateway's `MAX_TIMEOUT`. Bump LiteLLM's timeout. |

Quick LiteLLM smoke test (bypassing gateway/OpenCode):

```bash
curl http://litellm:4000/chat/completions \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"ping"}]}'
```

If this works but `opencode/litellm/claude-sonnet-4-5` doesn't, the issue is in OpenCode's provider definition or the gateway's allowlist (`OPENCODE_MODELS`).
