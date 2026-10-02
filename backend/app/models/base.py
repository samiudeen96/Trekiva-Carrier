"""Shared column helpers for ORM models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, Identity, func
from sqlalchemy.orm import Mapped, mapped_column


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def str_enum(enum_cls: type[StrEnum], **kwargs: Any) -> Enum:
    """Store a StrEnum as VARCHAR (no native PG enum, so new values need no migration)."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=40,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
        **kwargs,
    )
