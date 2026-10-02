"""HTTP helper for real carrier adapters: maps transport failures onto the error taxonomy.

The key distinction is *whether the carrier may have applied the request*:

- connection refused / connect timeout -> request never reached the carrier -> transient
- read timeout / dropped connection after sending -> outcome unknown:
    - idempotent requests (GET, lookups, or carriers that dedup on our reference) -> transient
    - non-idempotent requests (create shipment) -> CarrierAmbiguousError (reconcile first)
"""

from __future__ import annotations

from typing import Any

import httpx

from app.carriers.errors import (
    CarrierAmbiguousError,
    CarrierAuthError,
    CarrierError,
    CarrierTransientError,
    CarrierValidationError,
)


class CarrierHttpClient:
    def __init__(
        self,
        *,
        carrier_code: str,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.carrier_code = carrier_code
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            transport=transport,
            headers=default_headers,
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        idempotent: bool,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            resp = self._client.request(method, path, json=json, params=params, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise CarrierTransientError(
                f"Could not reach carrier: {exc}", carrier_code=self.carrier_code
            ) from exc
        except httpx.TransportError as exc:
            cls: type[CarrierError] = CarrierTransientError if idempotent else CarrierAmbiguousError
            raise cls(
                f"Carrier request outcome unknown: {type(exc).__name__}",
                carrier_code=self.carrier_code,
            ) from exc
        self.raise_for_status(resp, idempotent=idempotent)
        return resp

    def raise_for_status(self, resp: httpx.Response, *, idempotent: bool) -> None:
        code = resp.status_code
        if code < 400:
            return
        body = _safe_body(resp)
        kwargs: dict[str, Any] = {
            "carrier_code": self.carrier_code,
            "status_code": code,
            "raw": body,
        }
        if code in (401, 403):
            raise CarrierAuthError(f"Carrier rejected credentials (HTTP {code})", **kwargs)
        if code in (429, 503):
            raise CarrierTransientError(f"Carrier busy (HTTP {code})", **kwargs)
        if code >= 500:
            # A 500/502/504 on a non-idempotent call may still have created the shipment.
            cls: type[CarrierError] = CarrierTransientError if idempotent else CarrierAmbiguousError
            raise cls(f"Carrier server error (HTTP {code})", **kwargs)
        raise CarrierValidationError(f"Carrier rejected request (HTTP {code})", **kwargs)


def _safe_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]
