"""Database-level duplicate-shipment protection: one active AWB per fulfillment order."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import ShipmentStatus
from app.logistics.pipeline import apply_order_snapshot
from app.models import Shipment, Shop, ShopifyFulfillmentOrder
from tests.factories import order_snapshot

pytestmark = pytest.mark.db


@pytest.fixture
def fo(db: Session, shop: Shop) -> ShopifyFulfillmentOrder:
    apply_order_snapshot(db, shop, order_snapshot(), trigger="test")
    row = db.query(ShopifyFulfillmentOrder).one()
    return row


def shipment(
    fo: ShopifyFulfillmentOrder,
    attempt: int,
    *,
    carrier: str = "ekart",
    awb: str | None = None,
    status: ShipmentStatus = ShipmentStatus.AWB_CREATED,
) -> Shipment:
    return Shipment(
        shop_id=fo.shop_id,
        order_id=fo.order_id,
        fulfillment_order_id=fo.id,
        shopify_order_id="gid://shopify/Order/1500",
        shopify_order_name="TR-001500",
        shopify_fulfillment_order_id=fo.shopify_fulfillment_order_id,
        carrier_code=carrier,
        idempotency_key=f"{fo.id}:{attempt}",
        attempt=attempt,
        awb=awb or f"AWB{attempt}",
        status=status,
    )


def insert(db: Session, row: Shipment) -> None:
    with db.begin_nested():
        db.add(row)
        db.flush()


def test_second_active_shipment_is_rejected_by_database(
    db: Session, fo: ShopifyFulfillmentOrder
) -> None:
    insert(db, shipment(fo, 1, carrier="ekart"))
    with pytest.raises(IntegrityError, match="uq_shipments_active_fulfillment_order"):
        insert(db, shipment(fo, 2, carrier="xpressbees"))


def test_in_flight_creation_also_occupies_the_slot(
    db: Session, fo: ShopifyFulfillmentOrder
) -> None:
    insert(db, shipment(fo, 1, status=ShipmentStatus.CREATING, awb=None))
    with pytest.raises(IntegrityError):
        insert(db, shipment(fo, 2))


def test_ambiguous_creation_blocks_another_carrier(
    db: Session, fo: ShopifyFulfillmentOrder
) -> None:
    insert(db, shipment(fo, 1, status=ShipmentStatus.CREATION_UNKNOWN))
    with pytest.raises(IntegrityError):
        insert(db, shipment(fo, 2, carrier="xpressbees"))


@pytest.mark.parametrize("inactive", [ShipmentStatus.CANCELLED, ShipmentStatus.CREATE_FAILED])
def test_reship_allowed_after_cancel_or_failed_create(
    db: Session, fo: ShopifyFulfillmentOrder, inactive: ShipmentStatus
) -> None:
    insert(db, shipment(fo, 1, status=inactive))
    insert(db, shipment(fo, 2, carrier="xpressbees"))
    with pytest.raises(IntegrityError):
        insert(db, shipment(fo, 3))


def test_idempotency_key_and_awb_are_unique(db: Session, fo: ShopifyFulfillmentOrder) -> None:
    insert(db, shipment(fo, 1, status=ShipmentStatus.CANCELLED, awb="SAME"))
    with pytest.raises(IntegrityError):
        insert(db, shipment(fo, 1, status=ShipmentStatus.CANCELLED, awb="OTHER"))  # same key
    with pytest.raises(IntegrityError):
        insert(db, shipment(fo, 2, status=ShipmentStatus.CANCELLED, awb="SAME"))  # same carrier+AWB
