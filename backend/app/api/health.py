from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine
from app.ops import heartbeat

router = APIRouter(tags=["health"])


@router.get("/healthz")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readiness() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        import redis

        redis.Redis.from_url(get_settings().redis_url, socket_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", **checks}, status_code=200 if healthy else 503
    )


@router.get("/healthz/worker")
def worker_liveness() -> JSONResponse:
    """503 unless a Celery worker ran the beat-scheduled heartbeat in the last 3 minutes, i.e.
    beat, Redis and a worker are all up. Point an external uptime monitor here: when the worker
    is down, the app cannot send alerts itself."""
    try:
        age = heartbeat.age_seconds()
    except Exception as exc:
        return JSONResponse({"status": "unknown", "redis": f"error: {type(exc).__name__}"}, 503)
    if age is None or age > heartbeat.STALE_AFTER_SECONDS:
        return JSONResponse(
            {"status": "stale", "heartbeat_age_seconds": None if age is None else round(age)}, 503
        )
    return JSONResponse({"status": "ok", "heartbeat_age_seconds": round(age)})
