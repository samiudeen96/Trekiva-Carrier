"""The `CarrierAdapter` contract every courier integration implements.

The logistics engine only ever talks to this interface. To add a carrier:

1. Create `app/carriers/<code>/adapter.py` with a `CarrierAdapter` subclass
   (copy `app/carriers/_template/adapter.py`).
2. Decorate it with `@register_carrier` and import the package in `app/carriers/__init__.py`.
3. Enter credentials in the admin UI (the form is generated from `credentials_schema`).

No change to the allocation engine, pipeline, database or UI is required.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel

from app.carriers.errors import CarrierNotSupportedError
from app.carriers.types import (
    AwbResult,
    CancelResult,
    CarrierAccountConfig,
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
from app.core.enums import TrackingStatus

log = logging.getLogger(__name__)


class CarrierAdapter(ABC):
    #: Stable identifier stored in the database ("ekart", "xpressbees", ...).
    code: ClassVar[str]
    display_name: ClassVar[str]
    #: Pydantic model describing the credentials this carrier needs. Use `SecretStr` for
    #: anything secret: those fields are encrypted, masked in the UI and never returned.
    credentials_schema: ClassVar[type[BaseModel]]
    capabilities: ClassVar[CarrierCapabilities]
    #: Raw carrier status code (upper-cased) -> normalised status.
    status_map: ClassVar[Mapping[str, TrackingStatus]] = {}

    def __init__(self, config: CarrierAccountConfig) -> None:
        if not isinstance(config.credentials, self.credentials_schema):
            raise TypeError(
                f"{type(self).__name__} expects credentials of type "
                f"{self.credentials_schema.__name__}"
            )
        self.config = config

    @property
    def credentials(self) -> BaseModel:
        return self.config.credentials

    # --- Pre-shipment ----------------------------------------------------------------------

    @abstractmethod
    def check_serviceability(self, request: ServiceabilityRequest) -> ServiceabilityResult: ...

    def get_quote(self, request: ServiceabilityRequest) -> QuoteResult:
        raise CarrierNotSupportedError("Rate quotes not supported", carrier_code=self.code)

    def get_edd(self, request: ServiceabilityRequest) -> EddResult:
        raise CarrierNotSupportedError("EDD not supported", carrier_code=self.code)

    def get_offer(self, request: ServiceabilityRequest) -> CarrierOffer:
        """Serviceability + rate + EDD in one normalised object.

        The default composes the three calls according to `capabilities`. Carriers whose API
        returns everything in one response should set `combined_offer=True` and override this.
        """
        service = self.check_serviceability(request)
        if not service.serviceable:
            return CarrierOffer(
                carrier_code=self.code,
                serviceable=False,
                cod_available=service.cod_available,
                prepaid_available=service.prepaid_available,
                pickup_available=service.pickup_available,
                reason=service.reason,
                raw={"serviceability": service.raw},
            )
        quote = self.get_quote(request) if self.capabilities.quote else None
        edd = self.get_edd(request) if self.capabilities.edd else None
        return CarrierOffer(
            carrier_code=self.code,
            serviceable=True,
            cod_available=service.cod_available,
            prepaid_available=service.prepaid_available,
            pickup_available=service.pickup_available,
            cost=quote.cost if quote else None,
            currency=quote.currency if quote else request.currency,
            edd=edd.edd if edd else None,
            transit_days=edd.transit_days if edd else None,
            raw={
                "serviceability": service.raw,
                "quote": quote.raw if quote else None,
                "edd": edd.raw if edd else None,
            },
        )

    # --- Shipment --------------------------------------------------------------------------

    @abstractmethod
    def create_shipment(self, request: CreateShipmentRequest) -> ShipmentResult: ...

    def generate_awb(self, shipment: ShipmentRef) -> AwbResult:
        """Only needed for carriers that allocate the AWB in a separate call."""
        if shipment.awb:
            return AwbResult(awb=shipment.awb, tracking_url=self.tracking_url(shipment.awb))
        raise CarrierNotSupportedError(
            "Separate AWB generation not supported", carrier_code=self.code
        )

    def find_shipment_by_reference(self, idempotency_key: str) -> ShipmentResult | None:
        """Return the shipment created with this reference, or None if it does not exist.

        Used to resolve CarrierAmbiguousError. Raise CarrierNotSupportedError if the carrier
        cannot look shipments up by our reference (staff then resolve it manually)."""
        raise CarrierNotSupportedError("Lookup by reference not supported", carrier_code=self.code)

    def cancel_shipment(self, shipment: ShipmentRef) -> CancelResult:
        raise CarrierNotSupportedError("Cancellation not supported", carrier_code=self.code)

    # --- Tracking --------------------------------------------------------------------------

    def track_shipment(self, shipment: ShipmentRef) -> list[TrackingUpdate]:
        raise CarrierNotSupportedError("Tracking poll not supported", carrier_code=self.code)

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        """Authenticate an inbound carrier webhook. Default: reject everything."""
        return False

    def process_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> list[TrackingUpdate]:
        raise CarrierNotSupportedError("Tracking webhooks not supported", carrier_code=self.code)

    def tracking_url(self, awb: str) -> str | None:
        return None

    def normalize_status(self, raw_status: str) -> TrackingStatus:
        key = raw_status.strip().upper()
        status = self.status_map.get(key)
        if status is None:
            log.warning(
                "Unmapped carrier status; treating as EXCEPTION",
                extra={"carrier": self.code, "raw_status": raw_status},
            )
            return TrackingStatus.EXCEPTION
        return status
