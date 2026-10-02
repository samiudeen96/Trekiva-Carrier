# syntax=docker/dockerfile:1.7
# Multi-stage build: admin UI (Node) -> Python runtime. One image runs api, worker and beat.

# --- Admin UI ------------------------------------------------------------------------------
FROM node:20-alpine AS ui
WORKDIR /ui
COPY admin-ui/package.json admin-ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY admin-ui/ ./
RUN npm run build

# --- Python base ---------------------------------------------------------------------------
FROM python:3.12-slim AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend
RUN useradd --create-home --uid 10001 trekiva
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# --- Development / test (backend only, dev tools, source bind-mounted by compose) ----------
FROM python-base AS dev
COPY backend/requirements-dev.txt ./requirements-dev.txt
RUN pip install -r requirements-dev.txt
COPY backend/ ./
USER trekiva
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --- Production ----------------------------------------------------------------------------
FROM python-base AS prod
COPY backend/ ./
COPY --from=ui /ui/dist /app/admin-ui/dist
ENV ADMIN_UI_DIST=/app/admin-ui/dist APP_ENV=production
USER trekiva
EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "3", "-b", "0.0.0.0:8000", "--timeout", "60", "--graceful-timeout", "30", "--access-logfile", "-"]
