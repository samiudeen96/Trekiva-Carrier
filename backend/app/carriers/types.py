"""Carrier-neutral data types exchanged between the logistics engine and carrier adapters.

Adapters translate these to and from each carrier's own API format. Nothing outside an adapter
package ever sees carrier-specific request or response shapes (only the opaque `raw` dicts,
which are stored for audit).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import CarrierEnvironment, PaymentMode, TrackingStatus


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class CarrierCapabilities(_Model):
    serviceability: bool = True
    quote: bool = False
    edd: bool = False
    combined_offer: bool = False
    """A single API call returns serviceability + rate + EDD (adapter overrides get_offer)."""
    awb_on_create: bool = True
    """create_shipment already returns the AWB (generate_awb is then a no-op)."""
    idempotent_create: bool = False
    """The carrier deduplicates create requests on our reference (idempotency key)."""
    lookup_by_reference: bool = False
    """find_shipment_by_reference can confirm whether a timed-out create succeeded."""
    cancel: bool = False
    tracking_poll: bool = False
    tracking_webhook: bool = False
    label: bool = False


class PickupLocation(_Model):
    warehouse_id: int
    name: str
    contact_name: str | None = None
    phone: str
    email: str | None = None
    address1: str
    address2: str | None = None
    city: str
    state: str
    pincode: str
    country: str = "IN"
    carrier_warehouse_ref: str | None = None
    """The carrier's own pickup-location identifier, if it uses one."""


class DeliveryAddress(_Model):
    name: str
    phone: str | None = None
    email: str | None = None
    company: str | None = None
    address1: str
    address2: str | None = None
    city: str
    state: str | None = None
    pincode: str
    country: str = "IN"


class Parcel(_Model):
    weight_g: int = Field(gt=0)
    length_cm: Decimal | None = None
    breadth_cm: Decimal | None = None
    height_cm: Decimal | None = None


class ShipmentItem(_Model):
    sku: str | None
    name: str
    quantity: int
    unit_price: Decimal | None = None


class ServiceabilityRequest(_Model):
    pickup: PickupLocation
    delivery_pincode: str
    delivery_city: str | None = None
    delivery_state: str | None = None
    delivery_country: str = "IN"
    parcel: Parcel
    payment_mode: PaymentMode
    order_value: Decimal
    cod_amount: Decimal = Decimal("0")
    currency: str = "INR"


class ServiceabilityResult(_Model):
    serviceable: bool
    cod_available: bool | None = None
    prepaid_available: bool | None = None
    pickup_available: bool | None = None
    reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class QuoteResult(_Model):
    cost: Decimal
    currency: str = "INR"
    raw: dict[str, Any] = Field(default_factory=dict)


class EddResult(_Model):
    edd: date | None = None
    transit_days: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CarrierOffer(_Model):
    """Everything the allocation engine needs to compare one carrier against others."""

    carrier_code: str
    serviceable: bool
    cod_available: bool | None = None
    prepaid_available: bool | None = None
    pickup_available: bool | None = None
    cost: Decimal | None = None
    currency: str = "INR"
    edd: date | None = None
    transit_days: int | None = None
    performance_score: Decimal | None = None
    """Carrier-reported score, if the API provides one (otherwise carrier settings are used)."""
    reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CreateShipmentRequest(_Model):
    idempotency_key: str
    """Stable per attempt. Send it to the carrier as the client reference / order number so a
    retried or timed-out create can be deduplicated or looked up."""
    order_name: str
    shopify_order_id: str
    shopify_fulfillment_order_id: str
    pickup: PickupLocation
    delivery: DeliveryAddress
    parcel: Parcel
    items: tuple[ShipmentItem, ...]
    payment_mode: PaymentMode
    cod_amount: Decimal = Decimal("0")
    declared_value: Decimal
    currency: str = "INR"


class ShipmentResult(_Model):
    carrier_shipment_id: str | None
    awb: str | None
    tracking_url: str | None = None
    label_url: str | None = None
    cost: Decimal | None = None
    currency: str | None = None
    edd: date | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ShipmentRef(_Model):
    idempotency_key: str
    carrier_shipment_id: str | None = None
    awb: str | None = None


class AwbResult(_Model):
    awb: str
    tracking_url: str | None = None
    label_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CancelResult(_Model):
    cancelled: bool
    reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TrackingUpdate(_Model):
    awb: str
    raw_status: str
    status: TrackingStatus
    occurred_at: datetime
    description: str | None = None
    location: str | None = None
    edd: date | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CarrierAccountConfig(_Model):
    """What an adapter instance is constructed with."""

    account_id: int | None
    carrier_code: str
    environment: CarrierEnvironment
    credentials: BaseModel
    """An instance of the adapter's `credentials_schema`."""
