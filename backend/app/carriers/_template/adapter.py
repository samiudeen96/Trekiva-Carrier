"""Template for a new carrier adapter. Copy this package to `app/carriers/<code>/`.

Steps:
1. Rename the class, set `code` (lowercase, stable forever) and `display_name`.
2. Define the credentials model from the carrier's docs. Use SecretStr for secrets.
3. Set `capabilities` truthfully: the engine relies on them.
4. Implement each method using `CarrierHttpClient` and translate EVERY failure into the
   error taxonomy in `app.carriers.errors` (the retry system depends on it).
5. Fill `status_map` with every raw tracking code the carrier documents.
6. Uncomment `@register_carrier()` and add `import app.carriers.<code>.adapter` to
   `app/carriers/__init__.py`.
7. Add contract tests under `tests/carriers/<code>/` using recorded sandbox responses.

This file is NOT imported, so the template itself is never registered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, SecretStr

from app.carriers.base import CarrierAdapter
from app.carriers.http import CarrierHttpClient
from app.carriers.types import (
    CarrierAccountConfig,
    CarrierCapabilities,
    CreateShipmentRequest,
    ServiceabilityRequest,
    ServiceabilityResult,
    ShipmentResult,
)
from app.core.config import get_settings
from app.core.enums import TrackingStatus

# from app.carriers.registry import register_carrier


class NewCarrierCredentials(BaseModel):
    base_url: str
    api_key: SecretStr


# @register_carrier()
class NewCarrierAdapter(CarrierAdapter):
    code = "new_carrier"
    display_name = "New Carrier"
    credentials_schema = NewCarrierCredentials
    capabilities = CarrierCapabilities(serviceability=True, awb_on_create=True)
    status_map: ClassVar[Mapping[str, TrackingStatus]] = {
        # "RAW_CODE": TrackingStatus.IN_TRANSIT,
    }

    def __init__(self, config: CarrierAccountConfig) -> None:
        super().__init__(config)
        creds = self.credentials
        assert isinstance(creds, NewCarrierCredentials)
        self._http = CarrierHttpClient(
            carrier_code=self.code,
            base_url=creds.base_url,
            timeout_seconds=get_settings().carrier_http_timeout_seconds,
        )

    def check_serviceability(self, request: ServiceabilityRequest) -> ServiceabilityResult:
        raise NotImplementedError

    def create_shipment(self, request: CreateShipmentRequest) -> ShipmentResult:
        # Non-idempotent unless the carrier dedups on our reference:
        # self._http.request("POST", "...", idempotent=False, json={...})
        raise NotImplementedError
