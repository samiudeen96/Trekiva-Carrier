"""Order processing pipeline: Shopify snapshot -> persist -> fulfillment gate -> state change.

A fulfillment order that the gate declares ready moves to AWAITING_ALLOCATION and allocation is
enqueued (after commit). Allocation and shipment creation live in `allocation_run` and
`shipments`; both re-run this pipeline against live Shopify data before acting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import LogisticsStatus, LogLevel, PaymentMode, ShipmentStatus
from app.core.time import utcnow
from app.logistics.hold_gate import (
    GateAction,
    GateDecision,
    GateHistory,
    GateReason,
    evaluate_gate,
)
from app.logistics.payment import detect_payment_mode
from app.logistics.state import GATE_BLOCK_ONLY_EVAL, GATE_FULL_EVAL, ensure_transition
from app.models import Shipment, Shop, ShopifyFulfillmentOrder, ShopifyOrder
from app.schemas.settings import ShopSettings
from app.schemas.shopify import FulfillmentOrderSnapshot, OrderSnapshot
from app.services import audit, orders
from app.shopify.api import ShopifyAdmin
from app.tracking.transitions import CANCELLABLE
from app.workers import enqueue

log = logging.getLogger(__name__)

ACTION_TO_STATUS: dict[GateAction, LogisticsStatus] = {
    GateAction.PROCEED: LogisticsStatus.AWAITING_ALLOCATION,
    GateAction.WAIT: LogisticsStatus.SETTLING,
    GateAction.MANUAL_REVIEW: LogisticsStatus.MANUAL_REVIEW,
    GateAction.SKIP: LogisticsStatus.SKIPPED,
    GateAction.CANCEL: LogisticsStatus.CANCELLED,
    GateAction.DISABLED: LogisticsStatus.AUTOMATION_DISABLED,
}

BLOCKING_ACTIONS = frozenset({GateAction.MANUAL_REVIEW, GateAction.SKIP, GateAction.CANCEL})

_STEP_FOR_ACTION: dict[GateAction, audit.Step] = {
    GateAction.PROCEED: audit.Step.READY_FOR_ALLOCATION,
    GateAction.WAIT: audit.Step.WAITING,
    GateAction.MANUAL_REVIEW: audit.Step.REVIEW_REQUIRED,
    GateAction.SKIP: audit.Step.SKIPPED,
    GateAction.CANCEL: audit.Step.CANCELLED,
    GateAction.DISABLED: audit.Step.AUTOMATION_DISABLED,
}


@dataclass(frozen=True)
class Evaluation:
    fulfillment_order_id: int
    shopify_fulfillment_order_id: str
    previous: LogisticsStatus
    current: LogisticsStatus
    decision: GateDecision | None
    """None when the fulfillment order is past the gate (in-flight or terminal)."""

    @property
    def changed(self) -> bool:
        return self.previous != self.current


def sync_order(
    db: Session,
    shop: Shop,
    admin: ShopifyAdmin,
    order_gid: str,
    *,
    trigger: str,
    now: datetime | None = None,
) -> list[Evaluation]:
    """Fetch the live order from Shopify and run every fulfillment order through the gate."""
    snapshot = admin.fetch_order(order_gid)
    return apply_order_snapshot(db, shop, snapshot, trigger=trigger, now=now)


def apply_order_snapshot(
    db: Session, shop: Shop, snapshot: OrderSnapshot, *, trigger: str, now: datetime | None = None
) -> list[Evaluation]:
    now = now or utcnow()
    settings = ShopSettings.load(shop.settings)
    payment_mode = detect_payment_mode(
        snapshot.payment_gateways, snapshot.outstanding, settings.cod_gateway_names
    )
    order, created = orders.upsert_order(db, shop.id, snapshot, payment_mode)
    if created:
        audit.record(
            db,
            shop_id=shop.id,
            order_id=order.id,
            step=audit.Step.ORDER_RECEIVED,
            message=f"Order {snapshot.name} received ({payment_mode.value}) via {trigger}",
            data={"trigger": trigger, "payment_gateways": list(snapshot.payment_gateways)},
        )
    results = []
    for fo_snap in snapshot.fulfillment_orders:
        fo = orders.upsert_fulfillment_order(db, shop.id, order, fo_snap)
        results.append(
            evaluate_fulfillment_order(
                db,
                shop=shop,
                order=order,
                fo=fo,
                order_snap=snapshot,
                fo_snap=fo_snap,
                settings=settings,
                payment_mode=payment_mode,
                trigger=trigger,
                now=now,
            )
        )
    return results


def evaluate_fulfillment_order(
    db: Session,
    *,
    shop: Shop,
    order: ShopifyOrder,
    fo: ShopifyFulfillmentOrder,
    order_snap: OrderSnapshot,
    fo_snap: FulfillmentOrderSnapshot,
    settings: ShopSettings,
    payment_mode: PaymentMode,
    trigger: str,
    now: datetime,
) -> Evaluation:
    """Apply the gate to one (row-locked) fulfillment order and record any change."""
    previous = fo.logistics_status
    if previous == LogisticsStatus.SHIPPED and order_snap.cancelled_at is not None:
        _on_shipped_order_cancelled(db, shop, order, fo, settings)
    if previous not in GATE_FULL_EVAL and previous not in GATE_BLOCK_ONLY_EVAL:
        return Evaluation(fo.id, fo.shopify_fulfillment_order_id, previous, previous, None)

    decision = evaluate_gate(
        order=order_snap,
        fulfillment_order=fo_snap,
        history=GateHistory(
            hold_last_seen_at=fo.hold_last_seen_at,
            hold_released_at=fo.hold_released_at,
            review_override_at=fo.review_override_at,
        ),
        settings=settings,
        payment_mode=payment_mode,
        # An explicit staff action (manual carrier, re-run) proceeds even when automation is off
        # for the order. Holds and every other check still apply.
        automation_disabled=order.automation_disabled and not fo.staff_initiated,
        now=now,
    )
    fo.last_evaluated_at = now
    if decision.reason == GateReason.SHOPIFY_FULFILLMENT_HOLD:
        fo.hold_last_seen_at = now

    if previous in GATE_BLOCK_ONLY_EVAL and not _applies_in_block_only(previous, decision):
        return Evaluation(fo.id, fo.shopify_fulfillment_order_id, previous, previous, decision)

    target = ACTION_TO_STATUS[decision.action]
    ensure_transition(previous, target)
    previous_reason = fo.status_reason
    reason_changed = previous_reason != decision.reason.value

    fo.logistics_status = target
    fo.status_reason = decision.reason.value
    fo.status_detail = decision.detail
    fo.next_check_at = decision.retry_at if decision.action == GateAction.WAIT else None

    if previous != target or reason_changed:
        _record_change(db, shop, order, fo, previous, previous_reason, decision, trigger)
    return Evaluation(fo.id, fo.shopify_fulfillment_order_id, previous, target, decision)


def _applies_in_block_only(current: LogisticsStatus, decision: GateDecision) -> bool:
    if decision.action in BLOCKING_ACTIONS:
        return True
    if current == LogisticsStatus.AUTOMATION_DISABLED:
        # Re-enabling automation lets a disabled order continue normally.
        return decision.action != GateAction.DISABLED
    # Disabling automation also stops an order that is allocated but not yet shipped.
    return decision.action == GateAction.DISABLED


def _record_change(
    db: Session,
    shop: Shop,
    order: ShopifyOrder,
    fo: ShopifyFulfillmentOrder,
    previous: LogisticsStatus,
    previous_reason: str | None,
    decision: GateDecision,
    trigger: str,
) -> None:
    def log_step(step: audit.Step, message: str, level: LogLevel = LogLevel.INFO) -> None:
        audit.record(
            db,
            shop_id=shop.id,
            order_id=order.id,
            fulfillment_order_id=fo.id,
            step=step,
            level=level,
            message=message,
            data=data,
        )

    data = {
        "trigger": trigger,
        "from": previous.value,
        "to": fo.logistics_status.value,
        "reason": decision.reason.value,
        "shopify_status": fo.shopify_status,
        "holds": fo.hold_reasons,
    }
    log_step(
        audit.Step.FULFILLMENT_CHECKED,
        f"{order.name}: fulfillment status checked ({fo.shopify_status})",
    )
    was_held = previous_reason == GateReason.SHOPIFY_FULFILLMENT_HOLD.value
    is_held = decision.reason == GateReason.SHOPIFY_FULFILLMENT_HOLD
    if was_held and not is_held:
        log_step(
            audit.Step.HOLD_RELEASED,
            f"{order.name}: Shopify fulfillment hold released; processing can continue",
        )
    if is_held:
        step = audit.Step.FULFILLMENT_HELD
        level = LogLevel.WARNING
    else:
        step = _STEP_FOR_ACTION[decision.action]
        level = LogLevel.WARNING if decision.action == GateAction.MANUAL_REVIEW else LogLevel.INFO
    log_step(step, f"{order.name}: {decision.detail}", level)
    if fo.logistics_status == LogisticsStatus.AWAITING_ALLOCATION:
        _on_ready_for_allocation(db, shop, fo, trigger)


def _on_ready_for_allocation(
    db: Session, shop: Shop, fo: ShopifyFulfillmentOrder, trigger: str
) -> None:
    """Enqueue carrier allocation once this transaction commits. With shop automation off the
    order waits in AWAITING_ALLOCATION (the dispatcher picks it up when automation is enabled)."""
    if shop.automation_enabled or fo.staff_initiated:
        enqueue.enqueue_after_commit(db, enqueue.ALLOCATE, fo.id, trigger)


def _on_shipped_order_cancelled(
    db: Session,
    shop: Shop,
    order: ShopifyOrder,
    fo: ShopifyFulfillmentOrder,
    settings: ShopSettings,
) -> None:
    """The order was cancelled in Shopify after its shipment was created."""
    if fo.status_reason == "ORDER_CANCELLED":
        return  # already handled
    fo.status_reason = "ORDER_CANCELLED"
    shipment = db.scalar(
        select(Shipment)
        .where(
            Shipment.fulfillment_order_id == fo.id,
            Shipment.status.not_in([ShipmentStatus.CANCELLED, ShipmentStatus.CREATE_FAILED]),
        )
        .order_by(Shipment.id.desc())
    )
    if shipment is None:
        return
    if settings.cancel_shipment_on_order_cancel and shipment.status in CANCELLABLE:
        fo.status_detail = "Order cancelled in Shopify; cancelling the courier shipment"
        audit.record(
            db,
            step=audit.Step.CANCELLED,
            message=f"{order.name}: order cancelled in Shopify; cancelling {shipment.carrier_code} AWB {shipment.awb}",
            shop_id=shop.id,
            order_id=order.id,
            fulfillment_order_id=fo.id,
            shipment_id=shipment.id,
        )
        enqueue.enqueue_after_commit(
            db, enqueue.CANCEL_SHIPMENT, shipment.id, False, "system", "Order cancelled in Shopify"
        )
    else:
        fo.status_detail = (
            "Order cancelled in Shopify after pickup: arrange the return (RTO) with the carrier"
            if shipment.status not in CANCELLABLE
            else "Order cancelled in Shopify; automatic shipment cancellation is turned off"
        )
        audit.record(
            db,
            step=audit.Step.CANCELLED,
            level=LogLevel.WARNING,
            message=f"{order.name}: {fo.status_detail} (AWB {shipment.awb})",
            shop_id=shop.id,
            order_id=order.id,
            fulfillment_order_id=fo.id,
            shipment_id=shipment.id,
        )
