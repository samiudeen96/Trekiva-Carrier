# API reference

The live OpenAPI schema is served at `/api/docs` and `/api/openapi.json` (disabled when `APP_ENV=production`).

## Authentication

| Surface | Auth |
|---|---|
| `/api/admin/*` | `Authorization: Bearer <App Bridge session token>`. App Bridge adds it to `fetch` calls from the embedded UI. Verified for HS256 signature (client secret), `exp`/`nbf`, `aud` = client id, and matching shop in `iss`/`dest`. The shop must be in `SHOPIFY_SHOP_ALLOWLIST` (if set). On first use the token is exchanged for an offline access token. |
| `/webhooks/shopify` | `X-Shopify-Hmac-Sha256`: base64 HMAC-SHA256 of the raw body with the client secret |
| `/healthz`, `/readyz` | none |

Errors return `{"error": "<code>", "message": "<text>"}` with an appropriate status (401, 403, 404, 409, 422, 502). Request validation errors use FastAPI's `422 {"detail": [...]}`.

## Admin API

### Session and dashboard

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/session` | `{shop_domain, user_id, automation_enabled, app_env, mock_carriers_enabled, api_version}` |
| GET | `/api/admin/dashboard` | Counts: `orders_today`, `shipments_created_today`, `manual_review`, `awaiting_allocation`, `failed`, `delivered` (30 d), `rto` (30 d); `carrier_breakdown`; `recent_activity` (last 15 audit entries) |

### Settings

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/settings` | `{automation_enabled, settings: ShopSettings}` |
| PUT | `/api/admin/settings` | Body `{automation_enabled?, settings?}` |

`ShopSettings` fields:

| Field | Default |
|---|---|
| `timezone` | `"Asia/Kolkata"` |
| `settle_window_seconds` | `300` |
| `wait_for_risk_analysis` | `true` |
| `risk_wait_max_seconds` | `900` |
| `review_tags` | `["DUPLICATE-REVIEW","RISK-REVIEW"]` |
| `block_on_high_risk` | `true` |
| `cod_gateway_names` | `["Cash on Delivery (COD)","Cash on Delivery","COD"]` |
| `fulfill_on` | `"AWB_CREATED"` (or `"PICKED_UP"`) |
| `notify_customer` | `false` |
| `shipped_tag` | `null` |

### Carriers

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/carriers` | All registered carriers (see `CarrierOut` below) |
| GET | `/api/admin/carriers/{code}` | One carrier |
| PUT | `/api/admin/carriers/{code}/settings` | Partial update. Body (all optional): `{enabled, cod_enabled, prepaid_enabled, priority, max_shipping_cost, min_weight_g, max_weight_g, performance_score}`. Enabling requires an active account (otherwise 422). |
| PUT | `/api/admin/carriers/{code}/account` | Create or update credentials. Body `{environment: MOCK\|SANDBOX\|PRODUCTION, label="default", credentials: {...}, is_active=true, make_active=true}`. Credentials are validated against the adapter's schema and encrypted. **Blank secret fields keep the stored value.** `MOCK` is refused in production. |
| PUT | `/api/admin/carriers/{code}/warehouses` | Body `[{warehouse_id, carrier_warehouse_ref?, enabled}]`. Applies to the active account. |

`CarrierOut` contains:
- `code`, `display_name`
- `has_real_adapter`, `has_mock_adapter`
- `implementation_status` (`ready` or `awaiting_api_docs`)
- `capabilities`
- `credentials_schema` and `mock_credentials_schema` (JSON Schema; drives the UI form)
- `settings`
- `accounts[]`: `{id, environment, label, credentials_hint (masked), is_active, webhook_path}`
- `warehouse_mappings[]`

### Warehouses

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/warehouses` | List |
| POST | `/api/admin/warehouses` | Create. Body `{code, name, phone, address1, address2?, city, state, pincode (6 digits), country="IN", shopify_location_id? (gid://shopify/Location/…), contact_name?, email?, default_package{length_cm, breadth_cm, height_cm, min_weight_g}, is_active}`. Returns 409 on a duplicate code or location. |
| PUT | `/api/admin/warehouses/{id}` | Update (same body) |

### Allocation rules

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/allocation-rules` | List, ordered by priority |
| POST | `/api/admin/allocation-rules` | Body `{name, priority=100, is_active=true, strategy=FASTEST, conditions={}, params={}}`. Returns 422 on an invalid DSL, unknown carriers, or CUSTOM without `carrier_order`. |
| PUT | `/api/admin/allocation-rules/{id}` | Update |
| DELETE | `/api/admin/allocation-rules/{id}` | Returns 204 |

Condition DSL:

```json
{"all": [
  {"field": "payment_mode", "op": "eq", "value": "COD"},
  {"any": [
    {"field": "destination_pincode", "op": "starts_with", "value": "56"},
    {"field": "order_value", "op": "gte", "value": 2000}
  ]}
]}
```

| Field type | Fields | Operators |
|---|---|---|
| String | `payment_mode`, `destination_pincode`, `destination_state`, `destination_country` | `eq`, `ne`, `in`, `not_in`, `starts_with` (case-insensitive) |
| Number | `order_value`, `cod_amount`, `weight_g`, `warehouse_id` | `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in` |
| List | `skus`, `tags` | `contains`, `contains_any`, `not_contains` |

Combinators: `all`, `any`, `not`. Nesting is limited to 10 levels.

### Manual review

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/manual-review` | Fulfillment orders in `MANUAL_REVIEW`, each with `order_name`, `tags`, `hold_reasons`, `reason`, `detail`, customer, phone, shipping address, line items (name, SKU, quantity), `shopify_status`, `is_shopify_hold`, `can_override` |
| POST | `/api/admin/manual-review/{fulfillment_order_id}/approve` | Staff approval for **non-hold** reasons (`REVIEW_TAG_WITHOUT_HOLD`, `HIGH_RISK_WITHOUT_HOLD`, `PAYMENT_MODE_UNRESOLVED`). Returns 409 for a Shopify hold, which must be released in Shopify. Queues a live re-check. |

### Orders, shipments, tracking, logs

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/orders?status=&carrier=&q=&limit=&offset=` | One row per fulfillment order (fields below). `q` searches order name, phone, customer, AWB and pincode. |
| GET | `/api/admin/orders/{order_id}` | Full detail (contents below) |
| GET | `/api/admin/shipments?status=&carrier=&q=&limit=&offset=` | Shipment rows with `can_cancel`, `can_resolve`, `can_retry_shopify_sync` |
| GET | `/api/admin/tracking?shipment_id=&limit=` | Tracking events: raw and normalised status, source, applied, pushed to Shopify |
| GET | `/api/admin/logs?order_id=&level=&step=&q=&limit=&offset=` | Audit log (chronological when filtered by order) |

Order list row fields: order, customer, phone, destination, payment type, Shopify fulfillment status, review status (`logistics_status` + reason/detail), selected carrier, latest shipment (AWB, status, EDD).

Order detail contents:
- order, customer, address, payment, tags
- per fulfillment order: status, holds, line items, allocation ranking, shipments, latest allocation run (quotes + **raw carrier requests/responses**), tracking timeline
- the order's full audit trail

### Staff overrides

| Method | Path | Description |
|---|---|---|
| POST | `/api/admin/fulfillment-orders/{id}/preview-allocation` | Re-run serviceability and allocation without changing state. Stores the carrier responses. Returns 422 with the exact reason for plan errors (e.g. warehouse not mapped). |
| POST | `/api/admin/fulfillment-orders/{id}/allocate` | Body `{carrier_code?}`. Re-run allocation, retry a failed order, or (with `carrier_code`) manually select a carrier. Returns 409 while an active shipment exists: cancel it first. Holds still apply. |
| POST | `/api/admin/orders/{id}/automation` | Body `{disabled}`. Disable or enable automation for one order. |
| POST | `/api/admin/shipments/{id}/cancel` | Body `{reallocate=false, reason}`. Cancels with the carrier first, then the Shopify fulfillment. Only before pickup (otherwise 409). |
| POST | `/api/admin/shipments/{id}/resolve` | Body `{created, awb?, carrier_shipment_id?}`. Staff decision for a `CREATION_UNKNOWN` shipment after checking the carrier panel. |
| POST | `/api/admin/shipments/{id}/sync-shopify` | Retry the Shopify fulfillment sync now |
| POST | `/api/admin/shipments/{id}/simulate-tracking` | Body `{status, location?}`. **Development only, MOCK carriers only.** Feeds a tracking status (404 in production). |

## Webhooks

| Method | Path | Notes |
|---|---|---|
| POST | `/webhooks/shopify` | All Shopify topics (`X-Shopify-Topic` selects the handler). Responds `{"status": "accepted"\|"duplicate", "id"}` within milliseconds; processing is asynchronous. |
| POST | `/webhooks/carriers/{carrier_code}/{webhook_token}` | Carrier tracking webhooks. The path is shown per account in the admin UI. Status codes: 404 unknown token, 401 failed `verify_webhook`, 400 unparseable (stored as FAILED), 200 `{"status": "accepted"\|"duplicate", "updates": n}`. |

Handled Shopify topics:
- `orders/create`, `orders/updated`, `orders/cancelled`
- `fulfillments/create`, `fulfillments/update`
- `fulfillment_orders/placed_on_hold`, `hold_released`, `cancelled`, `split`, `merged`, `moved`, `order_routing_complete`, `rescheduled`, `scheduled_fulfillment_order_ready`
- `app/uninstalled`
- compliance topics

## Health

- `GET /healthz`: liveness, `{"status":"ok"}`.
- `GET /readyz`: checks the database and Redis. Returns 503 with details if either fails.
