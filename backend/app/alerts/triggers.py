"""State-change alerts, raised automatically at flush time.

Whenever a fulfillment order or shipment *enters* a state that needs a person, an alert row is
added to the same flush. Hooking the state change (instead of every code path that can cause
it) means a new route into FAILED can never forget to alert.

| Change                                         | Alert                  |
|------------------------------------------------|------------------------|
| fulfillment order -> FAILED (any reason)       | SHIPMENT_FAILED        |
| reason -> RECONCILIATION_NEEDS_STAFF           | SHIPMENT_NEEDS_STAFF   |
| shipment.shopify_sync_status -> FAILED         | SHOPIFY_SYNC_FAILED    |

Only ids and the status text are stored; names and links are rendered when the alert is sent,
so this listener never loads relationships mid-flush.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.alerts.service import AlertKind, raise_alert
from app.core.db import Base
from app.core.enums import AlertSeverity, LogisticsStatus, ShopifySyncStatus
from app.models import Shipment, ShopifyFulfillmentOrder

#: Same value as `app.logistics.shipments.STAFF_RECONCILE` (not imported: logistics imports us).
NEEDS_STAFF_REASON = "RECONCILIATION_NEEDS_STAFF"

AlertSpec = tuple[AlertKind, AlertSeverity, str]


def fulfillment_order_alert(status: LogisticsStatus, reason: str | None) -> AlertSpec | None:
    if status == LogisticsStatus.FAILED:
        return (
            AlertKind.SHIPMENT_FAILED,
            AlertSeverity.ERROR,
            f"Order could not be shipped ({reason or 'FAILED'})",
        )
    if reason == NEEDS_STAFF_REASON:
        return (
            AlertKind.SHIPMENT_NEEDS_STAFF,
            AlertSeverity.CRITICAL,
            "Carrier could not confirm a shipment: check the carrier panel",
        )
    return None


def shipment_alert(sync_status: ShopifySyncStatus) -> AlertSpec | None:
    if sync_status == ShopifySyncStatus.FAILED:
        return (
            AlertKind.SHOPIFY_SYNC_FAILED,
            AlertSeverity.ERROR,
            "Shopify was not updated with the AWB",
        )
    return None


def _changed(obj: Base, *attrs: str) -> bool:
    state = inspect(obj)
    return any(state.attrs[a].history.added for a in attrs)


@event.listens_for(Session, "before_flush")
def _raise_state_alerts(session: Session, _flush_context: Any, _instances: Any) -> None:
    # New rows are skipped: they have no id yet, and nothing is *created* in a failed state.
    for obj in list(session.dirty):
        if isinstance(obj, ShopifyFulfillmentOrder):
            if not _changed(obj, "logistics_status", "status_reason"):
                continue
            spec = fulfillment_order_alert(obj.logistics_status, obj.status_reason)
            if spec is None:
                continue
            kind, severity, title = spec
            raise_alert(
                session,
                kind=kind,
                severity=severity,
                title=title,
                detail=obj.status_detail or "",
                fingerprint=f"{kind.value}:{obj.shop_id}:{obj.status_reason}",
                shop_id=obj.shop_id,
                order_id=obj.order_id,
                fulfillment_order_id=obj.id,
                data={"reason": obj.status_reason},
            )
        elif isinstance(obj, Shipment):
            if not _changed(obj, "shopify_sync_status"):
                continue
            spec = shipment_alert(obj.shopify_sync_status)
            if spec is None:
                continue
            kind, severity, title = spec
            raise_alert(
                session,
                kind=kind,
                severity=severity,
                title=title,
                detail=f"{obj.carrier_code} AWB {obj.awb}: {obj.shopify_sync_error or 'unknown error'}",
                shop_id=obj.shop_id,
                order_id=obj.order_id,
                fulfillment_order_id=obj.fulfillment_order_id,
                shipment_id=obj.id,
                data={"awb": obj.awb, "carrier_code": obj.carrier_code},
            )
