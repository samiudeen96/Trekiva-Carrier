"""Database-driven safety nets, run by Celery beat.

Enqueued tasks can be lost (Redis restart) and workers can die mid-step. Every in-flight state is
therefore re-discovered from PostgreSQL and resumed. All resumed steps are idempotent.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import or_, select

from app.core.db import session_scope
from app.core.enums import LogisticsStatus, LogLevel, ShipmentStatus, ShopifySyncStatus
from app.core.time import utcnow
from app.models import Shipment, Shop, ShopifyFulfillmentOrder
from app.services import audit
from app.workers import enqueue

STALE = timedelta(minutes=10)
DISPATCH_IDLE = timedelta(minutes=2)
_LIMIT = 200
_STAFF = ShopifyFulfillmentOrder.selected_by.like("staff%")


def dispatch_awaiting_allocation() -> int:
    """Enqueue allocation for ready orders (e.g. after shop automation was switched on)."""
    now = utcnow()
    with session_scope() as db:
        ids = list(
            db.scalars(
                select(ShopifyFulfillmentOrder.id)
                .join(Shop, Shop.id == ShopifyFulfillmentOrder.shop_id)
                .where(
                    ShopifyFulfillmentOrder.logistics_status == LogisticsStatus.AWAITING_ALLOCATION,
                    Shop.uninstalled_at.is_(None),
                    or_(Shop.automation_enabled.is_(True), _STAFF),
                    or_(
                        ShopifyFulfillmentOrder.last_evaluated_at.is_(None),
                        ShopifyFulfillmentOrder.last_evaluated_at < now - DISPATCH_IDLE,
                    ),
                )
                .limit(_LIMIT)
            )
        )
    for fo_id in ids:
        enqueue.enqueue(enqueue.ALLOCATE, fo_id, "dispatcher")
    return len(ids)


def sweep() -> dict[str, int]:
    now = utcnow()
    cutoff = now - STALE
    counts = {"allocating": 0, "allocated": 0, "creating": 0, "unknown": 0, "shopify": 0}
    with session_scope() as db:
        # Allocation interrupted mid-run: start over.
        for fo in db.scalars(
            select(ShopifyFulfillmentOrder)
            .where(
                ShopifyFulfillmentOrder.logistics_status == LogisticsStatus.ALLOCATING,
                ShopifyFulfillmentOrder.updated_at < cutoff,
            )
            .with_for_update(skip_locked=True)
            .limit(_LIMIT)
        ):
            fo.logistics_status = LogisticsStatus.AWAITING_ALLOCATION
            fo.status_reason = "ALLOCATION_RESTARTED"
            fo.status_detail = "Allocation was interrupted; restarting"
            counts["allocating"] += 1
            enqueue.enqueue_after_commit(db, enqueue.ALLOCATE, fo.id, "sweeper")

        # Allocated but the create task was lost.
        for fo_id in db.scalars(
            select(ShopifyFulfillmentOrder.id)
            .join(Shop, Shop.id == ShopifyFulfillmentOrder.shop_id)
            .where(
                ShopifyFulfillmentOrder.logistics_status == LogisticsStatus.ALLOCATED,
                ShopifyFulfillmentOrder.updated_at < cutoff,
                or_(Shop.automation_enabled.is_(True), _STAFF),
            )
            .limit(_LIMIT)
        ):
            counts["allocated"] += 1
            enqueue.enqueue_after_commit(db, enqueue.CREATE_SHIPMENT, fo_id)

        # Worker died between reserving and recording: outcome unknown -> reconcile.
        for shipment in db.scalars(
            select(Shipment)
            .where(Shipment.status == ShipmentStatus.CREATING, Shipment.updated_at < cutoff)
            .with_for_update(skip_locked=True)
            .limit(_LIMIT)
        ):
            shipment.status = ShipmentStatus.CREATION_UNKNOWN
            shipment.last_error = "Creation was interrupted; outcome unknown"
            owner = db.get(ShopifyFulfillmentOrder, shipment.fulfillment_order_id)
            if owner is not None and owner.logistics_status == LogisticsStatus.SHIPMENT_PENDING:
                owner.logistics_status = LogisticsStatus.RECONCILING
                owner.status_reason = "CREATION_UNKNOWN"
                owner.status_detail = (
                    "Shipment creation was interrupted; confirming with the carrier"
                )
            audit.record(
                db,
                shop_id=shipment.shop_id,
                order_id=shipment.order_id,
                fulfillment_order_id=shipment.fulfillment_order_id,
                shipment_id=shipment.id,
                step=audit.Step.SHIPMENT_UNCERTAIN,
                level=LogLevel.WARNING,
                message=f"{shipment.shopify_order_name}: shipment creation interrupted; reconciling with {shipment.carrier_code}",
            )
            counts["creating"] += 1
            enqueue.enqueue_after_commit(db, enqueue.RECONCILE, shipment.id)

        # Reconciliation that stopped retrying (not the ones waiting for staff).
        for shipment_id in db.scalars(
            select(Shipment.id)
            .join(
                ShopifyFulfillmentOrder, ShopifyFulfillmentOrder.id == Shipment.fulfillment_order_id
            )
            .where(
                Shipment.status == ShipmentStatus.CREATION_UNKNOWN,
                Shipment.updated_at < now - timedelta(minutes=15),
                Shipment.reconcile_attempts < 6,
                ShopifyFulfillmentOrder.status_reason != "RECONCILIATION_NEEDS_STAFF",
            )
            .limit(_LIMIT)
        ):
            counts["unknown"] += 1
            enqueue.enqueue_after_commit(db, enqueue.RECONCILE, shipment_id)

        # Created at the carrier but not yet on Shopify (sync task lost / transient errors).
        for shipment_id in db.scalars(
            select(Shipment.id)
            .where(
                Shipment.shopify_sync_status == ShopifySyncStatus.PENDING,
                Shipment.awb.is_not(None),
                Shipment.status.not_in(
                    [
                        ShipmentStatus.CANCELLED,
                        ShipmentStatus.CREATE_FAILED,
                        ShipmentStatus.CREATION_UNKNOWN,
                    ]
                ),
                Shipment.updated_at < cutoff,
            )
            .limit(_LIMIT)
        ):
            counts["shopify"] += 1
            enqueue.enqueue_after_commit(db, enqueue.SYNC_SHOPIFY, shipment_id)
    return counts


def due_polls() -> int:
    now = utcnow()
    with session_scope() as db:
        shipments = list(
            db.scalars(
                select(Shipment)
                .where(Shipment.next_poll_at.is_not(None), Shipment.next_poll_at <= now)
                .with_for_update(skip_locked=True)
                .limit(_LIMIT)
            )
        )
        for shipment in shipments:
            shipment.next_poll_at = now + timedelta(minutes=10)  # claim; the poll reschedules
            enqueue.enqueue_after_commit(db, enqueue.POLL_TRACKING, shipment.id)
    return len(shipments)
