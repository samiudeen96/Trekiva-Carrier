"""Audit trail: every automation step and staff action is recorded in `automation_logs`."""

from __future__ import annotations

import logging
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import LogLevel
from app.models import AutomationLog

log = logging.getLogger("trekiva.audit")


class Step(StrEnum):
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    ORDER_RECEIVED = "ORDER_RECEIVED"
    FULFILLMENT_CHECKED = "FULFILLMENT_CHECKED"
    FULFILLMENT_HELD = "FULFILLMENT_HELD"
    HOLD_RELEASED = "HOLD_RELEASED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REVIEW_OVERRIDE = "REVIEW_OVERRIDE"
    REVIEW_HOLD_PLACED = "REVIEW_HOLD_PLACED"
    WAITING = "WAITING"
    READY_FOR_ALLOCATION = "READY_FOR_ALLOCATION"
    ALLOCATION_STARTED = "ALLOCATION_STARTED"
    SERVICEABILITY_CHECKED = "SERVICEABILITY_CHECKED"
    CARRIER_SELECTED = "CARRIER_SELECTED"
    NO_CARRIER = "NO_CARRIER"
    SHIPMENT_REQUESTED = "SHIPMENT_REQUESTED"
    SHIPMENT_BLOCKED = "SHIPMENT_BLOCKED"
    SHIPMENT_CREATED = "SHIPMENT_CREATED"
    AWB_GENERATED = "AWB_GENERATED"
    SHIPMENT_UNCERTAIN = "SHIPMENT_UNCERTAIN"
    SHIPMENT_FAILED = "SHIPMENT_FAILED"
    CARRIER_FALLBACK = "CARRIER_FALLBACK"
    SHIPMENT_CANCELLED = "SHIPMENT_CANCELLED"
    SHIPMENT_CANCEL_FAILED = "SHIPMENT_CANCEL_FAILED"
    SHOPIFY_SYNCED = "SHOPIFY_SYNCED"
    SHOPIFY_SYNC_FAILED = "SHOPIFY_SYNC_FAILED"
    TRACKING_UPDATED = "TRACKING_UPDATED"
    STAFF_ACTION = "STAFF_ACTION"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    AUTOMATION_DISABLED = "AUTOMATION_DISABLED"
    AUTOMATION_ENABLED = "AUTOMATION_ENABLED"
    SETTINGS_CHANGED = "SETTINGS_CHANGED"
    CARRIER_CONFIG_CHANGED = "CARRIER_CONFIG_CHANGED"
    APP_INSTALLED = "APP_INSTALLED"
    APP_UNINSTALLED = "APP_UNINSTALLED"
    ERROR = "ERROR"


def record(
    db: Session,
    *,
    shop_id: int,
    step: Step,
    message: str,
    level: LogLevel = LogLevel.INFO,
    order_id: int | None = None,
    fulfillment_order_id: int | None = None,
    shipment_id: int | None = None,
    run_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
    actor: str = "system",
) -> AutomationLog:
    entry = AutomationLog(
        shop_id=shop_id,
        order_id=order_id,
        fulfillment_order_id=fulfillment_order_id,
        shipment_id=shipment_id,
        run_id=run_id,
        step=step.value,
        level=level,
        message=message,
        data=data or {},
        actor=actor,
    )
    db.add(entry)
    log.log(
        logging.getLevelName(level.value),
        message,
        extra={
            "step": step.value,
            "shop_id": shop_id,
            "order_id": order_id,
            "fo_id": fulfillment_order_id,
        },
    )
    return entry
