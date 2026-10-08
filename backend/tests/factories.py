"""Builders for Shopify GraphQL responses, snapshots and allocation inputs used across tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.carriers.types import CarrierOffer, Parcel, PickupLocation, ServiceabilityRequest
from app.core.enums import PaymentMode
from app.logistics.allocation.types import AllocationContext, CarrierProfile
from app.schemas.shopify import OrderSnapshot
from app.shopify.parse import parse_order

#: "Now" for tests. Real time, because the pipeline under test reads the wall clock.
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
TODAY = NOW.date()
LOCATION_GID = "gid://shopify/Location/1001"


def fo_json(
    fo_id: int = 501,
    *,
    status: str = "OPEN",
    holds: list[dict[str, Any]] | None = None,
    method: str | None = "SHIPPING",
    location_gid: str = LOCATION_GID,
    quantity: int = 1,
    weight_grams: float | None = 500,
) -> dict[str, Any]:
    return {
        "id": f"gid://shopify/FulfillmentOrder/{fo_id}",
        "status": status,
        "requestStatus": "UNSUBMITTED",
        "fulfillAt": None,
        "deliveryMethod": {"methodType": method} if method else None,
        "assignedLocation": {"name": "Main Warehouse", "location": {"id": location_gid}},
        "fulfillmentHolds": holds or [],
        "lineItems": {
            "nodes": [
                {
                    "id": f"gid://shopify/FulfillmentOrderLineItem/{fo_id}1",
                    "sku": "TRK-TEE-M",
                    "productTitle": "Trekking Tee",
                    "variantTitle": "M",
                    "remainingQuantity": quantity,
                    "totalQuantity": quantity,
                    "weight": {"unit": "GRAMS", "value": weight_grams}
                    if weight_grams is not None
                    else None,
                    "lineItem": {"id": f"gid://shopify/LineItem/{fo_id}1"},
                }
            ]
        },
    }


DUPLICATE_HOLD = {
    "id": "gid://shopify/FulfillmentHold/1",
    "reason": "OTHER",
    "reasonNotes": "Possible duplicate order (Flow)",
    "displayReason": "Other",
}
RISK_HOLD = {
    "id": "gid://shopify/FulfillmentHold/2",
    "reason": "HIGH_RISK_OF_FRAUD",
    "reasonNotes": "High risk (Flow)",
    "displayReason": "High risk of fraud",
}


def order_json(
    order_id: int = 1500,
    *,
    name: str = "TR-001500",
    created_at: datetime | None = None,
    tags: list[str] | None = None,
    gateways: list[str] | None = None,
    outstanding: str = "0.00",
    total: str = "999.00",
    pincode: str = "560001",
    cancelled_at: datetime | None = None,
    risk_levels: list[str] | None = None,
    recommendation: str | None = "ACCEPT",
    fulfillment_orders: list[dict[str, Any]] | None = None,
    shipping_address: dict[str, Any] | bool | None = True,
    phone: str | None = None,
    customer_name: str = "Asha Rao",
    client_ip: str | None = None,
    billing_address: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created = created_at or NOW - timedelta(hours=1)
    # Unique per order by default, so unrelated test orders never look like duplicates.
    phone = phone or f"+9198{order_id:08d}"
    address: dict[str, Any] | None
    if shipping_address is True:
        address = {
            "name": customer_name,
            "company": None,
            "address1": "12 MG Road",
            "address2": None,
            "city": "Bengaluru",
            "province": "Karnataka",
            "provinceCode": "KA",
            "zip": pincode,
            "country": "India",
            "countryCodeV2": "IN",
            "phone": phone,
        }
    elif shipping_address in (False, None):
        address = None
    else:
        address = shipping_address  # type: ignore[assignment]
    return {
        "id": f"gid://shopify/Order/{order_id}",
        "name": name,
        "createdAt": created.isoformat(),
        "cancelledAt": cancelled_at.isoformat() if cancelled_at else None,
        "closed": False,
        "tags": tags or [],
        "email": "asha@example.com",
        "phone": phone,
        "displayFinancialStatus": "PAID" if outstanding == "0.00" else "PENDING",
        "displayFulfillmentStatus": "UNFULFILLED",
        "paymentGatewayNames": gateways if gateways is not None else ["razorpay"],
        "currencyCode": "INR",
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "INR"}},
        "totalOutstandingSet": {"shopMoney": {"amount": outstanding, "currencyCode": "INR"}},
        "customer": {"displayName": customer_name},
        "clientIp": client_ip,
        "shippingAddress": address,
        "billingAddress": billing_address,
        "risk": {
            "recommendation": recommendation,
            "assessments": [
                {"riskLevel": lvl} for lvl in (risk_levels if risk_levels is not None else ["LOW"])
            ],
        },
        "fulfillmentOrders": {
            "nodes": fulfillment_orders if fulfillment_orders is not None else [fo_json()]
        },
    }


def order_snapshot(**kwargs: Any) -> OrderSnapshot:
    return parse_order(order_json(**kwargs))


@dataclass
class FakeShopifyAdmin:
    """Stands in for ShopifyAdmin: serves GraphQL-shaped orders through the real parser and
    records every mutation. Creating a fulfillment closes the fulfillment order, as Shopify does."""

    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    error: Exception | None = None
    fulfillments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    cancelled_fulfillments: list[str] = field(default_factory=list)
    tags_added: list[tuple[str, list[str]]] = field(default_factory=list)
    fulfillment_error: Exception | None = None
    holds_placed: list[tuple[str, str, str]] = field(default_factory=list)
    hold_error: Exception | None = None

    def put(self, raw: dict[str, Any]) -> None:
        self.orders[raw["id"]] = raw

    def fo(self, fo_gid: str) -> dict[str, Any]:
        for raw in self.orders.values():
            for fo in raw["fulfillmentOrders"]["nodes"]:
                if fo["id"] == fo_gid:
                    return fo  # type: ignore[no-any-return]
        raise KeyError(fo_gid)

    def fetch_order(self, order_gid: str) -> OrderSnapshot:
        self.calls.append(order_gid)
        if self.error:
            raise self.error
        return parse_order(self.orders[order_gid])

    def fetch_order_id_for_fulfillment_order(self, fo_gid: str) -> str | None:
        for gid, raw in self.orders.items():
            if any(fo["id"] == fo_gid for fo in raw["fulfillmentOrders"]["nodes"]):
                return gid
        return None

    def find_fulfillment_by_tracking(self, fo_gid: str, number: str) -> str | None:
        for f in self.fulfillments.get(fo_gid, []):
            if f["number"] == number and f["status"] != "CANCELLED":
                return str(f["id"])
        return None

    def create_fulfillment(
        self, fo_gid: str, *, company: str, number: str, url: str | None, notify_customer: bool
    ) -> str:
        if self.fulfillment_error:
            raise self.fulfillment_error
        fid = f"gid://shopify/Fulfillment/{sum(len(v) for v in self.fulfillments.values()) + 1}"
        self.fulfillments.setdefault(fo_gid, []).append(
            {
                "id": fid,
                "company": company,
                "number": number,
                "url": url,
                "notify": notify_customer,
                "status": "SUCCESS",
            }
        )
        fo = self.fo(fo_gid)
        fo["status"] = "CLOSED"
        for li in fo["lineItems"]["nodes"]:
            li["remainingQuantity"] = 0
        return fid

    def create_fulfillment_event(
        self,
        fulfillment_gid: str,
        *,
        status: str,
        happened_at: datetime,
        message: str | None = None,
        city: str | None = None,
        estimated_delivery_at: datetime | None = None,
    ) -> str:
        self.events.append(
            {"fulfillment": fulfillment_gid, "status": status, "message": message, "city": city}
        )
        return f"gid://shopify/FulfillmentEvent/{len(self.events)}"

    def cancel_fulfillment(self, fulfillment_gid: str) -> None:
        self.cancelled_fulfillments.append(fulfillment_gid)
        for items in self.fulfillments.values():
            for f in items:
                if f["id"] == fulfillment_gid:
                    f["status"] = "CANCELLED"

    def hold_fulfillment_order(self, fo_gid: str, *, reason: str, notes: str) -> str:
        if self.hold_error:
            raise self.hold_error
        fo = self.fo(fo_gid)
        hold = {
            "id": f"gid://shopify/FulfillmentHold/{len(self.holds_placed) + 100}",
            "reason": reason,
            "reasonNotes": notes,
            "displayReason": reason,
        }
        fo["status"] = "ON_HOLD"
        fo["fulfillmentHolds"] = [*fo["fulfillmentHolds"], hold]
        self.holds_placed.append((fo_gid, reason, notes))
        return str(hold["id"])

    def release_holds(self, fo_gid: str) -> None:
        """What staff do in Shopify admin after verifying the customer."""
        fo = self.fo(fo_gid)
        fo["status"] = "OPEN"
        fo["fulfillmentHolds"] = []

    def add_tags(self, resource_gid: str, tags: list[str]) -> None:
        self.tags_added.append((resource_gid, tags))


# --- allocation ----------------------------------------------------------------------------


def ctx(
    *,
    payment_mode: PaymentMode = PaymentMode.PREPAID,
    weight_g: int = 500,
    warehouse_id: int = 1,
    pincode: str = "560001",
    state: str | None = "Karnataka",
    order_value: str = "999",
    tags: tuple[str, ...] = (),
    skus: tuple[str, ...] = ("TRK-TEE-M",),
) -> AllocationContext:
    return AllocationContext(
        payment_mode=payment_mode,
        order_value=Decimal(order_value),
        cod_amount=Decimal(order_value) if payment_mode == PaymentMode.COD else Decimal(0),
        weight_g=weight_g,
        warehouse_id=warehouse_id,
        destination_pincode=pincode,
        destination_state=state,
        destination_country="IN",
        today=TODAY,
        skus=skus,
        tags=tags,
    )


def profile(code: str, **kwargs: Any) -> CarrierProfile:
    defaults: dict[str, Any] = {"enabled": True, "supported_warehouse_ids": frozenset({1})}
    defaults.update(kwargs)
    return CarrierProfile(carrier_code=code, **defaults)


def offer(
    code: str, *, days: int | None = 3, cost: str | None = "75", **kwargs: Any
) -> CarrierOffer:
    defaults: dict[str, Any] = {
        "serviceable": True,
        "cod_available": True,
        "prepaid_available": True,
        "pickup_available": True,
    }
    defaults.update(kwargs)
    return CarrierOffer(
        carrier_code=code,
        cost=Decimal(cost) if cost is not None else None,
        edd=TODAY + timedelta(days=days) if days is not None else None,
        **defaults,
    )


def pickup() -> PickupLocation:
    return PickupLocation(
        warehouse_id=1,
        name="Main Warehouse",
        phone="+919811111111",
        address1="Plot 7, Industrial Area",
        city="Bengaluru",
        state="Karnataka",
        pincode="560058",
    )


def serviceability_request(
    pincode: str = "560001", payment_mode: PaymentMode = PaymentMode.PREPAID
) -> ServiceabilityRequest:
    return ServiceabilityRequest(
        pickup=pickup(),
        delivery_pincode=pincode,
        delivery_city="Bengaluru",
        delivery_state="Karnataka",
        parcel=Parcel(weight_g=500),
        payment_mode=payment_mode,
        order_value=Decimal("999"),
        cod_amount=Decimal("999") if payment_mode == PaymentMode.COD else Decimal(0),
    )


__all__ = ["NOW", "TODAY", "date"]
