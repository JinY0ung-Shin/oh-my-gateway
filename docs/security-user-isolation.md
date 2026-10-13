# Credential-scoped user isolation

Oh My Gateway supports a credential-bound user identity in addition to the legacy single service key.

> This page covers **application-layer** isolation: how a credential scopes a caller to its own
> workspace/session over HTTP. The **OS/process-layer** boundary (keeping one session's CLI subprocess
> from reading another session's or the gateway's files/memory/secrets — uid, Landlock, namespaces) is a
> separate, in-design concern: see [security-process-isolation.md](security-process-isolation.md).

## Configuration

Set `USER_API_KEYS` to a JSON object mapping the gateway user/workspace id to its bearer token:

```bash
export USER_API_KEYS='{"alice":"replace-with-a-long-random-key","bob":"replace-with-another-key"}'
```

User ids must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$`. Workspace paths accept a slightly wider set (`@` and up to 127 characters, so an email identity can key a workspace whole — see below), but an operator-configured id is deliberately held to the narrower shape. Duplicate or malformed entries fail fast at startup.

`API_KEY` may still be configured at the same time. It remains a **legacy unscoped service key** for backward compatibility: requests authenticated with `API_KEY` retain the historical ability to act across users. Treat it as an operator credential and do not distribute it to tenant users.

## What a user-scoped key binds

When a bearer token matches `USER_API_KEYS`, the gateway derives the user from the credential and ignores caller-selected tenant identity. The authenticated user is projected onto:

- `POST /v1/responses`: the request body's `user` field is overwritten before FastAPI parses it.
- `GET`, `DELETE`, and cancel routes under `/v1/responses/{id}`: the existing `user` query scope is replaced with the authenticated user.
- `/v1/sessions`: list/get/delete and pending-event access are restricted to sessions owned by the authenticated user.
- `/v1/sessions/stats`: gateway-wide statistics are operator-only and require the unscoped `API_KEY`.
- `/files/*`: `WORKSPACE_USER_HEADER` is overwritten with the authenticated user before the file routes resolve a workspace.
- per-user turn concurrency: `MAX_CONCURRENT_TURNS_PER_USER` uses the credential-derived identity instead of a caller-controlled body field.

This means changing `user`, the workspace identity header, or the `user` query parameter cannot move a user-scoped credential into another tenant's workspace/session.

## Workspace identity is used whole

The workspace key is the **entire** identity — `body.user` for `/v1/responses`, the
`WORKSPACE_USER_HEADER` value for `/files/*` and `GET /v1/agent-resources`. It is not
truncated, so `alice@a.com`, `alice@b.com` and bare `alice` are three principals with
three workspaces.

Releases before this one keyed the file routes on the identity's *localpart* (everything
before `@`). Any two identities sharing a localpart then shared one workspace: each could
list, read, overwrite and delete the others' files, and `/v1/agent-resources` reported the
others' private skills and subagents. The same truncation also made the file browser and
the agent disagree about which workspace they were in for one and the same caller, because
`/v1/responses` never truncated.

`WORKSPACE_LEGACY_LOCALPART_KEY=true` restores the old truncating key so an existing
deployment can stage a directory migration. The switch is resolved by the shared
`WorkspaceManager`, so while it is enabled **all** workspace consumers — `/v1/responses`,
`/files/*`, agent resources, and other direct resolver users — land on the same legacy
path. This avoids reintroducing the old file-browser-vs-agent split, but it still re-opens
the cross-user collision described above and logs a warning on every named resolve. Leave
it unset except during a controlled migration window.

If you are upgrading a deployment whose identities contain `@`, the on-disk directory for
those users changes from `<localpart>/` to `<full-identity>/`. Prefer renaming the
directories before pointing traffic at the new build. If a staged migration is unavoidable,
the legacy switch may be used temporarily; because it deliberately collapses principals
sharing a localpart, restrict access during that window and disable it as soon as the
filesystem move is complete.

## Workspace file browser note

The existing `/files/*` routes still keep their additional fail-closed check that `API_KEY` is configured. If you use the file browser together with `USER_API_KEYS`, keep a private operator `API_KEY` configured on the gateway, but authenticate tenant traffic with the per-user key. The middleware will still overwrite the forwarded workspace identity with the credential-derived user.

## Request-size enforcement

The ASGI admission middleware enforces `MAX_REQUEST_SIZE` against bytes actually received for `POST`, `PUT`, `PATCH`, and `DELETE` requests. Requests without `Content-Length` (including `Transfer-Encoding: chunked`) and requests that understate `Content-Length` are rejected with `413` once the received body exceeds the limit. Accepted buffered bodies are replayed to downstream FastAPI handlers with a normalized `Content-Length`.
