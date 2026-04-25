FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Install system dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*

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
