"""Trekiva's own review checks, run once when a fulfillment order is first ready to ship.

    location  - the customer ordered from a different state than the delivery address, judged by
                IP location and by billing address. Example: IP in Mumbai, delivery to Chennai.
    duplicate - another order within `duplicate_window_hours` has the same phone number, the same
                customer name and at least one SKU in common.

The checks never stop a customer from placing an order; they only keep it out of automatic
shipping. A flagged fulfillment order gets a Shopify fulfillment hold plus a review tag, so the
alert shows on the Shopify order. Staff verify the customer and release the hold in Shopify; the
order then ships normally and is not checked again.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import geoip
from app.core.db import session_scope
from app.core.enums import LogLevel
from app.logistics.hold_gate import GateReason
from app.logistics.results import StepResult
from app.models import Shop, ShopifyFulfillmentOrder, ShopifyOrder
from app.schemas.settings import ShopSettings
from app.schemas.shopify import FO_OPEN, Address, OrderSnapshot
from app.services import audit, shops
from app.shopify.errors import ShopifyError
from app.workers import enqueue
from app.workers.retry import backoff_seconds

LOCATION_MISMATCH = "LOCATION_MISMATCH"
DUPLICATE_ORDER = "DUPLICATE_ORDER"

#: How many matching orders a duplicate flag names.
MAX_DUPLICATES_LISTED = 3


@dataclass(frozen=True)
class ReviewFlag:
    kind: str
    tag: str
    detail: str

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "tag": self.tag, "detail": self.detail}


def run_checks(
    db: Session, shop_id: int, order: ShopifyOrder, snap: OrderSnapshot, settings: ShopSettings
) -> list[ReviewFlag]:
    return [*location_flags(snap, settings), *duplicate_flags(db, shop_id, order, snap, settings)]


# --- location ------------------------------------------------------------------------------

# ISO 3166-2:IN codes that changed; Shopify and GeoIP databases may use either spelling.
_IN_STATE_CODE_ALIASES = {"TS": "TG", "CT": "CG", "UT": "UK", "OR": "OD", "DD": "DH", "DN": "DH"}
_STATE_NAME_ALIASES = {
    "orissa": "odisha",
    "pondicherry": "puducherry",
    "uttaranchal": "uttarakhand",
    "nationalcapitalterritoryofdelhi": "delhi",
    "nctofdelhi": "delhi",
}


@dataclass(frozen=True)
class _Place:
    country: str | None
    state_code: str | None
    state_name: str | None
    label: str


def _place(
    country: str | None, state_code: str | None, state_name: str | None, label: str
) -> _Place:
    country = country.strip().upper() if country else None
    code = state_code.strip().upper().removeprefix(f"{country}-") if state_code else None
    if code and country == "IN":
        code = _IN_STATE_CODE_ALIASES.get(code, code)
    name = re.sub(r"[^a-z]", "", state_name.casefold().replace("&", "and")) if state_name else None
    if name:
        name = _STATE_NAME_ALIASES.get(name, name)
    return _Place(country, code or None, name or None, label)


def _address_place(a: Address) -> _Place:
    label = ", ".join(p for p in (a.city, a.province) if p) or a.zip or "unknown"
    return _place(a.country_code, a.province_code, a.province, label)


def _differs(a: _Place, b: _Place) -> bool:
    """True only when the two places are known to be in different states (or countries).
    Missing data never flags an order."""
    if a.country and b.country and a.country != b.country:
        return True
    if a.state_code and a.state_code == b.state_code:
        return False
    if a.state_name and a.state_name == b.state_name:
        return False
    return bool((a.state_code and b.state_code) or (a.state_name and b.state_name))


def location_flags(
    snap: OrderSnapshot,
    settings: ShopSettings,
    lookup: Callable[[str], geoip.IpLocation | None] | None = None,
) -> list[ReviewFlag]:
    lookup = lookup or geoip.lookup
    ship = snap.shipping_address
    if not settings.location_check_enabled or ship is None:
        return []
    delivery = _address_place(ship)
    reasons = []
    if settings.location_check_ip and snap.client_ip:
        loc = lookup(snap.client_ip)
        if loc is not None:
            ip_place = _place(loc.country_code, loc.state_code, loc.state_name, loc.describe())
            if _differs(ip_place, delivery):
                reasons.append(f"ordered from {ip_place.label} (IP {snap.client_ip})")
    if settings.location_check_billing and snap.billing_address:
        billing = _address_place(snap.billing_address)
        if _differs(billing, delivery):
            reasons.append(f"has a billing address in {billing.label}")
    if not reasons:
        return []
    return [
        ReviewFlag(
            LOCATION_MISMATCH,
            settings.risk_review_tag,
            f"Risk order: customer {' and '.join(reasons)}, but delivery is to {delivery.label}.",
        )
    ]


# --- duplicates ----------------------------------------------------------------------------


def normalise_phone(phone: str | None) -> str | None:
    """Last 10 digits, so +91 98765 43210, 098765-43210 and 9876543210 all match."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else None


def normalise_name(name: str | None) -> str | None:
    return " ".join((name or "").split()).casefold() or None


def _order_skus(order: ShopifyOrder) -> set[str]:
    return {
        str(li["sku"]).casefold()
        for fo in order.fulfillment_orders
        for li in fo.line_items
        if li.get("sku")
    }


def _ago(delta: timedelta) -> str:
    minutes = int(abs(delta.total_seconds()) // 60)
    span = f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"
    return f"{span} earlier" if delta.total_seconds() >= 0 else f"{span} later"


def duplicate_flags(
    db: Session, shop_id: int, order: ShopifyOrder, snap: OrderSnapshot, settings: ShopSettings
) -> list[ReviewFlag]:
    if not settings.duplicate_check_enabled:
        return []
    phone = normalise_phone(order.phone)
    name = normalise_name(order.customer_name)
    skus = set(snap.skus)
    if not (phone and name and skus):
        return []
    window = timedelta(hours=settings.duplicate_window_hours)
    created = order.shopify_created_at
    candidates = db.scalars(
        select(ShopifyOrder)
        .where(
            ShopifyOrder.shop_id == shop_id,
            ShopifyOrder.id != order.id,
            ShopifyOrder.cancelled_at.is_(None),
            ShopifyOrder.shopify_created_at.between(created - window, created + window),
            func.right(func.regexp_replace(ShopifyOrder.phone, r"\D", "", "g"), 10) == phone,
        )
        .order_by(ShopifyOrder.shopify_created_at)
    ).all()
    matches = []
    for other in candidates:
        if normalise_name(other.customer_name) != name:
            continue
        common = skus & _order_skus(other)
        if common:
            matches.append(
                f"{other.name} ({_ago(created - other.shopify_created_at)}, SKU "
                f"{', '.join(sorted(common)).upper()})"
            )
    if not matches:
        return []
    listed = "; ".join(matches[:MAX_DUPLICATES_LISTED])
    more = len(matches) - MAX_DUPLICATES_LISTED
    return [
        ReviewFlag(
            DUPLICATE_ORDER,
            settings.duplicate_review_tag,
            "Possible duplicate order: same phone, customer name and product as "
            f"{listed}{f' and {more} more' if more > 0 else ''}.",
        )
    ]


# --- Shopify hold --------------------------------------------------------------------------


def place_review_hold(
    fo_id: int,
    *,
    admin_factory: shops.AdminFactory = shops.default_admin_factory,
    attempt: int = 0,
) -> StepResult:
    """Put a flagged fulfillment order on hold in Shopify and tag the order. Idempotent: an
    order already on hold is only tagged."""
    with session_scope() as db:
        fo = db.scalar(
            select(ShopifyFulfillmentOrder)
            .where(ShopifyFulfillmentOrder.id == fo_id)
            .with_for_update()
        )
        if fo is None:
            return StepResult("not_found")
        if (
            not fo.review_flags
            or fo.review_override_at is not None
            or fo.status_reason != GateReason.REVIEW_CHECK_FLAGGED.value
        ):
            return StepResult("not_needed")
        shop = db.get(Shop, fo.shop_id)
        order = db.get(ShopifyOrder, fo.order_id)
        assert shop is not None and order is not None
        flags = fo.review_flags
        tags = sorted({str(f["tag"]) for f in flags})
        reason = (
            "HIGH_RISK_OF_FRAUD" if any(f["kind"] == LOCATION_MISMATCH for f in flags) else "OTHER"
        )
        notes = "Trekiva: " + " ".join(str(f["detail"]) for f in flags)

        admin = admin_factory(db, shop)
        held = False
        try:
            snap = admin.fetch_order(order.shopify_order_id)
            fo_snap = next(
                (f for f in snap.fulfillment_orders if f.id == fo.shopify_fulfillment_order_id),
                None,
            )
            if fo_snap is not None and fo_snap.status == FO_OPEN and not fo_snap.is_on_hold:
                admin.hold_fulfillment_order(
                    fo.shopify_fulfillment_order_id, reason=reason, notes=notes
                )
                held = True
            admin.add_tags(order.shopify_order_id, tags)
        except ShopifyError as exc:
            if exc.retryable:
                return StepResult("retry", detail=str(exc), retry_in=backoff_seconds(attempt))
            fo.status_detail = (
                f"{fo.status_detail} Trekiva could not place the Shopify hold ({exc}): "
                "verify the customer, then approve the order here or cancel it in Shopify."
            )
            audit.record(
                db,
                shop_id=shop.id,
                order_id=order.id,
                fulfillment_order_id=fo.id,
                step=audit.Step.ERROR,
                level=LogLevel.ERROR,
                message=f"{order.name}: could not place the review hold in Shopify: {exc}",
            )
            return StepResult("failed", detail=str(exc))

        audit.record(
            db,
            shop_id=shop.id,
            order_id=order.id,
            fulfillment_order_id=fo.id,
            step=audit.Step.REVIEW_HOLD_PLACED,
            level=LogLevel.WARNING,
            message=(
                f"{order.name}: put on hold in Shopify and tagged {', '.join(tags)}"
                if held
                else f"{order.name}: already on hold in Shopify; tagged {', '.join(tags)}"
            ),
            data={"flags": flags, "hold_reason": reason, "hold_placed": held},
        )
        enqueue.enqueue_after_commit(
            db, enqueue.ORDER_SYNC, shop.id, order.shopify_order_id, "review-hold"
        )
        return StepResult("held" if held else "already_held")
