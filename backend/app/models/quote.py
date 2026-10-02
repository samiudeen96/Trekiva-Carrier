from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin


class CarrierServiceabilityCheck(IdMixin, Base):
    """One carrier call made during an allocation run (request + response kept for audit)."""

    __tablename__ = "carrier_serviceability_checks"
    __table_args__ = (Index(None, "fulfillment_order_id", "created_at"),)

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    fulfillment_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shopify_fulfillment_orders.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    carrier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    serviceable: Mapped[bool | None] = mapped_column(Boolean)
    cod_available: Mapped[bool | None] = mapped_column(Boolean)
    prepaid_available: Mapped[bool | None] = mapped_column(Boolean)
    pickup_available: Mapped[bool | None] = mapped_column(Boolean)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_class: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CarrierQuote(IdMixin, Base):
    """A carrier's offer in an allocation run and the engine's verdict on it."""

    __tablename__ = "carrier_quotes"
    __table_args__ = (Index(None, "fulfillment_order_id", "run_id"),)

    check_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("carrier_serviceability_checks.id", ondelete="CASCADE")
    )
    fulfillment_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shopify_fulfillment_orders.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    carrier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    transit_days: Mapped[int | None] = mapped_column(Integer)
    edd: Mapped[date | None] = mapped_column(Date)
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    rank: Mapped[int | None] = mapped_column(Integer)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(64))
    rejection_detail: Mapped[str | None] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("allocation_rules.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
