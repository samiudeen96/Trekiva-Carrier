"""Record carrier tracking updates (webhook or poll) and advance shipments.

Every event is stored raw + normalised and deduplicated on (awb, raw status, time). It changes
the shipment only if `can_apply_tracking` allows (out-of-order and duplicate events are kept for
audit but ignored). Applied changes are pushed to Shopify as fulfillment events.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import alerts
from app.carriers.errors import CarrierError, CarrierNotSupportedError
from app.carriers.types import ShipmentRef, TrackingUpdate
from app.core.db import session_scope
from app.core.enums import (
    LogisticsStatus,
    LogLevel,
    ShipmentStatus,
    ShopifySyncStatus,
    TrackingSource,
    TrackingStatus,
)
from app.core.time import utcnow
from app.logistics.results import StepResult
from app.logistics.state import can_transition
from app.models import Shipment, Shop, ShopifyFulfillmentOrder, TrackingEvent
from app.schemas.settings import ShopSettings
from app.services import audit
from app.tracking.transitions import TERMINAL, can_apply_tracking, to_shipment_status
from app.workers import enqueue

log = logging.getLogger(__name__)

_POLL_EVERY: dict[ShipmentStatus, timedelta] = {
    ShipmentStatus.AWB_CREATED: timedelta(hours=3),
    ShipmentStatus.PICKUP_SCHEDULED: timedelta(hours=3),
    ShipmentStatus.PICKED_UP: timedelta(hours=2),
    ShipmentStatus.IN_TRANSIT: timedelta(hours=2),
    ShipmentStatus.OUT_FOR_DELIVERY: timedelta(minutes=30),
    ShipmentStatus.DELIVERY_FAILED: timedelta(hours=2),
    ShipmentStatus.EXCEPTION: timedelta(hours=2),
    ShipmentStatus.RTO_INITIATED: timedelta(hours=6),
    ShipmentStatus.RTO_IN_TRANSIT: timedelta(hours=6),
}


def schedule_next_poll(shipment: Shipment) -> None:
    interval = _POLL_EVERY.get(shipment.status)
    shipment.next_poll_at = (
        utcnow() + interval if interval and shipment.status not in TERMINAL else None
    )


def dedup_hash(update: TrackingUpdate) -> str:
    key = f"{update.awb}|{update.raw_status.strip().upper()}|{update.occurred_at.isoformat()}"
    return hashlib.sha256(key.encode()).hexdigest()


def _insert_event(
    db: Session, shipment: Shipment, update: TrackingUpdate, source: TrackingSource
) -> TrackingEvent | None:
    event_id = db.execute(
        insert(TrackingEvent)
        .values(
            shipment_id=shipment.id,
            carrier_code=shipment.carrier_code,
            awb=update.awb,
            raw_status=update.raw_status[:128],
            normalized_status=update.status.value,
            description=update.description,
            location=update.location,
            occurred_at=update.occurred_at,
            source=source.value,
            dedup_hash=dedup_hash(update),
            raw=update.raw,
            applied=False,
        )
        .on_conflict_do_nothing(
            index_elements=[TrackingEvent.shipment_id, TrackingEvent.dedup_hash]
        )
        .returning(TrackingEvent.id)
    ).scalar_one_or_none()
    return db.get(TrackingEvent, event_id) if event_id is not None else None


def record_system_event(
    db: Session, shipment: Shipment, status: TrackingStatus, description: str
) -> None:
    """Internal milestone (e.g. AWB created) so the tracking timeline starts at creation."""
    assert shipment.awb
    event = _insert_event(
        db,
        shipment,
        TrackingUpdate(
            awb=shipment.awb,
            raw_status=status.value,
            status=status,
            occurred_at=utcnow(),
            description=description,
        ),
        TrackingSource.SYSTEM,
    )
    if event is not None:
        event.applied = True


def record_updates(
    db: Session,
    shipment: Shipment,
    updates: Iterable[TrackingUpdate],
    source: TrackingSource,
    *,
    actor: str = "system",
) -> list[TrackingEvent]:
    """Store updates and advance the (locked) shipment. Returns the events that changed it."""
    shop = db.get(Shop, shipment.shop_id)
    fo = db.get(ShopifyFulfillmentOrder, shipment.fulfillment_order_id)
    assert shop is not None and fo is not None
    settings = ShopSettings.load(shop.settings)
    applied: list[TrackingEvent] = []
    for update in sorted(updates, key=lambda u: u.occurred_at):
        if shipment.awb and update.awb != shipment.awb:
            log.warning(
                "Tracking update for another AWB ignored",
                extra={"awb": update.awb, "shipment": shipment.id},
            )
            continue
        event = _insert_event(db, shipment, update, source)
        if event is None:
            continue  # duplicate
        new_status = to_shipment_status(update.status)
        if not can_apply_tracking(shipment.status, new_status):
            continue
        previous = shipment.status
        event.applied = True
        applied.append(event)
        shipment.status = new_status
        shipment.last_tracking_at = update.occurred_at
        if update.edd:
            shipment.estimated_delivery_date = update.edd
        if new_status == ShipmentStatus.DELIVERED:
            shipment.delivered_at = update.occurred_at
        audit.record(
            db,
            shop_id=shop.id,
            order_id=shipment.order_id,
            fulfillment_order_id=fo.id,
            shipment_id=shipment.id,
            step=audit.Step.TRACKING_UPDATED,
            level=LogLevel.WARNING if new_status in _WARN else LogLevel.INFO,
            message=f"{shipment.shopify_order_name}: AWB {shipment.awb} {previous.value} → {new_status.value} "
            f"({shipment.carrier_code} '{update.raw_status}'{', ' + update.location if update.location else ''})",
            data={"source": source.value, "raw": update.raw},
            actor=actor,
        )
        if new_status == ShipmentStatus.CANCELLED:
            _carrier_cancelled(db, shipment, fo)
        if (
            new_status == ShipmentStatus.PICKED_UP
            and settings.fulfill_on == "PICKED_UP"
            and shipment.shopify_sync_status != ShopifySyncStatus.SYNCED
        ):
            enqueue.enqueue_after_commit(db, enqueue.SYNC_SHOPIFY, shipment.id)
    schedule_next_poll(shipment)
    if applied:
        enqueue.enqueue_after_commit(db, enqueue.PUSH_TRACKING, shipment.id)
    return applied


_WARN = {
    ShipmentStatus.DELIVERY_FAILED,
    ShipmentStatus.RTO_INITIATED,
    ShipmentStatus.RTO_IN_TRANSIT,
    ShipmentStatus.RTO_DELIVERED,
    ShipmentStatus.EXCEPTION,
    ShipmentStatus.CANCELLED,
}


def _carrier_cancelled(db: Session, shipment: Shipment, fo: ShopifyFulfillmentOrder) -> None:
    shipment.cancelled_at = shipment.cancelled_at or utcnow()
    shipment.cancel_reason = shipment.cancel_reason or "Cancelled by carrier"
    if fo.logistics_status == LogisticsStatus.SHIPPED and can_transition(
        fo.logistics_status, LogisticsStatus.FAILED
    ):
        fo.logistics_status = LogisticsStatus.FAILED
        fo.status_reason = "CARRIER_CANCELLED"
        fo.status_detail = (
            f"{shipment.carrier_code} cancelled AWB {shipment.awb}. Re-allocate or pick a carrier."
        )


def poll_shipment(shipment_id: int) -> StepResult:
    from app.logistics.shipments import lock_shipment
    from app.services.carriers import adapter_for_shipment

    with session_scope() as db:
        shipment = db.get(Shipment, shipment_id)
        if shipment is None or shipment.status in TERMINAL or not shipment.awb:
            return StepResult("skip")
        adapter = adapter_for_shipment(db, shipment)
        ref = ShipmentRef(
            idempotency_key=shipment.idempotency_key,
            carrier_shipment_id=shipment.carrier_shipment_id,
            awb=shipment.awb,
        )
    try:
        updates = adapter.track_shipment(ref)
    except CarrierNotSupportedError:
        with session_scope() as db:
            lock_shipment(db, shipment_id).next_poll_at = None
        return StepResult("unsupported")
    except CarrierError as exc:
        with session_scope() as db:
            s = lock_shipment(db, shipment_id)
            s.next_poll_at = utcnow() + timedelta(minutes=30)
            alerts.carrier_error(
                db,
                shop_id=s.shop_id,
                carrier_code=s.carrier_code,
                error_class=exc.error_class,
                message=exc.message,
                during="tracking poll",
                order_id=s.order_id,
                fulfillment_order_id=s.fulfillment_order_id,
                shipment_id=s.id,
            )
        return StepResult("error", detail=exc.message)
    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        applied = record_updates(db, shipment, updates, TrackingSource.POLL)
        return StepResult("polled", detail=f"{len(applied)} new")
