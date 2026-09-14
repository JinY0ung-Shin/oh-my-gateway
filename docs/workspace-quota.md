# Per-user workspace storage quota

`USER_WORKSPACE_QUOTA_MB` optionally limits the cumulative logical file bytes owned by one named user's workspace tree.

```env
# 500 MiB across every backend directory below <USER_WORKSPACES_DIR>/<user>/.
# Unset or 0 means unlimited.
USER_WORKSPACE_QUOTA_MB=500
```

This is separate from the existing single-file upload controls:

- `WORKSPACE_UPLOAD_MAX_BYTES` limits one `POST /files/upload` file. Its default is 10 MiB.
- `MAX_REQUEST_SIZE` limits the whole HTTP request body. Its default is also 10 MiB.
- Multipart overhead means the actual advertised per-file upload ceiling is `min(WORKSPACE_UPLOAD_MAX_BYTES, MAX_REQUEST_SIZE - 8192)`.
- `USER_WORKSPACE_QUOTA_MB` limits the aggregate user workspace. It does not replace either request/upload limit.

## Scope

The quota bucket is the user root, `<USER_WORKSPACES_DIR>/<user>/`, not a backend-specific subdirectory. For example, with a 500 MiB quota, bytes in both `<user>/claude/` and `<user>/codex/` consume the same 500 MiB allowance. A Claude filesystem alias such as `CLAUDE_WORKSPACE_DIR=pro` therefore does not create a new quota bucket.

Usage counts regular-file payload bytes recursively. Symlinks are not followed, special files do not contribute payload bytes, and hard-linked files are counted once by inode for current usage. Directory/file copies account for the bytes the copy would materialize at the destination.

## File API behavior

`GET /files/limits` exposes both `max_upload_bytes` and `workspace_quota_bytes`. `GET /files/quota` exposes current `used_bytes`, `limit_bytes`, `remaining_bytes`, `enabled`, and `over_quota` for the caller.

Quota-growing `POST /files/upload` and `POST /files/copy` operations are preflighted. Within one gateway process, the file API serializes its own quota-growing operations per user so the usage check and upload/copy mutation share one accounting critical section. An operation whose projected usage exceeds the quota returns HTTP `507 Insufficient Storage` with error code `workspace_quota_exceeded`. Overwriting a file with a smaller replacement is allowed even when the workspace is already at its limit.

This serialization only covers mutations that enter through the file API. A simultaneous Claude tool, shell command, direct filesystem writer, or another gateway process can still change the same user root while a file API operation is in flight; the quota remains application-level rather than a filesystem transaction.

When the cumulative quota is disabled, the upload/copy path does not scan workspace usage or acquire quota locks; existing behavior and cost remain unchanged.

## Claude agent writes

When the quota is enabled, the existing Claude `PreToolUse` hook transport is also installed for workspace writes whose projected final size can be estimated. `Write`, `Edit`, and `MultiEdit` receive a **best-effort projected-size preflight**. Enabling the quota does **not** implicitly enable `WORKSPACE_SANDBOX_ENABLED`; path-boundary policy remains a separate setting.

The Claude hook does **not** reserve bytes between `PreToolUse` approval and the later tool execution. Consequently, two concurrent sessions for the same named user can both inspect the same pre-write usage, each independently fit, and then together push the workspace above the configured limit. Multiple gateway processes introduce the same class of race. Once the workspace is over quota, subsequent deterministic growth is denied by later preflight checks until usage is reduced; shrinking replacements remain allowed when their projected result fits.

Arbitrary Bash commands, direct filesystem writers, and other opaque subprocess behavior are also outside a transactional accounting boundary. `USER_WORKSPACE_QUOTA_MB` is therefore a **soft application quota**, not a hard storage ceiling. Deployments that require an unbreakable byte ceiling or cross-process reservation semantics should enforce an OS/filesystem project quota in addition to this setting.
