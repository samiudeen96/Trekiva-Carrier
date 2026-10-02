"""Fulfillment-order processing state machine (`shopify_fulfillment_orders.logistics_status`)."""

from __future__ import annotations

from app.core.enums import LogisticsStatus as S
from app.core.errors import InvalidTransition

_PRE_ALLOCATION_TARGETS = {
    S.SETTLING,
    S.MANUAL_REVIEW,
    S.AWAITING_ALLOCATION,
    S.SKIPPED,
    S.CANCELLED,
    S.AUTOMATION_DISABLED,
}

ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = {
    S.RECEIVED: frozenset(_PRE_ALLOCATION_TARGETS),
    S.SETTLING: frozenset(_PRE_ALLOCATION_TARGETS),
    S.MANUAL_REVIEW: frozenset(_PRE_ALLOCATION_TARGETS),
    S.AWAITING_ALLOCATION: frozenset(_PRE_ALLOCATION_TARGETS | {S.ALLOCATING, S.FAILED}),
    S.ALLOCATING: frozenset(
        {
            S.ALLOCATED,
            S.NO_CARRIER_AVAILABLE,
            S.AWAITING_ALLOCATION,
            S.MANUAL_REVIEW,
            S.FAILED,
            S.CANCELLED,
        }
    ),
    S.ALLOCATED: frozenset(
        {
            S.SHIPMENT_PENDING,
            S.AWAITING_ALLOCATION,
            S.MANUAL_REVIEW,
            S.SKIPPED,
            S.CANCELLED,
            S.AUTOMATION_DISABLED,
        }
    ),
    S.NO_CARRIER_AVAILABLE: frozenset(_PRE_ALLOCATION_TARGETS | {S.ALLOCATING}),
    S.SHIPMENT_PENDING: frozenset(
        {S.SHIPPED, S.RECONCILING, S.ALLOCATED, S.FAILED, S.MANUAL_REVIEW}
    ),
    S.RECONCILING: frozenset(
        {S.SHIPPED, S.ALLOCATED, S.FAILED, S.MANUAL_REVIEW, S.AWAITING_ALLOCATION}
    ),
    S.SHIPPED: frozenset({S.CANCELLED, S.AWAITING_ALLOCATION, S.AUTOMATION_DISABLED, S.FAILED}),
    S.FAILED: frozenset(_PRE_ALLOCATION_TARGETS | {S.ALLOCATING}),
    S.AUTOMATION_DISABLED: frozenset(_PRE_ALLOCATION_TARGETS),
    S.SKIPPED: frozenset({S.SETTLING, S.MANUAL_REVIEW, S.AWAITING_ALLOCATION, S.CANCELLED}),
    S.CANCELLED: frozenset(),
}

#: States the gate fully re-evaluates on every Shopify change (any decision applies).
GATE_FULL_EVAL: frozenset[S] = frozenset(
    {S.RECEIVED, S.SETTLING, S.MANUAL_REVIEW, S.AWAITING_ALLOCATION}
)

#: States where only *blocking* gate decisions apply (hold, cancel, skip). A "ready" verdict
#: does not restart work that is waiting on staff (FAILED, NO_CARRIER_AVAILABLE, DISABLED).
GATE_BLOCK_ONLY_EVAL: frozenset[S] = frozenset(
    {S.ALLOCATED, S.NO_CARRIER_AVAILABLE, S.FAILED, S.AUTOMATION_DISABLED}
)

#: States in which no courier shipment exists or is being created.
PRE_SHIPMENT: frozenset[S] = GATE_FULL_EVAL | GATE_BLOCK_ONLY_EVAL | {S.ALLOCATING, S.SKIPPED}


def can_transition(current: S, target: S) -> bool:
    return current == target or target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"Cannot move fulfillment order from {current} to {target}")
