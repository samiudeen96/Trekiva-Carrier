"""Process one stored webhook event exactly once (claim -> dispatch -> mark)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.carriers.errors import CarrierError
from app.core.db import session_scope
from app.core.enums import LogLevel, WebhookStatus
from app.core.time import utcnow
from app.models import Shop, WebhookEvent
from app.services import audit, shops
from app.shopify.errors import ShopifyError
from app.webhooks.dispatch import (
    HANDLERS,
    AdminFactory,
    default_admin_factory,
    handle_carrier_tracking,
)

log = logging.getLogger(__name__)

STALE_PROCESSING_AFTER = timedelta(minutes=15)


@dataclass(frozen=True)
class ProcessResult:
    status: WebhookStatus | None
    """None when the event could not be claimed (already processed, or being processed)."""
    retry: bool = False
    error: str | None = None


def claim(db: Session, event_id: int) -> bool:
    """Atomically move RECEIVED/FAILED -> PROCESSING. Only one worker can win."""
    result = db.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.id == event_id,
            WebhookEvent.processing_status.in_([WebhookStatus.RECEIVED, WebhookStatus.FAILED]),
        )
        .values(
            processing_status=WebhookStatus.PROCESSING,
            processing_started_at=utcnow(),
            attempts=WebhookEvent.attempts + 1,
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ShopifyError | CarrierError):
        return exc.retryable
    from sqlalchemy.exc import OperationalError

    return isinstance(exc, OperationalError | ConnectionError | TimeoutError)


def process_event(
    event_id: int, *, admin_factory: AdminFactory = default_admin_factory
) -> ProcessResult:
    with session_scope() as db:
        if not claim(db, event_id):
            return ProcessResult(status=None)

    try:
        with session_scope() as db:
            event = db.get(WebhookEvent, event_id)
            assert event is not None
            handler = (
                HANDLERS.get(event.topic) if event.source == "shopify" else handle_carrier_tracking
            )
            shop = shops.get_by_domain(db, event.shop_domain or "") if event.shop_domain else None
            if handler is None or shop is None:
                note = "No handler for topic" if handler is None else "Unknown shop"
                _finish(db, event, WebhookStatus.IGNORED, note)
                return ProcessResult(status=WebhookStatus.IGNORED, error=note)
            result = handler(db, shop, event, admin_factory)
            status = WebhookStatus.IGNORED if result.ignored else WebhookStatus.PROCESSED
            _finish(db, event, status, result.note)
            return ProcessResult(status=status)
    except Exception as exc:
        transient = _is_transient(exc)
        message = f"{type(exc).__name__}: {exc}"[:2000]
        log.warning(
            "Webhook processing failed",
            extra={"event_id": event_id, "transient": transient},
            exc_info=not transient,
        )
        with session_scope() as db:
            event = db.get(WebhookEvent, event_id)
            if event is not None:
                event.processing_status = WebhookStatus.FAILED
                event.error_message = message
                _audit_failure(db, event, message, transient)
        return ProcessResult(status=WebhookStatus.FAILED, retry=transient, error=message)


def _finish(db: Session, event: WebhookEvent, status: WebhookStatus, note: str | None) -> None:
    event.processing_status = status
    event.processed_at = utcnow()
    event.error_message = note if status == WebhookStatus.IGNORED else None


def _audit_failure(db: Session, event: WebhookEvent, message: str, transient: bool) -> None:
    shop = db.scalar(select(Shop).where(Shop.shop_domain == (event.shop_domain or "")))
    if shop is None:
        return
    audit.record(
        db,
        shop_id=shop.id,
        step=audit.Step.ERROR,
        level=LogLevel.WARNING if transient else LogLevel.ERROR,
        message=f"Webhook {event.topic} failed ({'will retry' if transient else 'not retried'}): {message}",
        data={"webhook_event_id": event.id, "attempts": event.attempts},
    )


def reset_stale_processing(db: Session) -> list[int]:
    """Events stuck in PROCESSING (worker died) go back to RECEIVED."""
    cutoff = utcnow() - STALE_PROCESSING_AFTER
    rows = db.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.processing_status == WebhookStatus.PROCESSING,
            WebhookEvent.processing_started_at < cutoff,
        )
        .values(processing_status=WebhookStatus.RECEIVED)
        .returning(WebhookEvent.id)
    )
    return [r[0] for r in rows]


def unprocessed_event_ids(db: Session, *, older_than: timedelta, limit: int = 500) -> list[int]:
    cutoff = utcnow() - older_than
    return list(
        db.scalars(
            select(WebhookEvent.id)
            .where(
                WebhookEvent.processing_status == WebhookStatus.RECEIVED,
                WebhookEvent.received_at < cutoff,
            )
            .order_by(WebhookEvent.id)
            .limit(limit)
        )
    )
