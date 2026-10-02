"""Order evaluation tasks: on-demand sync plus scheduled re-checks."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import Task
from sqlalchemy import select

from app.core.db import session_scope
from app.core.enums import LogisticsStatus
from app.core.time import utcnow
from app.logistics.pipeline import sync_order
from app.models import Shop, ShopifyFulfillmentOrder, ShopifyOrder
from app.services import shops
from app.shopify.errors import ShopifyError
from app.workers.celery_app import celery_app
from app.workers.enqueue import enqueue_order_sync
from app.workers.retry import MAX_RETRIES, backoff_seconds

log = logging.getLogger(__name__)

REVIEW_RECHECK_AFTER = timedelta(minutes=5)


@celery_app.task(bind=True, name="orders.sync", max_retries=MAX_RETRIES)
def sync_order_task(self: Task, shop_id: int, order_gid: str, trigger: str) -> int:
    try:
        with session_scope() as db:
            shop = db.get(Shop, shop_id)
            if shop is None or not shop.is_installed:
                return 0
            return len(
                sync_order(db, shop, shops.shopify_admin(db, shop), order_gid, trigger=trigger)
            )
    except ShopifyError as exc:
        if exc.retryable and self.request.retries < MAX_RETRIES:
            raise self.retry(countdown=backoff_seconds(self.request.retries)) from exc
        raise


def _enqueue_for(
    statuses: list[LogisticsStatus],
    *,
    due_only: bool,
    synced_before: timedelta | None,
    trigger: str,
) -> int:
    now = utcnow()
    with session_scope() as db:
        stmt = (
            select(ShopifyOrder.shop_id, ShopifyOrder.shopify_order_id)
            .join(ShopifyFulfillmentOrder, ShopifyFulfillmentOrder.order_id == ShopifyOrder.id)
            .join(Shop, Shop.id == ShopifyOrder.shop_id)
            .where(
                ShopifyFulfillmentOrder.logistics_status.in_(statuses),
                Shop.uninstalled_at.is_(None),
            )
            .distinct()
            .limit(500)
        )
        if due_only:
            stmt = stmt.where(ShopifyFulfillmentOrder.next_check_at <= now)
        if synced_before is not None:
            stmt = stmt.where(ShopifyFulfillmentOrder.last_synced_at < now - synced_before)
        rows = db.execute(stmt).all()
    for shop_id, gid in rows:
        enqueue_order_sync(shop_id, gid, trigger)
    return len(rows)


@celery_app.task(name="orders.evaluate_due")
def evaluate_due() -> int:
    """Re-check fulfillment orders whose settle window / risk wait has elapsed."""
    return _enqueue_for(
        [LogisticsStatus.SETTLING], due_only=True, synced_before=None, trigger="schedule:settle"
    )


@celery_app.task(name="orders.reconcile_review_queue")
def reconcile_review_queue() -> int:
    """Safety net for missed `fulfillment_orders/hold_released` webhooks: re-read held orders."""
    return _enqueue_for(
        [LogisticsStatus.MANUAL_REVIEW],
        due_only=False,
        synced_before=REVIEW_RECHECK_AFTER,
        trigger="schedule:review-reconcile",
    )
