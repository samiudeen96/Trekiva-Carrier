"""Celery wrappers around the logistics steps. Logic lives in `app.logistics`; these only map a
`StepResult.retry_in` onto a Celery retry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from celery import Task

from app.logistics import allocation_run, review_checks, shipments, shopify_sync, sweeper
from app.logistics.results import StepResult
from app.tracking import service as tracking
from app.workers.celery_app import celery_app

MAX_RETRIES = 6


def _run(task: Task, fn: Callable[..., StepResult], *args: Any, **kwargs: Any) -> str:
    result = fn(*args, **kwargs)
    if result.retry_in is not None and task.request.retries < MAX_RETRIES:
        raise task.retry(countdown=result.retry_in)
    return result.outcome


@celery_app.task(bind=True, name="logistics.allocate", max_retries=MAX_RETRIES)
def allocate(self: Task, fo_id: int, trigger: str = "queue") -> str:
    return _run(
        self,
        allocation_run.run_allocation,
        fo_id,
        trigger=trigger,
        is_retry=self.request.retries > 0,
    )


@celery_app.task(bind=True, name="logistics.create_shipment", max_retries=MAX_RETRIES)
def create_shipment(self: Task, fo_id: int) -> str:
    return _run(self, shipments.create_shipment, fo_id)


@celery_app.task(bind=True, name="logistics.reconcile", max_retries=MAX_RETRIES)
def reconcile(self: Task, shipment_id: int) -> str:
    return _run(self, shipments.reconcile_shipment, shipment_id)


@celery_app.task(bind=True, name="logistics.sync_shopify", max_retries=MAX_RETRIES)
def sync_shopify(self: Task, shipment_id: int) -> str:
    return _run(self, shopify_sync.sync_to_shopify, shipment_id, attempt=self.request.retries)


@celery_app.task(bind=True, name="logistics.place_review_hold", max_retries=MAX_RETRIES)
def place_review_hold(self: Task, fo_id: int) -> str:
    return _run(self, review_checks.place_review_hold, fo_id, attempt=self.request.retries)


@celery_app.task(bind=True, name="logistics.push_tracking", max_retries=MAX_RETRIES)
def push_tracking(self: Task, shipment_id: int) -> str:
    return _run(self, shopify_sync.push_tracking, shipment_id)


@celery_app.task(bind=True, name="logistics.cancel_shipment", max_retries=MAX_RETRIES)
def cancel_shipment(self: Task, shipment_id: int, reallocate: bool, actor: str, reason: str) -> str:
    return _run(
        self,
        shipments.cancel_shipment,
        shipment_id,
        reallocate=reallocate,
        actor=actor,
        reason=reason,
        attempt=self.request.retries,
    )


@celery_app.task(bind=True, name="tracking.poll", max_retries=0)
def poll(self: Task, shipment_id: int) -> str:
    return tracking.poll_shipment(shipment_id).outcome


@celery_app.task(name="logistics.dispatch_awaiting")
def dispatch_awaiting() -> int:
    return sweeper.dispatch_awaiting_allocation()


@celery_app.task(name="logistics.sweep")
def sweep() -> dict[str, int]:
    return sweeper.sweep()


@celery_app.task(name="tracking.poll_due")
def poll_due() -> int:
    return sweeper.due_polls()
