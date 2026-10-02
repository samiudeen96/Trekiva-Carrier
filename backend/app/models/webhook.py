from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import WebhookStatus
from app.models.base import IdMixin, str_enum


class WebhookEvent(IdMixin, Base):
    """Every inbound webhook (Shopify or carrier), stored before processing.

    UNIQUE(source, external_event_id) makes ingestion idempotent: Shopify retries reuse the same
    `X-Shopify-Event-Id`, so a retried delivery is recognised and never processed twice.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("source", "external_event_id"),
        Index(None, "processing_status", "received_at"),
    )

    source: Mapped[str] = mapped_column(String(40), nullable=False)
    """'shopify' or a carrier code."""
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    shop_domain: Mapped[str | None] = mapped_column(String(255))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[WebhookStatus] = mapped_column(
        str_enum(WebhookStatus), default=WebhookStatus.RECEIVED, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
