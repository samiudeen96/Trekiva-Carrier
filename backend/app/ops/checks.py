"""Periodic health checks (Celery beat: `ops.health_check`, every 5 min).

The pipeline alerts on failures as they happen (`app.alerts.triggers`). These checks catch the
failures that never happen *as an event*: work that silently stops moving.

| Check                | Condition (no progress for OPS_STUCK_MINUTES)                          |
|----------------------|------------------------------------------------------------------------|
| STUCK_ORDERS         | FO in ALLOCATING / SHIPMENT_PENDING / RECONCILING (not waiting on      |
|                      | staff), ALLOCATED or AWAITING_ALLOCATION with automation on, carriers  |
|                      | still unreachable, or SETTLING past its re-check time. RECONCILING     |
|                      | gets RECONCILE_GRACE extra (automatic reconciliation retries ~30 min). |
| SHOPIFY_SYNC_STALLED | AWB created but no Shopify fulfillment, sync not failed or deferred    |
| WEBHOOKS_FAILING     | webhook events FAILED in the last 24 h                                 |
| QUEUE_BACKLOG        | a Celery queue longer than OPS_QUEUE_BACKLOG_THRESHOLD                 |

Each check alerts only about *new* items: the ids already reported are stored on the alert, so
an order that stays stuck is reported once (then reminded daily), not every five minutes.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app import alerts
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.enums import (
    AlertSeverity,
    AlertStatus,
    LogisticsStatus,
    ShopifySyncStatus,
    WebhookStatus,
)
from app.core.time import utcnow
from app.logistics.allocation_run import CARRIERS_UNREACHABLE
from app.logistics.shipments import STAFF_RECONCILE
from app.logistics.shopify_sync import BEFORE_PICKUP, NOT_SYNCABLE
from app.models import Alert, Shipment, Shop, ShopifyFulfillmentOrder, ShopifyOrder, WebhookEvent
from app.ops import heartbeat
from app.schemas.settings import ShopSettings

log = logging.getLogger(__name__)

ALERT_RETENTION = timedelta(days=90)
REMIND_AFTER = timedelta(hours=24)
RECONCILE_GRACE = timedelta(minutes=45)
MAX_TRACKED_IDS = 1000
MAX_NAMES = 10


@dataclass(frozen=True)
class Item:
    id: int
    shop_id: int | None
    label: str
    """Order name (or webhook topic) shown in the alert."""
    status: str


# --- queries (shared with /metrics) ------------------------------------------------------------


def stuck_fulfillment_orders(db: Session, cutoff: datetime) -> list[Item]:
    fo = ShopifyFulfillmentOrder
    automation_on = Shop.automation_enabled.is_(True)
    rows = db.execute(
        select(fo.id, fo.shop_id, ShopifyOrder.name, fo.logistics_status)
        .join(ShopifyOrder, ShopifyOrder.id == fo.order_id)
        .join(Shop, Shop.id == fo.shop_id)
        .where(
            Shop.uninstalled_at.is_(None),
            fo.updated_at < cutoff,
            or_(
                fo.logistics_status.in_(
                    [LogisticsStatus.ALLOCATING, LogisticsStatus.SHIPMENT_PENDING]
                ),
                and_(
                    fo.logistics_status == LogisticsStatus.RECONCILING,
                    # Automatic reconciliation retries for ~30 min without touching the row.
                    fo.updated_at < cutoff - RECONCILE_GRACE,
                    or_(fo.status_reason.is_(None), fo.status_reason != STAFF_RECONCILE),
                ),
                and_(
                    fo.logistics_status.in_(
                        [LogisticsStatus.ALLOCATED, LogisticsStatus.AWAITING_ALLOCATION]
                    ),
                    automation_on,
                ),
                and_(
                    fo.logistics_status == LogisticsStatus.NO_CARRIER_AVAILABLE,
                    fo.status_reason == CARRIERS_UNREACHABLE,
                ),
                and_(fo.logistics_status == LogisticsStatus.SETTLING, fo.next_check_at < cutoff),
            ),
        )
        .order_by(fo.id)
    )
    return [Item(r[0], r[1], r[2], r[3].value) for r in rows]


def stalled_shopify_syncs(db: Session, cutoff: datetime) -> list[Item]:
    rows = db.execute(
        select(
            Shipment.id,
            Shipment.shop_id,
            Shipment.shopify_order_name,
            Shipment.status,
            Shop.settings,
        )
        .join(Shop, Shop.id == Shipment.shop_id)
        .where(
            Shop.uninstalled_at.is_(None),
            Shipment.awb.is_not(None),
            Shipment.shopify_fulfillment_id.is_(None),
            Shipment.shopify_sync_status == ShopifySyncStatus.PENDING,
            Shipment.status.not_in(list(NOT_SYNCABLE)),
            Shipment.updated_at < cutoff,
        )
        .order_by(Shipment.id)
    )
    items: list[Item] = []
    for shipment_id, shop_id, name, status, raw_settings in rows:
        deferred = ShopSettings.load(raw_settings).fulfill_on == "PICKED_UP"
        if deferred and status in BEFORE_PICKUP:
            continue  # waiting for pickup by design
        items.append(Item(shipment_id, shop_id, name, status.value))
    return items


def failed_webhooks(db: Session, *, since: datetime, cutoff: datetime) -> list[Item]:
    rows = db.execute(
        select(WebhookEvent.id, WebhookEvent.source, WebhookEvent.topic)
        .where(
            WebhookEvent.processing_status == WebhookStatus.FAILED,
            WebhookEvent.received_at >= since,
            WebhookEvent.received_at < cutoff,
        )
        .order_by(WebhookEvent.id)
    )
    return [Item(r[0], None, f"{r[1]} {r[2]}", WebhookStatus.FAILED.value) for r in rows]


# --- checks ------------------------------------------------------------------------------------


def run_health_checks(*, now: datetime | None = None) -> dict[str, int]:
    settings = get_settings()
    now = now or utcnow()
    cutoff = now - timedelta(minutes=settings.ops_stuck_minutes)
    minutes = settings.ops_stuck_minutes
    raised: Counter[str] = Counter()

    with session_scope() as db:
        for shop_id, items in _by_shop(stuck_fulfillment_orders(db, cutoff)).items():
            if _alert_new(
                db,
                now=now,
                kind=alerts.AlertKind.STUCK_ORDERS,
                severity=AlertSeverity.WARNING,
                shop_id=shop_id,
                items=items,
                title=f"Orders have not progressed for {minutes}+ minutes",
                hint="Open each order in Trekiva Logistics and re-run allocation or retry. If "
                "many are stuck, check that the worker and beat containers are running.",
            ):
                raised[alerts.AlertKind.STUCK_ORDERS.value] += 1

        for shop_id, items in _by_shop(stalled_shopify_syncs(db, cutoff)).items():
            if _alert_new(
                db,
                now=now,
                kind=alerts.AlertKind.SHOPIFY_SYNC_STALLED,
                severity=AlertSeverity.ERROR,
                shop_id=shop_id,
                items=items,
                title=f"AWBs not sent to Shopify after {minutes}+ minutes",
                hint="Customers have no tracking yet. Use Retry Shopify sync on the order page; "
                "check the Logs page for Shopify errors.",
            ):
                raised[alerts.AlertKind.SHOPIFY_SYNC_STALLED.value] += 1

        webhooks = failed_webhooks(db, since=now - timedelta(hours=24), cutoff=cutoff)
        if _alert_new(
            db,
            now=now,
            kind=alerts.AlertKind.WEBHOOKS_FAILING,
            severity=AlertSeverity.ERROR,
            shop_id=None,
            items=webhooks,
            title="Webhook events failed in the last 24 hours",
            hint="See the error per event: SELECT id, topic, error_message FROM webhook_events "
            "WHERE processing_status = 'FAILED' ORDER BY id DESC;",
        ):
            raised[alerts.AlertKind.WEBHOOKS_FAILING.value] += 1

        if _check_queues(db, settings.ops_queue_backlog_threshold):
            raised[alerts.AlertKind.QUEUE_BACKLOG.value] += 1

        db.execute(
            delete(Alert).where(
                Alert.created_at < now - ALERT_RETENTION,
                Alert.status.in_([AlertStatus.SENT, AlertStatus.SKIPPED, AlertStatus.FAILED]),
            )
        )
    return dict(raised)


def _by_shop(items: Iterable[Item]) -> dict[int | None, list[Item]]:
    grouped: dict[int | None, list[Item]] = defaultdict(list)
    for item in items:
        grouped[item.shop_id].append(item)
    return grouped


def _alert_new(
    db: Session,
    *,
    now: datetime,
    kind: alerts.AlertKind,
    severity: AlertSeverity,
    shop_id: int | None,
    items: list[Item],
    title: str,
    hint: str,
) -> bool:
    """Raise one alert if `items` contains anything not reported by this kind's alert for this
    shop in the last REMIND_AFTER. The alert stores every current id, so items that stay stuck
    are not reported again until the reminder is due."""
    if not items:
        return False
    fingerprint = f"{kind.value}:{shop_id or '-'}"
    previous = db.scalar(
        select(Alert.data)
        .where(Alert.fingerprint == fingerprint, Alert.created_at >= now - REMIND_AFTER)
        .order_by(Alert.id.desc())
        .limit(1)
    )
    known = set((previous or {}).get("ids", []))
    # Compare only what can be stored, or the oldest ids would look new on every run.
    tracked = [i.id for i in items][-MAX_TRACKED_IDS:]
    if not set(tracked) - known:
        return False
    by_status = Counter(i.status for i in items)
    names = ", ".join(i.label for i in items[:MAX_NAMES])
    more = f" and {len(items) - MAX_NAMES} more" if len(items) > MAX_NAMES else ""
    alerts.raise_alert(
        db,
        kind=kind,
        severity=severity,
        title=title,
        detail=(
            f"{len(items)} affected ("
            + ", ".join(f"{status}: {count}" for status, count in sorted(by_status.items()))
            + f"): {names}{more}. {hint}"
        ),
        fingerprint=fingerprint,
        shop_id=shop_id,
        data={"ids": tracked},
    )
    return True


def _check_queues(db: Session, threshold: int) -> bool:
    try:
        lengths = heartbeat.queue_lengths()
    except Exception as exc:  # Redis down: the worker running this could not have started
        log.warning("Could not read queue lengths", extra={"error": repr(exc)})
        return False
    backlog = {q: n for q, n in lengths.items() if n > threshold}
    fingerprint = f"{alerts.AlertKind.QUEUE_BACKLOG.value}:-"
    if not backlog or alerts.has_open_alert(db, fingerprint):
        return False
    alerts.raise_alert(
        db,
        kind=alerts.AlertKind.QUEUE_BACKLOG,
        severity=AlertSeverity.WARNING,
        title="Background work is piling up",
        detail=", ".join(f"queue {q}: {n} waiting" for q, n in sorted(backlog.items()))
        + ". Check worker logs; consider more worker concurrency.",
        fingerprint=fingerprint,
    )
    return True
