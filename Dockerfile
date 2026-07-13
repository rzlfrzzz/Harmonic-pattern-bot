# ==========================================================
# Harmonic Pattern Telegram Bot — Dockerfile
# ==========================================================
FROM python:3.11-slim AS base

# Prevent .pyc files / enable unbuffered stdout for clean docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps needed to build some scientific-python wheels on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy application code
COPY . .

# Non-root user for security
RUN useradd --create-home --shell /bin/bash botuser \
    && mkdir -p /app/logs \
    && chown -R botuser:botuser /app
USER botuser

# config.yaml and logs are meant to be mounted from the host (see docker-compose.yml)
VOLUME ["/app/config", "/app/logs"]

# Basic liveness check: process is alive and log file is being written to.
# (The bot has no HTTP server, so we check the log file's mtime instead.)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD find /app/logs/bot.log -mmin -10 | grep -q . || exit 1

ENTRYPOINT ["python", "main.py"]
