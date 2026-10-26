# Per-user agent sandbox image (issue #248 spike).
#
# One long-lived container per gateway user runs that user's Claude CLI and
# everything the CLI starts (Bash, scripts, stdio MCP servers) under gVisor
# (`--runtime=runsc`). The image carries no CLI and no gateway code: the
# spawner bind-mounts the CLI binary read-only and the user's own workspace
# and config dir read-write at the same absolute paths the gateway uses, so
# argv/settings paths need no translation.
#
# Spike base: python + CA certificates, no apt step. A production image adds
# the agent toolchain (git, node/npx, uv, build tools) the Bash tool and stdio
# MCP servers expect.
FROM python:3.12-slim-bookworm

# The CLI runs as this unprivileged user inside the sandbox (defense in depth
# on top of gVisor; the container also drops every capability).
RUN groupadd -g 1000 agent && useradd -m -u 1000 -g 1000 -s /bin/bash agent

COPY sandbox_exec.sh /usr/local/bin/sandbox-exec
RUN chmod 0755 /usr/local/bin/sandbox-exec

USER agent
# PID 1 only idles; `docker run --init` puts docker-init in front of it to
# reap orphans of exec'd sessions.
CMD ["sleep", "infinity"]
