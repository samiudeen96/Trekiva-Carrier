"""Build carrier-neutral requests for one fulfillment order from the local (freshly synced) data.

Problems that a human must fix (unmapped location, missing weight, COD split across several
shipments, incomplete address) raise `PlanError` with a reason code and an exact explanation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.carriers.types import (
    CreateShipmentRequest,
    DeliveryAddress,
    Parcel,
    PickupLocation,
    ServiceabilityRequest,
    ShipmentItem,
)
from app.core.enums import LogisticsStatus, PaymentMode
from app.core.time import utcnow
from app.logistics.allocation.types import AllocationContext
from app.logistics.payment import cod_amount
from app.models import ShopifyFulfillmentOrder, ShopifyOrder, Warehouse

_FINISHED = (LogisticsStatus.SKIPPED, LogisticsStatus.CANCELLED)


class PlanError(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ShipmentPlan:
    warehouse_id: int
    pickup: PickupLocation
    delivery: DeliveryAddress
    parcel: Parcel
    items: tuple[ShipmentItem, ...]
    payment_mode: PaymentMode
    cod_amount: Decimal
    declared_value: Decimal
    currency: str
    context: AllocationContext

    def pickup_with_ref(self, carrier_warehouse_ref: str | None) -> PickupLocation:
        return self.pickup.model_copy(update={"carrier_warehouse_ref": carrier_warehouse_ref})

    def serviceability_request(self, carrier_warehouse_ref: str | None) -> ServiceabilityRequest:
        return ServiceabilityRequest(
            pickup=self.pickup_with_ref(carrier_warehouse_ref),
            delivery_pincode=self.delivery.pincode,
            delivery_city=self.delivery.city,
            delivery_state=self.delivery.state,
            delivery_country=self.delivery.country,
            parcel=self.parcel,
            payment_mode=self.payment_mode,
            order_value=self.declared_value,
            cod_amount=self.cod_amount,
            currency=self.currency,
        )

    def create_request(
        self,
        *,
        idempotency_key: str,
        order: ShopifyOrder,
        fo: ShopifyFulfillmentOrder,
        carrier_warehouse_ref: str | None,
    ) -> CreateShipmentRequest:
        return CreateShipmentRequest(
            idempotency_key=idempotency_key,
            order_name=order.name,
            shopify_order_id=order.shopify_order_id,
            shopify_fulfillment_order_id=fo.shopify_fulfillment_order_id,
            pickup=self.pickup_with_ref(carrier_warehouse_ref),
            delivery=self.delivery,
            parcel=self.parcel,
            items=self.items,
            payment_mode=self.payment_mode,
            cod_amount=self.cod_amount,
            declared_value=self.declared_value,
            currency=self.currency,
        )


def build_plan(db: Session, order: ShopifyOrder, fo: ShopifyFulfillmentOrder) -> ShipmentPlan:
    warehouse = db.get(Warehouse, fo.warehouse_id) if fo.warehouse_id else None
    if warehouse is None or not warehouse.is_active:
        raise PlanError(
            "WAREHOUSE_NOT_MAPPED",
            f"Shopify location {fo.shopify_location_id or '(none)'} is not mapped to an active "
            "warehouse. Add it under Warehouses.",
        )

    package = warehouse.default_package or {}
    min_weight = package.get("min_weight_g")
    weight = fo.weight_g or 0
    if min_weight:
        weight = max(weight, int(min_weight))
    if weight <= 0:
        raise PlanError(
            "MISSING_WEIGHT",
            "Product weights are missing in Shopify and the warehouse has no minimum package "
            "weight. Set variant weights or a minimum weight on the warehouse.",
        )

    address = order.shipping_address or {}
    pincode = (address.get("zip") or "").strip()
    if not pincode or not address.get("address1") or not address.get("city"):
        raise PlanError(
            "INCOMPLETE_ADDRESS", "Shipping address needs address line 1, city and pincode."
        )

    payment_mode = order.payment_mode
    if payment_mode == PaymentMode.UNKNOWN:
        payment_mode = PaymentMode.PREPAID  # reached only after staff approval
    collect = cod_amount(payment_mode, order.outstanding_amount)
    if payment_mode == PaymentMode.COD and _other_open_fulfillment_orders(db, order, fo) > 0:
        raise PlanError(
            "MULTI_SHIPMENT_COD",
            "This COD order is split across several fulfillment orders, and Trekiva will not "
            "guess how to divide the COD amount. Merge the fulfillment orders in Shopify, or "
            "ship this order manually.",
        )

    items = tuple(
        ShipmentItem(
            sku=li.get("sku"),
            name=str(li.get("name") or "Item"),
            quantity=int(li["remaining_quantity"]),
        )
        for li in fo.line_items
        if int(li.get("remaining_quantity") or 0) > 0
    )
    delivery = DeliveryAddress(
        name=address.get("name") or order.customer_name or "Customer",
        phone=address.get("phone") or order.phone,
        email=order.email,
        company=address.get("company"),
        address1=address["address1"],
        address2=address.get("address2"),
        city=address["city"],
        state=address.get("province"),
        pincode=pincode,
        country=address.get("country_code") or "IN",
    )
    parcel = Parcel(
        weight_g=weight,
        length_cm=_dec(package.get("length_cm")),
        breadth_cm=_dec(package.get("breadth_cm")),
        height_cm=_dec(package.get("height_cm")),
    )
    pickup = PickupLocation(
        warehouse_id=warehouse.id,
        name=warehouse.name,
        contact_name=warehouse.contact_name,
        phone=warehouse.phone,
        email=warehouse.email,
        address1=warehouse.address1,
        address2=warehouse.address2,
        city=warehouse.city,
        state=warehouse.state,
        pincode=warehouse.pincode,
        country=warehouse.country,
    )
    context = AllocationContext(
        payment_mode=payment_mode,
        order_value=order.total_price,
        cod_amount=collect,
        weight_g=weight,
        warehouse_id=warehouse.id,
        destination_pincode=pincode,
        destination_state=address.get("province"),
        destination_country=delivery.country,
        today=utcnow().date(),
        skus=tuple(i.sku for i in items if i.sku),
        tags=tuple(order.tags),
    )
    return ShipmentPlan(
        warehouse_id=warehouse.id,
        pickup=pickup,
        delivery=delivery,
        parcel=parcel,
        items=items,
        payment_mode=payment_mode,
        cod_amount=collect,
        declared_value=order.total_price,
        currency=order.currency,
        context=context,
    )


def _other_open_fulfillment_orders(
    db: Session, order: ShopifyOrder, fo: ShopifyFulfillmentOrder
) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ShopifyFulfillmentOrder)
            .where(
                ShopifyFulfillmentOrder.order_id == order.id,
                ShopifyFulfillmentOrder.id != fo.id,
                ShopifyFulfillmentOrder.logistics_status.not_in(_FINISHED),
                ShopifyFulfillmentOrder.shopify_status.in_(
                    ("OPEN", "ON_HOLD", "SCHEDULED", "IN_PROGRESS")
                ),
            )
        )
        or 0
    )


def _dec(value: object) -> Decimal | None:
    return Decimal(str(value)) if value not in (None, "") else None
