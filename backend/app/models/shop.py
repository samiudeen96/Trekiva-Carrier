from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin


class Shop(IdMixin, TimestampMixin, Base):
    """An installed Shopify store. Access tokens are stored encrypted."""

    __tablename__ = "shops"

    shop_domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """Master switch. Off by default: staff enable it once carriers are configured."""
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    """Validated by `app.schemas.settings.ShopSettings`."""

    @property
    def is_installed(self) -> bool:
        return self.access_token_enc is not None and self.uninstalled_at is None
