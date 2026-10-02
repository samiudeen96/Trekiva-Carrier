from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.enums import AllocationStrategy
from app.models.base import IdMixin, TimestampMixin, str_enum


class AllocationRule(IdMixin, TimestampMixin, Base):
    """A carrier-allocation rule. The highest-priority active rule whose `conditions` match an
    order decides the strategy. A rule with empty conditions is the catch-all default."""

    __tablename__ = "allocation_rules"
    __table_args__ = (Index(None, "shop_id", "is_active", "priority"),)

    shop_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    strategy: Mapped[AllocationStrategy] = mapped_column(
        str_enum(AllocationStrategy), default=AllocationStrategy.FASTEST, nullable=False
    )
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """Validated by `app.logistics.allocation.types.StrategyParams`."""
