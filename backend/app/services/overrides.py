"""Staff overrides: re-run allocation, pick a carrier, toggle automation per order.

Overrides never bypass a Shopify hold: allocation and shipment creation re-run the gate against
live Shopify data. Changing carrier requires that no active shipment exists (cancel it first).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.carriers import registry
from app.core.enums import LogisticsStatus
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.logistics.allocation_run import active_shipment, lock_fo, log, move
from app.models import Shop, ShopifyFulfillmentOrder, ShopifyOrder
from app.services import audit, carriers
from app.workers import enqueue

REALLOCATABLE = frozenset(
    {
        LogisticsStatus.AWAITING_ALLOCATION,
        LogisticsStatus.NO_CARRIER_AVAILABLE,
        LogisticsStatus.FAILED,
        LogisticsStatus.ALLOCATED,
        LogisticsStatus.AUTOMATION_DISABLED,
    }
)


def get_fo(db: Session, shop: Shop, fo_id: int, *, lock: bool = False) -> ShopifyFulfillmentOrder:
    fo = lock_fo(db, fo_id) if lock else db.get(ShopifyFulfillmentOrder, fo_id)
    if fo is None or fo.shop_id != shop.id:
        raise NotFoundError("Fulfillment order not found")
    return fo


def request_allocation(
    db: Session, shop: Shop, fo_id: int, *, actor: str, carrier_code: str | None = None
) -> ShopifyFulfillmentOrder:
    fo = get_fo(db, shop, fo_id, lock=True)
    if (shipment := active_shipment(db, fo.id)) is not None:
        raise ConflictError(
            f"{shipment.carrier_code} shipment {shipment.awb or shipment.status.value} is active. "
            "Cancel it before choosing another carrier."
        )
    if fo.logistics_status not in REALLOCATABLE:
        raise ConflictError(
            f"Cannot allocate a fulfillment order in status {fo.logistics_status.value}"
        )
    if carrier_code is not None:
        if carrier_code not in registry.registered_codes():
            raise ValidationFailed(f"Unknown carrier {carrier_code}")
        if carriers.active_account(db, shop, carrier_code) is None:
            raise ValidationFailed(f"{carrier_code} has no active account")
    fo.forced_carrier_code = carrier_code
    fo.selected_by = actor
    fo.allocation_ranking = []
    what = f"manual carrier selection: {carrier_code}" if carrier_code else "re-run allocation"
    move(fo, LogisticsStatus.AWAITING_ALLOCATION, "STAFF_REQUEST", f"Staff requested {what}")
    log(db, fo, audit.Step.STAFF_ACTION, f"staff requested {what}", actor=actor)
    enqueue.enqueue_after_commit(db, enqueue.ALLOCATE, fo.id, f"staff:{actor}")
    return fo


def set_order_automation(
    db: Session, shop: Shop, order_id: int, *, disabled: bool, actor: str
) -> ShopifyOrder:
    order = db.get(ShopifyOrder, order_id)
    if order is None or order.shop_id != shop.id:
        raise NotFoundError("Order not found")
    order.automation_disabled = disabled
    audit.record(
        db,
        shop_id=shop.id,
        order_id=order.id,
        step=audit.Step.AUTOMATION_DISABLED if disabled else audit.Step.AUTOMATION_ENABLED,
        message=f"{order.name}: automation {'disabled' if disabled else 'enabled'} by staff",
        actor=actor,
    )
    enqueue.enqueue_after_commit(
        db, enqueue.ORDER_SYNC, shop.id, order.shopify_order_id, f"staff:{actor}"
    )
    return order
