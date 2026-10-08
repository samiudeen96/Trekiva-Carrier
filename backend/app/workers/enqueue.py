"""Enqueue helpers. Business logic never imports Celery tasks directly: it enqueues by task
name, ideally via `enqueue_after_commit` so workers only ever see committed state.

A failed enqueue (Redis down) is not fatal: every queue has a database-backed sweeper in the beat
schedule that re-enqueues work left behind.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import on_commit

log = logging.getLogger(__name__)

# Task names (kept here so logic modules do not import Celery task objects).
WEBHOOK_PROCESS = "webhooks.process"
ORDER_SYNC = "orders.sync"
ALLOCATE = "logistics.allocate"
CREATE_SHIPMENT = "logistics.create_shipment"
RECONCILE = "logistics.reconcile"
SYNC_SHOPIFY = "logistics.sync_shopify"
PUSH_TRACKING = "logistics.push_tracking"
CANCEL_SHIPMENT = "logistics.cancel_shipment"
POLL_TRACKING = "tracking.poll"
PLACE_REVIEW_HOLD = "logistics.place_review_hold"


def enqueue(task_name: str, *args: Any, countdown: float | None = None) -> bool:
    from app.workers.celery_app import celery_app

    try:
        celery_app.send_task(task_name, args=list(args), countdown=countdown)
        return True
    except Exception:
        log.warning(
            "Could not enqueue task; sweeper will retry", extra={"task": task_name, "args": args}
        )
        return False


def enqueue_after_commit(
    db: Session, task_name: str, *args: Any, countdown: float | None = None
) -> None:
    on_commit(db, lambda: enqueue(task_name, *args, countdown=countdown))


def enqueue_webhook_event(event_id: int) -> bool:
    return enqueue(WEBHOOK_PROCESS, event_id)


def enqueue_order_sync(shop_id: int, order_gid: str, trigger: str) -> bool:
    return enqueue(ORDER_SYNC, shop_id, order_gid, trigger)
