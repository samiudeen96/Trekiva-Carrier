from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import LogLevel
from app.models.base import IdMixin, str_enum


class AutomationLog(IdMixin, Base):
    """Append-only audit trail. Every automation step and staff action writes one row."""

    __tablename__ = "automation_logs"
    __table_args__ = (
        Index(None, "order_id", "created_at"),
        Index(None, "shop_id", "created_at"),
    )

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shopify_orders.id", ondelete="CASCADE")
    )
    fulfillment_order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shopify_fulfillment_orders.id", ondelete="CASCADE")
    )
    shipment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shipments.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[LogLevel] = mapped_column(str_enum(LogLevel), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    actor: Mapped[str] = mapped_column(String(255), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
