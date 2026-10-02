from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import TrackingSource, TrackingStatus
from app.models.base import IdMixin, str_enum


class TrackingEvent(IdMixin, Base):
    """A carrier tracking event (raw + normalised). Deduplicated per shipment."""

    __tablename__ = "tracking_events"
    __table_args__ = (UniqueConstraint("shipment_id", "dedup_hash"),)

    shipment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    carrier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    awb: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_status: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_status: Mapped[TrackingStatus] = mapped_column(
        str_enum(TrackingStatus), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[TrackingSource] = mapped_column(str_enum(TrackingSource), nullable=False)
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """False when the event was stale/out of order and did not change the shipment."""
    shopify_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
