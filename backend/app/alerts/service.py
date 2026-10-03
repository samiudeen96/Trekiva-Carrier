"""Raising operator alerts.

`raise_alert` only adds an `alerts` row to the caller's transaction (the outbox): the alert
exists exactly when the failure it describes is committed, and nothing is sent from inside a
business transaction. `alerts.dispatch` delivers it afterwards (see `dispatch.py`).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AlertSeverity, AlertStatus
from app.models import Alert

log = logging.getLogger("trekiva.alerts")

#: `CarrierAuthError.__name__`, compared as a string so this module does not import the carrier
#: package (offer failures only carry the error class name).
CARRIER_AUTH_ERROR = "CarrierAuthError"


class AlertKind(StrEnum):
    # Raised by the pipeline as it happens.
    CARRIER_AUTH_FAILED = "CARRIER_AUTH_FAILED"
    SHIPMENT_FAILED = "SHIPMENT_FAILED"
    SHIPMENT_NEEDS_STAFF = "SHIPMENT_NEEDS_STAFF"
    SHOPIFY_SYNC_FAILED = "SHOPIFY_SYNC_FAILED"
    CARRIER_CANCEL_FAILED = "CARRIER_CANCEL_FAILED"
    RTO_NEEDED = "RTO_NEEDED"
    # Raised by the periodic health checks (`app.ops.checks`).
    STUCK_ORDERS = "STUCK_ORDERS"
    SHOPIFY_SYNC_STALLED = "SHOPIFY_SYNC_STALLED"
    WEBHOOKS_FAILING = "WEBHOOKS_FAILING"
    QUEUE_BACKLOG = "QUEUE_BACKLOG"
    TEST = "TEST"


def raise_alert(
    db: Session,
    *,
    kind: AlertKind,
    severity: AlertSeverity,
    title: str,
    detail: str = "",
    fingerprint: str | None = None,
    shop_id: int | None = None,
    order_id: int | None = None,
    fulfillment_order_id: int | None = None,
    shipment_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> Alert:
    """Queue an alert. Alerts with the same `fingerprint` (default: kind + shop) are grouped
    and rate-limited when sent, so raising one per failing order is fine."""
    alert = Alert(
        shop_id=shop_id,
        kind=kind.value,
        severity=severity,
        fingerprint=fingerprint or f"{kind.value}:{shop_id or '-'}",
        title=title,
        detail=detail[:4000],
        order_id=order_id,
        fulfillment_order_id=fulfillment_order_id,
        shipment_id=shipment_id,
        data=data or {},
        status=AlertStatus.PENDING,
        attempts=0,
        delivered_channels=[],
    )
    db.add(alert)
    log.warning(
        "Alert raised: %s",
        title,
        extra={"alert_kind": kind.value, "severity": severity.value, "shop_id": shop_id},
    )
    return alert


def has_open_alert(db: Session, fingerprint: str) -> bool:
    """True while an alert with this fingerprint is still waiting to be sent. Periodic checks
    use it so a persisting condition does not queue a new row every run."""
    return (
        db.scalar(
            select(Alert.id)
            .where(
                Alert.fingerprint == fingerprint,
                Alert.status.in_([AlertStatus.PENDING, AlertStatus.SENDING]),
            )
            .limit(1)
        )
        is not None
    )


def carrier_error(
    db: Session,
    *,
    shop_id: int,
    carrier_code: str,
    error_class: str,
    message: str,
    during: str,
    order_id: int | None = None,
    fulfillment_order_id: int | None = None,
    shipment_id: int | None = None,
) -> Alert | None:
    """Alert when a carrier rejected our credentials. Every other carrier error is handled by
    retries, fallback or the fulfillment order's own state, so it is ignored here."""
    if error_class != CARRIER_AUTH_ERROR:
        return None
    fingerprint = f"{AlertKind.CARRIER_AUTH_FAILED.value}:{shop_id}:{carrier_code}"
    if has_open_alert(db, fingerprint):
        return None  # one per carrier until sent: polling alone would add one per shipment
    return raise_alert(
        db,
        kind=AlertKind.CARRIER_AUTH_FAILED,
        severity=AlertSeverity.CRITICAL,
        title=f"{carrier_code} rejected our credentials",
        detail=(
            f"During {during}: {message}. Orders skip {carrier_code} until its credentials are "
            "fixed on the Carriers page."
        ),
        fingerprint=fingerprint,
        shop_id=shop_id,
        order_id=order_id,
        fulfillment_order_id=fulfillment_order_id,
        shipment_id=shipment_id,
        data={"carrier_code": carrier_code, "during": during},
    )
