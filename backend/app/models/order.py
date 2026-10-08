from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.enums import LogisticsStatus, PaymentMode
from app.models.base import IdMixin, TimestampMixin, str_enum


class ShopifyOrder(IdMixin, TimestampMixin, Base):
    """Local snapshot of a Shopify order. Shopify remains the source of truth: this row is
    refreshed from the GraphQL Admin API every time the order is evaluated."""

    __tablename__ = "shopify_orders"
    __table_args__ = (
        UniqueConstraint("shop_id", "shopify_order_id"),
        Index(None, "shop_id", "shopify_created_at"),
    )

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    shopify_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    shipping_address: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    payment_mode: Mapped[PaymentMode] = mapped_column(str_enum(PaymentMode), nullable=False)
    payment_gateways: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    outstanding_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    financial_status: Mapped[str | None] = mapped_column(String(40))
    fulfillment_status: Mapped[str | None] = mapped_column(String(40))
    risk_level: Mapped[str | None] = mapped_column(String(20))
    risk_recommendation: Mapped[str | None] = mapped_column(String(20))
    shopify_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automation_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fulfillment_orders: Mapped[list[ShopifyFulfillmentOrder]] = relationship(
        back_populates="order", order_by="ShopifyFulfillmentOrder.id"
    )


class ShopifyFulfillmentOrder(IdMixin, TimestampMixin, Base):
    """A Shopify fulfillment order and its Trekiva processing state (`logistics_status`)."""

    __tablename__ = "shopify_fulfillment_orders"
    __table_args__ = (Index(None, "logistics_status", "next_check_at"),)

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shopify_orders.id", ondelete="CASCADE"), nullable=False
    )
    shopify_fulfillment_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    shopify_location_id: Mapped[str | None] = mapped_column(String(64))
    warehouse_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("warehouses.id", ondelete="SET NULL")
    )
    shopify_status: Mapped[str] = mapped_column(String(40), nullable=False)
    request_status: Mapped[str | None] = mapped_column(String(40))
    delivery_method: Mapped[str | None] = mapped_column(String(40))
    hold_reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    weight_g: Mapped[int | None] = mapped_column(Integer)

    logistics_status: Mapped[LogisticsStatus] = mapped_column(
        str_enum(LogisticsStatus), default=LogisticsStatus.RECEIVED, nullable=False
    )
    status_reason: Mapped[str | None] = mapped_column(String(64))
    """Machine-readable reason code for the current status, e.g. SHOPIFY_FULFILLMENT_HOLD."""
    status_detail: Mapped[str | None] = mapped_column(Text)
    """Human-readable explanation shown in the admin UI."""
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    hold_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hold_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_override_by: Mapped[str | None] = mapped_column(String(255))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Trekiva review checks (location risk, duplicates) -------------------------------------
    review_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """When the review checks ran. They run once, when the order is first ready to ship."""
    review_flags: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    """What the checks found: [{"kind", "tag", "detail"}]. Empty = nothing suspicious."""

    # --- allocation ------------------------------------------------------------------------
    selected_carrier_code: Mapped[str | None] = mapped_column(String(40))
    forced_carrier_code: Mapped[str | None] = mapped_column(String(40))
    """Set when staff manually choose a carrier: only that carrier is considered."""
    selected_by: Mapped[str | None] = mapped_column(String(255))
    """Who requested the current allocation: 'system' or 'staff:<user id>'."""
    allocation_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    allocation_ranking: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    """Ranked eligible carriers of the latest run, used for fallback after a definitive
    create failure: [{"carrier_code", "cost", "edd", "days", "score", "failed"}]."""

    order: Mapped[ShopifyOrder] = relationship(back_populates="fulfillment_orders")

    @property
    def staff_initiated(self) -> bool:
        return bool(self.selected_by and self.selected_by.startswith("staff"))
