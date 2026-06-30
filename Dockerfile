FROM python:3.12-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

ARG APT_MIRROR_URL=
ARG APT_SECURITY_MIRROR_URL=

# Install system dependencies.
COPY docker/apt_mirror_sources.sh /usr/local/bin/apt_mirror_sources.sh

RUN sh /usr/local/bin/apt_mirror_sources.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/local/bin/apt_mirror_sources.sh

# Install uv for skills/scripts that invoke `uv run` inside the container.
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /usr/local/bin/
RUN uv --version

WORKDIR /app

# Optional: point pip at a private/corporate PyPI mirror.
# PIP_TRUSTED_HOST is required when the mirror is served over plain HTTP;
# pip silently ignores HTTP indexes otherwise. Space-separated for multiple hosts.
ARG PIP_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=

# Install runtime dependencies pinned by uv.lock. The export happens at build
# time (offline, reads only the lock) so the pins can never drift from the lock;
# pip does the actual install so PIP_INDEX_URL mirrors keep working.
COPY pyproject.toml uv.lock ./
RUN uv export --format requirements.txt --no-dev --no-hashes --no-emit-project --locked -o /tmp/requirements.txt \
    && PIP_INDEX_URL="$PIP_INDEX_URL" \
    PIP_EXTRA_INDEX_URL="$PIP_EXTRA_INDEX_URL" \
    PIP_TRUSTED_HOST="$PIP_TRUSTED_HOST" \
    python -m pip install --upgrade pip \
    && PIP_INDEX_URL="$PIP_INDEX_URL" \
    PIP_EXTRA_INDEX_URL="$PIP_EXTRA_INDEX_URL" \
    PIP_TRUSTED_HOST="$PIP_TRUSTED_HOST" \
    python -m pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Copy runtime files used by the app.
COPY src ./src
# The admin system-prompt UI loads these templates at runtime.
COPY docs/*system-prompt*.md ./docs/

# Optional marketplace plugin auto-install on container start. Supports multiple
# repos and multiple plugins per repo, and fresh-clones each repo on every start
# so the latest plugin version is installed (see CLAUDE_PLUGIN_* env vars).
COPY docker/install_plugins.py /usr/local/bin/install_plugins.py

# Optional build-time install hook for private/corporate additions. Compose
# supplies a no-op script by default; set GATEWAY_BUILD_INSTALL_SCRIPT to run
# a local script without committing it to this repository.
ARG GATEWAY_BUILD_INSTALL_CACHE_BUST=
RUN --mount=type=secret,id=gateway_build_install_script,target=/tmp/gateway-build-install.sh \
    echo "$GATEWAY_BUILD_INSTALL_CACHE_BUST" >/dev/null \
    && bash /tmp/gateway-build-install.sh

# Startup shim repairs writable bind mounts while still root, then drops to the
# unprivileged app uid before running the server.
COPY docker/entrypoint.py /usr/local/bin/docker-entrypoint.py

# The Claude CLI refuses --dangerously-skip-permissions under root, and the
# gateway always opens sessions with permission_mode=bypassPermissions (see
# src/routes/responses.py), so the server process must run as a regular user.
# The entrypoint starts as root only long enough to repair Docker bind-mount
# permissions for gateway-owned data, then drops to APP_UID/APP_GID.
RUN useradd -m -u 1000 -s /bin/bash app \
    && mkdir -p /app/data /home/app/.claude /home/app/.cache/uv \
    && chown -R app:app /app /home/app
ENV HOME=/home/app \
    APP_UID=1000 \
    APP_GID=1000 \
    UV_CACHE_DIR=/home/app/.cache/uv

# Expose the port (default 8000; overridable via PORT env var at runtime).
EXPOSE 8000

# Run the app with Uvicorn and honor PORT env var.
# exec ensures SIGTERM from docker stop reaches uvicorn.
#
# Optional start-time hook for private/corporate setup that must run on every
# container start (e.g. writing ~/.claude/settings.json). Point
# GATEWAY_STARTUP_SCRIPT at a script (bind-mounted, or added via the build-time
# install hook) and it runs — as the unprivileged app user, fail-fast — before
# plugins + server, so overlays don't have to rewrite this CMD. Unset = no-op.
# Mirrors the build-time GATEWAY_BUILD_INSTALL_SCRIPT hook above.
ENTRYPOINT ["python", "/usr/local/bin/docker-entrypoint.py"]
CMD ["sh", "-c", "if [ -n \"$GATEWAY_STARTUP_SCRIPT\" ]; then python \"$GATEWAY_STARTUP_SCRIPT\" || exit 1; fi; python /usr/local/bin/install_plugins.py && exec python -m uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
