"""Shared scaffolding for carrier adapters whose official API documentation is not yet available.

These adapters deliberately contain NO endpoint URLs, auth mechanisms, request fields or status
codes. Every operation raises `CarrierNotImplementedError` until the real integration is written
from the official documentation.
"""

from __future__ import annotations

from typing import NoReturn

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.carriers.base import CarrierAdapter
from app.carriers.errors import CarrierNotImplementedError
from app.carriers.types import (
    AwbResult,
    CancelResult,
    CarrierCapabilities,
    CreateShipmentRequest,
    EddResult,
    QuoteResult,
    ServiceabilityRequest,
    ServiceabilityResult,
    ShipmentRef,
    ShipmentResult,
    TrackingUpdate,
)


class PendingCredentials(BaseModel):
    """Placeholder credential form. Replace with the carrier's real fields (marking secrets as
    SecretStr) once its API documentation is supplied."""

    model_config = ConfigDict(extra="forbid")

    api_base_url: str | None = Field(default=None, description="Provided by the carrier")
    secret_values: dict[str, SecretStr] = Field(
        default_factory=dict,
        description="Credential values issued by the carrier (field names TBD from official docs)",
    )


#: Placeholder adapters advertise no capabilities, so the engine never selects them.
PENDING_CAPABILITIES = CarrierCapabilities(
    serviceability=False,
    quote=False,
    edd=False,
    awb_on_create=False,
    idempotent_create=False,
    lookup_by_reference=False,
)


class PendingCarrierAdapter(CarrierAdapter):
    credentials_schema = PendingCredentials
    capabilities = PENDING_CAPABILITIES

    def _pending(self, operation: str) -> NoReturn:
        raise CarrierNotImplementedError(
            f"{self.display_name} {operation} is not implemented: awaiting official API "
            "documentation and credentials",
            carrier_code=self.code,
        )

    def authenticate(self) -> None:
        self._pending("authentication")

    def check_serviceability(self, request: ServiceabilityRequest) -> ServiceabilityResult:
        self._pending("serviceability")

    def get_quote(self, request: ServiceabilityRequest) -> QuoteResult:
        self._pending("rate quote")

    def get_edd(self, request: ServiceabilityRequest) -> EddResult:
        self._pending("EDD")

    def create_shipment(self, request: CreateShipmentRequest) -> ShipmentResult:
        self._pending("shipment creation")

    def generate_awb(self, shipment: ShipmentRef) -> AwbResult:
        self._pending("AWB generation")

    def find_shipment_by_reference(self, idempotency_key: str) -> ShipmentResult | None:
        self._pending("shipment lookup")

    def cancel_shipment(self, shipment: ShipmentRef) -> CancelResult:
        self._pending("cancellation")

    def track_shipment(self, shipment: ShipmentRef) -> list[TrackingUpdate]:
        self._pending("tracking")
