"""Development/test mock carriers.

They behave like real adapters (same interface, same error taxonomy) with no network calls, so
the whole allocation and shipment flow can be built and tested before carrier credentials exist.
Behaviour per destination pincode is controlled by scenarios (see `scenarios.py`); test code
can also pass scenarios through the mock account's credentials.

All raw status codes used here are MOCK codes, not the carriers' real codes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, Field, SecretStr

from app.carriers.base import CarrierAdapter
from app.carriers.errors import (
    CarrierAmbiguousError,
    CarrierAuthError,
    CarrierTransientError,
    CarrierValidationError,
)
from app.carriers.mock.scenarios import DEFAULT_SCENARIOS, MockScenario
from app.carriers.registry import register_carrier
from app.carriers.types import (
    CancelResult,
    CarrierCapabilities,
    CarrierOffer,
    CreateShipmentRequest,
    EddResult,
    QuoteResult,
    ServiceabilityRequest,
    ServiceabilityResult,
    ShipmentRef,
    ShipmentResult,
    TrackingUpdate,
)
from app.core.enums import PaymentMode, TrackingStatus
from app.core.time import parse_iso, utcnow


class MockCredentials(BaseModel):
    api_key: SecretStr = SecretStr("mock-api-key")
    transit_days: int | None = Field(
        default=None, ge=0, description="Override default transit days"
    )
    cost: Decimal | None = Field(default=None, ge=0, description="Override default cost")
    scenarios: dict[str, MockScenario] = Field(
        default_factory=dict, description="Destination pincode -> scenario overrides"
    )
    lost_references: list[str] = Field(
        default_factory=list,
        description="Idempotency keys that reconciliation should report as NOT created",
    )


class _MockCarrierAdapter(CarrierAdapter):
    credentials_schema = MockCredentials
    capabilities = CarrierCapabilities(
        serviceability=True,
        quote=True,
        edd=True,
        combined_offer=True,
        awb_on_create=True,
        idempotent_create=True,
        lookup_by_reference=True,
        cancel=True,
        tracking_poll=True,
        tracking_webhook=True,
        label=False,
    )
    default_transit_days: ClassVar[int]
    default_cost: ClassVar[Decimal]
    awb_prefix: ClassVar[str]
    tracking_url_template: ClassVar[str]

    @property
    def _creds(self) -> MockCredentials:
        assert isinstance(self.credentials, MockCredentials)
        return self.credentials

    # --- helpers ---------------------------------------------------------------------------

    def scenario_for(self, pincode: str) -> MockScenario:
        override = self._creds.scenarios.get(pincode)
        if override is not None:
            return override
        return DEFAULT_SCENARIOS.get(pincode, {}).get(self.code, MockScenario.OK)

    def _transit_days(self) -> int:
        return (
            self._creds.transit_days
            if self._creds.transit_days is not None
            else self.default_transit_days
        )

    def _cost(self) -> Decimal:
        return self._creds.cost if self._creds.cost is not None else self.default_cost

    def _raise_for_preshipment(self, scenario: MockScenario) -> None:
        if scenario == MockScenario.TIMEOUT:
            raise CarrierTransientError("Mock: carrier timed out", carrier_code=self.code)
        if scenario == MockScenario.SERVER_ERROR:
            raise CarrierTransientError("Mock: HTTP 500", carrier_code=self.code, status_code=500)
        if scenario == MockScenario.AUTH_FAILURE:
            raise CarrierAuthError("Mock: invalid API key", carrier_code=self.code, status_code=401)

    def _ids(self, idempotency_key: str) -> tuple[str, str]:
        digest = hashlib.sha256(f"{self.code}:{idempotency_key}".encode()).hexdigest()
        awb = f"{self.awb_prefix}{int(digest[:15], 16) % 10**10:010d}"
        return f"MOCK-{self.code.upper()}-{digest[:12]}", awb

    def _shipment_result(self, request_key: str) -> ShipmentResult:
        shipment_id, awb = self._ids(request_key)
        return ShipmentResult(
            carrier_shipment_id=shipment_id,
            awb=awb,
            tracking_url=self.tracking_url(awb),
            cost=self._cost(),
            currency="INR",
            edd=(utcnow() + timedelta(days=self._transit_days())).date(),
            raw={"mock": True, "shipment_id": shipment_id, "awb": awb, "reference": request_key},
        )

    # --- pre-shipment ----------------------------------------------------------------------

    def check_serviceability(self, request: ServiceabilityRequest) -> ServiceabilityResult:
        scenario = self.scenario_for(request.delivery_pincode)
        self._raise_for_preshipment(scenario)
        # INVALID_PINCODE passes serviceability and is rejected at create time.
        serviceable = scenario != MockScenario.NOT_SERVICEABLE
        cod = serviceable and scenario != MockScenario.COD_UNAVAILABLE
        return ServiceabilityResult(
            serviceable=serviceable,
            cod_available=cod,
            prepaid_available=serviceable,
            pickup_available=serviceable,
            reason=None
            if serviceable
            else f"Mock: pincode {request.delivery_pincode} not serviceable",
            raw={"mock": True, "scenario": scenario.value, "pincode": request.delivery_pincode},
        )

    def get_quote(self, request: ServiceabilityRequest) -> QuoteResult:
        self._raise_for_preshipment(self.scenario_for(request.delivery_pincode))
        return QuoteResult(
            cost=self._cost(), currency="INR", raw={"mock": True, "cost": str(self._cost())}
        )

    def get_edd(self, request: ServiceabilityRequest) -> EddResult:
        self._raise_for_preshipment(self.scenario_for(request.delivery_pincode))
        days = self._transit_days()
        return EddResult(
            edd=(utcnow() + timedelta(days=days)).date(),
            transit_days=days,
            raw={"mock": True, "transit_days": days},
        )

    def get_offer(self, request: ServiceabilityRequest) -> CarrierOffer:
        service = self.check_serviceability(request)
        if not service.serviceable:
            return CarrierOffer(
                carrier_code=self.code,
                serviceable=False,
                cod_available=False,
                prepaid_available=False,
                pickup_available=False,
                reason=service.reason,
                raw=service.raw,
            )
        edd = self.get_edd(request)
        return CarrierOffer(
            carrier_code=self.code,
            serviceable=True,
            cod_available=service.cod_available,
            prepaid_available=service.prepaid_available,
            pickup_available=service.pickup_available,
            cost=self._cost(),
            currency="INR",
            edd=edd.edd,
            transit_days=edd.transit_days,
            raw={**service.raw, "cost": str(self._cost()), "transit_days": edd.transit_days},
        )

    # --- shipment --------------------------------------------------------------------------

    def create_shipment(self, request: CreateShipmentRequest) -> ShipmentResult:
        scenario = self.scenario_for(request.delivery.pincode)
        self._raise_for_preshipment(scenario)
        if scenario in (MockScenario.NOT_SERVICEABLE, MockScenario.INVALID_PINCODE):
            raise CarrierValidationError(
                f"Mock: invalid delivery pincode {request.delivery.pincode}", carrier_code=self.code
            )
        if scenario == MockScenario.COD_UNAVAILABLE and request.payment_mode == PaymentMode.COD:
            raise CarrierValidationError("Mock: COD not available", carrier_code=self.code)
        if scenario in (MockScenario.CREATE_TIMEOUT, MockScenario.CREATE_TIMEOUT_LOST):
            raise CarrierAmbiguousError(
                "Mock: create timed out after sending", carrier_code=self.code
            )
        if scenario == MockScenario.CREATE_SERVER_ERROR:
            raise CarrierTransientError(
                "Mock: HTTP 503 on create", carrier_code=self.code, status_code=503
            )
        return self._shipment_result(request.idempotency_key)

    def find_shipment_by_reference(self, idempotency_key: str) -> ShipmentResult | None:
        # Mock state is derived from the key, so lookups work across processes. Tests control
        # "was it created?" through the CREATE_TIMEOUT / CREATE_TIMEOUT_LOST scenarios.
        if idempotency_key in self._creds.lost_references:
            return None
        return self._shipment_result(idempotency_key)

    def cancel_shipment(self, shipment: ShipmentRef) -> CancelResult:
        return CancelResult(cancelled=True, raw={"mock": True, "awb": shipment.awb})

    # --- tracking --------------------------------------------------------------------------

    def track_shipment(self, shipment: ShipmentRef) -> list[TrackingUpdate]:
        awb = shipment.awb or self._ids(shipment.idempotency_key)[1]
        raw_status = self._raw_code_for(TrackingStatus.AWB_CREATED)
        return [
            TrackingUpdate(
                awb=awb,
                raw_status=raw_status,
                status=self.normalize_status(raw_status),
                occurred_at=utcnow(),
                description="Mock: shipment manifested",
                raw={"mock": True},
            )
        ]

    def _raw_code_for(self, status: TrackingStatus) -> str:
        for raw, normalised in self.status_map.items():
            if normalised == status:
                return raw
        return status.value

    def sign_webhook(self, raw_body: bytes) -> str:
        key = self._creds.api_key.get_secret_value().encode()
        return hmac.new(key, raw_body, hashlib.sha256).hexdigest()

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        signature = lowered.get("x-mock-signature", "")
        return bool(signature) and hmac.compare_digest(self.sign_webhook(raw_body), signature)

    def process_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> list[TrackingUpdate]:
        body = json.loads(raw_body)
        events = body if isinstance(body, list) else [body]
        updates = []
        for event in events:
            raw_status = str(event["status"])
            occurred = parse_iso(event.get("timestamp")) or utcnow()
            updates.append(
                TrackingUpdate(
                    awb=str(event["awb"]),
                    raw_status=raw_status,
                    status=self.normalize_status(raw_status),
                    occurred_at=occurred,
                    description=event.get("description"),
                    location=event.get("location"),
                    raw=event,
                )
            )
        return updates

    def tracking_url(self, awb: str) -> str | None:
        return self.tracking_url_template.format(awb=awb)


@register_carrier(mock=True)
class MockEkartAdapter(_MockCarrierAdapter):
    code = "ekart"
    display_name = "Ekart (Mock)"
    default_transit_days = 3
    default_cost = Decimal("75.00")
    awb_prefix = "EKM"
    tracking_url_template = "https://mock.trekiva.local/track/ekart/{awb}"
    status_map: ClassVar[Mapping[str, TrackingStatus]] = {
        "MOCK_MANIFESTED": TrackingStatus.AWB_CREATED,
        "MOCK_PICKUP_SCHEDULED": TrackingStatus.PICKUP_SCHEDULED,
        "MOCK_PICKED": TrackingStatus.PICKED_UP,
        "MOCK_IN_TRANSIT": TrackingStatus.IN_TRANSIT,
        "MOCK_OUT_FOR_DELIVERY": TrackingStatus.OUT_FOR_DELIVERY,
        "MOCK_DELIVERED": TrackingStatus.DELIVERED,
        "MOCK_UNDELIVERED": TrackingStatus.DELIVERY_FAILED,
        "MOCK_RTO": TrackingStatus.RTO_INITIATED,
        "MOCK_RTO_IN_TRANSIT": TrackingStatus.RTO_IN_TRANSIT,
        "MOCK_RTO_DELIVERED": TrackingStatus.RTO_DELIVERED,
        "MOCK_CANCELLED": TrackingStatus.CANCELLED,
    }


@register_carrier(mock=True)
class MockXpressBeesAdapter(_MockCarrierAdapter):
    code = "xpressbees"
    display_name = "XpressBees (Mock)"
    default_transit_days = 2
    default_cost = Decimal("85.00")
    awb_prefix = "XBM"
    tracking_url_template = "https://mock.trekiva.local/track/xpressbees/{awb}"
    status_map: ClassVar[Mapping[str, TrackingStatus]] = {
        "MOCK_DRC": TrackingStatus.AWB_CREATED,
        "MOCK_PKS": TrackingStatus.PICKUP_SCHEDULED,
        "MOCK_PUD": TrackingStatus.PICKED_UP,
        "MOCK_IT": TrackingStatus.IN_TRANSIT,
        "OFD": TrackingStatus.OUT_FOR_DELIVERY,
        "MOCK_DLVD": TrackingStatus.DELIVERED,
        "MOCK_UD": TrackingStatus.DELIVERY_FAILED,
        "MOCK_RTO": TrackingStatus.RTO_INITIATED,
        "MOCK_RTO_IT": TrackingStatus.RTO_IN_TRANSIT,
        "MOCK_RTD": TrackingStatus.RTO_DELIVERED,
        "MOCK_CAN": TrackingStatus.CANCELLED,
    }
