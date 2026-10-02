"""Carrier error taxonomy.

Adapters MUST translate every failure into one of these classes. The retry system decides what
to do purely from the class, so adapter authors never write retry logic themselves:

| Class                      | Meaning                                         | Engine behaviour                  |
|----------------------------|-------------------------------------------------|-----------------------------------|
| CarrierTransientError      | network error, 5xx, 429 - request not applied   | retry with exponential backoff    |
| CarrierValidationError     | bad pincode / weight / payload                  | no retry; try next ranked carrier |
| CarrierAuthError           | credentials rejected                            | no retry; alert admin             |
| CarrierAmbiguousError      | request sent, outcome unknown (e.g. timeout)    | reconcile with SAME carrier first |
| CarrierNotSupportedError   | adapter lacks this capability                   | no retry                          |
"""

from __future__ import annotations

from typing import Any


class CarrierError(Exception):
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        carrier_code: str | None = None,
        status_code: int | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.carrier_code = carrier_code
        self.status_code = status_code
        self.raw = raw

    @property
    def error_class(self) -> str:
        return type(self).__name__


class CarrierTransientError(CarrierError):
    retryable = True


class CarrierValidationError(CarrierError):
    pass


class CarrierAuthError(CarrierError):
    pass


class CarrierAmbiguousError(CarrierError):
    """The request may have been applied by the carrier. Never fall back to another carrier
    until `find_shipment_by_reference` (or staff) has resolved the outcome."""


class CarrierNotSupportedError(CarrierError):
    pass


class CarrierNotImplementedError(CarrierNotSupportedError):
    """Adapter placeholder awaiting official carrier API documentation."""
