"""Deliver queued alerts (Celery beat: `alerts.dispatch`, every 30 s).

    1. (transaction) claim due PENDING rows (FOR UPDATE SKIP LOCKED), group them by fingerprint,
       mark them SENDING and render one message per group, then COMMIT
    2. (no locks)    send each message to every channel that has not delivered it yet
    3. (transaction) record the outcome per row: SENT, or back to PENDING with backoff (only
       the failed channels are retried), or FAILED after MAX_ATTEMPTS

Rate limiting: once a fingerprint has been sent, newer rows with that fingerprint wait until
ALERT_COOLDOWN_MINUTES have passed and then go out together as one message. A burst of 50
failing orders is one message now and one summary later, not 50 messages.
A dispatcher that dies mid-send leaves rows SENDING; they are retried after CLAIM_TIMEOUT. While
a fingerprint is SENDING, overlapping runs leave its new rows for later (no cooldown bypass).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.alerts.channels import AlertMessage, Channel, configured_channels
from app.core.config import get_settings
from app.core.db import session_scope
from app.core.enums import AlertSeverity, AlertStatus
from app.core.time import utcnow
from app.models import Alert, Shop, ShopifyOrder
from app.workers.retry import backoff_seconds

log = logging.getLogger("trekiva.alerts")

MAX_ATTEMPTS = 8
CLAIM_TIMEOUT = timedelta(minutes=30)
BATCH_LIMIT = 500
MAX_LINES = 10


@dataclass
class _Batch:
    ids: list[int]
    channels: list[Channel]
    message: AlertMessage


def dispatch_pending(
    *, channels: Sequence[Channel] | None = None, now: datetime | None = None
) -> dict[str, int]:
    settings = get_settings()
    active = list(configured_channels(settings) if channels is None else channels)
    now = now or utcnow()
    cooldown = timedelta(minutes=settings.alert_cooldown_minutes)
    min_severity = AlertSeverity(settings.alert_min_severity)
    summary: Counter[str] = Counter()
    batches: list[_Batch] = []

    # --- 1. claim + render ----------------------------------------------------------------
    with session_scope() as db:
        db.execute(
            update(Alert)
            .where(Alert.status == AlertStatus.SENDING, Alert.claimed_at < now - CLAIM_TIMEOUT)
            .values(status=AlertStatus.PENDING, claimed_at=None)
        )
        rows = list(
            db.scalars(
                select(Alert)
                .where(
                    Alert.status == AlertStatus.PENDING,
                    or_(Alert.next_attempt_at.is_(None), Alert.next_attempt_at <= now),
                )
                .order_by(Alert.id)
                .limit(BATCH_LIMIT)
                .with_for_update(skip_locked=True)
            )
        )
        groups: dict[tuple[str, tuple[str, ...]], list[Alert]] = defaultdict(list)
        for row in rows:
            if not active or row.severity.rank < min_severity.rank:
                row.status = AlertStatus.SKIPPED
                summary["skipped"] += 1
                continue
            groups[(row.fingerprint, tuple(sorted(row.delivered_channels)))].append(row)

        for (fingerprint, delivered), group in groups.items():
            todo = [c for c in active if c.name not in delivered]
            if not todo:  # every channel now configured already has it
                for row in group:
                    row.status = AlertStatus.SENT
                continue
            if not delivered:
                in_flight = db.scalar(
                    select(Alert.id)
                    .where(Alert.fingerprint == fingerprint, Alert.status == AlertStatus.SENDING)
                    .limit(1)
                )
                if in_flight is not None:  # an overlapping run is sending it right now
                    summary["deferred"] += len(group)
                    continue
                last_sent = db.scalar(
                    select(func.max(Alert.sent_at)).where(Alert.fingerprint == fingerprint)
                )
                if last_sent is not None and now - last_sent < cooldown:
                    for row in group:
                        row.next_attempt_at = last_sent + cooldown
                    summary["deferred"] += len(group)
                    continue
            for row in group:
                row.status = AlertStatus.SENDING
                row.claimed_at = now
            batches.append(_Batch([r.id for r in group], todo, render_group(db, group)))

    # --- 2. send (no locks held) ----------------------------------------------------------
    for batch in batches:
        errors: dict[str, str] = {}
        for channel in batch.channels:
            try:
                channel.send(batch.message)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                errors[channel.name] = error
                log.error(
                    "Alert delivery failed",
                    extra={"channel": channel.name, "alert_ids": batch.ids, "error": error},
                )
        delivered_now = [c.name for c in batch.channels if c.name not in errors]

        # --- 3. record outcome --------------------------------------------------------------
        with session_scope() as db:
            sent_at = utcnow()
            for row in db.scalars(select(Alert).where(Alert.id.in_(batch.ids)).with_for_update()):
                row.claimed_at = None
                row.delivered_channels = sorted(set(row.delivered_channels) | set(delivered_now))
                if delivered_now and row.sent_at is None:
                    row.sent_at = sent_at
                if not errors:
                    row.status = AlertStatus.SENT
                    row.last_error = None
                    summary["sent"] += 1
                    continue
                row.attempts += 1
                row.last_error = "; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = AlertStatus.FAILED
                    summary["failed"] += 1
                else:
                    row.status = AlertStatus.PENDING
                    row.next_attempt_at = sent_at + timedelta(
                        seconds=backoff_seconds(row.attempts - 1)
                    )
                    summary["retry"] += 1
    return dict(summary)


# --- rendering ---------------------------------------------------------------------------------


def shopify_order_url(shop_domain: str, shopify_order_id: str) -> str:
    handle = shop_domain.removesuffix(".myshopify.com")
    numeric = shopify_order_id.rsplit("/", 1)[-1]
    return f"https://admin.shopify.com/store/{handle}/orders/{numeric}"


def render_message(
    *,
    severity: AlertSeverity,
    title: str,
    shop_domain: str | None,
    lines: Sequence[str],
    count: int,
    first_seen: datetime,
    last_seen: datetime,
) -> AlertMessage:
    subject = title if count == 1 else f"{title} ({count}x)"
    body: list[str] = []
    if shop_domain:
        body.append(f"Store: {shop_domain}")
    unique = list(dict.fromkeys(line for line in lines if line))
    body.extend(f"- {line}" for line in unique[:MAX_LINES])
    if len(unique) > MAX_LINES:
        body.append(f"- ... and {len(unique) - MAX_LINES} more")
    if count > 1:
        body.append(
            f"{count} occurrences between {first_seen:%Y-%m-%d %H:%M} and "
            f"{last_seen:%Y-%m-%d %H:%M} UTC."
        )
    body.append("Details: Trekiva Logistics in Shopify admin (Orders, Logs).")
    return AlertMessage(severity=severity, subject=subject, body="\n".join(body))


def render_group(db: Session, group: Sequence[Alert]) -> AlertMessage:
    first = group[0]
    shop = db.get(Shop, first.shop_id) if first.shop_id else None
    order_ids = {a.order_id for a in group if a.order_id}
    orders = (
        {o.id: o for o in db.scalars(select(ShopifyOrder).where(ShopifyOrder.id.in_(order_ids)))}
        if order_ids
        else {}
    )
    lines: list[str] = []
    for alert in group:
        order = orders.get(alert.order_id) if alert.order_id else None
        if order is None:
            lines.append(alert.detail)
            continue
        line = f"{order.name}: {alert.detail}" if alert.detail else order.name
        if shop is not None:
            line += f" ({shopify_order_url(shop.shop_domain, order.shopify_order_id)})"
        lines.append(line)
    return render_message(
        severity=max((a.severity for a in group), key=lambda s: s.rank),
        title=first.title,
        shop_domain=shop.shop_domain if shop else None,
        lines=lines,
        count=len(group),
        first_seen=min(a.created_at for a in group),
        last_seen=max(a.created_at for a in group),
    )


def send_test(channels: Sequence[Channel] | None = None) -> dict[str, str]:
    """Send a test message straight to every configured channel (bypasses the outbox)."""
    active = list(configured_channels(get_settings()) if channels is None else channels)
    message = AlertMessage(
        severity=AlertSeverity.WARNING,
        subject="Test alert",
        body="Trekiva Logistics alerts are working. No action needed.",
    )
    results: dict[str, str] = {}
    for channel in active:
        try:
            channel.send(message)
            results[channel.name] = "ok"
        except Exception as exc:
            results[channel.name] = f"{type(exc).__name__}: {exc}"
    return results
