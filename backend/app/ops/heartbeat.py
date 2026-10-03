"""Worker heartbeat and queue depth, read from Redis.

Celery beat schedules `ops.heartbeat` every minute and a worker stamps the time in Redis, so a
fresh stamp proves beat, the broker and at least one worker are all running. `/healthz/worker`
turns a stale stamp into HTTP 503 for an external uptime monitor; nothing inside the app can
alert about the worker being down, because the worker is what sends alerts.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import cast

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.core.config import get_settings

HEARTBEAT_KEY = "trekiva:worker:heartbeat"
STALE_AFTER_SECONDS = 180
QUEUES = ("default", "webhooks", "ops")


@lru_cache
def _client() -> redis.Redis:
    # Fail fast, no retries: callers are health checks, and a slow answer is a wrong answer.
    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_timeout=2,
        socket_connect_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )


def stamp() -> None:
    _client().set(HEARTBEAT_KEY, str(time.time()), ex=86400)


def age_seconds() -> float | None:
    """Seconds since the last heartbeat; None if there has never been one (or it expired)."""
    raw = cast("bytes | str | None", _client().get(HEARTBEAT_KEY))
    if raw is None:
        return None
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    return max(0.0, time.time() - float(value))


def queue_lengths() -> dict[str, int]:
    """Messages waiting in each Celery queue (Redis lists named after the queue)."""
    client = _client()
    return {queue: cast(int, client.llen(queue)) for queue in QUEUES}
