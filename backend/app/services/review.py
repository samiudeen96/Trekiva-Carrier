"""Manual review queue: fulfillment orders blocked by a Shopify hold or a review signal."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.enums import LogisticsStatus
from app.core.errors import ConflictError, NotFoundError
from app.core.time import utcnow
from app.logistics.hold_gate import OVERRIDABLE_REASONS, GateReason
from app.models import Shop, ShopifyFulfillmentOrder
from app.services import audit

_OVERRIDABLE = {r.value for r in OVERRIDABLE_REASONS}


def list_queue(db: Session, shop: Shop, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(ShopifyFulfillmentOrder)
        .options(joinedload(ShopifyFulfillmentOrder.order))
        .where(
            ShopifyFulfillmentOrder.shop_id == shop.id,
            ShopifyFulfillmentOrder.logistics_status == LogisticsStatus.MANUAL_REVIEW,
        )
        .order_by(ShopifyFulfillmentOrder.updated_at.desc())
        .limit(limit)
    ).all()
    return [_row(fo) for fo in rows]


def _row(fo: ShopifyFulfillmentOrder) -> dict[str, Any]:
    order = fo.order
    return {
        "fulfillment_order_id": fo.id,
        "shopify_fulfillment_order_id": fo.shopify_fulfillment_order_id,
        "order_id": order.id,
        "shopify_order_id": order.shopify_order_id,
        "order_name": order.name,
        "order_created_at": order.shopify_created_at.isoformat(),
        "tags": order.tags,
        "customer_name": order.customer_name,
        "phone": order.phone,
        "shipping_address": order.shipping_address,
        "payment_mode": order.payment_mode.value,
        "risk_level": order.risk_level,
        "shopify_status": fo.shopify_status,
        "hold_reasons": fo.hold_reasons,
        "reason": fo.status_reason,
        "detail": fo.status_detail,
        "review_flags": fo.review_flags,
        "is_shopify_hold": fo.status_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value,
        "can_override": fo.status_reason in _OVERRIDABLE,
        "line_items": [
            {"name": li.get("name"), "sku": li.get("sku"), "quantity": li.get("remaining_quantity")}
            for li in fo.line_items
        ],
        "updated_at": fo.updated_at.isoformat(),
    }


def approve(
    db: Session, shop: Shop, fulfillment_order_id: int, *, actor: str
) -> ShopifyFulfillmentOrder:
    """Staff approval for review signals that are NOT Shopify holds. The order is then
    re-evaluated against live Shopify data (a hold placed meanwhile still blocks)."""
    fo = db.scalar(
        select(ShopifyFulfillmentOrder)
        .where(
            ShopifyFulfillmentOrder.id == fulfillment_order_id,
            ShopifyFulfillmentOrder.shop_id == shop.id,
        )
        .with_for_update()
    )
    if fo is None:
        raise NotFoundError("Fulfillment order not found")
    if fo.logistics_status != LogisticsStatus.MANUAL_REVIEW:
        raise ConflictError("Fulfillment order is not in manual review")
    if fo.status_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value:
        raise ConflictError("This order is on hold in Shopify. Release the hold in Shopify admin.")
    if fo.status_reason not in _OVERRIDABLE:
        raise ConflictError(f"Reason {fo.status_reason} cannot be overridden")
    fo.review_override_at = utcnow()
    fo.review_override_by = actor
    audit.record(
        db,
        shop_id=shop.id,
        order_id=fo.order_id,
        fulfillment_order_id=fo.id,
        step=audit.Step.REVIEW_OVERRIDE,
        message=f"Review approved by staff (was {fo.status_reason}); re-checking Shopify",
        actor=actor,
    )
    return fo
