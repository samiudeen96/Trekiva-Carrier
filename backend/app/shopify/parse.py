"""Convert GraphQL response JSON into typed snapshots."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.time import parse_iso
from app.schemas.shopify import (
    Address,
    FulfillmentHold,
    FulfillmentOrderLineItem,
    FulfillmentOrderSnapshot,
    OrderSnapshot,
    RiskSnapshot,
)

_WEIGHT_TO_GRAMS = {
    "GRAMS": Decimal(1),
    "KILOGRAMS": Decimal(1000),
    "OUNCES": Decimal("28.349523125"),
    "POUNDS": Decimal("453.59237"),
}


def weight_to_grams(weight: dict[str, Any] | None) -> int | None:
    if not weight or weight.get("value") is None:
        return None
    factor = _WEIGHT_TO_GRAMS.get(str(weight.get("unit", "")).upper())
    if factor is None:
        return None
    return int((Decimal(str(weight["value"])) * factor).to_integral_value())


def _money(node: dict[str, Any] | None) -> Decimal:
    if not node:
        return Decimal("0")
    return Decimal(str(node["shopMoney"]["amount"]))


def _nodes(conn: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list((conn or {}).get("nodes") or [])


def parse_address(raw: dict[str, Any] | None) -> Address | None:
    if not raw:
        return None
    return Address(
        name=raw.get("name"),
        company=raw.get("company"),
        address1=raw.get("address1"),
        address2=raw.get("address2"),
        city=raw.get("city"),
        province=raw.get("province"),
        province_code=raw.get("provinceCode"),
        zip=(raw.get("zip") or "").replace(" ", "") or None,
        country=raw.get("country"),
        country_code=raw.get("countryCodeV2"),
        phone=raw.get("phone"),
    )


def parse_fulfillment_order(raw: dict[str, Any]) -> FulfillmentOrderSnapshot:
    location = (raw.get("assignedLocation") or {}) or {}
    line_items = []
    for li in _nodes(raw.get("lineItems")):
        title = li.get("productTitle") or ""
        variant = li.get("variantTitle")
        line_items.append(
            FulfillmentOrderLineItem(
                id=li["id"],
                line_item_id=(li.get("lineItem") or {}).get("id"),
                sku=li.get("sku") or None,
                name=f"{title} - {variant}" if variant else title,
                remaining_quantity=int(li.get("remainingQuantity") or 0),
                total_quantity=int(li.get("totalQuantity") or 0),
                unit_weight_g=weight_to_grams(li.get("weight")),
            )
        )
    return FulfillmentOrderSnapshot(
        id=raw["id"],
        status=raw["status"],
        request_status=raw.get("requestStatus"),
        location_id=(location.get("location") or {}).get("id"),
        location_name=location.get("name"),
        delivery_method=(raw.get("deliveryMethod") or {}).get("methodType"),
        fulfill_at=parse_iso(raw.get("fulfillAt")),
        holds=tuple(
            FulfillmentHold(
                id=h.get("id"),
                reason=h.get("reason") or "OTHER",
                reason_notes=h.get("reasonNotes"),
                display_reason=h.get("displayReason"),
            )
            for h in raw.get("fulfillmentHolds") or []
        ),
        line_items=tuple(line_items),
    )


def parse_order(raw: dict[str, Any]) -> OrderSnapshot:
    risk_raw = raw.get("risk") or {}
    created_at = parse_iso(raw["createdAt"])
    assert created_at is not None
    return OrderSnapshot(
        id=raw["id"],
        name=raw["name"],
        created_at=created_at,
        cancelled_at=parse_iso(raw.get("cancelledAt")),
        closed=bool(raw.get("closed")),
        tags=tuple(raw.get("tags") or ()),
        financial_status=raw.get("displayFinancialStatus"),
        fulfillment_status=raw.get("displayFulfillmentStatus"),
        payment_gateways=tuple(raw.get("paymentGatewayNames") or ()),
        email=raw.get("email"),
        phone=raw.get("phone"),
        customer_name=(raw.get("customer") or {}).get("displayName"),
        client_ip=raw.get("clientIp") or None,
        currency=raw.get("currencyCode") or "INR",
        total_price=_money(raw.get("totalPriceSet")),
        outstanding=_money(raw.get("totalOutstandingSet")),
        shipping_address=parse_address(raw.get("shippingAddress")),
        billing_address=parse_address(raw.get("billingAddress")),
        risk=RiskSnapshot(
            recommendation=risk_raw.get("recommendation"),
            levels=tuple(
                str(a.get("riskLevel"))
                for a in risk_raw.get("assessments") or []
                if a.get("riskLevel")
            ),
        ),
        fulfillment_orders=tuple(
            parse_fulfillment_order(fo) for fo in _nodes(raw.get("fulfillmentOrders"))
        ),
    )
