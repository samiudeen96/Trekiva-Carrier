from __future__ import annotations

from datetime import timedelta

from app.core.db import session_scope
from app.webhooks.processor import reset_stale_processing, unprocessed_event_ids
from app.workers.celery_app import celery_app
from app.workers.enqueue import enqueue_webhook_event


@celery_app.task(name="maintenance.sweep_webhooks")
def sweep_webhooks() -> int:
    """Re-enqueue webhook events that were stored but never processed (e.g. Redis was down
    when they arrived, or a worker died mid-processing)."""
    with session_scope() as db:
        reset_stale_processing(db)
        ids = unprocessed_event_ids(db, older_than=timedelta(minutes=2))
    for event_id in ids:
        enqueue_webhook_event(event_id)
    return len(ids)
