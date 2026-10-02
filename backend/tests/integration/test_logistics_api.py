"""Phase 2 HTTP surface: carrier tracking webhooks and the admin logistics API."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus, ShipmentStatus, WebhookStatus
from app.core.time import utcnow
from app.main import app
from app.models import CarrierAccount, Shipment, Shop, ShopifyFulfillmentOrder, WebhookEvent
from app.services import carriers
from app.workers import enqueue as q
from tests.factories import FakeShopifyAdmin, order_json
from tests.harness import Runner, TaskQueue, setup_logistics
from tests.integration.test_admin_api import token
from tests.integration.test_shipping_flow import place_order

pytestmark = pytest.mark.db


@pytest.fixture
def admin() -> FakeShopifyAdmin:
    return FakeShopifyAdmin()


@pytest.fixture
def runner(
    task_queue: TaskQueue, admin: FakeShopifyAdmin, monkeypatch: pytest.MonkeyPatch
) -> Runner:
    r = Runner(task_queue, admin)
    # Admin endpoints that call Shopify inline go through the default factory, which looks up
    # `shops.shopify_admin` at call time.
    monkeypatch.setattr("app.services.shops.shopify_admin", r.factory)
    return r


@pytest.fixture
def api(shop: Shop) -> TestClient:
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token()}"
    return client


@pytest.fixture
def shipped(db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner) -> Shipment:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    db.expire_all()
    shipment = db.scalar(select(Shipment))
    assert shipment is not None and shipment.status == ShipmentStatus.AWB_CREATED
    return shipment


def account(db: Session, code: str) -> CarrierAccount:
    acc = db.scalar(select(CarrierAccount).where(CarrierAccount.carrier_code == code))
    assert acc is not None
    return acc


# --- carrier tracking webhook --------------------------------------------------------------------


def post_tracking(
    client: TestClient,
    db: Session,
    payload: Any,
    *,
    sign: bool = True,
    token_value: str | None = None,
) -> Any:
    acc = account(db, "xpressbees")
    adapter = carriers.build_adapter_for_account(db, acc)
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Mock-Signature"] = adapter.sign_webhook(body)  # type: ignore[attr-defined]
    return client.post(
        f"/webhooks/carriers/xpressbees/{token_value or acc.webhook_token}",
        content=body,
        headers=headers,
    )


def test_tracking_webhook_updates_shipment_and_shopify(
    db: Session, shop: Shop, shipped: Shipment, runner: Runner, admin: FakeShopifyAdmin
) -> None:
    client = TestClient(app)
    payload = {
        "awb": shipped.awb,
        "status": "OFD",
        "timestamp": utcnow().isoformat(),
        "location": "Indiranagar",
    }
    resp = post_tracking(client, db, payload)
    assert resp.status_code == 200 and resp.json() == {"status": "accepted", "updates": 1}
    assert runner.queue.names() == [q.WEBHOOK_PROCESS]
    runner.drain()
    db.expire_all()
    shipment = db.get(Shipment, shipped.id)
    assert shipment is not None and shipment.status == ShipmentStatus.OUT_FOR_DELIVERY
    assert (
        admin.events[-1]["status"] == "OUT_FOR_DELIVERY"
        and admin.events[-1]["city"] == "Indiranagar"
    )

    again = post_tracking(client, db, payload)  # identical redelivery
    assert again.json()["status"] == "duplicate"


def test_tracking_webhook_rejects_bad_signature_and_unknown_token(
    db: Session, shipped: Shipment
) -> None:
    client = TestClient(app)
    payload = {"awb": shipped.awb, "status": "OFD"}
    assert post_tracking(client, db, payload, sign=False).status_code == 401
    assert post_tracking(client, db, payload, token_value="not-a-token").status_code == 404
    assert db.scalar(select(WebhookEvent).where(WebhookEvent.source == "xpressbees")) is None


def test_tracking_webhook_unknown_awb_is_ignored(
    db: Session, shipped: Shipment, runner: Runner
) -> None:
    post_tracking(TestClient(app), db, {"awb": "NOPE", "status": "OFD"})
    runner.drain()
    db.expire_all()
    event = db.scalar(select(WebhookEvent).where(WebhookEvent.source == "xpressbees"))
    assert event is not None and event.processing_status == WebhookStatus.IGNORED
    assert "Unknown AWB" in (event.error_message or "")


def test_unparseable_tracking_webhook_is_400(db: Session, shipped: Shipment) -> None:
    resp = post_tracking(TestClient(app), db, {"no_awb": True})
    assert resp.status_code == 400
    event = db.scalar(select(WebhookEvent).where(WebhookEvent.source == "xpressbees"))
    assert event is not None and event.processing_status == WebhookStatus.FAILED


# --- admin reads -------------------------------------------------------------------------------


def test_orders_list_detail_logs(api: TestClient, shipped: Shipment) -> None:
    listing = api.get("/api/admin/orders").json()
    assert listing["total"] == 1
    row = listing["items"][0]
    assert (row["order_name"], row["logistics_status"], row["payment_mode"]) == (
        "TR-001500",
        "SHIPPED",
        "PREPAID",
    )
    assert row["shipment"]["awb"] == shipped.awb and row["destination"]["pincode"] == "560001"
    assert api.get("/api/admin/orders", params={"q": shipped.awb}).json()["total"] == 1
    assert api.get("/api/admin/orders", params={"status": "FAILED"}).json()["total"] == 0
    assert api.get("/api/admin/orders", params={"status": "BOGUS"}).status_code == 422

    detail = api.get(f"/api/admin/orders/{row['order_id']}").json()
    [fo] = detail["fulfillment_orders"]
    assert fo["shipments"][0]["awb"] == shipped.awb
    assert {c["carrier_code"] for c in fo["latest_run"]["checks"]} == {"ekart", "xpressbees"}
    assert any(qt["selected"] for qt in fo["latest_run"]["quotes"])
    assert fo["tracking"][0]["status"] == "AWB_CREATED"
    assert detail["logs"][0]["step"] == "ORDER_RECEIVED"  # chronological

    shipments = api.get("/api/admin/shipments", params={"carrier": "xpressbees"}).json()
    assert shipments["items"][0]["can_cancel"] is True
    assert api.get("/api/admin/tracking").json()[0]["awb"] == shipped.awb
    logs = api.get("/api/admin/logs", params={"step": "AWB_GENERATED"}).json()
    assert logs["total"] == 1 and shipped.awb in logs["items"][0]["message"]


def test_preview_allocation_changes_nothing(
    api: TestClient, db: Session, shipped: Shipment
) -> None:
    resp = api.post(
        f"/api/admin/fulfillment-orders/{shipped.fulfillment_order_id}/preview-allocation"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["selected"] == "xpressbees" and [r["carrier_code"] for r in body["ranked"]] == [
        "xpressbees",
        "ekart",
    ]
    db.expire_all()
    fo = db.get(ShopifyFulfillmentOrder, shipped.fulfillment_order_id)
    assert fo is not None and fo.logistics_status == LogisticsStatus.SHIPPED


# --- overrides -----------------------------------------------------------------------------------


def test_carrier_change_requires_cancellation_first(
    api: TestClient,
    db: Session,
    shop: Shop,
    shipped: Shipment,
    runner: Runner,
    admin: FakeShopifyAdmin,
) -> None:
    fo_id = shipped.fulfillment_order_id
    blocked = api.post(
        f"/api/admin/fulfillment-orders/{fo_id}/allocate", json={"carrier_code": "ekart"}
    )
    assert blocked.status_code == 409 and "Cancel it" in blocked.json()["message"]

    cancelled = api.post(
        f"/api/admin/shipments/{shipped.id}/cancel",
        json={"reallocate": False, "reason": "Wrong carrier"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert admin.cancelled_fulfillments == [shipped.shopify_fulfillment_id]
    db.expire_all()
    fo = db.get(ShopifyFulfillmentOrder, fo_id)
    assert fo is not None and fo.logistics_status == LogisticsStatus.AUTOMATION_DISABLED
    assert fo.order.automation_disabled is True

    # Shopify reopens the fulfillment order once the fulfillment is cancelled.
    raw = admin.orders["gid://shopify/Order/1500"]
    nodes = raw["fulfillmentOrders"]["nodes"][0]
    nodes["status"] = "OPEN"
    for li in nodes["lineItems"]["nodes"]:
        li["remainingQuantity"] = 1

    chosen = api.post(
        f"/api/admin/fulfillment-orders/{fo_id}/allocate", json={"carrier_code": "ekart"}
    )
    assert chosen.status_code == 200 and chosen.json()["forced_carrier"] == "ekart"
    runner.drain()
    db.expire_all()
    rows = list(db.scalars(select(Shipment).order_by(Shipment.id)))
    assert [(s.carrier_code, s.status) for s in rows] == [
        ("xpressbees", ShipmentStatus.CANCELLED),
        ("ekart", ShipmentStatus.AWB_CREATED),
    ]
    # Automation stays off for the order (staff shipped it explicitly).
    fo = db.get(ShopifyFulfillmentOrder, fo_id)
    assert (
        fo is not None
        and fo.logistics_status == LogisticsStatus.SHIPPED
        and fo.order.automation_disabled
    )


def test_cancel_with_reallocate(
    api: TestClient, db: Session, shipped: Shipment, runner: Runner, admin: FakeShopifyAdmin
) -> None:
    raw = admin.orders["gid://shopify/Order/1500"]
    resp = api.post(f"/api/admin/shipments/{shipped.id}/cancel", json={"reallocate": True})
    assert resp.status_code == 200
    node = raw["fulfillmentOrders"]["nodes"][0]
    node["status"] = "OPEN"
    for li in node["lineItems"]["nodes"]:
        li["remainingQuantity"] = 1
    assert runner.queue.names()[-1] == q.ALLOCATE
    runner.drain()
    db.expire_all()
    statuses = [s.status for s in db.scalars(select(Shipment).order_by(Shipment.id))]
    assert statuses == [ShipmentStatus.CANCELLED, ShipmentStatus.AWB_CREATED]


def test_cannot_cancel_after_pickup(api: TestClient, db: Session, shipped: Shipment) -> None:
    resp = api.post(
        f"/api/admin/shipments/{shipped.id}/simulate-tracking", json={"status": "PICKED_UP"}
    )
    assert resp.status_code == 200 and resp.json()["applied"] is True
    blocked = api.post(f"/api/admin/shipments/{shipped.id}/cancel", json={})
    assert blocked.status_code == 409


def test_simulated_tracking_is_mock_only(
    api: TestClient, db: Session, shipped: Shipment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "app_env", "production")
    assert (
        api.post(
            f"/api/admin/shipments/{shipped.id}/simulate-tracking", json={"status": "DELIVERED"}
        ).status_code
        == 404
    )


def test_disable_automation_for_order(
    api: TestClient, db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    shop.settings = {**shop.settings, "settle_window_seconds": 0}
    db.commit()
    admin.put(order_json(created_at=utcnow() - timedelta(seconds=30), risk_levels=["PENDING"]))
    from app.logistics.pipeline import apply_order_snapshot
    from app.shopify.parse import parse_order

    apply_order_snapshot(
        db, shop, parse_order(admin.orders["gid://shopify/Order/1500"]), trigger="t"
    )
    db.commit()
    db.expire_all()
    fo = db.scalar(select(ShopifyFulfillmentOrder))
    assert fo is not None and fo.logistics_status == LogisticsStatus.SETTLING
    resp = api.post(f"/api/admin/orders/{fo.order_id}/automation", json={"disabled": True})
    assert resp.status_code == 200
    runner.drain()
    db.expire_all()
    assert (
        db.get(ShopifyFulfillmentOrder, fo.id).logistics_status
        == LogisticsStatus.AUTOMATION_DISABLED
    )  # type: ignore[union-attr]


def test_retry_failed_order(
    api: TestClient, db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999030")  # both carriers reject at create
    runner.drain()
    db.expire_all()
    fo = db.scalar(select(ShopifyFulfillmentOrder))
    assert fo is not None and fo.logistics_status == LogisticsStatus.FAILED
    # Staff fix the address in Shopify, then retry.
    admin.orders["gid://shopify/Order/1500"]["shippingAddress"]["zip"] = "560001"
    from app.logistics.pipeline import apply_order_snapshot
    from app.shopify.parse import parse_order

    apply_order_snapshot(
        db, shop, parse_order(admin.orders["gid://shopify/Order/1500"]), trigger="orders/updated"
    )
    db.commit()
    assert api.post(f"/api/admin/fulfillment-orders/{fo.id}/allocate", json={}).status_code == 200
    runner.drain()
    db.expire_all()
    assert db.get(ShopifyFulfillmentOrder, fo.id).logistics_status == LogisticsStatus.SHIPPED  # type: ignore[union-attr]


def test_retry_shopify_sync_endpoint(
    api: TestClient, db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    from app.shopify.errors import ShopifyUserError

    setup_logistics(db, shop)
    admin.fulfillment_error = ShopifyUserError("rejected", user_errors=[{"message": "rejected"}])
    place_order(db, shop, admin)
    runner.drain()
    shipment = db.scalar(select(Shipment))
    assert shipment is not None
    admin.fulfillment_error = None
    resp = api.post(f"/api/admin/shipments/{shipment.id}/sync-shopify")
    assert resp.status_code == 200 and resp.json()["outcome"] == "synced"
    assert resp.json()["shipment"]["shopify_sync_status"] == "SYNCED"
