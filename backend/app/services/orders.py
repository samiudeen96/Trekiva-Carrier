"""Persist Shopify order / fulfillment-order snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus, PaymentMode
from app.core.time import utcnow
from app.models import ShopifyFulfillmentOrder, ShopifyOrder, Warehouse
from app.schemas.shopify import FulfillmentOrderSnapshot, OrderSnapshot


def upsert_order(
    db: Session, shop_id: int, snap: OrderSnapshot, payment_mode: PaymentMode
) -> tuple[ShopifyOrder, bool]:
    """Insert or refresh the local order row. Returns (order, created)."""
    now = utcnow()
    values = {
        "name": snap.name,
        "customer_name": snap.customer_name
        or (snap.shipping_address.name if snap.shipping_address else None),
        "phone": snap.phone or (snap.shipping_address.phone if snap.shipping_address else None),
        "email": snap.email,
        "shipping_address": snap.shipping_address.model_dump(mode="json")
        if snap.shipping_address
        else None,
        "payment_mode": payment_mode.value,
        "payment_gateways": list(snap.payment_gateways),
        "currency": snap.currency,
        "total_price": snap.total_price,
        "outstanding_amount": snap.outstanding,
        "tags": list(snap.tags),
        "financial_status": snap.financial_status,
        "fulfillment_status": snap.fulfillment_status,
        "risk_level": snap.risk.highest_level,
        "risk_recommendation": snap.risk.recommendation,
        "shopify_created_at": snap.created_at,
        "cancelled_at": snap.cancelled_at,
        "raw": snap.model_dump(mode="json", exclude={"fulfillment_orders"}),
        "last_synced_at": now,
    }
    stmt: Any = (
        insert(ShopifyOrder)
        .values(shop_id=shop_id, shopify_order_id=snap.id, **values)
        .on_conflict_do_update(
            index_elements=[ShopifyOrder.shop_id, ShopifyOrder.shopify_order_id],
            set_={**values, "updated_at": now},
        )
        # xmax = 0 only for a freshly inserted row (PostgreSQL idiom for "inserted vs updated").
        .returning(ShopifyOrder.id, literal_column("(xmax = 0)").label("inserted"))
    )
    row = db.execute(stmt).one()
    order = db.get(ShopifyOrder, row.id, populate_existing=True)
    assert order is not None
    return order, bool(row.inserted)


def warehouse_for_location(db: Session, shop_id: int, location_id: str | None) -> Warehouse | None:
    if not location_id:
        return None
    return db.scalar(
        select(Warehouse).where(
            Warehouse.shop_id == shop_id,
            Warehouse.shopify_location_id == location_id,
            Warehouse.is_active.is_(True),
        )
    )


def upsert_fulfillment_order(
    db: Session, shop_id: int, order: ShopifyOrder, snap: FulfillmentOrderSnapshot
) -> ShopifyFulfillmentOrder:
    """Refresh Shopify-owned fields and return the row locked FOR UPDATE.

    The row lock serialises all processing of one fulfillment order across API and workers.
    Trekiva-owned fields (logistics_status, history) are never overwritten here.
    """
    now = utcnow()
    warehouse = warehouse_for_location(db, shop_id, snap.location_id)
    shopify_values = {
        "shopify_location_id": snap.location_id,
        "warehouse_id": warehouse.id if warehouse else None,
        "shopify_status": snap.status,
        "request_status": snap.request_status,
        "delivery_method": snap.delivery_method,
        "hold_reasons": [h.model_dump(mode="json") for h in snap.holds],
        "line_items": [li.model_dump(mode="json") for li in snap.line_items],
        "weight_g": snap.total_weight_g,
        "last_synced_at": now,
    }
    db.execute(
        insert(ShopifyFulfillmentOrder)
        .values(
            shop_id=shop_id,
            order_id=order.id,
            shopify_fulfillment_order_id=snap.id,
            logistics_status=LogisticsStatus.RECEIVED.value,
            **shopify_values,
        )
        .on_conflict_do_update(
            index_elements=[ShopifyFulfillmentOrder.shopify_fulfillment_order_id],
            set_={**shopify_values, "order_id": order.id, "updated_at": now},
        )
    )
    fo = db.scalar(
        select(ShopifyFulfillmentOrder)
        .where(ShopifyFulfillmentOrder.shopify_fulfillment_order_id == snap.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert fo is not None
    return fo


def mark_hold_released(db: Session, fulfillment_order_gids: Iterable[str], at: datetime) -> int:
    gids = list(fulfillment_order_gids)
    if not gids:
        return 0
    result = db.execute(
        update(ShopifyFulfillmentOrder)
        .where(ShopifyFulfillmentOrder.shopify_fulfillment_order_id.in_(gids))
        .values(hold_released_at=at)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def order_gids_for_fulfillment_orders(
    db: Session, fulfillment_order_gids: Iterable[str]
) -> list[str]:
    rows = db.execute(
        select(ShopifyOrder.shopify_order_id)
        .join(ShopifyFulfillmentOrder, ShopifyFulfillmentOrder.order_id == ShopifyOrder.id)
        .where(
            ShopifyFulfillmentOrder.shopify_fulfillment_order_id.in_(list(fulfillment_order_gids))
        )
        .distinct()
    )
    return [r[0] for r in rows]
