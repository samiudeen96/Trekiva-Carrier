from __future__ import annotations

from celery import Task

from app.workers.celery_app import celery_app
from app.workers.retry import MAX_RETRIES, backoff_seconds


@celery_app.task(bind=True, name="webhooks.process", max_retries=MAX_RETRIES)
def process_webhook_event(self: Task, event_id: int) -> str:
    from app.webhooks.processor import process_event

    result = process_event(event_id)
    if result.retry and self.request.retries < MAX_RETRIES:
        raise self.retry(countdown=backoff_seconds(self.request.retries))
    return result.status.value if result.status else "not-claimed"
