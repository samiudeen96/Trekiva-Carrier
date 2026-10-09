"""Location risk check (IP and billing address vs delivery state) and duplicate normalisation."""

from __future__ import annotations

from typing import Any

from app.core.enums import PaymentMode
from app.core.geoip import IpLocation
from app.logistics.hold_gate import GateAction, GateHistory, GateReason, evaluate_gate
from app.logistics.review_checks import (
    LOCATION_MISMATCH,
    location_flags,
    normalise_name,
    normalise_phone,
)
from app.schemas.settings import ShopSettings
from tests.factories import NOW, order_snapshot

MUMBAI = IpLocation(country_code="IN", state_code="MH", state_name="Maharashtra", city="Mumbai")
BENGALURU = IpLocation(country_code="IN", state_code="KA", state_name="Karnataka", city="Bengaluru")
DELHI = IpLocation(
    country_code="IN", state_code="DL", state_name="National Capital Territory of Delhi", city=None
)
SINGAPORE = IpLocation(country_code="SG", state_code=None, state_name=None, city="Singapore")


def address(city: str, province: str, code: str, country: str = "IN") -> dict[str, Any]:
    return {
        "name": "Asha Rao",
        "city": city,
        "province": province,
        "provinceCode": code,
        "zip": "400001",
        "country": "India",
        "countryCodeV2": country,
    }


CHENNAI = address("Chennai", "Tamil Nadu", "TN")


def flags(
    ip_location: IpLocation | None = None, settings: ShopSettings | None = None, **kwargs: Any
) -> list[Any]:
    snap = order_snapshot(client_ip="49.36.10.1" if ip_location else None, **kwargs)
    settings = settings or ShopSettings(location_check_ip=True)
    return location_flags(snap, settings, lambda _ip: ip_location)


def test_ip_in_another_state_is_a_risk_order() -> None:
    # Customer orders from Mumbai, delivery is to Chennai.
    [flag] = flags(MUMBAI, shipping_address=CHENNAI)
    assert flag.kind == LOCATION_MISMATCH
    assert flag.tag == "RISK-REVIEW"
    assert "Mumbai, Maharashtra" in flag.detail
    assert "Chennai, Tamil Nadu" in flag.detail


def test_ip_check_is_off_by_default() -> None:
    assert flags(MUMBAI, ShopSettings(), shipping_address=CHENNAI) == []


def test_ip_in_the_delivery_state_is_fine() -> None:
    # Default shipping address is Bengaluru, Karnataka.
    assert flags(BENGALURU) == []


def test_state_names_match_when_codes_are_missing() -> None:
    ip = IpLocation(country_code="IN", state_code=None, state_name="Tamil Nadu", city="Madurai")
    assert flags(ip, shipping_address=CHENNAI) == []


def test_delhi_long_name_matches() -> None:
    assert flags(DELHI, shipping_address=address("New Delhi", "Delhi", "DL")) == []


def test_old_and_new_state_codes_match() -> None:
    # Telangana: TS (older ISO code, used by some sources) and TG (current).
    ip = IpLocation(country_code="IN", state_code="TG", state_name=None, city="Hyderabad")
    assert flags(ip, shipping_address=address("Hyderabad", "Telangana", "TS")) == []


def test_ip_abroad_is_a_risk_order() -> None:
    [flag] = flags(SINGAPORE)
    assert "Singapore" in flag.detail


def test_unknown_ip_location_never_flags() -> None:
    assert flags(None) == []
    no_state = IpLocation(country_code="IN", state_code=None, state_name=None, city=None)
    assert flags(no_state, shipping_address=CHENNAI) == []


def test_billing_address_in_another_state_is_a_risk_order() -> None:
    [flag] = flags(billing_address=address("Mumbai", "Maharashtra", "MH"))
    assert "billing address in Mumbai, Maharashtra" in flag.detail


def test_ip_and_billing_reasons_are_combined_into_one_flag() -> None:
    [flag] = flags(
        MUMBAI, shipping_address=CHENNAI, billing_address=address("Pune", "Maharashtra", "MH")
    )
    assert "ordered from Mumbai" in flag.detail and "billing address in Pune" in flag.detail


def test_location_check_can_be_switched_off() -> None:
    assert flags(MUMBAI, ShopSettings(location_check_enabled=False), shipping_address=CHENNAI) == []
    assert flags(MUMBAI, ShopSettings(location_check_ip=False), shipping_address=CHENNAI) == []
    assert (
        flags(
            settings=ShopSettings(location_check_billing=False),
            billing_address=address("Mumbai", "Maharashtra", "MH"),
        )
        == []
    )


def test_phone_normalisation() -> None:
    assert normalise_phone("+91 98765 43210") == "9876543210"
    assert normalise_phone("098765-43210") == "9876543210"
    assert normalise_phone("9876543210") == "9876543210"
    assert normalise_phone("12345") is None
    assert normalise_phone(None) is None


def test_name_normalisation() -> None:
    assert normalise_name("  Asha   RAO ") == "asha rao"
    assert normalise_name("") is None


def test_gate_blocks_flagged_order_until_reviewed() -> None:
    order = order_snapshot()
    kwargs: dict[str, Any] = {
        "order": order,
        "fulfillment_order": order.fulfillment_orders[0],
        "settings": ShopSettings(),
        "payment_mode": PaymentMode.PREPAID,
        "automation_disabled": False,
        "now": NOW,
        "review_flags": ("Possible duplicate order.",),
    }
    decision = evaluate_gate(history=GateHistory(), **kwargs)
    assert decision.action == GateAction.MANUAL_REVIEW
    assert decision.reason == GateReason.REVIEW_CHECK_FLAGGED
    assert "Possible duplicate order." in decision.detail
    # Once the Trekiva hold has been seen and released in Shopify, the order proceeds.
    released = GateHistory(hold_last_seen_at=NOW, hold_released_at=NOW)
    assert evaluate_gate(history=released, **kwargs).action == GateAction.PROCEED
