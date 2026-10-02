"""Read models for the admin UI: orders, order detail, shipments, tracking, automation logs."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.core.enums import LogisticsStatus, ShipmentStatus
from app.core.errors import NotFoundError, ValidationFailed
from app.models import (
    AutomationLog,
    CarrierQuote,
    CarrierServiceabilityCheck,
    Shipment,
    Shop,
    ShopifyFulfillmentOrder,
    ShopifyOrder,
    TrackingEvent,
)
from app.services.overrides import REALLOCATABLE
from app.tracking.transitions import CANCELLABLE


def _enum[E: StrEnum](cls: type[E], value: str) -> E:
    try:
        return cls(value)
    except ValueError:
        raise ValidationFailed(f"Unknown status {value!r}") from None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Any) -> str | None:
    return str(value) if value is not None else None


def _latest_shipment_subquery() -> Any:
    return (
        select(func.max(Shipment.id))
        .where(Shipment.fulfillment_order_id == ShopifyFulfillmentOrder.id)
        .correlate(ShopifyFulfillmentOrder)
        .scalar_subquery()
    )


def shipment_row(s: Shipment) -> dict[str, Any]:
    return {
        "id": s.id,
        "order_id": s.order_id,
        "fulfillment_order_id": s.fulfillment_order_id,
        "shopify_order_name": s.shopify_order_name,
        "carrier_code": s.carrier_code,
        "carrier_shipment_id": s.carrier_shipment_id,
        "awb": s.awb,
        "tracking_url": s.tracking_url,
        "label_url": s.label_url,
        "shipping_cost": _money(s.shipping_cost),
        "currency": s.currency,
        "cod_amount": _money(s.cod_amount),
        "estimated_delivery_date": _iso(s.estimated_delivery_date),
        "status": s.status.value,
        "attempt": s.attempt,
        "idempotency_key": s.idempotency_key,
        "shopify_sync_status": s.shopify_sync_status.value,
        "shopify_sync_error": s.shopify_sync_error,
        "shopify_fulfillment_id": s.shopify_fulfillment_id,
        "last_error": s.last_error,
        "created_by": s.created_by,
        "created_at": _iso(s.created_at),
        "updated_at": _iso(s.updated_at),
        "delivered_at": _iso(s.delivered_at),
        "cancelled_at": _iso(s.cancelled_at),
        "cancel_reason": s.cancel_reason,
        "can_cancel": s.status in CANCELLABLE and s.status != ShipmentStatus.CREATION_UNKNOWN,
        "can_resolve": s.status == ShipmentStatus.CREATION_UNKNOWN,
        "can_retry_shopify_sync": s.awb is not None
        and s.shopify_fulfillment_id is None
        and s.status not in (ShipmentStatus.CANCELLED, ShipmentStatus.CREATE_FAILED),
    }


def list_orders(
    db: Session,
    shop: Shop,
    *,
    status: str | None = None,
    carrier: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    latest = aliased(Shipment)
    stmt = (
        select(ShopifyFulfillmentOrder, ShopifyOrder, latest)
        .join(ShopifyOrder, ShopifyOrder.id == ShopifyFulfillmentOrder.order_id)
        .outerjoin(latest, latest.id == _latest_shipment_subquery())
        .where(ShopifyFulfillmentOrder.shop_id == shop.id)
    )
    if status:
        stmt = stmt.where(
            ShopifyFulfillmentOrder.logistics_status == _enum(LogisticsStatus, status)
        )
    if carrier:
        stmt = stmt.where(
            or_(
                latest.carrier_code == carrier,
                ShopifyFulfillmentOrder.selected_carrier_code == carrier,
            )
        )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                ShopifyOrder.name.ilike(like),
                ShopifyOrder.phone.ilike(like),
                ShopifyOrder.customer_name.ilike(like),
                latest.awb.ilike(like),
                cast(ShopifyOrder.shipping_address["zip"], String).ilike(like),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(ShopifyOrder.shopify_created_at.desc(), ShopifyFulfillmentOrder.id.desc())
        .limit(min(limit, 200))
        .offset(offset)
    ).all()
    items = []
    for fo, order, shipment in rows:
        address = order.shipping_address or {}
        items.append(
            {
                "order_id": order.id,
                "fulfillment_order_id": fo.id,
                "shopify_order_id": order.shopify_order_id,
                "order_name": order.name,
                "created_at": _iso(order.shopify_created_at),
                "customer_name": order.customer_name,
                "phone": order.phone,
                "destination": {
                    "city": address.get("city"),
                    "state": address.get("province"),
                    "pincode": address.get("zip"),
                },
                "payment_mode": order.payment_mode.value,
                "total_price": _money(order.total_price),
                "fulfillment_status": fo.shopify_status,
                "logistics_status": fo.logistics_status.value,
                "status_reason": fo.status_reason,
                "status_detail": fo.status_detail,
                "selected_carrier": fo.selected_carrier_code,
                "shipment": shipment_row(shipment) if shipment else None,
                "automation_disabled": order.automation_disabled,
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def order_detail(db: Session, shop: Shop, order_id: int) -> dict[str, Any]:
    order = db.get(ShopifyOrder, order_id)
    if order is None or order.shop_id != shop.id:
        raise NotFoundError("Order not found")
    fos = []
    for fo in order.fulfillment_orders:
        shipments = list(
            db.scalars(
                select(Shipment)
                .where(Shipment.fulfillment_order_id == fo.id)
                .order_by(Shipment.id.desc())
            )
        )
        active = next(
            (
                s
                for s in shipments
                if s.status not in (ShipmentStatus.CANCELLED, ShipmentStatus.CREATE_FAILED)
            ),
            None,
        )
        latest_run = db.scalar(
            select(CarrierQuote.run_id)
            .where(CarrierQuote.fulfillment_order_id == fo.id)
            .order_by(CarrierQuote.id.desc())
            .limit(1)
        )
        quotes = (
            list(
                db.scalars(
                    select(CarrierQuote)
                    .where(CarrierQuote.run_id == latest_run)
                    .order_by(CarrierQuote.rank.nulls_last(), CarrierQuote.carrier_code)
                )
            )
            if latest_run
            else []
        )
        checks = (
            list(
                db.scalars(
                    select(CarrierServiceabilityCheck).where(
                        CarrierServiceabilityCheck.run_id == latest_run
                    )
                )
            )
            if latest_run
            else []
        )
        fos.append(
            {
                "id": fo.id,
                "shopify_fulfillment_order_id": fo.shopify_fulfillment_order_id,
                "shopify_status": fo.shopify_status,
                "logistics_status": fo.logistics_status.value,
                "status_reason": fo.status_reason,
                "status_detail": fo.status_detail,
                "hold_reasons": fo.hold_reasons,
                "line_items": fo.line_items,
                "weight_g": fo.weight_g,
                "warehouse_id": fo.warehouse_id,
                "selected_carrier": fo.selected_carrier_code,
                "forced_carrier": fo.forced_carrier_code,
                "allocation_ranking": fo.allocation_ranking,
                "can_reallocate": fo.logistics_status in REALLOCATABLE and active is None,
                "shipments": [shipment_row(s) for s in shipments],
                "latest_run": {
                    "run_id": str(latest_run) if latest_run else None,
                    "quotes": [
                        {
                            "carrier_code": qt.carrier_code,
                            "eligible": qt.eligible,
                            "selected": qt.selected,
                            "rank": qt.rank,
                            "cost": _money(qt.cost),
                            "currency": qt.currency,
                            "transit_days": qt.transit_days,
                            "edd": _iso(qt.edd),
                            "score": _money(qt.score),
                            "rejection_reason": qt.rejection_reason,
                            "rejection_detail": qt.rejection_detail,
                            "strategy": qt.strategy,
                        }
                        for qt in quotes
                    ],
                    "checks": [
                        {
                            "carrier_code": c.carrier_code,
                            "serviceable": c.serviceable,
                            "cod_available": c.cod_available,
                            "error_class": c.error_class,
                            "error_message": c.error_message,
                            "duration_ms": c.duration_ms,
                            "request": c.request,
                            "response": c.response,
                            "created_at": _iso(c.created_at),
                        }
                        for c in checks
                    ],
                },
                "tracking": tracking_events(db, shop, fulfillment_order_id=fo.id, limit=200),
            }
        )
    return {
        "id": order.id,
        "shopify_order_id": order.shopify_order_id,
        "name": order.name,
        "created_at": _iso(order.shopify_created_at),
        "cancelled_at": _iso(order.cancelled_at),
        "customer_name": order.customer_name,
        "phone": order.phone,
        "email": order.email,
        "shipping_address": order.shipping_address,
        "payment_mode": order.payment_mode.value,
        "payment_gateways": order.payment_gateways,
        "total_price": _money(order.total_price),
        "outstanding_amount": _money(order.outstanding_amount),
        "currency": order.currency,
        "tags": order.tags,
        "risk_level": order.risk_level,
        "automation_disabled": order.automation_disabled,
        "fulfillment_orders": fos,
        "logs": list_logs(db, shop, order_id=order.id, limit=500)["items"],
    }


def list_shipments(
    db: Session,
    shop: Shop,
    *,
    status: str | None = None,
    carrier: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(Shipment).where(Shipment.shop_id == shop.id)
    if status:
        stmt = stmt.where(Shipment.status == _enum(ShipmentStatus, status))
    if carrier:
        stmt = stmt.where(Shipment.carrier_code == carrier)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Shipment.awb.ilike(like), Shipment.shopify_order_name.ilike(like)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(stmt.order_by(Shipment.id.desc()).limit(min(limit, 200)).offset(offset))
    return {
        "items": [shipment_row(s) for s in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def tracking_events(
    db: Session,
    shop: Shop,
    *,
    shipment_id: int | None = None,
    fulfillment_order_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = (
        select(TrackingEvent, Shipment)
        .join(Shipment, Shipment.id == TrackingEvent.shipment_id)
        .where(Shipment.shop_id == shop.id)
    )
    if shipment_id is not None:
        stmt = stmt.where(TrackingEvent.shipment_id == shipment_id)
    if fulfillment_order_id is not None:
        stmt = stmt.where(Shipment.fulfillment_order_id == fulfillment_order_id)
    rows = db.execute(
        stmt.order_by(TrackingEvent.occurred_at.desc(), TrackingEvent.id.desc()).limit(
            min(limit, 500)
        )
    ).all()
    return [
        {
            "id": ev.id,
            "shipment_id": s.id,
            "order_id": s.order_id,
            "shopify_order_name": s.shopify_order_name,
            "carrier_code": ev.carrier_code,
            "awb": ev.awb,
            "raw_status": ev.raw_status,
            "status": ev.normalized_status.value,
            "description": ev.description,
            "location": ev.location,
            "occurred_at": _iso(ev.occurred_at),
            "source": ev.source.value,
            "applied": ev.applied,
            "pushed_to_shopify": ev.shopify_pushed_at is not None,
        }
        for ev, s in rows
    ]


def list_logs(
    db: Session,
    shop: Shop,
    *,
    order_id: int | None = None,
    level: str | None = None,
    step: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    stmt = select(AutomationLog).where(AutomationLog.shop_id == shop.id)
    if order_id is not None:
        stmt = stmt.where(AutomationLog.order_id == order_id)
    if level:
        stmt = stmt.where(AutomationLog.level == level)
    if step:
        stmt = stmt.where(AutomationLog.step == step)
    if q:
        stmt = stmt.where(AutomationLog.message.ilike(f"%{q.strip()}%"))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    order_clause = (
        (AutomationLog.created_at.asc(), AutomationLog.id.asc())
        if order_id
        else (AutomationLog.created_at.desc(), AutomationLog.id.desc())
    )
    rows = db.scalars(stmt.order_by(*order_clause).limit(min(limit, 500)).offset(offset))
    return {
        "items": [
            {
                "id": r.id,
                "created_at": _iso(r.created_at),
                "order_id": r.order_id,
                "fulfillment_order_id": r.fulfillment_order_id,
                "shipment_id": r.shipment_id,
                "step": r.step,
                "level": r.level.value,
                "message": r.message,
                "actor": r.actor,
                "data": r.data,
            }
            for r in rows
        ],
        "total": total,
    }
