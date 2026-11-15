# LifeManager backend container image.
# Built and pushed to Cloudflare's registry by `wrangler deploy`
# (see wrangler.jsonc -> containers.image, build context = repo root).
# Also runnable standalone: docker build -t lifemanager . && docker run -p 8080:8080 lifemanager

# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# uv for reproducible installs from uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependencies first so this layer is cached across app-code changes.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

# Application code and raw SQL migrations (applied on startup in D1 mode).
COPY backend/app ./app
COPY backend/migrations ./migrations

RUN useradd --create-home appuser

# SQLite mode needs a writable location for the database file (D1 mode never
# touches this). Mount a volume at /data for local persistence.
ENV LIFEMANAGER_DB_PATH=/data/lifemanager.db
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

# Cloudflare Containers expect the app to listen on port 8080 and answer
# health checks (the Worker probes GET /api/health).
EXPOSE 8080

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
