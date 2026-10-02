"""Order pipeline against the database: gate decisions, state changes, audit trail."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus, PaymentMode
from app.core.time import utcnow
from app.logistics.pipeline import apply_order_snapshot
from app.models import AutomationLog, Shop, ShopifyFulfillmentOrder, ShopifyOrder, Warehouse
from tests.factories import DUPLICATE_HOLD, LOCATION_GID, RISK_HOLD, fo_json, order_snapshot

pytestmark = pytest.mark.db


def created_ago(minutes: int = 60) -> dict[str, object]:
    return {"created_at": utcnow() - timedelta(minutes=minutes)}


def steps(db: Session) -> list[str]:
    return [log.step for log in db.scalars(select(AutomationLog).order_by(AutomationLog.id))]


def test_safe_order_timeline(db: Session, shop: Shop) -> None:
    [ev] = apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    assert ev.current == LogisticsStatus.AWAITING_ALLOCATION
    assert steps(db) == ["ORDER_RECEIVED", "FULFILLMENT_CHECKED", "READY_FOR_ALLOCATION"]
    order = db.scalar(select(ShopifyOrder))
    assert order is not None and order.payment_mode == PaymentMode.PREPAID


def test_reevaluation_without_change_writes_no_new_logs(db: Session, shop: Shop) -> None:
    snap = order_snapshot(**created_ago())
    apply_order_snapshot(db, shop, snap, trigger="test")
    before = len(steps(db))
    [ev] = apply_order_snapshot(db, shop, snap, trigger="orders/updated")
    assert not ev.changed
    assert len(steps(db)) == before


def test_cod_order_detected(db: Session, shop: Shop) -> None:
    snap = order_snapshot(
        gateways=["Cash on Delivery (COD)"], outstanding="1049.00", **created_ago()
    )
    apply_order_snapshot(db, shop, snap, trigger="test")
    order = db.scalar(select(ShopifyOrder))
    assert order is not None and order.payment_mode == PaymentMode.COD


def test_high_risk_held_order(db: Session, shop: Shop) -> None:
    snap = order_snapshot(
        tags=["RISK-REVIEW"],
        risk_levels=["HIGH"],
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])],
        **created_ago(),
    )
    [ev] = apply_order_snapshot(db, shop, snap, trigger="test")
    assert ev.current == LogisticsStatus.MANUAL_REVIEW
    assert "FULFILLMENT_HELD" in steps(db)


def test_flow_race_tag_then_hold_then_release(db: Session, shop: Shop) -> None:
    """Flow tags first, places the hold a moment later, staff release it: never ships early."""
    tagged = order_snapshot(tags=["DUPLICATE-REVIEW"], **created_ago())
    [ev] = apply_order_snapshot(db, shop, tagged, trigger="orders/create")
    assert (ev.current, ev.decision.reason) == (
        LogisticsStatus.MANUAL_REVIEW,
        "REVIEW_TAG_WITHOUT_HOLD",
    )  # type: ignore[union-attr]

    held = order_snapshot(
        tags=["DUPLICATE-REVIEW"],
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD])],
        **created_ago(),
    )
    [ev] = apply_order_snapshot(db, shop, held, trigger="fulfillment_orders/placed_on_hold")
    assert ev.decision.reason == "SHOPIFY_FULFILLMENT_HOLD"  # type: ignore[union-attr]

    released = order_snapshot(tags=["DUPLICATE-REVIEW"], **created_ago())
    [ev] = apply_order_snapshot(db, shop, released, trigger="schedule:review-reconcile")
    assert ev.current == LogisticsStatus.AWAITING_ALLOCATION
    assert "HOLD_RELEASED" in steps(db)


def test_new_order_waits_in_settle_window(db: Session, shop: Shop) -> None:
    [ev] = apply_order_snapshot(db, shop, order_snapshot(**created_ago(1)), trigger="orders/create")
    assert ev.current == LogisticsStatus.SETTLING
    fo = db.scalar(select(ShopifyFulfillmentOrder))
    assert fo is not None and fo.next_check_at is not None and fo.next_check_at > utcnow()


def test_hold_placed_after_ready_moves_back_to_review(db: Session, shop: Shop) -> None:
    apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    held = order_snapshot(
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])], **created_ago()
    )
    [ev] = apply_order_snapshot(db, shop, held, trigger="test")
    assert ev.current == LogisticsStatus.MANUAL_REVIEW


def test_failed_state_is_not_restarted_by_ready_verdict(db: Session, shop: Shop) -> None:
    apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    fo = db.scalar(select(ShopifyFulfillmentOrder))
    assert fo is not None
    fo.logistics_status = LogisticsStatus.FAILED
    db.flush()
    [ev] = apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    assert ev.current == LogisticsStatus.FAILED
    held = order_snapshot(
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])], **created_ago()
    )
    [ev] = apply_order_snapshot(db, shop, held, trigger="test")
    assert ev.current == LogisticsStatus.MANUAL_REVIEW  # blocking decisions still apply


def test_order_cancelled(db: Session, shop: Shop) -> None:
    apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    [ev] = apply_order_snapshot(
        db, shop, order_snapshot(cancelled_at=utcnow(), **created_ago()), trigger="orders/cancelled"
    )
    assert ev.current == LogisticsStatus.CANCELLED
    # Terminal: a later update cannot revive it.
    [ev] = apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="orders/updated")
    assert ev.current == LogisticsStatus.CANCELLED and ev.decision is None


def test_disabled_order_is_not_processed(db: Session, shop: Shop) -> None:
    apply_order_snapshot(db, shop, order_snapshot(**created_ago(1)), trigger="test")
    order = db.scalar(select(ShopifyOrder))
    assert order is not None
    order.automation_disabled = True
    db.flush()
    [ev] = apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    assert ev.current == LogisticsStatus.AUTOMATION_DISABLED
    order.automation_disabled = False
    db.flush()
    [ev] = apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    assert ev.current == LogisticsStatus.AWAITING_ALLOCATION


def test_fulfillment_order_mapped_to_warehouse(db: Session, shop: Shop) -> None:
    wh = Warehouse(
        shop_id=shop.id,
        code="BLR",
        name="Bengaluru",
        phone="+919811111111",
        address1="Plot 7",
        city="Bengaluru",
        state="Karnataka",
        pincode="560058",
        shopify_location_id=LOCATION_GID,
    )
    db.add(wh)
    db.flush()
    apply_order_snapshot(db, shop, order_snapshot(**created_ago()), trigger="test")
    fo = db.scalar(select(ShopifyFulfillmentOrder))
    assert fo is not None and fo.warehouse_id == wh.id and fo.weight_g == 500


def test_multiple_fulfillment_orders_evaluated_independently(db: Session, shop: Shop) -> None:
    snap = order_snapshot(
        fulfillment_orders=[fo_json(501), fo_json(502, status="ON_HOLD", holds=[RISK_HOLD])],
        **created_ago(),
    )
    evs = apply_order_snapshot(db, shop, snap, trigger="test")
    assert [e.current for e in evs] == [
        LogisticsStatus.AWAITING_ALLOCATION,
        LogisticsStatus.MANUAL_REVIEW,
    ]
