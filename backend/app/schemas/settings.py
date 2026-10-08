"""Per-shop automation settings (stored in `shops.settings` JSONB)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ShopSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timezone: str = "Asia/Kolkata"

    # --- Order gate ------------------------------------------------------------------------
    settle_window_seconds: int = Field(default=300, ge=0, le=86_400)
    """Wait this long after order creation before allocating, so Shopify Flow's duplicate and
    risk workflows have time to tag the order and place a fulfillment hold."""
    wait_for_risk_analysis: bool = True
    """Also wait (up to `risk_wait_max_seconds`) until Shopify's risk analysis has completed."""
    risk_wait_max_seconds: int = Field(default=900, ge=0, le=86_400)
    review_tags: list[str] = Field(default_factory=lambda: ["DUPLICATE-REVIEW", "RISK-REVIEW"])
    """Tags that block processing even if Shopify shows no hold (defence in depth). Tags can only
    ever block shipping; they never allow it."""
    block_on_high_risk: bool = True
    """Block HIGH-risk orders that have no Shopify hold (in case Flow failed to place one)."""

    # --- Trekiva review checks (run once, when an order is otherwise ready to ship) ----------
    # A flagged order gets a Shopify fulfillment hold plus a review tag. Staff verify the
    # customer and release the hold in Shopify; the order then ships normally.
    location_check_enabled: bool = True
    """Flag orders whose delivery state differs from where the customer ordered from."""
    location_check_ip: bool = True
    """Compare the customer's IP location (needs GEOIP_CITY_DB_PATH) with the delivery state."""
    location_check_billing: bool = True
    """Compare the billing address state with the delivery state."""
    risk_review_tag: str = "RISK-REVIEW"
    duplicate_check_enabled: bool = True
    """Flag orders with the same phone, customer name and at least one SKU as another order."""
    duplicate_window_hours: int = Field(default=24, ge=1, le=720)
    duplicate_review_tag: str = "DUPLICATE-REVIEW"

    # --- Payment detection (COD King stays responsible for OTP + COD fee) ------------------
    cod_gateway_names: list[str] = Field(
        default_factory=lambda: ["Cash on Delivery (COD)", "Cash on Delivery", "COD"]
    )
    """Payment gateway names (case-insensitive) that identify a COD order."""

    # --- Shopify sync ----------------------------------------------------------------------
    fulfill_on: Literal["AWB_CREATED", "PICKED_UP"] = "AWB_CREATED"
    notify_customer: bool = False
    shipped_tag: str | None = None
    """Optional tag added (never removed) when a shipment is created, e.g. TREKIVA-SHIPPED."""

    # --- Shipments -------------------------------------------------------------------------
    max_create_attempts_per_carrier: int = Field(default=3, ge=1, le=10)
    """Transient failures retry the same carrier this many times before falling back."""
    cancel_shipment_on_order_cancel: bool = True
    """Cancel the courier shipment automatically when the order is cancelled in Shopify
    (only possible before pickup)."""

    @field_validator("review_tags", "cod_gateway_names")
    @classmethod
    def _strip(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v and v.strip()]

    @field_validator("risk_review_tag", "duplicate_review_tag")
    @classmethod
    def _required_tag(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review tag must not be empty")
        return value.strip()

    @classmethod
    def load(cls, raw: dict[str, Any] | None) -> ShopSettings:
        return cls.model_validate(raw or {})
