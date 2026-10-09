"""Review checks end to end: a flagged order is held + tagged in Shopify instead of shipped, and
ships normally once staff release the hold."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus
from app.core.geoip import IpLocation
from app.core.time import utcnow
from app.logistics.hold_gate import GateReason
from app.logistics.pipeline import apply_order_snapshot
from app.models import Shipment, Shop, ShopifyFulfillmentOrder
from app.schemas.settings import ShopSettings
from app.shopify.errors import ShopifyUserError
from app.shopify.parse import parse_order
from app.workers import enqueue as q
from tests.factories import FakeShopifyAdmin, fo_json, order_json
from tests.harness import Runner, TaskQueue, setup_logistics

pytestmark = pytest.mark.db

PHONE = "+91 98765 43210"


@pytest.fixture
def admin() -> FakeShopifyAdmin:
    return FakeShopifyAdmin()


@pytest.fixture
def runner(task_queue: TaskQueue, admin: FakeShopifyAdmin) -> Runner:
    return Runner(task_queue, admin)


def place_order(
    db: Session,
    shop: Shop,
    admin: FakeShopifyAdmin,
    order_id: int,
    *,
    minutes_ago: int = 60,
    sku: str = "TRK-TEE-M",
    **kwargs: Any,
) -> dict[str, Any]:
    fo = fo_json(order_id)
    fo["lineItems"]["nodes"][0]["sku"] = sku
    raw = order_json(
        order_id,
        name=f"TR-{order_id}",
        created_at=utcnow() - timedelta(minutes=minutes_ago),
        fulfillment_orders=[fo],
        **kwargs,
    )
    admin.put(raw)
    apply_order_snapshot(db, shop, parse_order(raw), trigger="webhook:orders/create")
    db.commit()
    return raw


def fo_row(db: Session, order_id: int) -> ShopifyFulfillmentOrder:
    db.expire_all()
    row = db.scalar(
        select(ShopifyFulfillmentOrder).where(
            ShopifyFulfillmentOrder.shopify_fulfillment_order_id
            == f"gid://shopify/FulfillmentOrder/{order_id}"
        )
    )
    assert row is not None
    return row


def resync(db: Session, shop: Shop, admin: FakeShopifyAdmin, order_id: int) -> None:
    raw = admin.orders[f"gid://shopify/Order/{order_id}"]
    apply_order_snapshot(db, shop, parse_order(raw), trigger="test")
    db.commit()


def test_duplicate_order_is_held_and_tagged_not_shipped(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, 2001, minutes_ago=120, phone=PHONE)
    runner.drain()
    assert fo_row(db, 2001).logistics_status == LogisticsStatus.SHIPPED

    # Same customer, same phone (formatted differently), same SKU, 1h40m later.
    place_order(db, shop, admin, 2002, minutes_ago=20, phone="09876543210")
    fo = fo_row(db, 2002)
    assert fo.logistics_status == LogisticsStatus.MANUAL_REVIEW
    assert fo.status_reason == GateReason.REVIEW_CHECK_FLAGGED.value
    assert "TR-2001" in (fo.status_detail or "")
    assert [f["kind"] for f in fo.review_flags] == ["DUPLICATE_ORDER"]

    runner.drain()
    assert admin.holds_placed == [
        ("gid://shopify/FulfillmentOrder/2002", "OTHER", admin.holds_placed[0][2])
    ]
    assert "Possible duplicate order" in admin.holds_placed[0][2]
    assert ("gid://shopify/Order/2002", ["DUPLICATE-REVIEW"]) in admin.tags_added
    fo = fo_row(db, 2002)
    # The follow-up sync saw the Shopify hold.
    assert fo.status_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value
    assert db.scalar(select(Shipment).where(Shipment.fulfillment_order_id == fo.id)) is None


def test_released_hold_ships_without_flagging_again(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    # Two orders a minute apart: both are still in the settle window when the second arrives.
    place_order(db, shop, admin, 2101, minutes_ago=3, phone=PHONE)
    place_order(db, shop, admin, 2102, minutes_ago=2, phone=PHONE)
    assert fo_row(db, 2101).logistics_status == LogisticsStatus.SETTLING
    # The settle window ends (simulated by shortening it).
    shop.settings = ShopSettings(settle_window_seconds=0).model_dump(mode="json")
    db.commit()
    resync(db, shop, admin, 2101)
    resync(db, shop, admin, 2102)
    runner.drain()
    # Both copies wait for staff: neither is shipped automatically.
    assert fo_row(db, 2101).status_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value
    assert fo_row(db, 2102).status_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value

    # Staff call the customer, then release the hold on the order to keep.
    admin.release_holds("gid://shopify/FulfillmentOrder/2101")
    resync(db, shop, admin, 2101)
    runner.drain()
    assert fo_row(db, 2101).logistics_status == LogisticsStatus.SHIPPED
    assert len(admin.holds_placed) == 2  # no new hold after release
    assert fo_row(db, 2102).status_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        ({"phone": "+91 91234 56789"}, "different phone"),
        ({"customer_name": "Ravi Kumar"}, "different name"),
        ({"sku": "TRK-CAP"}, "different product"),
        ({"minutes_ago": 60 * 30}, "outside 24 hours"),
    ],
)
def test_not_a_duplicate_unless_all_conditions_match(
    db: Session,
    shop: Shop,
    admin: FakeShopifyAdmin,
    runner: Runner,
    second: dict[str, Any],
    reason: str,
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, 2201, minutes_ago=60, phone=PHONE)
    place_order(db, shop, admin, 2202, **{"phone": PHONE, **second})
    runner.drain()
    assert admin.holds_placed == [], reason
    assert fo_row(db, 2201).logistics_status == LogisticsStatus.SHIPPED
    assert fo_row(db, 2202).logistics_status == LogisticsStatus.SHIPPED


def test_cancelled_order_is_not_a_duplicate(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(
        db,
        shop,
        admin,
        2301,
        minutes_ago=90,
        phone=PHONE,
        cancelled_at=utcnow() - timedelta(hours=1),
    )
    place_order(db, shop, admin, 2302, minutes_ago=60, phone=PHONE)
    runner.drain()
    assert admin.holds_placed == []
    assert fo_row(db, 2302).logistics_status == LogisticsStatus.SHIPPED


def test_location_risk_order_is_held(
    db: Session,
    shop: Shop,
    admin: FakeShopifyAdmin,
    runner: Runner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mumbai = IpLocation(country_code="IN", state_code="MH", state_name="Maharashtra", city="Mumbai")
    monkeypatch.setattr("app.core.geoip.lookup", lambda _ip: mumbai)
    shop.settings = ShopSettings(location_check_ip=True).model_dump(mode="json")
    setup_logistics(db, shop)
    place_order(db, shop, admin, 2401, client_ip="49.36.10.1")  # delivery: Bengaluru, Karnataka
    fo = fo_row(db, 2401)
    assert fo.status_reason == GateReason.REVIEW_CHECK_FLAGGED.value
    assert "Risk order" in (fo.status_detail or "")
    runner.drain()
    [(_, hold_reason, notes)] = admin.holds_placed
    assert hold_reason == "HIGH_RISK_OF_FRAUD"
    assert "Mumbai" in notes
    assert ("gid://shopify/Order/2401", ["RISK-REVIEW"]) in admin.tags_added


def test_checks_switched_off(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    shop.settings = ShopSettings(duplicate_check_enabled=False).model_dump(mode="json")
    setup_logistics(db, shop)
    place_order(db, shop, admin, 2501, minutes_ago=90, phone=PHONE)
    place_order(db, shop, admin, 2502, minutes_ago=60, phone=PHONE)
    runner.drain()
    assert admin.holds_placed == []
    assert fo_row(db, 2502).logistics_status == LogisticsStatus.SHIPPED


def test_hold_rejected_by_shopify_keeps_order_blocked_for_staff(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    admin.hold_error = ShopifyUserError("fulfillmentOrderHold failed: not allowed", user_errors=[])
    place_order(db, shop, admin, 2601, minutes_ago=90, phone=PHONE)
    place_order(db, shop, admin, 2602, minutes_ago=60, phone=PHONE)
    assert q.PLACE_REVIEW_HOLD in runner.queue.names()
    runner.drain()
    fo = fo_row(db, 2602)
    assert fo.logistics_status == LogisticsStatus.MANUAL_REVIEW
    assert fo.status_reason == GateReason.REVIEW_CHECK_FLAGGED.value
    assert "could not place the Shopify hold" in (fo.status_detail or "")
    assert db.scalar(select(Shipment).where(Shipment.fulfillment_order_id == fo.id)) is None
