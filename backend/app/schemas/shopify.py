"""Typed snapshots of Shopify data, parsed from GraphQL Admin API responses.

These are the only shapes the logistics engine sees; it never handles raw GraphQL JSON.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

#: FulfillmentOrderStatus values (GraphQL enum).
FO_OPEN = "OPEN"
FO_ON_HOLD = "ON_HOLD"
FO_IN_PROGRESS = "IN_PROGRESS"
FO_SCHEDULED = "SCHEDULED"
FO_CLOSED = "CLOSED"
FO_CANCELLED = "CANCELLED"
FO_INCOMPLETE = "INCOMPLETE"

DELIVERY_SHIPPING = "SHIPPING"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Address(_Frozen):
    name: str | None = None
    company: str | None = None
    address1: str | None = None
    address2: str | None = None
    city: str | None = None
    province: str | None = None
    province_code: str | None = None
    zip: str | None = None
    country: str | None = None
    country_code: str | None = None
    phone: str | None = None


class RiskSnapshot(_Frozen):
    recommendation: str | None = None
    """ACCEPT / INVESTIGATE / CANCEL / NONE."""
    levels: tuple[str, ...] = ()
    """riskLevel of each assessment: HIGH / MEDIUM / LOW / NONE / PENDING."""

    @property
    def analysed(self) -> bool:
        return bool(self.levels) and all(level != "PENDING" for level in self.levels)

    @property
    def is_high(self) -> bool:
        return "HIGH" in self.levels or self.recommendation == "CANCEL"

    @property
    def highest_level(self) -> str | None:
        for level in ("HIGH", "MEDIUM", "LOW", "NONE", "PENDING"):
            if level in self.levels:
                return level
        return None


class FulfillmentHold(_Frozen):
    id: str | None = None
    reason: str
    """HIGH_RISK_OF_FRAUD, INCORRECT_ADDRESS, OTHER, ..."""
    reason_notes: str | None = None
    display_reason: str | None = None


class FulfillmentOrderLineItem(_Frozen):
    id: str
    line_item_id: str | None = None
    sku: str | None = None
    name: str
    remaining_quantity: int
    total_quantity: int
    unit_weight_g: int | None = None


class FulfillmentOrderSnapshot(_Frozen):
    id: str
    status: str
    request_status: str | None = None
    location_id: str | None = None
    location_name: str | None = None
    delivery_method: str | None = None
    fulfill_at: datetime | None = None
    holds: tuple[FulfillmentHold, ...] = ()
    line_items: tuple[FulfillmentOrderLineItem, ...] = ()

    @property
    def is_on_hold(self) -> bool:
        # Treat ANY hold as blocking, even if status disagrees (defensive).
        return self.status == FO_ON_HOLD or bool(self.holds)

    @property
    def remaining_quantity(self) -> int:
        return sum(li.remaining_quantity for li in self.line_items)

    @property
    def total_weight_g(self) -> int | None:
        if not self.line_items:
            return None
        if any(li.unit_weight_g is None for li in self.line_items if li.remaining_quantity):
            return None
        return sum((li.unit_weight_g or 0) * li.remaining_quantity for li in self.line_items)


class OrderSnapshot(_Frozen):
    id: str
    name: str
    created_at: datetime
    cancelled_at: datetime | None = None
    closed: bool = False
    tags: tuple[str, ...] = ()
    financial_status: str | None = None
    fulfillment_status: str | None = None
    payment_gateways: tuple[str, ...] = ()
    email: str | None = None
    phone: str | None = None
    customer_name: str | None = None
    currency: str
    total_price: Decimal
    outstanding: Decimal
    shipping_address: Address | None = None
    risk: RiskSnapshot = RiskSnapshot()
    fulfillment_orders: tuple[FulfillmentOrderSnapshot, ...] = ()

    def has_tag(self, tag: str) -> bool:
        wanted = tag.casefold()
        return any(t.casefold() == wanted for t in self.tags)
