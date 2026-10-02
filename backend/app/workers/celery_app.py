"""Celery application: worker settings and the beat schedule.

Run:
    celery -A app.workers.celery_app worker -Q default,webhooks -l info
    celery -A app.workers.celery_app beat -l info
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging, worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging

_settings = get_settings()

celery_app = Celery(
    "trekiva",
    broker=_settings.redis_url,
    include=[
        "app.workers.tasks.webhooks",
        "app.workers.tasks.orders",
        "app.workers.tasks.maintenance",
        "app.workers.tasks.logistics",
    ],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    task_default_queue="default",
    task_routes={"webhooks.*": {"queue": "webhooks"}},
    task_time_limit=300,
    task_soft_time_limit=240,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 3600},
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "evaluate-due-fulfillment-orders": {"task": "orders.evaluate_due", "schedule": 60.0},
        "reconcile-manual-review": {"task": "orders.reconcile_review_queue", "schedule": 600.0},
        "sweep-webhook-events": {"task": "maintenance.sweep_webhooks", "schedule": 120.0},
        "dispatch-awaiting-allocation": {"task": "logistics.dispatch_awaiting", "schedule": 60.0},
        "sweep-shipments": {"task": "logistics.sweep", "schedule": 300.0},
        "poll-tracking": {"task": "tracking.poll_due", "schedule": 300.0},
    },
)


@setup_logging.connect
def _setup_logging(**_: Any) -> None:
    """Use our (JSON) logging instead of Celery's. Configured once in the main process and
    inherited by worker processes, so task success/failure lines are kept."""
    configure_logging(_settings.log_level, json_output=_settings.log_json)


@worker_process_init.connect
def _init_worker_process(**_: Any) -> None:
    """Each forked worker process builds its own DB engine (connections must not cross forks)."""
    from app.core import db

    db.get_engine.cache_clear()
    db.set_session_factory(None)
