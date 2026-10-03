from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import AlertSeverity, AlertStatus
from app.models.base import IdMixin, str_enum


class Alert(IdMixin, Base):
    """Outbox of operator alerts (email / Slack).

    Rows are written in the same transaction as the failure they describe, so an alert exists
    exactly when the failure was committed. `alerts.dispatch` (Celery beat) delivers them;
    rows sharing a `fingerprint` are rate-limited and grouped into one message.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index(None, "status", "next_attempt_at"),
        Index(None, "fingerprint", "sent_at"),
    )

    shop_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(str_enum(AlertSeverity), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shopify_orders.id", ondelete="SET NULL")
    )
    fulfillment_order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shopify_fulfillment_orders.id", ondelete="SET NULL")
    )
    shipment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shipments.id", ondelete="SET NULL")
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[AlertStatus] = mapped_column(
        str_enum(AlertStatus), default=AlertStatus.PENDING, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_channels: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    """Channels that already delivered this alert; a retry only uses the others."""
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """First successful delivery on any channel (drives the cooldown)."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
