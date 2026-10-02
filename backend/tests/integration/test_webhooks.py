"""Webhook ingress and processing: HMAC, idempotency, dispatch to the pipeline."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus, WebhookStatus
from app.core.time import utcnow
from app.main import app
from app.models import Shop, ShopifyFulfillmentOrder, WebhookEvent
from app.shopify.errors import ShopifyTransientError
from app.shopify.security import compute_webhook_hmac
from app.webhooks.processor import process_event, reset_stale_processing
from tests.conftest import SHOP_DOMAIN
from tests.factories import DUPLICATE_HOLD, FakeShopifyAdmin, fo_json, order_json

pytestmark = pytest.mark.db

SECRET = "test-client-secret-0123456789abcdef"


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(
        "app.webhooks.shopify.enqueue_webhook_event",
        lambda event_id: calls.append(event_id) or True,
    )
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def post_webhook(
    client: TestClient,
    topic: str,
    payload: dict[str, Any],
    *,
    event_id: str = "evt-1",
    shop: str = SHOP_DOMAIN,
    hmac_value: str | None = None,
) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/shopify",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": shop,
            "X-Shopify-Event-Id": event_id,
            "X-Shopify-Hmac-Sha256": hmac_value
            if hmac_value is not None
            else compute_webhook_hmac(body, SECRET),
        },
    )


def event_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(WebhookEvent)) or 0)


# --- ingress --------------------------------------------------------------------------------


def test_valid_webhook_is_stored_and_enqueued(
    db: Session, shop: Shop, client: TestClient, enqueued: list[int]
) -> None:
    resp = post_webhook(
        client, "orders/create", {"admin_graphql_api_id": "gid://shopify/Order/1500"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "accepted"
    event = db.scalar(select(WebhookEvent))
    assert event is not None
    assert (event.topic, event.external_event_id, event.processing_status) == (
        "orders/create",
        "evt-1",
        WebhookStatus.RECEIVED,
    )
    assert "x-shopify-hmac-sha256" not in event.headers  # auth headers never stored
    assert enqueued == [event.id]


def test_duplicate_webhook_is_acknowledged_but_not_reprocessed(
    db: Session, shop: Shop, client: TestClient, enqueued: list[int]
) -> None:
    payload = {"admin_graphql_api_id": "gid://shopify/Order/1500"}
    first = post_webhook(client, "orders/create", payload, event_id="evt-dup")
    second = post_webhook(client, "orders/create", payload, event_id="evt-dup")
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert event_count(db) == 1
    assert len(enqueued) == 1


def test_invalid_hmac_rejected(
    db: Session, shop: Shop, client: TestClient, enqueued: list[int]
) -> None:
    resp = post_webhook(client, "orders/create", {"id": 1}, hmac_value="forged")
    assert resp.status_code == 401
    assert event_count(db) == 0 and not enqueued


def test_disallowed_shop_is_stored_as_ignored(
    db: Session, client: TestClient, enqueued: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOPIFY_SHOP_ALLOWLIST", SHOP_DOMAIN)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        resp = post_webhook(client, "orders/create", {"id": 1}, shop="someone-else.myshopify.com")
    finally:
        monkeypatch.delenv("SHOPIFY_SHOP_ALLOWLIST")
        get_settings.cache_clear()
    assert resp.status_code == 200
    event = db.scalar(select(WebhookEvent))
    assert event is not None and event.processing_status == WebhookStatus.IGNORED
    assert not enqueued


# --- processing -----------------------------------------------------------------------------


def store(db: Session, topic: str, payload: dict[str, Any], event_id: str = "evt-p") -> int:
    from app.webhooks.ingest import store_event

    event, _ = store_event(
        db,
        source="shopify",
        topic=topic,
        external_event_id=event_id,
        shop_domain=SHOP_DOMAIN,
        raw_body=json.dumps(payload).encode(),
        payload=payload,
        headers={},
    )
    db.commit()
    return event.id


def fo_row(db: Session) -> ShopifyFulfillmentOrder:
    db.expire_all()
    fo = db.scalar(select(ShopifyFulfillmentOrder))
    assert fo is not None
    return fo


def admin_with(raw: dict[str, Any]) -> FakeShopifyAdmin:
    fake = FakeShopifyAdmin()
    fake.put(raw)
    return fake


def test_safe_order_reaches_awaiting_allocation(db: Session, shop: Shop) -> None:
    fake = admin_with(order_json(created_at=utcnow() - timedelta(hours=1)))
    event_id = store(db, "orders/create", {"admin_graphql_api_id": "gid://shopify/Order/1500"})
    result = process_event(event_id, admin_factory=lambda db, shop: fake)  # type: ignore[arg-type,return-value]
    assert result.status == WebhookStatus.PROCESSED
    assert fo_row(db).logistics_status == LogisticsStatus.AWAITING_ALLOCATION
    assert fake.calls == ["gid://shopify/Order/1500"]  # live state was re-read from Shopify


def test_held_order_then_hold_release(db: Session, shop: Shop) -> None:
    raw = order_json(
        tags=["DUPLICATE-REVIEW"],
        created_at=utcnow() - timedelta(hours=1),
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD])],
    )
    fake = admin_with(raw)
    factory = lambda db, shop: fake  # noqa: E731

    process_event(
        store(db, "orders/create", {"admin_graphql_api_id": raw["id"]}, "e1"), admin_factory=factory
    )  # type: ignore[arg-type]
    fo = fo_row(db)
    assert fo.logistics_status == LogisticsStatus.MANUAL_REVIEW
    assert fo.status_reason == "SHOPIFY_FULFILLMENT_HOLD"
    assert fo.hold_last_seen_at is not None

    # Staff release the hold in Shopify (the DUPLICATE-REVIEW tag stays on the order).
    raw["fulfillmentOrders"]["nodes"] = [fo_json(status="OPEN")]
    payload = {"fulfillment_order": {"id": "gid://shopify/FulfillmentOrder/501", "status": "open"}}
    result = process_event(
        store(db, "fulfillment_orders/hold_released", payload, "e2"), admin_factory=factory
    )  # type: ignore[arg-type]
    assert result.status == WebhookStatus.PROCESSED
    fo = fo_row(db)
    assert fo.logistics_status == LogisticsStatus.AWAITING_ALLOCATION
    assert fo.hold_released_at is not None


def test_event_is_processed_only_once(db: Session, shop: Shop) -> None:
    fake = admin_with(order_json(created_at=utcnow() - timedelta(hours=1)))
    event_id = store(db, "orders/create", {"admin_graphql_api_id": "gid://shopify/Order/1500"})
    factory = lambda db, shop: fake  # noqa: E731
    assert process_event(event_id, admin_factory=factory).status == WebhookStatus.PROCESSED  # type: ignore[arg-type]
    assert process_event(event_id, admin_factory=factory).status is None  # type: ignore[arg-type]
    assert len(fake.calls) == 1


def test_transient_failure_is_marked_for_retry(db: Session, shop: Shop) -> None:
    fake = FakeShopifyAdmin(error=ShopifyTransientError("Shopify returned HTTP 502"))
    event_id = store(db, "orders/create", {"admin_graphql_api_id": "gid://shopify/Order/1500"})
    result = process_event(event_id, admin_factory=lambda db, shop: fake)  # type: ignore[arg-type,return-value]
    assert result.retry and result.status == WebhookStatus.FAILED
    db.expire_all()
    event = db.get(WebhookEvent, event_id)
    assert event is not None and event.attempts == 1 and "502" in (event.error_message or "")
    # A retry can claim a FAILED event again.
    fake.error = None
    fake.put(order_json(created_at=utcnow() - timedelta(hours=1)))
    assert (
        process_event(event_id, admin_factory=lambda db, shop: fake).status
        == WebhookStatus.PROCESSED
    )  # type: ignore[arg-type,return-value]


def test_unknown_topic_and_unknown_shop_are_ignored(db: Session, shop: Shop) -> None:
    assert (
        process_event(store(db, "products/update", {"id": 1}, "e-a")).status
        == WebhookStatus.IGNORED
    )
    from app.webhooks.ingest import store_event

    event, _ = store_event(
        db,
        source="shopify",
        topic="orders/create",
        external_event_id="e-b",
        shop_domain="nobody.myshopify.com",
        raw_body=b"{}",
        payload={},
        headers={},
    )
    db.commit()
    assert process_event(event.id).status == WebhookStatus.IGNORED


def test_app_uninstalled_revokes_tokens(db: Session, shop: Shop) -> None:
    process_event(store(db, "app/uninstalled", {"id": 1}))
    db.expire_all()
    refreshed = db.get(Shop, shop.id)
    assert (
        refreshed is not None
        and refreshed.access_token_enc is None
        and refreshed.uninstalled_at is not None
    )


def test_stale_processing_events_are_reset(db: Session, shop: Shop) -> None:
    event_id = store(db, "orders/create", {"id": 1})
    event = db.get(WebhookEvent, event_id)
    assert event is not None
    event.processing_status = WebhookStatus.PROCESSING
    event.processing_started_at = utcnow() - timedelta(hours=1)
    db.commit()
    assert reset_stale_processing(db) == [event_id]
