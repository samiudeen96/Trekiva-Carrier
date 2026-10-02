"""Carrier adapter architecture: registry, mock carriers, placeholders, HTTP error mapping."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import httpx
import pytest

from app.carriers import registry
from app.carriers.base import CarrierAdapter
from app.carriers.errors import (
    CarrierAmbiguousError,
    CarrierAuthError,
    CarrierNotImplementedError,
    CarrierTransientError,
    CarrierValidationError,
)
from app.carriers.http import CarrierHttpClient
from app.carriers.mock import MockEkartAdapter, MockScenario, MockXpressBeesAdapter
from app.carriers.types import (
    CarrierCapabilities,
    CreateShipmentRequest,
    DeliveryAddress,
    Parcel,
    ServiceabilityRequest,
    ServiceabilityResult,
    ShipmentItem,
    ShipmentRef,
    ShipmentResult,
)
from app.core.enums import CarrierEnvironment, PaymentMode, TrackingStatus
from app.core.errors import ConfigurationError
from app.logistics.allocation.types import OfferFailure
from app.logistics.offers import collect_offers
from tests.factories import pickup, serviceability_request


def mock(code: str, **creds: object) -> CarrierAdapter:
    return registry.build_adapter(
        code, CarrierEnvironment.MOCK, dict(creds), account_id=1, allow_mock=True
    )


def create_request(
    pincode: str = "560001", key: str = "42:1", payment: PaymentMode = PaymentMode.PREPAID
) -> CreateShipmentRequest:
    return CreateShipmentRequest(
        idempotency_key=key,
        order_name="TR-001500",
        shopify_order_id="gid://shopify/Order/1500",
        shopify_fulfillment_order_id="gid://shopify/FulfillmentOrder/501",
        pickup=pickup(),
        delivery=DeliveryAddress(
            name="Asha", address1="12 MG Road", city="Bengaluru", pincode=pincode
        ),
        parcel=Parcel(weight_g=500),
        items=(ShipmentItem(sku="TRK-TEE-M", name="Tee", quantity=1),),
        payment_mode=payment,
        declared_value=Decimal("999"),
    )


# --- registry -------------------------------------------------------------------------------


def test_registry_has_both_carriers_with_real_and_mock() -> None:
    assert registry.registered_codes() == ["ekart", "xpressbees"]
    for code in ("ekart", "xpressbees"):
        desc = registry.describe(code)
        assert desc.has_real_adapter and desc.has_mock_adapter
    assert registry.get_adapter_class("ekart", CarrierEnvironment.MOCK) is MockEkartAdapter
    assert (
        registry.get_adapter_class("ekart", CarrierEnvironment.PRODUCTION).__name__
        == "EkartAdapter"
    )


def test_mock_adapters_refused_when_not_allowed() -> None:
    with pytest.raises(ConfigurationError):
        registry.build_adapter("ekart", CarrierEnvironment.MOCK, {}, account_id=1, allow_mock=False)


def test_template_adapter_is_not_registered() -> None:
    import app.carriers._template.adapter  # noqa: F401

    assert "new_carrier" not in registry.registered_codes()


def test_registering_a_new_carrier_needs_no_engine_change() -> None:
    @registry.register_carrier()
    class ThirdCarrier(CarrierAdapter):
        code = "third_test_carrier"
        display_name = "Third"
        credentials_schema = MockEkartAdapter.credentials_schema
        capabilities = CarrierCapabilities(serviceability=True)

        def check_serviceability(self, request: ServiceabilityRequest) -> ServiceabilityResult:
            return ServiceabilityResult(serviceable=True)

        def create_shipment(self, request: CreateShipmentRequest) -> ShipmentResult:
            return ShipmentResult(carrier_shipment_id="1", awb="A1")

    try:
        adapter = registry.build_adapter(
            "third_test_carrier",
            CarrierEnvironment.PRODUCTION,
            {},
            account_id=None,
            allow_mock=False,
        )
        assert adapter.get_offer(serviceability_request()).serviceable
    finally:
        registry._REAL.pop("third_test_carrier")


@pytest.mark.parametrize("code", ["ekart", "xpressbees"])
def test_real_adapters_are_placeholders_until_docs_arrive(code: str) -> None:
    adapter = registry.build_adapter(
        code, CarrierEnvironment.PRODUCTION, {}, account_id=1, allow_mock=False
    )
    assert not adapter.capabilities.serviceability
    with pytest.raises(CarrierNotImplementedError, match="awaiting official API documentation"):
        adapter.get_offer(serviceability_request())
    with pytest.raises(CarrierNotImplementedError):
        adapter.create_shipment(create_request())
    with pytest.raises(CarrierNotImplementedError):
        adapter.track_shipment(ShipmentRef(idempotency_key="k"))


# --- mock carriers --------------------------------------------------------------------------


def test_mock_default_offers_match_development_profile() -> None:
    ekart = mock("ekart").get_offer(serviceability_request())
    xb = mock("xpressbees").get_offer(serviceability_request())
    assert (ekart.serviceable, ekart.transit_days, ekart.cost) == (True, 3, Decimal("75.00"))
    assert (xb.serviceable, xb.transit_days, xb.cost) == (True, 2, Decimal("85.00"))


def test_mock_pincode_scenarios() -> None:
    assert not mock("ekart").get_offer(serviceability_request("999001")).serviceable
    assert mock("xpressbees").get_offer(serviceability_request("999001")).serviceable
    assert not mock("xpressbees").get_offer(serviceability_request("999002")).serviceable
    assert mock("ekart").get_offer(serviceability_request("999004")).cod_available is False
    with pytest.raises(CarrierTransientError):
        mock("ekart").get_offer(serviceability_request("999010"))


def test_mock_credentials_override_scenarios() -> None:
    adapter = mock("ekart", scenarios={"110001": "AUTH_FAILURE"}, transit_days=1, cost="40")
    with pytest.raises(CarrierAuthError):
        adapter.get_offer(serviceability_request("110001"))
    offer = adapter.get_offer(serviceability_request())
    assert (offer.transit_days, offer.cost) == (1, Decimal("40"))


def test_mock_create_is_deterministic_per_idempotency_key() -> None:
    adapter = mock("xpressbees")
    first = adapter.create_shipment(create_request(key="7:1"))
    again = adapter.create_shipment(create_request(key="7:1"))
    other = adapter.create_shipment(create_request(key="7:2"))
    assert first.awb == again.awb and first.awb != other.awb
    assert first.awb and first.awb.startswith("XBM")
    assert first.tracking_url and first.awb in first.tracking_url


def test_mock_ambiguous_create_is_found_by_reconciliation() -> None:
    adapter = mock("xpressbees")
    with pytest.raises(CarrierAmbiguousError):
        adapter.create_shipment(create_request(pincode="999020", key="9:1"))
    found = adapter.find_shipment_by_reference("9:1")
    assert found is not None and found.awb


def test_mock_lost_create_reports_not_found() -> None:
    adapter = mock(
        "ekart",
        scenarios={"110002": MockScenario.CREATE_TIMEOUT_LOST.value},
        lost_references=["9:1"],
    )
    with pytest.raises(CarrierAmbiguousError):
        adapter.create_shipment(create_request(pincode="110002", key="9:1"))
    assert adapter.find_shipment_by_reference("9:1") is None


def test_mock_create_validation_errors() -> None:
    with pytest.raises(CarrierValidationError):
        mock("ekart").create_shipment(create_request(pincode="999030"))
    with pytest.raises(CarrierValidationError):
        mock("ekart").create_shipment(create_request(pincode="999004", payment=PaymentMode.COD))


def test_status_normalisation() -> None:
    xb = mock("xpressbees")
    assert xb.normalize_status("OFD") == TrackingStatus.OUT_FOR_DELIVERY
    assert xb.normalize_status(" ofd ") == TrackingStatus.OUT_FOR_DELIVERY
    assert xb.normalize_status("SOMETHING_NEW") == TrackingStatus.EXCEPTION


def test_mock_webhook_signature_and_parsing() -> None:
    adapter = mock("xpressbees")
    assert isinstance(adapter, MockXpressBeesAdapter)
    body = json.dumps(
        {"awb": "XBM1", "status": "OFD", "timestamp": "2026-10-02T10:00:00Z"}
    ).encode()
    good = {"X-Mock-Signature": adapter.sign_webhook(body)}
    assert adapter.verify_webhook(good, body)
    assert not adapter.verify_webhook({"X-Mock-Signature": "bad"}, body)
    assert not adapter.verify_webhook({}, body)
    [update] = adapter.process_webhook(good, body)
    assert update.status == TrackingStatus.OUT_FOR_DELIVERY and update.awb == "XBM1"


# --- parallel offer collection --------------------------------------------------------------


def test_collect_offers_isolates_failures() -> None:
    adapters = {"ekart": mock("ekart"), "xpressbees": mock("xpressbees")}
    requests = {code: serviceability_request("999011") for code in adapters}  # ekart -> HTTP 500
    results = collect_offers(adapters, requests, timeout_seconds=5)
    assert isinstance(results["ekart"].outcome, OfferFailure)
    assert results["ekart"].outcome.retryable
    assert results["xpressbees"].outcome.serviceable  # type: ignore[union-attr]


def test_collect_offers_times_out_slow_carrier() -> None:
    class Slow(MockEkartAdapter):
        def get_offer(self, request: ServiceabilityRequest):  # type: ignore[no-untyped-def]
            time.sleep(1.0)
            return super().get_offer(request)

    slow = Slow(mock("ekart").config)
    started = time.monotonic()
    results = collect_offers(
        {"ekart": slow, "xpressbees": mock("xpressbees")},
        {"ekart": serviceability_request(), "xpressbees": serviceability_request()},
        timeout_seconds=0.2,
    )
    assert time.monotonic() - started < 0.9
    assert isinstance(results["ekart"].outcome, OfferFailure)
    assert results["ekart"].outcome.error_class == "OfferTimeout"
    assert not isinstance(results["xpressbees"].outcome, OfferFailure)


# --- HTTP error mapping for real adapters ---------------------------------------------------


def _client(handler) -> CarrierHttpClient:  # type: ignore[no-untyped-def]
    return CarrierHttpClient(
        carrier_code="x",
        base_url="https://carrier.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("status", "idempotent", "expected"),
    [
        (401, True, CarrierAuthError),
        (422, False, CarrierValidationError),
        (429, False, CarrierTransientError),
        (503, False, CarrierTransientError),
        (500, True, CarrierTransientError),
        (500, False, CarrierAmbiguousError),
        (504, False, CarrierAmbiguousError),
    ],
)
def test_http_status_mapping(status: int, idempotent: bool, expected: type[Exception]) -> None:
    client = _client(lambda req: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(expected):
        client.request("POST", "/shipments", idempotent=idempotent, json={})


def test_connect_error_is_transient_but_read_timeout_on_create_is_ambiguous() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(CarrierTransientError):
        _client(refuse).request("POST", "/shipments", idempotent=False)
    with pytest.raises(CarrierAmbiguousError):
        _client(hang).request("POST", "/shipments", idempotent=False)
    with pytest.raises(CarrierTransientError):
        _client(hang).request("GET", "/track", idempotent=True)
