"""Prometheus metrics, computed from the database and Redis at scrape time.

Every value is a gauge read from shared state, so the numbers are identical whichever gunicorn
worker answers the scrape (no per-process counters, no multiprocess registry).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.config import get_settings
from app.core.enums import (
    AlertStatus,
    LogisticsStatus,
    ShipmentStatus,
    ShopifySyncStatus,
    WebhookStatus,
)
from app.core.time import utcnow
from app.models import Alert, Shipment, ShopifyFulfillmentOrder, WebhookEvent
from app.ops import heartbeat
from app.ops.checks import stalled_shopify_syncs, stuck_fulfillment_orders

Labels = dict[str, str]


@dataclass
class Metric:
    name: str
    help: str
    samples: list[tuple[Labels, float]] = field(default_factory=list)


def collect(db: Session) -> list[Metric]:
    cutoff = utcnow() - timedelta(minutes=get_settings().ops_stuck_minutes)
    metrics = [
        _by_status(
            db,
            "trekiva_fulfillment_orders",
            "Fulfillment orders by logistics status.",
            ShopifyFulfillmentOrder.logistics_status,
            LogisticsStatus,
        ),
        _by_status(db, "trekiva_shipments", "Shipments by status.", Shipment.status, ShipmentStatus),
        _by_status(
            db,
            "trekiva_shipments_shopify_sync",
            "Shipments by Shopify sync status.",
            Shipment.shopify_sync_status,
            ShopifySyncStatus,
        ),
        _by_status(
            db,
            "trekiva_webhook_events",
            "Stored webhook events by processing status.",
            WebhookEvent.processing_status,
            WebhookStatus,
        ),
        _by_status(db, "trekiva_alerts", "Alerts by delivery status.", Alert.status, AlertStatus),
        Metric(
            "trekiva_stuck_fulfillment_orders",
            "Fulfillment orders with no progress for OPS_STUCK_MINUTES.",
            [({}, float(len(stuck_fulfillment_orders(db, cutoff))))],
        ),
        Metric(
            "trekiva_stalled_shopify_syncs",
            "AWBs not sent to Shopify within OPS_STUCK_MINUTES.",
            [({}, float(len(stalled_shopify_syncs(db, cutoff))))],
        ),
    ]
    redis_up = Metric("trekiva_redis_up", "1 if Redis answered this scrape.")
    try:
        lengths = heartbeat.queue_lengths()
        age = heartbeat.age_seconds()
    except Exception:
        redis_up.samples.append(({}, 0.0))
    else:
        redis_up.samples.append(({}, 1.0))
        metrics.append(
            Metric(
                "trekiva_queue_length",
                "Celery messages waiting per queue.",
                [({"queue": q}, float(n)) for q, n in sorted(lengths.items())],
            )
        )
        if age is not None:
            metrics.append(
                Metric(
                    "trekiva_worker_heartbeat_age_seconds",
                    "Seconds since a worker last ran the beat-scheduled heartbeat.",
                    [({}, round(age, 1))],
                )
            )
    metrics.append(redis_up)
    return metrics


def _by_status[E: StrEnum](
    db: Session,
    name: str,
    help_text: str,
    column: InstrumentedAttribute[E],
    values: type[E],
) -> Metric:
    counts = {status: n for status, n in db.execute(select(column, func.count()).group_by(column))}
    return Metric(
        name,
        help_text,
        [({"status": v.value}, float(counts.get(v, 0))) for v in values],
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(metrics: Sequence[Metric]) -> str:
    """Prometheus text exposition format 0.0.4 (every metric is a gauge)."""
    lines: list[str] = []
    for metric in metrics:
        lines.append(f"# HELP {metric.name} {metric.help}")
        lines.append(f"# TYPE {metric.name} gauge")
        for labels, value in metric.samples:
            text = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
            number = int(value) if value.is_integer() else value
            lines.append(f"{metric.name}{{{text}}} {number}" if text else f"{metric.name} {number}")
    return "\n".join(lines) + "\n"
