"""Embedded admin API: authentication, carrier configuration, secrets, rules, review queue."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import get_encryptor
from app.core.enums import CarrierEnvironment, LogisticsStatus, PaymentMode
from app.core.time import utcnow
from app.logistics.allocation import allocate, prefilter
from app.logistics.offers import collect_offers
from app.logistics.pipeline import apply_order_snapshot
from app.main import app
from app.models import CarrierAccount, Shop, ShopifyFulfillmentOrder
from app.services import carriers
from tests.conftest import SHOP_DOMAIN
from tests.factories import RISK_HOLD, ctx, fo_json, order_snapshot, serviceability_request

pytestmark = pytest.mark.db

SECRET = "test-client-secret-0123456789abcdef"


def token(shop: str = SHOP_DOMAIN) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": f"https://{shop}/admin",
            "dest": f"https://{shop}",
            "aud": "test-client-id",
            "sub": "77",
            "exp": now + 60,
            "nbf": now - 5,
            "iat": now,
        },
        SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def api(shop: Shop) -> TestClient:
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token()}"
    return client


@pytest.fixture
def warehouse(api: TestClient) -> dict[str, Any]:
    resp = api.post(
        "/api/admin/warehouses",
        json={
            "code": "BLR",
            "name": "Bengaluru WH",
            "phone": "+919811111111",
            "address1": "Plot 7",
            "city": "Bengaluru",
            "state": "Karnataka",
            "pincode": "560058",
            "shopify_location_id": "gid://shopify/Location/1001",
            "default_package": {"length_cm": 20, "breadth_cm": 15, "height_cm": 5},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- auth -----------------------------------------------------------------------------------


def test_requests_without_session_token_are_rejected(shop: Shop) -> None:
    assert TestClient(app).get("/api/admin/session").status_code == 401


def test_forged_session_token_rejected(shop: Shop) -> None:
    forged = jwt.encode(
        {"dest": f"https://{SHOP_DOMAIN}"}, "not-the-secret-0123456789abcdef-xyz", algorithm="HS256"
    )
    resp = TestClient(app).get("/api/admin/session", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_session(api: TestClient) -> None:
    body = api.get("/api/admin/session").json()
    assert body["shop_domain"] == SHOP_DOMAIN and body["user_id"] == "77"
    assert body["mock_carriers_enabled"] is True


def test_unknown_api_route_is_404_not_spa(api: TestClient) -> None:
    assert api.get("/api/admin/does-not-exist").status_code == 404


def test_spa_sets_frame_ancestors(api: TestClient) -> None:
    resp = api.get(f"/?shop={SHOP_DOMAIN}&host=abc")
    assert resp.status_code == 200
    assert (
        f"frame-ancestors https://{SHOP_DOMAIN} https://admin.shopify.com"
        in resp.headers["content-security-policy"]
    )


# --- carriers -------------------------------------------------------------------------------


def test_carriers_list_shows_placeholders_awaiting_docs(api: TestClient) -> None:
    carriers_list = api.get("/api/admin/carriers").json()
    assert [c["code"] for c in carriers_list] == ["ekart", "xpressbees"]
    assert all(c["implementation_status"] == "awaiting_api_docs" for c in carriers_list)
    assert all(c["settings"]["enabled"] is False for c in carriers_list)


def test_cannot_enable_carrier_without_credentials(api: TestClient) -> None:
    resp = api.put("/api/admin/carriers/ekart/settings", json={"enabled": True})
    assert resp.status_code == 422


def test_secrets_are_encrypted_and_never_returned(db: Session, api: TestClient) -> None:
    resp = api.put(
        "/api/admin/carriers/xpressbees/account",
        json={"environment": "MOCK", "credentials": {"api_key": "mock-secret-key-12345678"}},
    )
    assert resp.status_code == 200, resp.text
    assert "mock-secret-key-12345678" not in resp.text
    [account] = resp.json()["accounts"]
    assert account["credentials_hint"]["api_key"] == "••••5678"

    row = db.scalar(select(CarrierAccount))
    assert row is not None and row.credentials_enc is not None
    assert b"mock-secret-key" not in row.credentials_enc
    assert (
        get_encryptor().decrypt_json(row.credentials_enc)["api_key"] == "mock-secret-key-12345678"
    )

    # Blank secret keeps the stored value.
    api.put(
        "/api/admin/carriers/xpressbees/account",
        json={"environment": "MOCK", "credentials": {"api_key": "", "transit_days": 1}},
    )
    db.expire_all()
    stored = get_encryptor().decrypt_json(db.scalar(select(CarrierAccount)).credentials_enc)  # type: ignore[union-attr,arg-type]
    assert stored["api_key"] == "mock-secret-key-12345678" and stored["transit_days"] == 1


def test_invalid_credentials_rejected(api: TestClient) -> None:
    resp = api.put(
        "/api/admin/carriers/ekart/account",
        json={"environment": "PRODUCTION", "credentials": {"bogus": 1}},
    )
    assert resp.status_code == 422


def test_full_mock_carrier_setup_drives_allocation(
    db: Session, shop: Shop, api: TestClient, warehouse: dict[str, Any]
) -> None:
    """Configure both mock carriers through the API, then allocate from DB config."""
    for code in ("ekart", "xpressbees"):
        assert (
            api.put(
                f"/api/admin/carriers/{code}/account",
                json={"environment": "MOCK", "credentials": {}},
            ).status_code
            == 200
        )
        assert (
            api.put(
                f"/api/admin/carriers/{code}/warehouses", json=[{"warehouse_id": warehouse["id"]}]
            ).status_code
            == 200
        )
        resp = api.put(f"/api/admin/carriers/{code}/settings", json={"enabled": True})
        assert resp.status_code == 200 and resp.json()["settings"]["enabled"] is True

    profiles = carriers.load_profiles(db, shop)
    context = ctx(warehouse_id=warehouse["id"])
    eligible, rejected = prefilter(context, profiles)
    assert eligible == ["ekart", "xpressbees"]
    adapters = {code: carriers.build_adapter(db, shop, code) for code in eligible}
    outcomes = collect_offers(
        adapters, {c: serviceability_request() for c in eligible}, timeout_seconds=5
    )
    result = allocate(
        context,
        profiles,
        {c: o.outcome for c, o in outcomes.items()},
        prefilter_rejections=rejected,
    )
    assert (
        result.selected is not None and result.selected.carrier_code == "xpressbees"
    )  # 2 days vs 3

    cod = ctx(warehouse_id=warehouse["id"], payment_mode=PaymentMode.COD)
    outcomes = collect_offers(
        adapters,
        {c: serviceability_request("999004", PaymentMode.COD) for c in eligible},
        timeout_seconds=5,
    )
    assert allocate(cod, profiles, {c: o.outcome for c, o in outcomes.items()}).selected is None


def test_mock_environment_refused_in_production(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "app_env", "production")
    resp = api.put(
        "/api/admin/carriers/ekart/account", json={"environment": "MOCK", "credentials": {}}
    )
    assert resp.status_code == 422


# --- warehouses & rules ---------------------------------------------------------------------


def test_duplicate_warehouse_code_conflicts(api: TestClient, warehouse: dict[str, Any]) -> None:
    body = {k: v for k, v in warehouse.items() if k != "id"}
    body["shopify_location_id"] = None
    assert api.post("/api/admin/warehouses", json=body).status_code == 409


def test_rule_crud_and_validation(api: TestClient) -> None:
    ok = api.post(
        "/api/admin/allocation-rules",
        json={
            "name": "COD cheapest",
            "priority": 10,
            "strategy": "CHEAPEST",
            "conditions": {"field": "payment_mode", "op": "eq", "value": "COD"},
            "params": {"max_cost": "120", "fallback_carrier": "ekart"},
        },
    )
    assert ok.status_code == 201, ok.text
    rule_id = ok.json()["id"]
    assert ok.json()["params"] == {"max_cost": "120", "fallback_carrier": "ekart"}

    assert (
        api.post(
            "/api/admin/allocation-rules",
            json={"name": "bad", "conditions": {"field": "x", "op": "eq", "value": 1}},
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/api/admin/allocation-rules",
            json={"name": "bad", "params": {"fallback_carrier": "dhl"}},
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/api/admin/allocation-rules", json={"name": "bad", "strategy": "CUSTOM"}
        ).status_code
        == 422
    )

    assert api.delete(f"/api/admin/allocation-rules/{rule_id}").status_code == 204
    assert api.get("/api/admin/allocation-rules").json() == []


def test_settings_roundtrip(api: TestClient) -> None:
    current = api.get("/api/admin/settings").json()
    current["settings"]["settle_window_seconds"] = 120
    current["settings"]["cod_gateway_names"] = ["Cash on Delivery (COD)", "COD King"]
    resp = api.put(
        "/api/admin/settings", json={"automation_enabled": False, "settings": current["settings"]}
    )
    assert resp.status_code == 200
    body = api.get("/api/admin/settings").json()
    assert body["automation_enabled"] is False
    assert body["settings"]["settle_window_seconds"] == 120
    assert "COD King" in body["settings"]["cod_gateway_names"]


# --- manual review --------------------------------------------------------------------------


def test_manual_review_queue_and_approval(
    db: Session, shop: Shop, api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[tuple[int, str, str]] = []
    monkeypatch.setattr("app.admin.routes.enqueue_order_sync", lambda *a: queued.append(a) or True)
    created = {"created_at": utcnow() - timedelta(hours=1)}
    apply_order_snapshot(
        db,
        shop,
        order_snapshot(order_id=1, name="TR-1", tags=["DUPLICATE-REVIEW"], **created),
        trigger="t",
    )
    apply_order_snapshot(
        db,
        shop,
        order_snapshot(
            order_id=2,
            name="TR-2",
            fulfillment_orders=[fo_json(502, status="ON_HOLD", holds=[RISK_HOLD])],
            **created,
        ),
        trigger="t",
    )
    db.commit()

    queue = {row["order_name"]: row for row in api.get("/api/admin/manual-review").json()}
    assert set(queue) == {"TR-1", "TR-2"}
    assert queue["TR-1"]["can_override"] and not queue["TR-1"]["is_shopify_hold"]
    assert queue["TR-2"]["is_shopify_hold"] and not queue["TR-2"]["can_override"]
    assert queue["TR-2"]["line_items"][0]["sku"] == "TRK-TEE-M"
    assert queue["TR-2"]["hold_reasons"][0]["reason"] == "HIGH_RISK_OF_FRAUD"

    # A Shopify hold can only be released in Shopify.
    assert (
        api.post(
            f"/api/admin/manual-review/{queue['TR-2']['fulfillment_order_id']}/approve"
        ).status_code
        == 409
    )

    resp = api.post(f"/api/admin/manual-review/{queue['TR-1']['fulfillment_order_id']}/approve")
    assert resp.status_code == 200 and resp.json()["approved"]
    assert queued == [(shop.id, "gid://shopify/Order/1", "staff:approve")]
    db.expire_all()
    fo = db.get(ShopifyFulfillmentOrder, queue["TR-1"]["fulfillment_order_id"])
    assert fo is not None and fo.review_override_by == "staff:77"

    # The re-check (normally run by the worker) now lets the tagged order through.
    [ev] = apply_order_snapshot(
        db,
        shop,
        order_snapshot(order_id=1, name="TR-1", tags=["DUPLICATE-REVIEW"], **created),
        trigger="staff:approve",
    )
    assert ev.current == LogisticsStatus.AWAITING_ALLOCATION


def test_dashboard(db: Session, shop: Shop, api: TestClient) -> None:
    apply_order_snapshot(
        db,
        shop,
        order_snapshot(fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])]),
        trigger="t",
    )
    db.commit()
    body = api.get("/api/admin/dashboard").json()
    assert body["manual_review"] == 1
    assert body["recent_activity"]


def test_carrier_account_environment_enum_roundtrip(db: Session, shop: Shop) -> None:
    account = carriers.upsert_account(
        db,
        shop,
        "ekart",
        environment=CarrierEnvironment.MOCK,
        label="default",
        credentials={},
        is_active=True,
        make_active=True,
        actor="test",
    )
    assert account.environment == CarrierEnvironment.MOCK
    assert carriers.active_account(db, shop, "ekart") is account
