# ==============================================================================
# Telegram Membership Bot - Production Dockerfile
# ==============================================================================
FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG APP_VERSION="Unknown"
ARG APP_DATE="Unknown"
ENV BOT_APP_VERSION=$APP_VERSION
ENV BOT_APP_DATE=$APP_DATE

WORKDIR /app

# Install system dependencies (build-essential/curl if needed, ca-certificates for HTTPS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user and persistent data directory
RUN groupadd -g 1001 botgroup && \
    useradd -u 1001 -g botgroup -m -s /bin/bash botuser && \
    mkdir -p /app/data && \
    chown -R botuser:botgroup /app

# Copy application source code
COPY --chown=botuser:botgroup bot/ /app/bot/

# Volume mount for persistent SQLite DB and WAL files
VOLUME ["/app/data"]

# Switch to non-root user
USER botuser

# Run bot application module
CMD ["python", "-m", "bot.main"]
