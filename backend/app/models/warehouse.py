from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin


class Warehouse(IdMixin, TimestampMixin, Base):
    """A pickup location. Mapped 1:1 to a Shopify Location via `shopify_location_id`."""

    __tablename__ = "warehouses"
    __table_args__ = (
        UniqueConstraint("shop_id", "code"),
        UniqueConstraint("shop_id", "shopify_location_id"),
    )

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255))
    address1: Mapped[str] = mapped_column(String(255), nullable=False)
    address2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    pincode: Mapped[str] = mapped_column(String(16), nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="IN", nullable=False)
    shopify_location_id: Mapped[str | None] = mapped_column(String(64))
    default_package: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """{"length_cm": .., "breadth_cm": .., "height_cm": .., "min_weight_g": ..}"""
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
