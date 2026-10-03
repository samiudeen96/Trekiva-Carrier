"""Push shipments and tracking to Shopify.

    sync_to_shopify  - create the Shopify fulfillment carrying the AWB + tracking URL. Before
                       creating, look for an existing fulfillment with our AWB so a retried sync
                       after a timeout never fulfills twice.
    push_tracking    - add Shopify fulfillment events for applied carrier status changes.

A Shopify failure never touches the courier shipment: it is tracked in shopify_sync_status and
retried independently.
"""

from __future__ import annotations

from sqlalchemy import select

from app.carriers import registry
from app.core.db import session_scope
from app.core.enums import LogLevel, ShipmentStatus, ShopifySyncStatus, TrackingStatus
from app.core.time import utcnow
from app.logistics.allocation_run import log
from app.logistics.results import StepResult
from app.logistics.shipments import lock_shipment
from app.models import Shop, ShopifyFulfillmentOrder, TrackingEvent
from app.schemas.settings import ShopSettings
from app.services import audit, shops
from app.shopify.errors import ShopifyError, ShopifyUserError
from app.workers import enqueue
from app.workers.retry import backoff_seconds

#: Normalised status -> Shopify FulfillmentEventStatus (None: nothing to push).
SHOPIFY_EVENT_STATUS: dict[TrackingStatus, str | None] = {
    TrackingStatus.AWB_CREATED: None,  # the fulfillment itself carries the AWB
    TrackingStatus.PICKUP_SCHEDULED: None,
    TrackingStatus.PICKED_UP: "CARRIER_PICKED_UP",
    TrackingStatus.IN_TRANSIT: "IN_TRANSIT",
    TrackingStatus.OUT_FOR_DELIVERY: "OUT_FOR_DELIVERY",
    TrackingStatus.DELIVERED: "DELIVERED",
    TrackingStatus.DELIVERY_FAILED: "ATTEMPTED_DELIVERY",
    TrackingStatus.RTO_INITIATED: "FAILURE",
    TrackingStatus.RTO_IN_TRANSIT: "FAILURE",
    TrackingStatus.RTO_DELIVERED: "FAILURE",
    TrackingStatus.EXCEPTION: "DELAYED",
    TrackingStatus.CANCELLED: None,
}

NOT_SYNCABLE = {
    ShipmentStatus.CREATING,
    ShipmentStatus.CREATION_UNKNOWN,
    ShipmentStatus.CREATE_FAILED,
    ShipmentStatus.CANCELLED,
    ShipmentStatus.CANCEL_REQUESTED,
}
BEFORE_PICKUP = {ShipmentStatus.AWB_CREATED, ShipmentStatus.PICKUP_SCHEDULED}


def sync_to_shopify(
    shipment_id: int,
    *,
    admin_factory: shops.AdminFactory = shops.default_admin_factory,
    attempt: int = 0,
) -> StepResult:
    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        if shipment.shopify_fulfillment_id:
            return StepResult("already_synced")
        if shipment.status in NOT_SYNCABLE or not shipment.awb:
            return StepResult("not_syncable", detail=shipment.status.value)
        shop = db.get(Shop, shipment.shop_id)
        fo = db.get(ShopifyFulfillmentOrder, shipment.fulfillment_order_id)
        assert shop is not None and fo is not None
        settings = ShopSettings.load(shop.settings)
        if settings.fulfill_on == "PICKED_UP" and shipment.status in BEFORE_PICKUP:
            return StepResult("deferred_until_pickup")

        admin = admin_factory(db, shop)
        company = registry.describe(shipment.carrier_code).display_name
        try:
            fulfillment_id = admin.find_fulfillment_by_tracking(
                shipment.shopify_fulfillment_order_id, shipment.awb
            ) or admin.create_fulfillment(
                shipment.shopify_fulfillment_order_id,
                company=company,
                number=shipment.awb,
                url=shipment.tracking_url,
                notify_customer=settings.notify_customer,
            )
        except ShopifyUserError as exc:
            shipment.shopify_sync_status = ShopifySyncStatus.FAILED
            shipment.shopify_sync_error = str(exc)
            log(
                db,
                fo,
                audit.Step.SHOPIFY_SYNC_FAILED,
                f"Shopify rejected the fulfillment for AWB {shipment.awb}: {exc}. If a hold was placed after the "
                "AWB was created, release it or cancel the shipment.",
                level=LogLevel.ERROR,
                shipment_id=shipment.id,
            )
            return StepResult("rejected", detail=str(exc))
        except ShopifyError as exc:
            shipment.shopify_sync_error = str(exc)
            if exc.retryable:
                return StepResult("retry", detail=str(exc), retry_in=backoff_seconds(attempt))
            shipment.shopify_sync_status = ShopifySyncStatus.FAILED
            log(
                db,
                fo,
                audit.Step.SHOPIFY_SYNC_FAILED,
                f"Shopify sync failed: {exc}",
                level=LogLevel.ERROR,
                shipment_id=shipment.id,
            )
            return StepResult("failed", detail=str(exc))

        shipment.shopify_fulfillment_id = fulfillment_id
        shipment.shopify_sync_status = ShopifySyncStatus.SYNCED
        shipment.shopify_sync_error = None
        log(
            db,
            fo,
            audit.Step.SHOPIFY_SYNCED,
            f"Shopify tracking updated: {company} {shipment.awb}",
            shipment_id=shipment.id,
            data={"fulfillment_id": fulfillment_id, "notify_customer": settings.notify_customer},
        )
        if settings.shipped_tag:
            try:
                admin.add_tags(shipment.shopify_order_id, [settings.shipped_tag])
            except ShopifyError as exc:
                log(
                    db,
                    fo,
                    audit.Step.SHOPIFY_SYNC_FAILED,
                    f"could not add tag {settings.shipped_tag}: {exc}",
                    level=LogLevel.WARNING,
                    shipment_id=shipment.id,
                )
        enqueue.enqueue_after_commit(db, enqueue.PUSH_TRACKING, shipment.id)
        return StepResult("synced", detail=fulfillment_id)


def push_tracking(
    shipment_id: int, *, admin_factory: shops.AdminFactory = shops.default_admin_factory
) -> StepResult:
    with session_scope() as db:
        shipment = lock_shipment(db, shipment_id)
        if not shipment.shopify_fulfillment_id:
            return StepResult("no_fulfillment_yet")
        events = list(
            db.scalars(
                select(TrackingEvent)
                .where(
                    TrackingEvent.shipment_id == shipment.id,
                    TrackingEvent.applied.is_(True),
                    TrackingEvent.shopify_pushed_at.is_(None),
                )
                .order_by(TrackingEvent.occurred_at, TrackingEvent.id)
            )
        )
        shop = db.get(Shop, shipment.shop_id)
        fo = db.get(ShopifyFulfillmentOrder, shipment.fulfillment_order_id)
        assert shop is not None and fo is not None
        admin = admin_factory(db, shop)
        pushed = 0
        for event in events:
            status = SHOPIFY_EVENT_STATUS.get(event.normalized_status)
            if status is None:
                event.shopify_pushed_at = utcnow()  # nothing to push; mark handled
                continue
            message = event.description or event.raw_status
            if event.normalized_status in (
                TrackingStatus.RTO_INITIATED,
                TrackingStatus.RTO_IN_TRANSIT,
                TrackingStatus.RTO_DELIVERED,
            ):
                message = f"Return to origin: {event.normalized_status.value.replace('_', ' ').lower()} ({message})"
            try:
                admin.create_fulfillment_event(
                    shipment.shopify_fulfillment_id,
                    status=status,
                    happened_at=event.occurred_at,
                    message=message,
                    city=event.location,
                )
            except ShopifyError as exc:
                log(
                    db,
                    fo,
                    audit.Step.SHOPIFY_SYNC_FAILED,
                    f"tracking event {status} not sent to Shopify: {exc}",
                    level=LogLevel.WARNING,
                    shipment_id=shipment.id,
                )
                return StepResult(
                    "retry" if exc.retryable else "failed",
                    detail=str(exc),
                    retry_in=backoff_seconds(0) if exc.retryable else None,
                )
            event.shopify_pushed_at = utcnow()
            pushed += 1
        if pushed:
            log(
                db,
                fo,
                audit.Step.SHOPIFY_SYNCED,
                f"{pushed} tracking update(s) sent to Shopify",
                shipment_id=shipment.id,
            )
        return StepResult("pushed", detail=str(pushed))
