"""Phase 2 end-to-end: Shopify order -> gate -> mock serviceability -> allocation -> shipment
-> AWB -> Shopify fulfillment + tracking, including every failure path."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    AllocationStrategy,
    LogisticsStatus,
    PaymentMode,
    ShipmentStatus,
    ShopifySyncStatus,
    TrackingStatus,
)
from app.core.time import utcnow
from app.logistics.pipeline import apply_order_snapshot
from app.models import (
    AllocationRule,
    AutomationLog,
    CarrierQuote,
    CarrierServiceabilityCheck,
    Shipment,
    Shop,
    ShopifyFulfillmentOrder,
    ShopifyOrder,
)
from app.shopify.errors import ShopifyUserError
from app.workers import enqueue as q
from tests.factories import DUPLICATE_HOLD, FakeShopifyAdmin, fo_json, order_json
from tests.harness import Runner, TaskQueue, setup_logistics

pytestmark = pytest.mark.db


@pytest.fixture
def admin() -> FakeShopifyAdmin:
    return FakeShopifyAdmin()


@pytest.fixture
def runner(task_queue: TaskQueue, admin: FakeShopifyAdmin) -> Runner:
    return Runner(task_queue, admin)


def place_order(db: Session, shop: Shop, admin: FakeShopifyAdmin, **kwargs: Any) -> dict[str, Any]:
    """Shopify creates an order; the orders/create webhook runs the pipeline."""
    raw = order_json(created_at=utcnow() - timedelta(hours=1), **kwargs)
    admin.put(raw)
    from app.shopify.parse import parse_order

    apply_order_snapshot(db, shop, parse_order(raw), trigger="webhook:orders/create")
    db.commit()
    return raw


def fo(db: Session) -> ShopifyFulfillmentOrder:
    db.expire_all()
    row = db.scalars(select(ShopifyFulfillmentOrder).order_by(ShopifyFulfillmentOrder.id)).first()
    assert row is not None
    return row


def shipments(db: Session) -> list[Shipment]:
    db.expire_all()
    return list(db.scalars(select(Shipment).order_by(Shipment.id)))


def steps(db: Session) -> list[str]:
    db.expire_all()
    return [r.step for r in db.scalars(select(AutomationLog).order_by(AutomationLog.id))]


# --- happy path --------------------------------------------------------------------------------


def test_safe_order_ships_end_to_end(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    raw = place_order(db, shop, admin)
    assert runner.queue.names() == [q.ALLOCATE]

    done = runner.drain()
    assert [name for name, _ in done] == [
        q.ALLOCATE,
        q.CREATE_SHIPMENT,
        q.SYNC_SHOPIFY,
        q.PUSH_TRACKING,
    ]

    [shipment] = shipments(db)
    assert shipment.carrier_code == "xpressbees"  # FASTEST: 2 days vs Ekart 3 days
    assert shipment.status == ShipmentStatus.AWB_CREATED
    assert shipment.awb and shipment.awb.startswith("XBM")
    assert shipment.shipping_cost is not None and str(shipment.shipping_cost) == "85.00"
    assert shipment.estimated_delivery_date is not None
    assert shipment.idempotency_key == f"{shipment.fulfillment_order_id}:1"
    assert shipment.shopify_sync_status == ShopifySyncStatus.SYNCED
    assert fo(db).logistics_status == LogisticsStatus.SHIPPED

    [fulfillment] = admin.fulfillments[raw["fulfillmentOrders"]["nodes"][0]["id"]]
    assert fulfillment["number"] == shipment.awb
    assert fulfillment["company"] == "XpressBees"
    assert fulfillment["url"] == shipment.tracking_url
    assert fulfillment["notify"] is False

    # Every step is auditable, in order.
    timeline = steps(db)
    expected = [
        "ORDER_RECEIVED",
        "FULFILLMENT_CHECKED",
        "READY_FOR_ALLOCATION",
        "ALLOCATION_STARTED",
        "SERVICEABILITY_CHECKED",
        "SERVICEABILITY_CHECKED",
        "CARRIER_SELECTED",
        "SHIPMENT_REQUESTED",
        "SHIPMENT_CREATED",
        "AWB_GENERATED",
        "SHOPIFY_SYNCED",
    ]
    assert [s for s in timeline if s in expected] == expected

    # Carrier requests and raw responses are stored for audit.
    checks = list(db.scalars(select(CarrierServiceabilityCheck)))
    assert {c.carrier_code for c in checks} == {"ekart", "xpressbees"}
    assert all(
        c.request["pickup"]["carrier_warehouse_ref"] == f"{c.carrier_code}-BLR" for c in checks
    )
    selected = db.scalar(select(CarrierQuote).where(CarrierQuote.selected.is_(True)))
    assert selected is not None and selected.carrier_code == "xpressbees"


# --- holds -------------------------------------------------------------------------------------


def test_held_order_never_reaches_a_carrier_until_released(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    raw = place_order(
        db,
        shop,
        admin,
        tags=["DUPLICATE-REVIEW"],
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD])],
    )
    assert runner.queue.items == []  # nothing enqueued for a held order
    assert fo(db).logistics_status == LogisticsStatus.MANUAL_REVIEW
    assert shipments(db) == []
    assert db.scalar(select(func.count()).select_from(CarrierServiceabilityCheck)) == 0

    # Staff release the hold in Shopify -> hold_released webhook -> re-check -> ships.
    raw["fulfillmentOrders"]["nodes"] = [fo_json(status="OPEN")]
    from app.webhooks.ingest import store_event

    payload = {"fulfillment_order": {"id": raw["fulfillmentOrders"]["nodes"][0]["id"]}}
    event, _ = store_event(
        db,
        source="shopify",
        topic="fulfillment_orders/hold_released",
        external_event_id="rel-1",
        shop_domain=shop.shop_domain,
        raw_body=b"{}",
        payload=payload,
        headers={},
    )
    db.commit()
    runner.queue.items.append((q.WEBHOOK_PROCESS, (event.id,), None))
    runner.drain()
    [shipment] = shipments(db)
    assert shipment.status == ShipmentStatus.AWB_CREATED
    assert "HOLD_RELEASED" in steps(db)


def test_hold_placed_after_allocation_blocks_shipment(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    """Flow places a hold while the carrier is being chosen: the pre-shipment live check wins."""
    setup_logistics(db, shop)
    raw = place_order(db, shop, admin)
    runner.step(1)  # allocation only
    assert fo(db).logistics_status == LogisticsStatus.ALLOCATED
    assert runner.queue.names() == [q.CREATE_SHIPMENT]

    raw["fulfillmentOrders"]["nodes"] = [fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD])]
    done = runner.drain()
    assert done == [(q.CREATE_SHIPMENT, "blocked")]
    assert shipments(db) == []
    row = fo(db)
    assert row.logistics_status == LogisticsStatus.MANUAL_REVIEW
    assert row.status_reason == "SHOPIFY_FULFILLMENT_HOLD"


# --- serviceability & strategies ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pincode", "carrier"),
    [("999002", "ekart"), ("999001", "xpressbees"), ("999011", "xpressbees")],
)
def test_only_one_carrier_available(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner, pincode: str, carrier: str
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode=pincode)
    runner.drain()
    [shipment] = shipments(db)
    assert shipment.carrier_code == carrier


def test_neither_carrier_serviceable(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999003")
    assert runner.drain() == [(q.ALLOCATE, "no_carrier")]
    row = fo(db)
    assert row.logistics_status == LogisticsStatus.NO_CARRIER_AVAILABLE
    assert "ekart" in (row.status_detail or "") and "xpressbees" in (row.status_detail or "")
    reasons = {qt.carrier_code: qt.rejection_reason for qt in db.scalars(select(CarrierQuote))}
    assert reasons == {"ekart": "NOT_SERVICEABLE", "xpressbees": "NOT_SERVICEABLE"}
    assert shipments(db) == []


def test_cheapest_rule_selects_ekart(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    db.add(
        AllocationRule(
            shop_id=shop.id,
            name="cheapest",
            priority=1,
            strategy=AllocationStrategy.CHEAPEST,
            conditions={},
            params={},
        )
    )
    db.commit()
    place_order(db, shop, admin)
    runner.drain()
    assert shipments(db)[0].carrier_code == "ekart"


def test_cod_order_collects_outstanding_amount(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(
        db, shop, admin, gateways=["Cash on Delivery (COD)"], outstanding="1049.00", total="1049.00"
    )
    runner.drain()
    [shipment] = shipments(db)
    assert str(shipment.cod_amount) == "1049.00"
    sent = db.scalar(select(AutomationLog).where(AutomationLog.step == "SHIPMENT_REQUESTED"))
    assert sent is not None and sent.data["request"]["payment_mode"] == PaymentMode.COD.value


def test_cod_unavailable_everywhere(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(
        db, shop, admin, pincode="999004", gateways=["Cash on Delivery (COD)"], outstanding="999.00"
    )
    runner.drain()
    assert fo(db).logistics_status == LogisticsStatus.NO_CARRIER_AVAILABLE


def test_shop_automation_off_waits_then_dispatcher_resumes(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    shop.automation_enabled = False
    db.commit()
    place_order(db, shop, admin)
    assert runner.queue.items == []
    assert fo(db).logistics_status == LogisticsStatus.AWAITING_ALLOCATION

    row = fo(db)
    row.last_evaluated_at = utcnow() - timedelta(minutes=5)
    shop.automation_enabled = True
    db.commit()
    from app.logistics.sweeper import dispatch_awaiting_allocation

    assert dispatch_awaiting_allocation() == 1
    runner.drain()
    assert len(shipments(db)) == 1


# --- duplicate protection ----------------------------------------------------------------------


def test_duplicate_webhooks_and_reruns_create_one_shipment(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    raw = place_order(db, shop, admin)
    runner.drain()
    from app.shopify.parse import parse_order

    # orders/updated after shipping, a replayed allocation and a replayed create: all no-ops.
    apply_order_snapshot(db, shop, parse_order(raw), trigger="webhook:orders/updated")
    db.commit()
    runner.queue.items += [
        (q.ALLOCATE, (fo(db).id, "replay"), None),
        (q.CREATE_SHIPMENT, (fo(db).id,), None),
    ]
    assert runner.drain() == [(q.ALLOCATE, "not_ready"), (q.CREATE_SHIPMENT, "not_allocated")]
    assert len(shipments(db)) == 1


def test_create_refuses_when_active_shipment_exists(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    """Even if state is somehow inconsistent, a second AWB is never requested."""
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    row = fo(db)
    row.logistics_status = LogisticsStatus.ALLOCATED  # simulate corrupted state
    row.selected_carrier_code = "ekart"
    admin.fo(row.shopify_fulfillment_order_id)["status"] = "OPEN"
    for li in admin.fo(row.shopify_fulfillment_order_id)["lineItems"]["nodes"]:
        li["remainingQuantity"] = 1
    db.commit()
    runner.queue.items.append((q.CREATE_SHIPMENT, (row.id,), None))
    assert runner.drain() == [(q.CREATE_SHIPMENT, "duplicate_prevented")]
    assert len(shipments(db)) == 1


# --- carrier failures --------------------------------------------------------------------------


def test_create_timeout_is_reconciled_with_same_carrier(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999020")  # XpressBees create times out after creating
    done = runner.drain()
    assert (q.RECONCILE, "created") in done
    [shipment] = shipments(db)  # exactly one: no fallback to Ekart while the outcome was unknown
    assert shipment.carrier_code == "xpressbees"
    assert shipment.status == ShipmentStatus.AWB_CREATED
    assert shipment.reconcile_attempts == 1
    assert fo(db).logistics_status == LogisticsStatus.SHIPPED
    assert "SHIPMENT_UNCERTAIN" in steps(db)


def test_create_timeout_not_created_retries_same_carrier(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(
        db, shop, scenarios={"xpressbees": {"scenarios": {"560001": "CREATE_TIMEOUT_LOST"}}}
    )
    place_order(db, shop, admin)
    first_key = f"{fo(db).id}:1"
    from app.models import CarrierAccount
    from app.services import carriers

    account = db.scalar(select(CarrierAccount).where(CarrierAccount.carrier_code == "xpressbees"))
    assert account is not None
    carriers.upsert_account(
        db,
        shop,
        "xpressbees",
        environment=account.environment,
        label="default",
        credentials={"lost_references": [first_key]},
        is_active=True,
        make_active=True,
        actor="t",
    )
    db.commit()
    runner.drain()
    rows = shipments(db)
    assert [(s.carrier_code, s.status) for s in rows] == [
        ("xpressbees", ShipmentStatus.CREATE_FAILED),
        ("xpressbees", ShipmentStatus.AWB_CREATED),
    ]
    assert rows[1].idempotency_key.endswith(":2")


def test_carrier_5xx_retries_then_falls_back(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999021")  # XpressBees create -> 503
    done = runner.drain()
    rows = shipments(db)
    assert [s.carrier_code for s in rows] == ["xpressbees"] * 3 + ["ekart"]
    assert [s.status for s in rows[:3]] == [ShipmentStatus.CREATE_FAILED] * 3
    assert rows[3].status == ShipmentStatus.AWB_CREATED
    assert (q.CREATE_SHIPMENT, "fallback") in done
    assert "CARRIER_FALLBACK" in steps(db)


def test_invalid_pincode_at_create_fails_without_endless_retries(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999030")  # both carriers reject at create
    runner.drain()
    rows = shipments(db)
    assert [s.carrier_code for s in rows] == ["xpressbees", "ekart"]  # one attempt each, no retries
    assert all(s.status == ShipmentStatus.CREATE_FAILED for s in rows)
    row = fo(db)
    assert row.logistics_status == LogisticsStatus.FAILED
    assert "invalid delivery pincode" in (row.status_detail or "")


def test_carrier_auth_failure_falls_back(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop, scenarios={"xpressbees": {"scenarios": {"560001": "AUTH_FAILURE"}}})
    place_order(db, shop, admin)
    runner.drain()
    # Auth failure is detected at serviceability, so XpressBees is excluded and Ekart ships.
    [shipment] = shipments(db)
    assert shipment.carrier_code == "ekart"
    quote = db.scalar(select(CarrierQuote).where(CarrierQuote.carrier_code == "xpressbees"))
    assert (
        quote is not None
        and quote.rejection_reason == "OFFER_FAILED"
        and "CarrierAuthError" in (quote.rejection_detail or "")
    )


def test_unreachable_carriers_retry_allocation(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(
        db,
        shop,
        scenarios={
            "xpressbees": {"scenarios": {"560001": "TIMEOUT"}},
            "ekart": {"scenarios": {"560001": "SERVER_ERROR"}},
        },
    )
    place_order(db, shop, admin)
    done = runner.drain(follow_retries=False)
    assert done == [(q.ALLOCATE, "no_carrier")]
    row = fo(db)
    assert (row.logistics_status, row.status_reason) == (
        LogisticsStatus.NO_CARRIER_AVAILABLE,
        "CARRIERS_UNREACHABLE",
    )
    from app.logistics.allocation_run import run_allocation

    # Carriers recover; the Celery retry (is_retry=True) re-runs allocation.
    from app.models import CarrierAccount
    from app.services import carriers

    for code in ("ekart", "xpressbees"):
        acc = db.scalar(select(CarrierAccount).where(CarrierAccount.carrier_code == code))
        assert acc is not None
        carriers.upsert_account(
            db,
            shop,
            code,
            environment=acc.environment,
            label="default",
            credentials={"scenarios": {}},
            is_active=True,
            make_active=True,
            actor="t",
        )
    db.commit()
    assert (
        run_allocation(row.id, trigger="retry", admin_factory=runner.factory, is_retry=True).outcome
        == "allocated"
    )  # type: ignore[arg-type]
    runner.drain()
    assert len(shipments(db)) == 1


def test_plan_errors_fail_with_exact_reason(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, fulfillment_orders=[fo_json(weight_grams=None)])
    runner.drain()
    row = fo(db)
    assert (row.logistics_status, row.status_reason) == (LogisticsStatus.FAILED, "MISSING_WEIGHT")


def test_unmapped_location_fails(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(
        db, shop, admin, fulfillment_orders=[fo_json(location_gid="gid://shopify/Location/999")]
    )
    runner.drain()
    assert fo(db).status_reason == "WAREHOUSE_NOT_MAPPED"


def test_split_cod_order_is_not_guessed(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(
        db,
        shop,
        admin,
        gateways=["Cash on Delivery (COD)"],
        outstanding="999.00",
        fulfillment_orders=[fo_json(501), fo_json(502)],
    )
    runner.drain()
    assert shipments(db) == []
    statuses = {r.status_reason for r in db.scalars(select(ShopifyFulfillmentOrder))}
    assert statuses == {"MULTI_SHIPMENT_COD"}


# --- Shopify sync ------------------------------------------------------------------------------


def test_shopify_rejection_keeps_awb_and_reports(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    admin.fulfillment_error = ShopifyUserError(
        "fulfillmentCreate failed: on hold", user_errors=[{"message": "on hold"}]
    )
    place_order(db, shop, admin)
    runner.drain()
    [shipment] = shipments(db)
    assert shipment.status == ShipmentStatus.AWB_CREATED
    assert shipment.shopify_sync_status == ShopifySyncStatus.FAILED
    assert "SHOPIFY_SYNC_FAILED" in steps(db)

    admin.fulfillment_error = None
    from app.logistics.shopify_sync import sync_to_shopify

    shipment.shopify_sync_status = ShopifySyncStatus.PENDING
    db.commit()
    assert sync_to_shopify(shipment.id, admin_factory=runner.factory).outcome == "synced"  # type: ignore[arg-type]


def test_shopify_sync_never_fulfills_twice(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    [shipment] = shipments(db)
    fo_gid = shipment.shopify_fulfillment_order_id
    # Simulate: Shopify created the fulfillment but our response was lost.
    shipment.shopify_fulfillment_id = None
    shipment.shopify_sync_status = ShopifySyncStatus.PENDING
    db.commit()
    from app.logistics.shopify_sync import sync_to_shopify

    sync_to_shopify(shipment.id, admin_factory=runner.factory)  # type: ignore[arg-type]
    assert len(admin.fulfillments[fo_gid]) == 1


def test_fulfill_on_pickup_defers_shopify_sync(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    shop.settings = {**shop.settings, "fulfill_on": "PICKED_UP"}
    db.commit()
    place_order(db, shop, admin)
    runner.drain()
    [shipment] = shipments(db)
    assert shipment.shopify_sync_status == ShopifySyncStatus.PENDING and not admin.fulfillments
    feed(db, shipment, "MOCK_PUD")
    runner.drain()
    assert shipments(db)[0].shopify_sync_status == ShopifySyncStatus.SYNCED
    assert [e["status"] for e in admin.events] == ["CARRIER_PICKED_UP"]


# --- tracking ----------------------------------------------------------------------------------


def feed(db: Session, shipment: Shipment, *raw_codes: str, minutes_apart: int = 10) -> None:
    from app.carriers.types import TrackingUpdate
    from app.logistics.shipments import lock_shipment
    from app.services.carriers import adapter_for_shipment
    from app.tracking.service import record_updates

    s = lock_shipment(db, shipment.id)
    adapter = adapter_for_shipment(db, s)
    start = utcnow()
    updates = [
        TrackingUpdate(
            awb=s.awb or "",
            raw_status=code,
            status=adapter.normalize_status(code),
            occurred_at=start + timedelta(minutes=i * minutes_apart),
            location="Bengaluru",
        )
        for i, code in enumerate(raw_codes)
    ]
    from app.core.enums import TrackingSource

    record_updates(db, s, updates, TrackingSource.POLL)
    db.commit()


def test_tracking_progression_to_delivered(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    [shipment] = shipments(db)
    feed(db, shipment, "MOCK_PUD", "MOCK_IT", "OFD", "MOCK_DLVD")
    runner.drain()
    shipment = shipments(db)[0]
    assert shipment.status == ShipmentStatus.DELIVERED
    assert shipment.delivered_at is not None and shipment.next_poll_at is None
    assert [e["status"] for e in admin.events] == [
        "CARRIER_PICKED_UP",
        "IN_TRANSIT",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
    ]

    # A late, stale event is stored but neither applied nor pushed.
    from app.carriers.types import TrackingUpdate
    from app.core.enums import TrackingSource
    from app.logistics.shipments import lock_shipment
    from app.tracking.service import record_updates

    s = lock_shipment(db, shipment.id)
    record_updates(
        db,
        s,
        [
            TrackingUpdate(
                awb=s.awb or "",
                raw_status="MOCK_IT",
                status=TrackingStatus.IN_TRANSIT,
                occurred_at=utcnow() - timedelta(days=1),
            )
        ],
        TrackingSource.POLL,
    )
    db.commit()
    runner.drain()
    assert shipments(db)[0].status == ShipmentStatus.DELIVERED
    assert len(admin.events) == 4


def test_rto_flow(db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    feed(db, shipments(db)[0], "MOCK_PUD", "OFD", "MOCK_UD", "MOCK_RTO", "MOCK_RTO_IT", "MOCK_RTD")
    runner.drain()
    assert shipments(db)[0].status == ShipmentStatus.RTO_DELIVERED
    assert [e["status"] for e in admin.events][-4:] == [
        "ATTEMPTED_DELIVERY",
        "FAILURE",
        "FAILURE",
        "FAILURE",
    ]
    assert "Return to origin" in (admin.events[-1]["message"] or "")


def test_duplicate_tracking_events_are_ignored(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.drain()
    from app.carriers.types import TrackingUpdate
    from app.core.enums import TrackingSource
    from app.logistics.shipments import lock_shipment
    from app.models import TrackingEvent
    from app.tracking.service import record_updates

    at = utcnow()
    update = TrackingUpdate(
        awb=shipments(db)[0].awb or "",
        raw_status="OFD",
        status=TrackingStatus.OUT_FOR_DELIVERY,
        occurred_at=at,
    )
    for _ in range(3):
        s = lock_shipment(db, shipments(db)[0].id)
        record_updates(db, s, [update], TrackingSource.WEBHOOK)
        db.commit()
    runner.drain()
    ofd = db.scalars(select(TrackingEvent).where(TrackingEvent.raw_status == "OFD")).all()
    assert len(ofd) == 1
    assert [e["status"] for e in admin.events] == ["OUT_FOR_DELIVERY"]


# --- cancellation & overrides -------------------------------------------------------------------


def test_order_cancelled_in_shopify_cancels_shipment(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    raw = place_order(db, shop, admin)
    runner.drain()
    raw["cancelledAt"] = utcnow().isoformat()
    from app.shopify.parse import parse_order

    apply_order_snapshot(db, shop, parse_order(raw), trigger="webhook:orders/cancelled")
    db.commit()
    assert runner.queue.names() == [q.CANCEL_SHIPMENT]
    runner.drain()
    [shipment] = shipments(db)
    assert shipment.status == ShipmentStatus.CANCELLED
    assert admin.cancelled_fulfillments == [shipment.shopify_fulfillment_id]
    assert fo(db).logistics_status == LogisticsStatus.CANCELLED


def test_order_cancelled_after_pickup_asks_for_rto(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    raw = place_order(db, shop, admin)
    runner.drain()
    feed(db, shipments(db)[0], "MOCK_PUD")
    runner.drain()
    raw["cancelledAt"] = utcnow().isoformat()
    from app.shopify.parse import parse_order

    apply_order_snapshot(db, shop, parse_order(raw), trigger="webhook:orders/cancelled")
    db.commit()
    assert runner.queue.items == []
    assert "arrange the return (RTO)" in (fo(db).status_detail or "")
    assert shipments(db)[0].status == ShipmentStatus.PICKED_UP


def test_sweeper_recovers_interrupted_creation(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    """Worker died after reserving the shipment: it becomes CREATION_UNKNOWN and is reconciled."""
    setup_logistics(db, shop)
    place_order(db, shop, admin)
    runner.step(1)  # allocated
    from app.logistics.allocation_run import lock_fo

    row = lock_fo(db, fo(db).id)
    db.add(
        Shipment(
            shop_id=shop.id,
            order_id=row.order_id,
            fulfillment_order_id=row.id,
            shopify_order_id="gid://shopify/Order/1500",
            shopify_order_name="TR-001500",
            shopify_fulfillment_order_id=row.shopify_fulfillment_order_id,
            carrier_code="xpressbees",
            carrier_account_id=_account_id(db, "xpressbees"),
            idempotency_key=f"{row.id}:1",
            status=ShipmentStatus.CREATING,
            created_by="system",
        )
    )
    row.logistics_status = LogisticsStatus.SHIPMENT_PENDING
    db.commit()
    from sqlalchemy import update

    db.execute(update(Shipment).values(updated_at=utcnow() - timedelta(hours=1)))
    db.commit()
    runner.queue.pop_all()
    from app.logistics.sweeper import sweep

    assert sweep()["creating"] == 1
    runner.drain()
    [shipment] = shipments(db)
    assert shipment.status == ShipmentStatus.AWB_CREATED  # mock confirms it exists
    assert fo(db).logistics_status == LogisticsStatus.SHIPPED


def _account_id(db: Session, code: str) -> int:
    from app.models import CarrierAccount

    acc = db.scalar(select(CarrierAccount).where(CarrierAccount.carrier_code == code))
    assert acc is not None
    return acc.id


def test_needs_staff_when_carrier_cannot_confirm(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    place_order(db, shop, admin, pincode="999020")
    runner.step(2)  # allocate + ambiguous create
    [shipment] = shipments(db)
    assert shipment.status == ShipmentStatus.CREATION_UNKNOWN
    runner.queue.pop_all()
    from app.carriers.errors import CarrierNotSupportedError
    from app.carriers.mock import MockXpressBeesAdapter

    original = MockXpressBeesAdapter.find_shipment_by_reference
    MockXpressBeesAdapter.find_shipment_by_reference = lambda self, key: (_ for _ in ()).throw(  # type: ignore[method-assign]
        CarrierNotSupportedError("lookup unsupported")
    )
    try:
        from app.logistics.shipments import reconcile_shipment

        assert reconcile_shipment(shipment.id).outcome == "needs_staff"
    finally:
        MockXpressBeesAdapter.find_shipment_by_reference = original  # type: ignore[method-assign]
    assert fo(db).status_reason == "RECONCILIATION_NEEDS_STAFF"
    assert shipments(db)[0].status == ShipmentStatus.CREATION_UNKNOWN  # never guessed
    # Staff checked the carrier panel: it was created.
    from app.logistics.shipments import resolve_unknown

    resolve_unknown(
        db,
        shop,
        shipment.id,
        created=True,
        awb="XBM-PANEL-1",
        carrier_shipment_id=None,
        actor="staff:1",
    )
    db.commit()
    runner.drain()
    final = shipments(db)[0]
    assert (final.status, final.awb, final.shopify_sync_status) == (
        ShipmentStatus.AWB_CREATED,
        "XBM-PANEL-1",
        ShopifySyncStatus.SYNCED,
    )


def test_order_count_sanity(
    db: Session, shop: Shop, admin: FakeShopifyAdmin, runner: Runner
) -> None:
    setup_logistics(db, shop)
    for i in range(3):
        place_order(
            db,
            shop,
            admin,
            order_id=2000 + i,
            name=f"TR-{2000 + i}",
            fulfillment_orders=[fo_json(600 + i)],
        )
    runner.drain()
    assert db.scalar(select(func.count()).select_from(ShopifyOrder)) == 3
    assert {s.status for s in shipments(db)} == {ShipmentStatus.AWB_CREATED}
    assert len({s.awb for s in shipments(db)}) == 3
