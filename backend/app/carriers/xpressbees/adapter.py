"""XpressBees adapter: PLACEHOLDER (Phase 4).

Not implemented until the official XpressBees API documentation and credentials are supplied.
Do not add endpoints, auth flows, field names or status codes from guesswork.

Implementation checklist (from official docs):
  [ ] XpressBeesCredentials model (secrets as SecretStr) replacing PendingCredentials
  [ ] authentication (token lifetime / refresh)
  [ ] serviceability, rate, EDD (or one combined call -> override get_offer)
  [ ] shipment creation, passing request.idempotency_key as our reference
  [ ] AWB retrieval (if not returned on create)
  [ ] lookup by reference (for timeout reconciliation)
  [ ] cancellation
  [ ] tracking poll + status_map (raw code -> TrackingStatus)
  [ ] tracking webhook: verify_webhook + process_webhook
  [ ] capabilities flags set to match what the API actually supports
  [ ] sandbox contract tests
"""

from __future__ import annotations

from app.carriers.pending import PendingCarrierAdapter
from app.carriers.registry import register_carrier


@register_carrier()
class XpressBeesAdapter(PendingCarrierAdapter):
    code = "xpressbees"
    display_name = "XpressBees"
