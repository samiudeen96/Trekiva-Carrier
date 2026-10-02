"""Idempotent webhook storage. Ingestion only stores; processing happens in a worker."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.enums import WebhookStatus
from app.models import WebhookEvent

#: Headers worth keeping for debugging. Never store authentication headers.
_KEEP_HEADERS = {
    "x-shopify-topic",
    "x-shopify-shop-domain",
    "x-shopify-event-id",
    "x-shopify-webhook-id",
    "x-shopify-triggered-at",
    "x-shopify-api-version",
    "content-type",
    "user-agent",
}


def payload_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() in _KEEP_HEADERS}


def store_event(
    db: Session,
    *,
    source: str,
    topic: str,
    external_event_id: str | None,
    shop_domain: str | None,
    raw_body: bytes,
    payload: dict[str, Any],
    headers: Mapping[str, str],
    status: WebhookStatus = WebhookStatus.RECEIVED,
    error_message: str | None = None,
) -> tuple[WebhookEvent, bool]:
    """Insert the event unless (source, external_event_id) already exists.

    Returns (event, created). `created` is False for a duplicate delivery, which callers must
    acknowledge without processing again. When the sender gives no event id, the payload hash
    is used, so byte-identical redeliveries are still deduplicated.
    """
    digest = payload_hash(raw_body)
    event_id = external_event_id or f"sha256:{digest}"
    stmt = (
        insert(WebhookEvent)
        .values(
            source=source,
            topic=topic,
            external_event_id=event_id,
            shop_domain=shop_domain,
            payload_hash=digest,
            payload=payload,
            headers=safe_headers(headers),
            processing_status=status.value,
            error_message=error_message,
        )
        .on_conflict_do_nothing(
            index_elements=[WebhookEvent.source, WebhookEvent.external_event_id]
        )
        .returning(WebhookEvent.id)
    )
    inserted_id = db.execute(stmt).scalar_one_or_none()
    if inserted_id is not None:
        event = db.get(WebhookEvent, inserted_id)
        assert event is not None
        return event, True
    existing = db.scalar(
        select(WebhookEvent).where(
            WebhookEvent.source == source, WebhookEvent.external_event_id == event_id
        )
    )
    assert existing is not None
    return existing, False
