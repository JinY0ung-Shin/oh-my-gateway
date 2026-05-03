FROM python:3.12-slim-trixie AS opencode-builder

ARG APT_MIRROR_URL=
ARG APT_SECURITY_MIRROR_URL=

COPY docker/apt_mirror_sources.sh /usr/local/bin/apt_mirror_sources.sh

RUN sh /usr/local/bin/apt_mirror_sources.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

ARG OPENCODE_VERSION=1.14.29
ARG NPM_CONFIG_REGISTRY=

# Install OpenCode through npm in the builder, then keep only the native binary.
RUN if [ -n "$NPM_CONFIG_REGISTRY" ]; then \
        npm install -g --include=optional --registry "$NPM_CONFIG_REGISTRY" "opencode-ai@${OPENCODE_VERSION}"; \
    else \
        npm install -g --include=optional "opencode-ai@${OPENCODE_VERSION}"; \
    fi \
    && opencode --version \
    && install -m 0755 /usr/local/lib/node_modules/opencode-ai/bin/.opencode /usr/local/bin/opencode \
    && /usr/local/bin/opencode --version

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

# Install OpenCode for the managed OpenCode backend.
COPY --from=opencode-builder /usr/local/bin/opencode /usr/local/bin/opencode
RUN opencode --version

# Install uv for skills/scripts that invoke `uv run` inside the container.
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /usr/local/bin/
RUN uv --version

WORKDIR /app

# Optional: point pip at a private/corporate PyPI mirror.
ARG PIP_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=

# Install pinned runtime dependencies exported from uv.lock.
COPY requirements.txt ./
RUN PIP_INDEX_URL="$PIP_INDEX_URL" \
    PIP_EXTRA_INDEX_URL="$PIP_EXTRA_INDEX_URL" \
    python -m pip install --upgrade pip \
    && PIP_INDEX_URL="$PIP_INDEX_URL" \
    PIP_EXTRA_INDEX_URL="$PIP_EXTRA_INDEX_URL" \
    python -m pip install -r requirements.txt

# Copy runtime files used by the app.
COPY src ./src
# The admin system-prompt UI loads these templates at runtime.
COPY docs/*system-prompt*.md ./docs/

# Expose the port (default 8000; overridable via PORT env var at runtime).
EXPOSE 8000

# Run the app with Uvicorn and honor PORT env var.
# exec ensures SIGTERM from docker stop reaches uvicorn.
CMD ["sh", "-c", "exec python -m uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
