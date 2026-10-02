from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus, ShipmentStatus
from app.core.time import utcnow
from app.models import AutomationLog, Shipment, Shop, ShopifyFulfillmentOrder, ShopifyOrder
from app.schemas.settings import ShopSettings

CREATED_STATUSES = tuple(
    s
    for s in ShipmentStatus
    if s
    not in (ShipmentStatus.CREATING, ShipmentStatus.CREATION_UNKNOWN, ShipmentStatus.CREATE_FAILED)
)
RTO_STATUSES = (
    ShipmentStatus.RTO_INITIATED,
    ShipmentStatus.RTO_IN_TRANSIT,
    ShipmentStatus.RTO_DELIVERED,
)


def _start_of_today(tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    local_now = utcnow().astimezone(tz)
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0)


def _fo_count(db: Session, shop_id: int, *statuses: LogisticsStatus) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ShopifyFulfillmentOrder)
            .where(
                ShopifyFulfillmentOrder.shop_id == shop_id,
                ShopifyFulfillmentOrder.logistics_status.in_(statuses),
            )
        )
        or 0
    )


def _shipment_count(
    db: Session, shop_id: int, *statuses: ShipmentStatus, since: datetime | None = None
) -> int:
    stmt = select(func.count()).select_from(Shipment).where(Shipment.shop_id == shop_id)
    if statuses:
        stmt = stmt.where(Shipment.status.in_(statuses))
    if since is not None:
        stmt = stmt.where(Shipment.created_at >= since)
    return int(db.scalar(stmt) or 0)


def summary(db: Session, shop: Shop) -> dict[str, Any]:
    settings = ShopSettings.load(shop.settings)
    today = _start_of_today(settings.timezone)
    last_30 = utcnow() - timedelta(days=30)

    orders_today = int(
        db.scalar(
            select(func.count())
            .select_from(ShopifyOrder)
            .where(ShopifyOrder.shop_id == shop.id, ShopifyOrder.shopify_created_at >= today)
        )
        or 0
    )
    breakdown_rows = db.execute(
        select(Shipment.carrier_code, func.count())
        .where(Shipment.shop_id == shop.id, Shipment.created_at >= last_30)
        .group_by(Shipment.carrier_code)
        .order_by(func.count().desc())
    ).all()
    recent = db.scalars(
        select(AutomationLog)
        .where(AutomationLog.shop_id == shop.id)
        .order_by(AutomationLog.created_at.desc(), AutomationLog.id.desc())
        .limit(15)
    ).all()

    return {
        "automation_enabled": shop.automation_enabled,
        "orders_today": orders_today,
        "shipments_created_today": _shipment_count(db, shop.id, *CREATED_STATUSES, since=today),
        "manual_review": _fo_count(db, shop.id, LogisticsStatus.MANUAL_REVIEW),
        "awaiting_allocation": _fo_count(
            db,
            shop.id,
            LogisticsStatus.AWAITING_ALLOCATION,
            LogisticsStatus.SETTLING,
            LogisticsStatus.NO_CARRIER_AVAILABLE,
        ),
        "failed": _fo_count(db, shop.id, LogisticsStatus.FAILED)
        + _shipment_count(db, shop.id, ShipmentStatus.CREATE_FAILED, since=last_30),
        "delivered": _shipment_count(db, shop.id, ShipmentStatus.DELIVERED, since=last_30),
        "rto": _shipment_count(db, shop.id, *RTO_STATUSES, since=last_30),
        "carrier_breakdown": [
            {"carrier_code": code, "shipments": int(n)} for code, n in breakdown_rows
        ],
        "recent_activity": [
            {
                "id": log.id,
                "created_at": log.created_at.isoformat(),
                "step": log.step,
                "level": log.level.value,
                "message": log.message,
                "order_id": log.order_id,
            }
            for log in recent
        ],
    }
