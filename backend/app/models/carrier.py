from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import CarrierEnvironment
from app.models.base import IdMixin, TimestampMixin, str_enum


class CarrierAccount(IdMixin, TimestampMixin, Base):
    """Credentials for one carrier account. Secrets are encrypted; `credentials_hint` is safe
    to display (non-secret values in clear, secret values masked)."""

    __tablename__ = "carrier_accounts"
    __table_args__ = (UniqueConstraint("shop_id", "carrier_code", "environment", "label"),)

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    carrier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    environment: Mapped[CarrierEnvironment] = mapped_column(
        str_enum(CarrierEnvironment), nullable=False
    )
    credentials_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    credentials_hint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    webhook_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    """Random path token identifying this account on inbound carrier webhooks."""
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CarrierSetting(IdMixin, TimestampMixin, Base):
    """Per-shop business configuration of a carrier, used by the allocation engine."""

    __tablename__ = "carrier_settings"
    __table_args__ = (UniqueConstraint("shop_id", "carrier_code"),)

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    carrier_code: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cod_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    prepaid_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    """Lower number = preferred."""
    max_shipping_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_weight_g: Mapped[int | None] = mapped_column(Integer)
    max_weight_g: Mapped[int | None] = mapped_column(Integer)
    performance_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    """0-100, higher is better. Manually set for now; computed from delivery data later."""
    active_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("carrier_accounts.id", ondelete="SET NULL")
    )


class CarrierWarehouseMapping(IdMixin, TimestampMixin, Base):
    """Which warehouses a carrier account picks up from, with the carrier's own pickup ID."""

    __tablename__ = "carrier_warehouse_mappings"
    __table_args__ = (UniqueConstraint("carrier_account_id", "warehouse_id"),)

    carrier_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("carrier_accounts.id", ondelete="CASCADE"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False
    )
    carrier_warehouse_ref: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
