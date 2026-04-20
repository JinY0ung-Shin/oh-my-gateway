FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*

# Install uv (pinned for reproducible builds; bump manually when upgrading)
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /usr/local/bin/

# Note: Claude Code CLI is bundled with claude-agent-sdk >= 0.1.8
# No separate Node.js/npm installation required

# Copy the app code
COPY . /app

# Set working directory
WORKDIR /app

# Optional: point uv at a private/internal index (e.g. a Nexus PyPI mirror).
# Pass via: docker compose build (values inherited from host shell env).
# UV_INDEX_STRATEGY=unsafe-best-match lets uv.lock entries (pypi.org URLs)
# resolve against the mirror when the mirror serves identical wheels.
ARG UV_INDEX_URL=
ARG UV_EXTRA_INDEX_URL=
ARG UV_INDEX_STRATEGY=

# Install Python dependencies with uv.
# Scope the UV_* env vars to this RUN so they do not persist into the runtime image.
RUN UV_INDEX_URL="$UV_INDEX_URL" \
    UV_EXTRA_INDEX_URL="$UV_EXTRA_INDEX_URL" \
    UV_INDEX_STRATEGY="$UV_INDEX_STRATEGY" \
    uv sync

# Expose the port (default 8000; overridable via PORT env var at runtime)
EXPOSE 8000

# Run the app with Uvicorn — honor PORT env var if set.
# `exec` ensures SIGTERM from `docker stop` reaches uvicorn (dash as PID 1 does not forward signals otherwise).
CMD ["sh", "-c", "exec uv run uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
