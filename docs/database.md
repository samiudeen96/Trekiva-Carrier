# Database schema

PostgreSQL 16. The schema is managed by Alembic (`backend/migrations/versions/`: `0001_initial_schema`, `0002_allocation_and_tracking_fields`, `0003_alerts`). Conventions:
- Primary keys are `bigint` identity columns.
- Shopify IDs are stored as GID text.
- Money is `numeric(12,2)` with a currency code.
- Timestamps are `timestamptz` in UTC.
- Enums are stored as `varchar(40)` (adding a value needs no migration).
- Secrets are stored as encrypted `bytea`.

```bash
docker compose run --rm api alembic upgrade head                        # apply
docker compose run --rm api alembic revision --autogenerate -m "..."    # after changing models
```

## Tables

### `shops`
An installed store. `shop_domain` is unique. Columns:
- `access_token_enc` and `refresh_token_enc` (Fernet)
- `access_token_expires_at`, `refresh_token_expires_at`
- `scopes`, `installed_at`, `uninstalled_at`
- `automation_enabled` (master switch, default **off**)
- `settings` (JSONB validated by `ShopSettings`: settle window, risk wait, review tags, COD gateway names, Shopify sync options)

### `warehouses`
Pickup locations, mapped 1:1 to a Shopify Location. Columns:
- `code`, `name`, `contact_name`, `phone`, `email`
- `address1`, `address2`, `city`, `state`, `pincode`, `country`
- `shopify_location_id`
- `default_package` (JSONB of dimensions)
- `is_active`

Unique on `(shop_id, code)` and `(shop_id, shopify_location_id)`.

### `carrier_accounts`
Credentials for a carrier. Columns:
- `carrier_code`, `label`
- `environment` (`MOCK`/`SANDBOX`/`PRODUCTION`)
- `credentials_enc`, `credentials_hint` (masked, safe to display)
- `webhook_token` (unique random path token for inbound carrier webhooks)
- `is_active`

Unique on `(shop_id, carrier_code, environment, label)`.

### `carrier_settings`
Business configuration used by allocation. Unique on `(shop_id, carrier_code)`. Columns:
- `enabled`, `cod_enabled`, `prepaid_enabled`
- `priority` (lower = preferred)
- `max_shipping_cost`, `min_weight_g`, `max_weight_g`
- `performance_score` (0–100)
- `active_account_id` (FK `carrier_accounts`)

### `carrier_warehouse_mappings`
Which warehouses a carrier account picks up from. Columns: `carrier_account_id`, `warehouse_id`, `carrier_warehouse_ref` (the carrier's own pickup ID), `enabled`. Unique on `(carrier_account_id, warehouse_id)`.

### `allocation_rules`
`name`, `priority` (lowest number evaluated first), `is_active`, `conditions` (JSON DSL), `strategy` (`FASTEST`/`CHEAPEST`/`BALANCED`/`PRIORITY`/`CUSTOM`), `params` (validated by `StrategyParams`: `max_cost`, `max_edd_days`, `min_performance_score`, `fallback_carrier`, `carrier_order`, `only_carriers`, `weights`).

### `shopify_orders`
Snapshot refreshed from GraphQL on every evaluation. Unique on `(shop_id, shopify_order_id)`. Columns:
- `name`, `customer_name`, `phone`, `email`, `shipping_address` (JSONB)
- `payment_mode` (`COD`/`PREPAID`/`UNKNOWN`), `payment_gateways[]`
- `currency`, `total_price`, `outstanding_amount`
- `tags[]`, `financial_status`, `fulfillment_status`
- `risk_level`, `risk_recommendation`
- `shopify_created_at`, `cancelled_at`
- `automation_disabled`, `raw`, `last_synced_at`

### `shopify_fulfillment_orders`
The unit of work. `shopify_fulfillment_order_id` is unique. Columns:
- Shopify-owned (refreshed on every sync): `shopify_status`, `request_status`, `delivery_method`, `shopify_location_id` → `warehouse_id`, `hold_reasons` (JSONB), `line_items` (JSONB with SKU, quantity, unit weight), `weight_g`
- Trekiva-owned (never overwritten by sync): `logistics_status`, `status_reason` (reason code), `status_detail` (human text), `next_check_at`, `hold_last_seen_at`, `hold_released_at`, `review_override_at`/`_by`, `last_evaluated_at`
- Allocation (migration 0002):
  - `selected_carrier_code`
  - `forced_carrier_code` (staff choice)
  - `selected_by` (`system` / `staff:<id>`)
  - `allocation_run_id`
  - `allocation_ranking` (ranked eligible carriers with cost/EDD/score and a `failed` flag, used for fallback)

Index on `(logistics_status, next_check_at)`.

### `carrier_serviceability_checks`
One row per carrier call in an allocation run. Columns: `run_id`, `carrier_code`, `request`, `serviceable`, `cod_available`, `prepaid_available`, `pickup_available`, `response`, `error_class`, `error_message`, `duration_ms`.

### `carrier_quotes`
The engine's verdict per carrier per run. Columns: `cost`, `currency`, `transit_days`, `edd`, `score`, `rank`, `eligible`, `rejection_reason`, `rejection_detail`, `selected`, `strategy`, `rule_id`.

### `shipments`
Columns:
- Identity: `id`, `shop_id`, `order_id`, `fulfillment_order_id`, `shopify_order_id`, `shopify_order_name`, `shopify_fulfillment_order_id`
- Carrier: `carrier_code`, `carrier_account_id`, `warehouse_id`, `idempotency_key` (unique), `attempt`, `carrier_shipment_id`, `awb`, `tracking_url`, `label_url`
- Money and parcel: `shipping_cost`, `currency`, `estimated_delivery_date`, `cod_amount`, `declared_value`, `weight_g`, `dimensions`
- State: `status`, `raw_create_response`, `last_error`, `reconcile_attempts`
- Shopify sync: `shopify_fulfillment_id`, `shopify_sync_status`, `shopify_sync_error`
- Lifecycle: `last_tracking_at`, `next_poll_at`, `delivered_at`, `cancelled_at`, `cancel_reason`, `created_by`, `created_at`, `updated_at`

Constraints:
- **`uq_shipments_active_fulfillment_order`**: unique on `shopify_fulfillment_order_id` `WHERE status NOT IN ('CANCELLED','CREATE_FAILED')`. At most one live shipment (including in-flight `CREATING`/`CREATION_UNKNOWN`) per fulfillment order. Cancelled history is kept, so a re-ship after a safe cancellation is possible.
- `UNIQUE(carrier_code, awb)`
- `UNIQUE(idempotency_key)`

### `tracking_events`
Columns: `shipment_id`, `carrier_code`, `awb`, `raw_status`, `normalized_status`, `description`, `location`, `occurred_at`, `source` (`WEBHOOK`/`POLL`/`MANUAL`/`SYSTEM`), `dedup_hash`, `raw`, `applied` (false for stale or out-of-order events, which are kept for audit only), `shopify_pushed_at`. Unique on `(shipment_id, dedup_hash)`.

### `webhook_events`
Columns:
- Identity: `source` (`shopify` or a carrier code), `topic`, `external_event_id`, `shop_domain`
- Content: `payload_hash`, `payload`, `headers` (allow-listed; never auth headers)
- Processing: `received_at`, `processing_started_at`, `processed_at`, `processing_status` (`RECEIVED`/`PROCESSING`/`PROCESSED`/`FAILED`/`IGNORED`), `attempts`, `error_message`

**`UNIQUE(source, external_event_id)`** prevents processing a webhook twice. If the sender gives no event id, `sha256:<payload hash>` is used instead.

### `automation_logs`
Append-only audit trail. Columns: `shop_id`, `order_id`, `fulfillment_order_id`, `shipment_id`, `run_id`, `step`, `level`, `message`, `data` (JSONB), `actor` (`system` or `staff:<user id>`), `created_at`. Indexed by `(order_id, created_at)` and `(shop_id, created_at)`.

### `alerts`
Operator alert outbox (migration `0003`). Rows are written in the same transaction as the failure they describe and delivered by `alerts.dispatch`.
- **What happened:** `shop_id` (null for system-wide alerts), `kind`, `severity` (`WARNING`/`ERROR`/`CRITICAL`), `fingerprint` (the grouping and rate-limit key), `title`, `detail`, `order_id`, `fulfillment_order_id`, `shipment_id` (`SET NULL` on delete), `data` (JSONB; health checks store the ids they reported).
- **Delivery:** `status` (`PENDING`/`SENDING`/`SENT`/`FAILED`/`SKIPPED`), `attempts`, `delivered_channels` (JSONB), `last_error`, `next_attempt_at`, `claimed_at`, `sent_at`, `created_at`.
- **Indexes:** `(status, next_attempt_at)` and `(fingerprint, sent_at)`.
- **Retention:** sent, skipped and failed rows are deleted after 90 days.
