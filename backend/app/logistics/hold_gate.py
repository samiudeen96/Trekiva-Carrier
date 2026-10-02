"""The fulfillment gate: may this fulfillment order be sent to a courier *right now*?

This is a pure function over live Shopify data (re-fetched before every evaluation) plus a little
local history. It is evaluated on every webhook, on schedule, and again under lock immediately
before any carrier shipment call.

Safety rules, in order of precedence:

1. A Shopify fulfillment hold ALWAYS blocks (MANUAL_REVIEW). Never released by this app.
2. Review tags (DUPLICATE-REVIEW / RISK-REVIEW) and HIGH risk block even without a hold, until a
   hold lifecycle has been observed (hold seen, or hold released) or staff override. This covers
   the race where Flow has tagged the order but not yet placed the hold. Tags can only ever
   block; they never allow shipping.
3. A settle window and (optionally) Shopify risk analysis must complete before allocation, so
   Flow's duplicate/risk workflows get the chance to act first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.enums import PaymentMode
from app.schemas.settings import ShopSettings
from app.schemas.shopify import (
    DELIVERY_SHIPPING,
    FO_CANCELLED,
    FO_CLOSED,
    FO_IN_PROGRESS,
    FO_INCOMPLETE,
    FO_OPEN,
    FO_SCHEDULED,
    FulfillmentOrderSnapshot,
    OrderSnapshot,
)

RISK_POLL_SECONDS = 60
SCHEDULED_FALLBACK_SECONDS = 3600


class GateAction(StrEnum):
    PROCEED = "PROCEED"
    WAIT = "WAIT"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    SKIP = "SKIP"
    CANCEL = "CANCEL"
    DISABLED = "DISABLED"


class GateReason(StrEnum):
    READY = "READY"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    FULFILLMENT_ORDER_CANCELLED = "FULFILLMENT_ORDER_CANCELLED"
    SHOPIFY_FULFILLMENT_HOLD = "SHOPIFY_FULFILLMENT_HOLD"
    FULFILLMENT_ORDER_CLOSED = "FULFILLMENT_ORDER_CLOSED"
    FULFILLMENT_ORDER_INCOMPLETE = "FULFILLMENT_ORDER_INCOMPLETE"
    FULFILLED_OUTSIDE_APP = "FULFILLED_OUTSIDE_APP"
    UNSUPPORTED_FULFILLMENT_STATUS = "UNSUPPORTED_FULFILLMENT_STATUS"
    SCHEDULED_FULFILLMENT = "SCHEDULED_FULFILLMENT"
    NOT_A_SHIPPING_ORDER = "NOT_A_SHIPPING_ORDER"
    NOTHING_TO_FULFILL = "NOTHING_TO_FULFILL"
    AUTOMATION_DISABLED = "AUTOMATION_DISABLED"
    REVIEW_TAG_WITHOUT_HOLD = "REVIEW_TAG_WITHOUT_HOLD"
    HIGH_RISK_WITHOUT_HOLD = "HIGH_RISK_WITHOUT_HOLD"
    PAYMENT_MODE_UNRESOLVED = "PAYMENT_MODE_UNRESOLVED"
    MISSING_SHIPPING_ADDRESS = "MISSING_SHIPPING_ADDRESS"
    SETTLE_WINDOW = "SETTLE_WINDOW"
    AWAITING_RISK_ANALYSIS = "AWAITING_RISK_ANALYSIS"


#: Reasons staff may override from the admin UI. A Shopify hold is NOT overridable here: it
#: must be released in Shopify.
OVERRIDABLE_REASONS: frozenset[GateReason] = frozenset(
    {
        GateReason.REVIEW_TAG_WITHOUT_HOLD,
        GateReason.HIGH_RISK_WITHOUT_HOLD,
        GateReason.PAYMENT_MODE_UNRESOLVED,
    }
)


@dataclass(frozen=True)
class GateHistory:
    """Local knowledge about this fulfillment order that Shopify does not expose."""

    hold_last_seen_at: datetime | None = None
    hold_released_at: datetime | None = None
    review_override_at: datetime | None = None

    @property
    def review_cleared(self) -> bool:
        """True once a review has demonstrably happened: a hold existed and is now gone (the
        gate only reaches this check when the FO is not held), or staff overrode."""
        return bool(self.review_override_at or self.hold_released_at or self.hold_last_seen_at)


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    reason: GateReason
    detail: str
    retry_at: datetime | None = None

    @property
    def allows_shipping(self) -> bool:
        return self.action == GateAction.PROCEED


def evaluate_gate(
    *,
    order: OrderSnapshot,
    fulfillment_order: FulfillmentOrderSnapshot,
    history: GateHistory,
    settings: ShopSettings,
    payment_mode: PaymentMode,
    automation_disabled: bool,
    now: datetime,
) -> GateDecision:
    fo = fulfillment_order

    if order.cancelled_at is not None:
        return GateDecision(
            GateAction.CANCEL, GateReason.ORDER_CANCELLED, "Order was cancelled in Shopify"
        )
    if fo.status == FO_CANCELLED:
        return GateDecision(
            GateAction.CANCEL,
            GateReason.FULFILLMENT_ORDER_CANCELLED,
            "Fulfillment order was cancelled",
        )

    # 1. Shopify fulfillment hold: the authoritative block.
    if fo.is_on_hold:
        reasons = ", ".join(
            (h.display_reason or h.reason) + (f" ({h.reason_notes})" if h.reason_notes else "")
            for h in fo.holds
        )
        return GateDecision(
            GateAction.MANUAL_REVIEW,
            GateReason.SHOPIFY_FULFILLMENT_HOLD,
            f"Fulfillment is on hold in Shopify: {reasons or 'no reason given'}. "
            "Release the hold in Shopify to continue.",
        )

    # 2. Fulfillment-order states that are not ours to ship.
    if fo.status == FO_CLOSED:
        return GateDecision(
            GateAction.SKIP, GateReason.FULFILLMENT_ORDER_CLOSED, "Already fulfilled or closed"
        )
    if fo.status == FO_INCOMPLETE:
        return GateDecision(
            GateAction.SKIP,
            GateReason.FULFILLMENT_ORDER_INCOMPLETE,
            "Fulfillment order is incomplete",
        )
    if fo.status == FO_IN_PROGRESS:
        return GateDecision(
            GateAction.SKIP,
            GateReason.FULFILLED_OUTSIDE_APP,
            "Fulfillment order is partially fulfilled outside Trekiva",
        )
    if fo.status == FO_SCHEDULED:
        retry_at = (
            fo.fulfill_at
            if fo.fulfill_at and fo.fulfill_at > now
            else now + timedelta(seconds=SCHEDULED_FALLBACK_SECONDS)
        )
        return GateDecision(
            GateAction.WAIT,
            GateReason.SCHEDULED_FULFILLMENT,
            "Scheduled fulfillment not yet due",
            retry_at,
        )
    if fo.status != FO_OPEN:
        return GateDecision(
            GateAction.SKIP,
            GateReason.UNSUPPORTED_FULFILLMENT_STATUS,
            f"Unsupported status {fo.status}",
        )
    if fo.delivery_method not in (None, DELIVERY_SHIPPING):
        return GateDecision(
            GateAction.SKIP,
            GateReason.NOT_A_SHIPPING_ORDER,
            f"Delivery method {fo.delivery_method} does not need a courier",
        )
    if fo.remaining_quantity <= 0:
        return GateDecision(
            GateAction.SKIP, GateReason.NOTHING_TO_FULFILL, "No remaining items to ship"
        )

    if automation_disabled:
        return GateDecision(
            GateAction.DISABLED,
            GateReason.AUTOMATION_DISABLED,
            "Automation disabled for this order",
        )

    # 3. Defence in depth: never trust "no hold" while review signals are present.
    if not history.review_cleared:
        matched = [t for t in settings.review_tags if order.has_tag(t)]
        if matched:
            return GateDecision(
                GateAction.MANUAL_REVIEW,
                GateReason.REVIEW_TAG_WITHOUT_HOLD,
                f"Order is tagged {', '.join(matched)} but Shopify shows no fulfillment hold. "
                "Waiting for the Flow hold, or approve manually.",
            )
        if settings.block_on_high_risk and order.risk.is_high:
            return GateDecision(
                GateAction.MANUAL_REVIEW,
                GateReason.HIGH_RISK_WITHOUT_HOLD,
                "Shopify rates this order high risk but no fulfillment hold was placed.",
            )

    if payment_mode == PaymentMode.UNKNOWN and history.review_override_at is None:
        return GateDecision(
            GateAction.MANUAL_REVIEW,
            GateReason.PAYMENT_MODE_UNRESOLVED,
            "Order has an outstanding balance but no COD payment gateway. Check the configured "
            "COD gateway names, or approve manually.",
        )

    address = order.shipping_address
    if address is None or not address.zip or not address.address1:
        return GateDecision(
            GateAction.MANUAL_REVIEW,
            GateReason.MISSING_SHIPPING_ADDRESS,
            "Shipping address or pincode is missing (or protected customer data access is not granted).",
        )

    # 4. Let Shopify Flow and risk analysis finish before we act.
    settle_until = order.created_at + timedelta(seconds=settings.settle_window_seconds)
    if now < settle_until:
        return GateDecision(
            GateAction.WAIT,
            GateReason.SETTLE_WINDOW,
            "Waiting for the order settle window",
            settle_until,
        )
    if settings.wait_for_risk_analysis and not order.risk.analysed:
        deadline = order.created_at + timedelta(seconds=settings.risk_wait_max_seconds)
        if now < deadline:
            return GateDecision(
                GateAction.WAIT,
                GateReason.AWAITING_RISK_ANALYSIS,
                "Waiting for Shopify risk analysis",
                min(deadline, now + timedelta(seconds=RISK_POLL_SECONDS)),
            )

    return GateDecision(
        GateAction.PROCEED, GateReason.READY, "Fulfillment is open; ready for allocation"
    )
