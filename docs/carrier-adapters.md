# Carrier adapters

Every courier integration implements `app.carriers.base.CarrierAdapter`. The logistics engine only talks to that interface and to the carrier-neutral types in `app/carriers/types.py`. Carrier-specific code lives entirely in its own package.

## Adding carrier #3

1. Copy `backend/app/carriers/_template/` to `backend/app/carriers/<code>/`.
2. Implement the adapter: credentials model, capabilities, methods, `status_map`.
3. Register it: decorate the class with `@register_carrier()` and add the import to `app/carriers/__init__.py`.
4. Add a mock adapter (`@register_carrier(mock=True)`, same `code`) if you want to test without credentials.
5. In the admin UI, open **Carriers → <carrier>**, enter credentials (the form is generated from your credentials model), map warehouses, and enable it.

The allocation engine, pipeline, database and UI need **no changes**. `tests/unit/test_allocation.py::test_engine_has_no_carrier_specific_logic` and `tests/unit/test_carriers.py::test_registering_a_new_carrier_needs_no_engine_change` guard this.

## The interface

| Method | Required | Purpose |
|---|---|---|
| `check_serviceability(req) -> ServiceabilityResult` | yes | serviceable, COD/prepaid/pickup availability |
| `get_quote(req) -> QuoteResult` | if `capabilities.quote` | shipping cost |
| `get_edd(req) -> EddResult` | if `capabilities.edd` | EDD date and/or transit days |
| `get_offer(req) -> CarrierOffer` | default provided | Composes the three above. Override when the carrier returns everything in one call (`combined_offer=True`). |
| `create_shipment(req) -> ShipmentResult` | yes | **Must send `req.idempotency_key` as the carrier's client reference / order id** |
| `generate_awb(ref) -> AwbResult` | if `awb_on_create=False` | Separate AWB allocation |
| `find_shipment_by_reference(key) -> ShipmentResult \| None` | strongly recommended | Resolves timeouts: "did my create succeed?" |
| `cancel_shipment(ref) -> CancelResult` | if `cancel` | |
| `track_shipment(ref) -> list[TrackingUpdate]` | if `tracking_poll` | |
| `verify_webhook(headers, body) -> bool` | if `tracking_webhook` | Authenticate the inbound webhook (default: reject) |
| `process_webhook(headers, body) -> list[TrackingUpdate]` | if `tracking_webhook` | |
| `normalize_status(raw) -> TrackingStatus` | default provided | Looks up `status_map` (upper-cased). Unknown codes become `EXCEPTION` and are logged. |

Declare `capabilities` truthfully. The engine uses them, for example to decide whether a timed-out create can be reconciled automatically or must go to staff.

## Error taxonomy (mandatory)

Translate **every** failure into one of these (`app/carriers/errors.py`):

| Raise | When | Engine reaction |
|---|---|---|
| `CarrierTransientError` | network failure before sending, 429, 503, 5xx on idempotent calls | retry with backoff |
| `CarrierValidationError` | invalid pincode/weight/payload (4xx) | no retry; next ranked carrier |
| `CarrierAuthError` | 401/403, invalid credentials | no retry; admin alert |
| `CarrierAmbiguousError` | request sent but outcome unknown (read timeout, dropped connection, 500/502/504 on create) | reconcile with the **same** carrier; never fall back |
| `CarrierNotSupportedError` | capability not offered | no retry |

`CarrierHttpClient` (`app/carriers/http.py`) does this mapping for you. Pass `idempotent=False` for create calls, unless the carrier documents server-side dedup on your reference.

## Credentials

Define a Pydantic model and use `SecretStr` for every secret. Secrets are:
- encrypted at rest (Fernet),
- shown only as masked hints (`••••1234`),
- never returned by the API,
- kept on update when the field is left blank.

Optional bootstrap: an `<CODE>_CREDENTIALS_JSON` environment variable is used if the account has no stored credentials.

## Environments and mocks

Each carrier account has an environment:
- `MOCK`: uses the mock adapter registered under the same code (refused when `APP_ENV=production`).
- `SANDBOX` / `PRODUCTION`: use the real adapter.

Because the code is the same, switching from mock to real keeps the carrier's settings, warehouse mappings and history.

Mock behaviour (`app/carriers/mock/`):

| Carrier | Transit | Cost | AWB prefix |
|---|---|---|---|
| `MockEkartAdapter` | 3 days | ₹75 | `EKM` |
| `MockXpressBeesAdapter` | 2 days | ₹85 | `XBM` |

AWBs are deterministic per idempotency key, so retries return the same AWB, and `find_shipment_by_reference` works across processes.

Scenario pincodes, usable in a development store:

| Pincode | Behaviour |
|---|---|
| `999001` | Only XpressBees serviceable |
| `999002` | Only Ekart serviceable |
| `999003` | Neither serviceable |
| `999004` | Both serviceable, COD unavailable |
| `999010` / `999011` | Ekart serviceability timeout / HTTP 500 |
| `999020` | XpressBees create times out *after* creating (reconciliation finds it) |
| `999021` | XpressBees create returns 503 |
| `999030` | Both pass serviceability, then reject the pincode at create time (fallback, then `FAILED`) |

Mock account credentials can override all of this:
- `scenarios`: pincode → scenario
- `transit_days`, `cost`
- `lost_references`: idempotency keys that reconciliation reports as not created

Mock tracking: mock carriers accept signed webhooks. The header is `X-Mock-Signature` = hex HMAC-SHA256 of the body with the mock `api_key`, and the body is `{"awb", "status", "timestamp", "location"}` using the mock status codes in `app/carriers/mock/adapter.py`. In development, the order page's **Simulate tracking** action does the same without crafting requests.

## Shipment-bound operations

Reconciliation, cancellation, tracking polls and staff resolution always use the **account that created the shipment** (`shipments.carrier_account_id`), not the carrier's currently active account. Switching an account from MOCK to PRODUCTION therefore never sends a mock shipment's cancellation to the real carrier.

## Ekart and XpressBees status

Both real adapters are **placeholders** (`app/carriers/pending.py`). They advertise no capabilities, so the engine never selects them, and every method raises `CarrierNotImplementedError("… awaiting official API documentation")`. Each adapter file has an implementation checklist.

Per project rules: **no endpoint URLs, auth mechanisms, request fields, response structures or status codes may be added until the official documentation is supplied.** The `OFD → OUT_FOR_DELIVERY` mapping in the XpressBees *mock* is an illustrative example only.
