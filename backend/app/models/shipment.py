from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import INACTIVE_SHIPMENT_STATUSES, ShipmentStatus, ShopifySyncStatus
from app.models.base import IdMixin, TimestampMixin, str_enum

_INACTIVE_SQL = ", ".join(f"'{s.value}'" for s in sorted(INACTIVE_SHIPMENT_STATUSES))


class Shipment(IdMixin, TimestampMixin, Base):
    """A courier shipment for one Shopify fulfillment order.

    Duplicate-AWB protection is enforced by PostgreSQL: the partial unique index
    `uq_shipments_active_fulfillment_order` allows at most ONE shipment per fulfillment order
    whose status is not CANCELLED / CREATE_FAILED. Cancelled rows are kept as history, so a
    fulfillment order can be re-shipped only after its active shipment was safely cancelled.
    """

    __tablename__ = "shipments"
    __table_args__ = (
        Index(
            "uq_shipments_active_fulfillment_order",
            "shopify_fulfillment_order_id",
            unique=True,
            postgresql_where=text(f"status NOT IN ({_INACTIVE_SQL})"),
        ),
        UniqueConstraint("carrier_code", "awb"),
        Index(None, "shop_id", "status"),
        Index(None, "next_poll_at"),
    )

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shopify_orders.id", ondelete="CASCADE"), nullable=False
    )
    fulfillment_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shopify_fulfillment_orders.id", ondelete="CASCADE"), nullable=False
    )
    shopify_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shopify_order_name: Mapped[str] = mapped_column(String(64), nullable=False)
    shopify_fulfillment_order_id: Mapped[str] = mapped_column(String(64), nullable=False)

    carrier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    carrier_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("carrier_accounts.id", ondelete="SET NULL")
    )
    warehouse_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("warehouses.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    """`{fulfillment_order_pk}:{attempt}`; sent to the carrier as our reference."""
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    carrier_shipment_id: Mapped[str | None] = mapped_column(String(128))
    awb: Mapped[str | None] = mapped_column(String(64))
    tracking_url: Mapped[str | None] = mapped_column(Text)
    label_url: Mapped[str | None] = mapped_column(Text)
    shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    estimated_delivery_date: Mapped[date | None] = mapped_column(Date)
    cod_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    declared_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    weight_g: Mapped[int | None] = mapped_column(Integer)
    dimensions: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    reconcile_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[ShipmentStatus] = mapped_column(
        str_enum(ShipmentStatus), default=ShipmentStatus.CREATING, nullable=False
    )
    raw_create_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)

    shopify_fulfillment_id: Mapped[str | None] = mapped_column(String(64))
    shopify_sync_status: Mapped[ShopifySyncStatus] = mapped_column(
        str_enum(ShopifySyncStatus), default=ShopifySyncStatus.PENDING, nullable=False
    )
    shopify_sync_error: Mapped[str | None] = mapped_column(Text)

    last_tracking_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(255), default="system", nullable=False)
