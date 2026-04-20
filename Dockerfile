FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends jq && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Note: Claude Code CLI is bundled with claude-agent-sdk >= 0.1.8
# No separate Node.js/npm installation required

# Copy the app code
COPY . /app

# Set working directory
WORKDIR /app

# Install Python dependencies with uv
RUN uv sync

# Expose the port (default 8000; overridable via PORT env var at runtime)
EXPOSE 8000

# Run the app with Uvicorn — honor PORT env var if set
CMD ["sh", "-c", "uv run uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
