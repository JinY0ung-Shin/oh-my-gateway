# OpenCode — External Mode

In external mode the gateway does **not** start `opencode serve` itself. Instead, it forwards HTTP traffic to an externally-managed OpenCode server. The external server owns its own provider config and MCP servers — the gateway is purely a proxy + router + auth layer.

```
┌─────────────────────────┐         ┌──────────────────────────┐
│ gateway container       │         │ trusted host             │
│ (sandboxed,             │         │ (broader fs access,      │
│  limited fs access)     │ ──HTTP─►│  shared across replicas, │
│                         │  basic  │  …)                      │
│   FastAPI on 8000       │  auth   │                          │
│                         │         │   opencode serve         │
│                         │         │   on 7891                │
└─────────────────────────┘         └──────────────────────────┘
```

## When to use

- **Sandbox/trust split** — the gateway runs in a locked-down container, but OpenCode needs broader filesystem access (e.g. read/write across a developer workspace mounted on a host)
- **Shared OpenCode** — multiple gateway replicas talk to one OpenCode instance
- **Independent debugging / lifecycle** — restart OpenCode without restarting the gateway, or vice versa
- **Existing OpenCode deployment** — you already operate `opencode serve` and just want to front it with the gateway's session/auth/responses-API surface

If none of these apply, prefer [managed mode](managed.md). It has fewer moving parts.

## Architectural trade-offs

### What the external server owns

When you go external, the external server reads its **own** config file at startup. The gateway has no way to inject config into a process it didn't spawn. Practically:

| Setting | Source in external mode |
|---------|-------------------------|
| Providers (`provider.*`) | The external server's own config file |
| MCP servers (`mcp.*`) | The external server's own config file |
| Model definitions (`provider.*.models`) | The external server's own config file |
| Auth (basic-auth realm, etc.) | However the external server / its reverse proxy is set up |

So the following **gateway-side env vars become no-ops**:

- `OPENCODE_CONFIG_CONTENT`
- `OPENCODE_USE_WRAPPER_MCP_CONFIG`
- `OPENCODE_BIN`, `OPENCODE_HOST`, `OPENCODE_PORT`, `OPENCODE_START_TIMEOUT_MS`

### What still applies in external mode

These flow through the gateway at request time and still take effect:

- `OPENCODE_MODELS` — public allowlist (`/v1/models`, `/v1/responses` accept gate)
- `OPENCODE_DEFAULT_MODEL` — used when request omits provider/model
- `OPENCODE_AGENT` — agent profile passed in each prompt
- `OPENCODE_QUESTION_PERMISSION` — passed in session creation
- `OPENCODE_SERVER_USERNAME`, `OPENCODE_SERVER_PASSWORD` — basic-auth credentials sent with every request
- `OPENCODE_BASE_URL` — the URL itself, obviously

## Step 1 — bring up the external OpenCode server

Set up `opencode serve` on the trusted host with:

1. The provider config you want (LiteLLM, OpenAI, etc.) baked into its config file (e.g. `~/.config/opencode/opencode.json` or wherever your install reads from)
2. Whatever MCP servers you want OpenCode to expose
3. Basic auth (recommended — anyone who can reach the URL gets full agent access otherwise)

Example `~/.config/opencode/opencode.json`:

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
        "gpt-4o": {}
      }
    }
  },
  "mcp": {
    "filesystem": {
      "type": "local",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/home/agent/workspace"]
    }
  }
}
```

Start it:

```bash
export LITELLM_API_KEY=sk-1234
export OPENCODE_SERVER_USERNAME=opencode
export OPENCODE_SERVER_PASSWORD=...
opencode serve --hostname 0.0.0.0 --port 7891
```

> **Auth note:** the basic-auth `WWW-Authenticate` realm (`Secure Area` is the default if you see it in browser challenges) usually comes from a built-in middleware or a reverse proxy you set up. Make sure the username/password actually configured there match what you give the gateway in step 2 — mismatch is by far the most common 401 source.

Verify the server is reachable from the gateway's network:

```bash
# from the gateway host (or container)
curl -u $OPENCODE_SERVER_USERNAME:$OPENCODE_SERVER_PASSWORD \
     http://opencode-host:7891/global/health
# {"healthy": true}
```

## Step 2 — point the gateway at it

```bash
# .env
BACKENDS=claude,opencode
OPENCODE_BASE_URL=http://opencode-host:7891
OPENCODE_SERVER_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=...   # must match the server side
OPENCODE_MODELS=litellm/claude-sonnet-4-5,litellm/gpt-4o
OPENCODE_DEFAULT_MODEL=litellm/claude-sonnet-4-5
```

Restart the gateway. Verify the mode:

```bash
curl -s http://localhost:8000/admin/api/backends \
  | jq '.backends[] | select(.name == "opencode") | .metadata'
```

```json
{"mode": "external", "base_url": "http://opencode-host:7891"}
```

## Step 3 — call it

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"opencode/litellm/claude-sonnet-4-5","input":"ping"}'
```

Streaming, multi-turn, per-user workspace isolation all work the same as managed mode and the Claude backend.

## Network considerations (Docker)

If the gateway runs in a container and OpenCode runs on the host, `OPENCODE_BASE_URL=http://127.0.0.1:7891` from inside the container resolves to the **container's** loopback, not the host's. Options:

- **`host.docker.internal`** — `OPENCODE_BASE_URL=http://host.docker.internal:7891` (Docker Desktop and recent Linux Docker)
- **Host network mode** — set `network_mode: host` on the gateway service in docker-compose
- **Docker bridge gateway IP** — typically `172.17.0.1`, but inspect with `docker network inspect bridge`

If both gateway and OpenCode are containerised on the same compose network, use the OpenCode service name as the hostname:

```yaml
services:
  opencode:
    image: your/opencode:latest
    ports: ["7891:7891"]
  gateway:
    environment:
      OPENCODE_BASE_URL: http://opencode:7891
```

## Security model

External mode shifts trust boundaries. Things to think about:

1. **Basic auth is mandatory in production.** The gateway can run unauthenticated against an external OpenCode (`OPENCODE_SERVER_PASSWORD` unset → no `Authorization` header sent), but anyone on the network who can reach the URL gets full agent access otherwise. Always set a password and use TLS where possible.
2. **The external server can read whatever its host can read.** That's the point — but make sure the `OPENCODE_BASE_URL` you trust really is the OpenCode server, not e.g. an attacker on the LAN listening on the same port. If you're crossing trust zones, terminate the link with TLS and pin certs.
3. **MCP server scope** is controlled on the external server. The gateway's `MCP_CONFIG` is **not** consulted for OpenCode in external mode. Audit the external server's own config to see what tools your agents actually have.
4. **Per-user workspace isolation** still works on the gateway side (assigning each user a subdirectory under `USER_WORKSPACES_DIR`), but the *external server* may or may not honor those paths — if its filesystem doesn't see the same directory tree, the cwd parameter is meaningless. For real isolation, run one external OpenCode per user/tenant or mount the same workspace volumes on both sides.
5. **Audit logs are split.** The gateway logs which user made each request and what `model` they targeted. The external OpenCode logs the actual provider call and tool use. Correlate via timestamps + the gateway's `chat_id` (passed as the OpenCode session title).

## Verification checklist

- [ ] `GET /admin/api/backends` shows the OpenCode backend item's `metadata.mode = "external"` and the right `base_url`
- [ ] `curl -u user:pass <url>/global/health` returns `{"healthy": true}` from the gateway's network
- [ ] `GET /v1/models` lists your `OPENCODE_MODELS` entries with `opencode/` prefix
- [ ] A streamed `/v1/responses` request returns content and the LiteLLM/provider logs show the corresponding upstream call
- [ ] Multi-turn (`previous_response_id`) preserves context across turns
- [ ] Restarting the gateway does **not** restart OpenCode (and vice versa)

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `401 Unauthorized` even with creds | `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` mismatch with the actual server. From inside the gateway container, run `curl -u user:pass <url>/global/health` directly to isolate. |
| `WWW-Authenticate: Basic realm="Secure Area"` in 401 response | A basic-auth middleware (built-in or reverse-proxy like nginx/Caddy) is in front of OpenCode. Verify the user/pass that middleware was configured with — not what `opencode serve` itself accepts. |
| `Connection refused` | Gateway can't reach `OPENCODE_BASE_URL`. Check container network — `127.0.0.1` from inside a container is **not** the host. Use `host.docker.internal` or service name. |
| `provider not found` from OpenCode | The external server's config doesn't include that provider. Update its config file and restart it (gateway-side `OPENCODE_CONFIG_CONTENT` is a no-op here). |
| Backend reports `mode: "managed"` after setting `OPENCODE_BASE_URL` | Env var didn't make it into the gateway container. `docker exec <container> env \| grep OPENCODE_BASE_URL` to confirm. Common cause: `.env` not picked up because of missing `env_file:` in compose. |
| Tool calls succeed but file changes don't appear in the gateway's `USER_WORKSPACES_DIR` | The external server's filesystem differs from the gateway's. Either share volumes or accept that workspaces are external-server-local. |
| 502 / 504 to clients | External OpenCode is slow or unhealthy. Bump `MAX_TIMEOUT` on the gateway and check the external server's logs / load. |

## Configuration reference (external mode)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCODE_BASE_URL` | unset | URL of the external `opencode serve`. Setting this enables external mode. |
| `OPENCODE_SERVER_USERNAME` | `opencode` | Basic-auth username sent with every request |
| `OPENCODE_SERVER_PASSWORD` | unset | Basic-auth password; if unset, no `Authorization` header is sent |
| `OPENCODE_AGENT` | `general` | Agent profile passed at request time |
| `OPENCODE_DEFAULT_MODEL` | unset | Used when the request omits provider/model |
| `OPENCODE_QUESTION_PERMISSION` | `ask` | `ask` / `allow` / `deny` for the question tool |
| `OPENCODE_MODELS` | unset | Public allowlist exposed via `/v1/models` |

These exist but are **no-ops** in external mode:

- `OPENCODE_BIN`, `OPENCODE_HOST`, `OPENCODE_PORT`, `OPENCODE_START_TIMEOUT_MS`
- `OPENCODE_CONFIG_CONTENT`
- `OPENCODE_USE_WRAPPER_MCP_CONFIG`
