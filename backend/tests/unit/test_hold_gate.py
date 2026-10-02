"""Fulfillment gate: held orders must never reach a courier."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.core.enums import PaymentMode
from app.logistics.hold_gate import GateAction, GateDecision, GateHistory, GateReason, evaluate_gate
from app.schemas.settings import ShopSettings
from tests.factories import DUPLICATE_HOLD, NOW, RISK_HOLD, fo_json, order_snapshot


def gate(
    *,
    history: GateHistory | None = None,
    settings: ShopSettings | None = None,
    payment_mode: PaymentMode = PaymentMode.PREPAID,
    disabled: bool = False,
    now: Any = None,
    **order_kwargs: Any,
) -> GateDecision:
    order = order_snapshot(**order_kwargs)
    return evaluate_gate(
        order=order,
        fulfillment_order=order.fulfillment_orders[0],
        history=history or GateHistory(),
        settings=settings or ShopSettings(),
        payment_mode=payment_mode,
        automation_disabled=disabled,
        now=now or NOW,
    )


def test_safe_order_proceeds() -> None:
    decision = gate()
    assert decision.action == GateAction.PROCEED
    assert decision.allows_shipping


def test_held_duplicate_order_goes_to_manual_review() -> None:
    decision = gate(
        tags=["DUPLICATE-REVIEW"],
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD])],
    )
    assert decision.action == GateAction.MANUAL_REVIEW
    assert decision.reason == GateReason.SHOPIFY_FULFILLMENT_HOLD
    assert "Possible duplicate order" in decision.detail
    assert not decision.allows_shipping


def test_held_high_risk_order_goes_to_manual_review() -> None:
    decision = gate(
        tags=["RISK-REVIEW"],
        risk_levels=["HIGH"],
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])],
    )
    assert decision.action == GateAction.MANUAL_REVIEW
    assert decision.reason == GateReason.SHOPIFY_FULFILLMENT_HOLD
    assert "High risk of fraud" in decision.detail


def test_hold_without_on_hold_status_still_blocks() -> None:
    """Any hold blocks, even if the status field disagrees (defensive)."""
    decision = gate(fulfillment_orders=[fo_json(status="OPEN", holds=[RISK_HOLD])])
    assert decision.reason == GateReason.SHOPIFY_FULFILLMENT_HOLD


def test_shopify_hold_cannot_be_overridden_by_staff_approval() -> None:
    decision = gate(
        history=GateHistory(review_override_at=NOW),
        fulfillment_orders=[fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD])],
    )
    assert decision.action == GateAction.MANUAL_REVIEW


@pytest.mark.parametrize("tag", ["DUPLICATE-REVIEW", "RISK-REVIEW", "duplicate-review"])
def test_review_tag_without_hold_blocks(tag: str) -> None:
    """Flow tagged the order but has not (yet) placed the hold: do not trust "no hold"."""
    decision = gate(tags=[tag])
    assert decision.action == GateAction.MANUAL_REVIEW
    assert decision.reason == GateReason.REVIEW_TAG_WITHOUT_HOLD


def test_hold_release_lets_tagged_order_continue() -> None:
    """Staff released the hold in Shopify but left the tag: processing continues."""
    decision = gate(
        tags=["DUPLICATE-REVIEW"], history=GateHistory(hold_last_seen_at=NOW - timedelta(minutes=5))
    )
    assert decision.action == GateAction.PROCEED


def test_hold_released_webhook_also_clears_tag_block() -> None:
    decision = gate(tags=["RISK-REVIEW"], history=GateHistory(hold_released_at=NOW))
    assert decision.action == GateAction.PROCEED


def test_staff_override_clears_tag_block() -> None:
    decision = gate(tags=["DUPLICATE-REVIEW"], history=GateHistory(review_override_at=NOW))
    assert decision.action == GateAction.PROCEED


def test_high_risk_without_hold_blocks() -> None:
    decision = gate(risk_levels=["HIGH"], recommendation="CANCEL")
    assert decision.reason == GateReason.HIGH_RISK_WITHOUT_HOLD


def test_high_risk_block_can_be_disabled() -> None:
    decision = gate(risk_levels=["HIGH"], settings=ShopSettings(block_on_high_risk=False))
    assert decision.action == GateAction.PROCEED


def test_settle_window_waits_for_flow() -> None:
    created = NOW - timedelta(seconds=60)
    decision = gate(created_at=created)
    assert decision.action == GateAction.WAIT
    assert decision.reason == GateReason.SETTLE_WINDOW
    assert decision.retry_at == created + timedelta(seconds=300)


def test_waits_for_risk_analysis_then_times_out() -> None:
    created = NOW - timedelta(minutes=6)
    waiting = gate(created_at=created, risk_levels=["PENDING"])
    assert waiting.reason == GateReason.AWAITING_RISK_ANALYSIS
    assert waiting.retry_at is not None and waiting.retry_at <= NOW + timedelta(seconds=60)

    late = gate(created_at=NOW - timedelta(minutes=20), risk_levels=["PENDING"])
    assert late.action == GateAction.PROCEED


def test_no_risk_assessment_counts_as_not_analysed() -> None:
    decision = gate(created_at=NOW - timedelta(minutes=6), risk_levels=[])
    assert decision.reason == GateReason.AWAITING_RISK_ANALYSIS


@pytest.mark.parametrize(
    ("status", "action", "reason"),
    [
        ("CLOSED", GateAction.SKIP, GateReason.FULFILLMENT_ORDER_CLOSED),
        ("IN_PROGRESS", GateAction.SKIP, GateReason.FULFILLED_OUTSIDE_APP),
        ("INCOMPLETE", GateAction.SKIP, GateReason.FULFILLMENT_ORDER_INCOMPLETE),
        ("CANCELLED", GateAction.CANCEL, GateReason.FULFILLMENT_ORDER_CANCELLED),
        ("SCHEDULED", GateAction.WAIT, GateReason.SCHEDULED_FULFILLMENT),
    ],
)
def test_fulfillment_order_statuses(status: str, action: GateAction, reason: GateReason) -> None:
    decision = gate(fulfillment_orders=[fo_json(status=status)])
    assert (decision.action, decision.reason) == (action, reason)


def test_cancelled_order_cancels() -> None:
    decision = gate(cancelled_at=NOW)
    assert decision.action == GateAction.CANCEL


def test_cancelled_and_held_order_reports_cancel() -> None:
    decision = gate(
        cancelled_at=NOW, fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])]
    )
    assert decision.action == GateAction.CANCEL


def test_local_pickup_is_skipped() -> None:
    decision = gate(fulfillment_orders=[fo_json(method="PICK_UP")])
    assert decision.reason == GateReason.NOT_A_SHIPPING_ORDER


def test_nothing_left_to_fulfill_is_skipped() -> None:
    decision = gate(fulfillment_orders=[fo_json(quantity=0)])
    assert decision.reason == GateReason.NOTHING_TO_FULFILL


def test_unresolved_payment_mode_needs_review() -> None:
    decision = gate(payment_mode=PaymentMode.UNKNOWN)
    assert decision.reason == GateReason.PAYMENT_MODE_UNRESOLVED
    assert gate(
        payment_mode=PaymentMode.UNKNOWN, history=GateHistory(review_override_at=NOW)
    ).allows_shipping


def test_missing_address_needs_review() -> None:
    decision = gate(shipping_address=None)
    assert decision.reason == GateReason.MISSING_SHIPPING_ADDRESS


def test_automation_disabled_for_order() -> None:
    assert gate(disabled=True).action == GateAction.DISABLED


def test_disabled_order_that_is_held_still_shows_in_review() -> None:
    decision = gate(
        disabled=True, fulfillment_orders=[fo_json(status="ON_HOLD", holds=[RISK_HOLD])]
    )
    assert decision.action == GateAction.MANUAL_REVIEW
