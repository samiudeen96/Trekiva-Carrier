"""Shipment status progression rules.

Carrier events arrive late, duplicated and out of order. Each status has a rank in its flow; an
update is applied only if it moves the shipment forward (with explicit exceptions such as a
failed delivery being re-attempted). Terminal statuses never change.
"""

from __future__ import annotations

from app.core.enums import ShipmentStatus as S
from app.core.enums import TrackingStatus

TERMINAL: frozenset[S] = frozenset({S.DELIVERED, S.RTO_DELIVERED, S.CANCELLED, S.CREATE_FAILED})

_FORWARD_RANK: dict[S, int] = {
    S.CREATING: 0,
    S.CREATION_UNKNOWN: 0,
    S.AWB_CREATED: 10,
    S.PICKUP_SCHEDULED: 20,
    S.PICKED_UP: 30,
    S.IN_TRANSIT: 40,
    S.OUT_FOR_DELIVERY: 50,
    S.DELIVERY_FAILED: 50,
    S.DELIVERED: 100,
}
_RTO_RANK: dict[S, int] = {S.RTO_INITIATED: 1, S.RTO_IN_TRANSIT: 2, S.RTO_DELIVERED: 3}

#: Statuses from which a shipment can still be cancelled with the carrier.
CANCELLABLE: frozenset[S] = frozenset(
    {S.AWB_CREATED, S.PICKUP_SCHEDULED, S.EXCEPTION, S.CREATION_UNKNOWN}
)


def to_shipment_status(status: TrackingStatus) -> S:
    return S(status.value)


def can_apply_tracking(current: S, new: S) -> bool:
    """Should a tracking update to `new` change a shipment currently in `current`?"""
    if current in TERMINAL or current == new:
        return False
    if current in (S.CREATING, S.CREATION_UNKNOWN):
        return False  # creation must be confirmed before tracking applies
    if new == S.CANCELLED:
        return current in CANCELLABLE or current == S.CANCEL_REQUESTED
    if new == S.EXCEPTION:
        return True
    if current == S.EXCEPTION:
        return True  # any concrete status resolves an exception
    if new in _RTO_RANK:
        return current not in _RTO_RANK or _RTO_RANK[new] > _RTO_RANK[current]
    if current in _RTO_RANK:
        return False  # once returning, forward-flow events are stale
    # Re-attempt after a failed delivery, or failure after going out for delivery.
    if {current, new} == {S.DELIVERY_FAILED, S.OUT_FOR_DELIVERY}:
        return True
    return _FORWARD_RANK.get(new, -1) > _FORWARD_RANK.get(current, -1)
