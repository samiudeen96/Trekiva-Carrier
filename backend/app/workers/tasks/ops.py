from __future__ import annotations

from app.alerts.dispatch import dispatch_pending
from app.ops import heartbeat as worker_heartbeat
from app.ops.checks import run_health_checks
from app.workers.celery_app import celery_app


@celery_app.task(name="alerts.dispatch")
def dispatch_alerts() -> dict[str, int]:
    return dispatch_pending()


@celery_app.task(name="ops.health_check")
def health_check() -> dict[str, int]:
    return run_health_checks()


@celery_app.task(name="ops.heartbeat")
def heartbeat() -> None:
    worker_heartbeat.stamp()
