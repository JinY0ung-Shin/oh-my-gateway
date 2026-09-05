# Credential-scoped user isolation

Oh My Gateway supports a credential-bound user identity in addition to the legacy single service key.

## Configuration

Set `USER_API_KEYS` to a JSON object mapping the gateway user/workspace id to its bearer token:

```bash
export USER_API_KEYS='{"alice":"replace-with-a-long-random-key","bob":"replace-with-another-key"}'
```

User ids must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$`, the same constraint used by persistent workspace paths. Duplicate or malformed entries fail fast at startup.

`API_KEY` may still be configured at the same time. It remains a **legacy unscoped service key** for backward compatibility: requests authenticated with `API_KEY` retain the historical ability to act across users. Treat it as an operator credential and do not distribute it to tenant users.

## What a user-scoped key binds

When a bearer token matches `USER_API_KEYS`, the gateway derives the user from the credential and ignores caller-selected tenant identity. The authenticated user is projected onto:

- `POST /v1/responses`: the request body's `user` field is overwritten before FastAPI parses it.
- `GET`, `DELETE`, and cancel routes under `/v1/responses/{id}`: the existing `user` query scope is replaced with the authenticated user.
- `/v1/sessions`: list/get/delete and pending-event access are restricted to sessions owned by the authenticated user.
- `/files/*`: `WORKSPACE_USER_HEADER` is overwritten with the authenticated user before the file routes resolve a workspace.
- per-user turn concurrency: `MAX_CONCURRENT_TURNS_PER_USER` uses the credential-derived identity instead of a caller-controlled body field.

This means changing `user`, the workspace identity header, or the `user` query parameter cannot move a user-scoped credential into another tenant's workspace/session.

## Workspace file browser note

The existing `/files/*` routes still keep their additional fail-closed check that `API_KEY` is configured. If you use the file browser together with `USER_API_KEYS`, keep a private operator `API_KEY` configured on the gateway, but authenticate tenant traffic with the per-user key. The middleware will still overwrite the forwarded workspace identity with the credential-derived user.

## Request-size enforcement

The ASGI admission middleware enforces `MAX_REQUEST_SIZE` against bytes actually received for `POST`, `PUT`, `PATCH`, and `DELETE` requests. Requests without `Content-Length` (including `Transfer-Encoding: chunked`) and requests that understate `Content-Length` are rejected with `413` once the received body exceeds the limit. Accepted buffered bodies are replayed to downstream FastAPI handlers with a normalized `Content-Length`.
