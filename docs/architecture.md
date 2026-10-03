# Architecture

## Overview

```
                      ┌──────────────────────────── Linux VM ─────────────────────────────┐
 Shopify Admin        │  Nginx (TLS, Let's Encrypt)                                       │
 (embedded iframe) ───┼─► /, /assets      → FastAPI: admin UI (index.html + CSP)          │
 App Bridge token     │   /api/admin/*    → FastAPI: admin API (session-token auth)       │
 Shopify webhooks ────┼─► /webhooks/shopify → HMAC → store (idempotent) → enqueue → 200   │
 Carrier webhooks ────┼─► /webhooks/carriers/{code}/{token} → verify → parse → store     │
                      │                                                                   │
                      │  FastAPI ──► PostgreSQL ◄── Celery workers ◄── Redis              │
                      │                               ▲                                   │
                      │                          Celery beat: settle re-checks, review    │
                      │                          reconciliation, webhook sweeper          │
                      │                     Carrier adapters (mock → real)                │
                      └───────────────────────────────────────────────────────────────────┘
```

Principles:

1. **Webhooks are triggers. Shopify is the source of truth.** Handlers identify the order and re-read the live order and fulfillment orders through the GraphQL Admin API. A webhook payload is never trusted as current state.
2. **The unit of work is the fulfillment order (FO).** Holds, locations and fulfillments all live on FOs in Shopify. One order can have several FOs, and each has at most one active shipment.
3. **One synchronous service layer** (SQLAlchemy 2.0 + psycopg 3) shared by FastAPI (threadpool) and Celery workers.
4. **The engine is carrier-agnostic.** It works with a registry of `CarrierAdapter`s and neutral types only.
5. **The app runs only after an order is placed.** No checkout extensions and no carrier-calculated rates. COD King is read-only context (payment gateway names, outstanding amount).

## Order flow

```
orders/create webhook
  → verify HMAC → INSERT webhook_events ON CONFLICT DO NOTHING → 200
  → worker: claim event (RECEIVED|FAILED → PROCESSING)
  → GraphQL: order + fulfillment orders (+ holds, risk, line items, weights, location)
  → upsert shopify_orders; upsert + lock (FOR UPDATE) each shopify_fulfillment_orders row
  → fulfillment gate → new logistics_status (+ audit log)
  → AWAITING_ALLOCATION → allocation → shipment → AWB → Shopify fulfillment + tracking
```

### Fulfillment gate (`logistics/hold_gate.py`)

The gate decides whether this FO may go to a courier right now. It is a pure function, and its checks run in this order:

| # | Check | Result |
|---|---|---|
| 1 | Order cancelled / FO cancelled | `CANCEL` |
| 2 | **FO `ON_HOLD` or has any `fulfillmentHolds`** | `MANUAL_REVIEW` (`SHOPIFY_FULFILLMENT_HOLD`); only Shopify can release it |
| 3 | FO `CLOSED` / `INCOMPLETE` / `IN_PROGRESS` / non-shipping delivery / nothing left to ship | `SKIP` |
| 3b | FO `SCHEDULED` | `WAIT` until `fulfillAt` |
| 4 | Automation disabled for the order | `DISABLED` |
| 5 | Review tag present (default `DUPLICATE-REVIEW`, `RISK-REVIEW`) and no review observed yet | `MANUAL_REVIEW` (`REVIEW_TAG_WITHOUT_HOLD`) |
| 6 | HIGH risk and no review observed yet (configurable) | `MANUAL_REVIEW` (`HIGH_RISK_WITHOUT_HOLD`) |
| 7 | Payment mode unresolved (money owed, no COD gateway) | `MANUAL_REVIEW` (`PAYMENT_MODE_UNRESOLVED`) |
| 8 | Shipping address or pincode missing | `MANUAL_REVIEW` (`MISSING_SHIPPING_ADDRESS`) |
| 9 | Within the settle window (default 5 min after order creation) | `WAIT` |
| 10 | Shopify risk analysis not finished (up to 15 min) | `WAIT` |
| 11 | Otherwise | `PROCEED` → `AWAITING_ALLOCATION` |

**Why steps 5, 6, 9 and 10 exist.** `orders/create` fires as soon as the order exists, but Flow's duplicate and risk workflows (and Shopify's risk analysis) finish later. Without these steps, an order could read `OPEN` in the gap before Flow places its hold, and ship.

**"Review observed".** A review counts as observed if any of these is true:
- we have seen a hold on this FO, or
- Shopify sent `fulfillment_orders/hold_released`, or
- staff approved the order in the Manual Review page.

Once observed, review tags no longer block. This matters because staff usually release the hold but leave the tag on the order. Staff approval never overrides an actual Shopify hold.

**Hold release.** It is detected two ways:
- the `fulfillment_orders/hold_released` webhook triggers an immediate re-check;
- as a safety net, Celery beat re-reads every `MANUAL_REVIEW` order every 10 minutes (`orders.reconcile_review_queue`).

**Re-checking before shipping.** Allocation and shipment creation both re-read the order from Shopify and re-run the gate. Shipment creation does this inside the FO row lock, immediately before the carrier call. A hold placed while the carrier was being chosen therefore still stops the shipment (`test_hold_placed_after_allocation_blocks_shipment`).

## Allocation run (`logistics/allocation_run.py`)

1. **Transaction.** Re-read Shopify and re-run the gate. Build the shipment plan (warehouse, parcel, COD amount, address). Run the prefilter and build an adapter for each remaining carrier. Move `AWAITING_ALLOCATION → ALLOCATING`.
2. **No locks held.** Ask carriers for offers in parallel.
3. **Transaction.** Rank the offers. Store every carrier request and response (`carrier_serviceability_checks`) and every verdict (`carrier_quotes`). Then either:
   - `ALLOCATED`, storing the ranking for fallback and enqueuing shipment creation, or
   - `NO_CARRIER_AVAILABLE`, with the exact reason per carrier. If carriers were only *unreachable*, the run is retried with backoff.

Plan problems end in `FAILED` with an exact reason:

| Reason | Meaning |
|---|---|
| `WAREHOUSE_NOT_MAPPED` | Shopify location not mapped to a warehouse |
| `MISSING_WEIGHT` | product weights missing and no warehouse minimum weight |
| `INCOMPLETE_ADDRESS` | address line 1, city or pincode missing |
| `MULTI_SHIPMENT_COD` | a COD order split across several fulfillment orders; Trekiva never guesses how to divide the COD amount |

## Shipment creation (`logistics/shipments.py`)

"Intent before call":

- **A. Transaction, FO row lock.**
  1. Re-run the live gate.
  2. Refuse if any active shipment exists.
  3. Insert the shipment as `CREATING` with `idempotency_key = {fo_id}:{attempt}`.
  4. Move the FO to `SHIPMENT_PENDING`, then **commit**.
- **B. No locks held.** Call the carrier, sending the idempotency key as our reference.
- **C. Transaction.** Record the outcome:

| Outcome | Shipment | Fulfillment order | Next |
|---|---|---|---|
| Success | `AWB_CREATED` (AWB, tracking URL, cost, EDD, raw response) | `SHIPPED` | Shopify sync |
| `CarrierAmbiguousError` or unexpected error | `CREATION_UNKNOWN` | `RECONCILING` | Ask the **same** carrier: found → `AWB_CREATED`; confirmed not created → retry the same carrier; cannot tell → staff resolve it from the order page |
| `CarrierTransientError` (e.g. 503) | `CREATE_FAILED` | `ALLOCATED` | Retry the same carrier with backoff (`max_create_attempts_per_carrier`, default 3), then fall back |
| Validation / auth / not supported | `CREATE_FAILED` | `ALLOCATED` (next carrier) or `FAILED` | Fall back to the next carrier in the stored ranking; no retries |

A forced (staff-selected) carrier never falls back.

## Shopify sync (`logistics/shopify_sync.py`)

- `fulfillmentCreate` runs for the whole fulfillment order, with `trackingInfo {company, number, url}`. `notifyCustomer` follows Settings, and the optional shipped tag is added with `tagsAdd`.
- Before creating, the FO's existing fulfillments are searched for our AWB. A retried sync after a lost response therefore never fulfills twice.
- `fulfill_on = PICKED_UP` defers the fulfillment until the carrier reports pickup.
- Applied tracking changes become `fulfillmentEventCreate` events:

| Tracking status | Shopify event |
|---|---|
| `PICKED_UP` | `CARRIER_PICKED_UP` |
| `IN_TRANSIT` | `IN_TRANSIT` |
| `OUT_FOR_DELIVERY` | `OUT_FOR_DELIVERY` |
| `DELIVERED` | `DELIVERED` |
| `DELIVERY_FAILED` | `ATTEMPTED_DELIVERY` |
| `RTO_*` | `FAILURE`, with a "Return to origin" message |
| `EXCEPTION` | `DELAYED` |

- A Shopify failure never touches the courier shipment. It is tracked in `shopify_sync_status`, retried, and can be retried from the UI.

## Tracking (`tracking/service.py`)

- **Carrier webhooks:** `POST /webhooks/carriers/{code}/{token}`. The random token identifies the account, `adapter.verify_webhook` authenticates the request, and `adapter.process_webhook` normalises it at ingress. The event is stored (deduplicated by payload hash) and applied by a worker.
- **Polling:** Celery beat polls shipments whose `next_poll_at` is due. The interval depends on status: 30 min when out for delivery, 2–3 h otherwise, and polling stops at terminal states.
- **Applying events:** each event is stored raw + normalised, deduplicated on (AWB, raw status, time), and applied only if it moves the shipment forward. A carrier-side cancellation moves the FO to `FAILED` for staff.

## Cancellation and staff overrides

- **Order cancelled in Shopify after the AWB:**
  - before pickup: the shipment is cancelled with the carrier, then the Shopify fulfillment is cancelled, then the FO becomes `CANCELLED`;
  - after pickup: the order is flagged for an RTO arranged with the carrier.
- **Staff cancel:** the carrier cancels first, and only then is the FO freed. The FO either re-allocates or pauses automation for the order, as chosen.
- **Carrier change:** refused while an AWB is active.
- **Staff actions:** re-run serviceability (stores results, changes nothing), re-run allocation or retry, manually select a carrier, disable/enable automation per order, retry Shopify sync, resolve an unknown creation outcome. Staff actions bypass "automation off" for that order, **never** a Shopify hold.

## Sweepers (`logistics/sweeper.py`, Celery beat)

Lost tasks and dead workers are recovered from the database:

| State | Recovery |
|---|---|
| `ALLOCATING` for more than 10 min | restart allocation |
| `ALLOCATED` for more than 10 min | re-enqueue shipment creation |
| `CREATING` for more than 10 min | becomes `CREATION_UNKNOWN`, then reconcile |
| Stalled reconciliation | re-enqueue |
| Shipment with an AWB but no Shopify fulfillment | re-sync |
| `AWAITING_ALLOCATION` orders | dispatched once shop automation is on |

## Alerts and monitoring (`alerts/`, `ops/`)

Failures that need a person are sent to Slack and/or email. Operating procedures for each alert are in [operations.md](operations.md).

**Outbox.** `alerts.raise_alert` only adds an `alerts` row to the current transaction. The alert therefore exists exactly when the failure is committed, and nothing is sent from inside a business transaction. `alerts.dispatch` (beat, every 30 s) delivers rows in three steps, like shipment creation:
1. Claim the rows (`FOR UPDATE SKIP LOCKED`, mark them `SENDING`) and commit.
2. Send with no locks held.
3. Record the outcome.

A failed channel is retried alone, with backoff, up to 8 attempts.

**Where alerts come from:**

| Source | Alert |
|---|---|
| `before_flush` listener (`alerts/triggers.py`): a fulfillment order *enters* `FAILED` | `SHIPMENT_FAILED` |
| same: reason becomes `RECONCILIATION_NEEDS_STAFF` | `SHIPMENT_NEEDS_STAFF` |
| same: `shipments.shopify_sync_status` becomes `FAILED` | `SHOPIFY_SYNC_FAILED` |
| `CarrierAuthError` during serviceability, setup, create, reconcile, cancel or tracking poll | `CARRIER_AUTH_FAILED` |
| Carrier refused a cancellation (not retryable); Shopify fulfillment not cancelled | `CARRIER_CANCEL_FAILED`, `SHOPIFY_SYNC_FAILED` |
| Order cancelled in Shopify while its shipment cannot be auto-cancelled | `RTO_NEEDED` |
| `ops.health_check` (beat, every 5 min): no progress for `OPS_STUCK_MINUTES` | `STUCK_ORDERS`, `SHOPIFY_SYNC_STALLED` |
| same: webhook events `FAILED` in the last 24 h; Celery queue over threshold | `WEBHOOKS_FAILING`, `QUEUE_BACKLOG` |

The state-change alerts hook the state, not the code paths into it. A new route into `FAILED` therefore always alerts.

**No floods:**
- Each alert has a `fingerprint`: kind + shop, plus the reason for `SHIPMENT_FAILED` and the carrier for `CARRIER_AUTH_FAILED`.
- Once a fingerprint is sent, newer rows with it wait for `ALERT_COOLDOWN_MINUTES`, then go out as one grouped message.
- Health checks store the ids they reported, so an order that stays stuck is reported once, then reminded daily.

**Monitoring:**
- Sentry (optional, `SENTRY_DSN`) for the API, worker and beat. It never receives customer data: no PII, no request bodies, no local variables.
- `/metrics` (Prometheus, token-protected) computes gauges from PostgreSQL and Redis at scrape time.
- `/healthz/worker` returns 503 when the beat-scheduled heartbeat is older than 3 min. The app cannot alert about its own worker being down, so an external uptime monitor watches this endpoint.

## State machines

### Fulfillment order (`logistics_status`)

```
RECEIVED ─► SETTLING ─(window elapsed & risk analysed)─► AWAITING_ALLOCATION ─► ALLOCATING
   │           │                                              │                     │
   └───────────┴──► MANUAL_REVIEW ◄───── hold placed ─────────┘     NO_CARRIER ◄─────┤
                      │  (hold released / staff approval)                           ▼
                      └──────────────► AWAITING_ALLOCATION                      ALLOCATED
                                                                                    │ lock + live gate re-check
   SKIPPED ◄─ closed / fulfilled outside app / pickup                               ▼
   CANCELLED ◄─ order or FO cancelled (terminal)                           SHIPMENT_PENDING
   AUTOMATION_DISABLED ⇄ staff toggle                    ambiguous ─► RECONCILING │ success
                                                                       │          ▼
                                                     FAILED ◄──────────┴────── SHIPPED
```

The transitions are listed in `logistics/state.py`. How a re-evaluation applies depends on the FO's current state:
- **Full re-evaluation** (any gate decision applies): `RECEIVED`, `SETTLING`, `MANUAL_REVIEW`, `AWAITING_ALLOCATION`.
- **Block-only re-evaluation** (only hold, cancel and skip apply): `ALLOCATED`, `NO_CARRIER_AVAILABLE`, `FAILED`, `AUTOMATION_DISABLED`. A "ready" verdict does not restart work that is waiting on staff.
- In-flight and terminal states are never changed by the gate.

### Shipment (`shipments.status`)

```
CREATING ──► AWB_CREATED ─► PICKUP_SCHEDULED ─► PICKED_UP ─► IN_TRANSIT ─► OUT_FOR_DELIVERY ─► DELIVERED
   │  │            │                                              ▲               │
   │  │            │                                              └── re-attempt ─ DELIVERY_FAILED
   │  │            │                               RTO_INITIATED ◄───────────────────┘
   │  │            │                                    └─► RTO_IN_TRANSIT ─► RTO_DELIVERED
   │  │            └─► CANCEL_REQUESTED ─► CANCELLED (before pickup only)
   │  └─► CREATION_UNKNOWN ─(reconcile with same carrier)─► AWB_CREATED | CREATE_FAILED
   └─► CREATE_FAILED
EXCEPTION reachable from any in-flight state; a later concrete status resolves it.
```

Tracking events arrive out of order and duplicated, so `tracking/transitions.py` applies an update only if it moves the shipment forward. Terminal states never change. The RTO flow cannot regress into the forward flow.

## Carrier architecture

See [carrier-adapters.md](carrier-adapters.md). In short:

- `carriers/base.py` defines `CarrierAdapter` with these methods: `check_serviceability`, `get_quote`, `get_edd`, `get_offer`, `create_shipment`, `generate_awb`, `find_shipment_by_reference`, `cancel_shipment`, `track_shipment`, `verify_webhook`, `process_webhook`, `normalize_status`.
- `carriers/registry.py` holds a real adapter and a mock adapter per carrier code. Each account's environment (`MOCK`, `SANDBOX` or `PRODUCTION`) selects which one is used. Mocks are refused in production.
- `carriers/errors.py` is the error taxonomy. The retry system acts on the error class alone.
- `carriers/mock/` provides `MockEkartAdapter` (3 days, ₹75) and `MockXpressBeesAdapter` (2 days, ₹85), with pincode-driven scenarios.
- `carriers/ekart` and `carriers/xpressbees` are placeholders that raise `CarrierNotImplementedError` until official docs arrive.

## Allocation engine (`logistics/allocation/`)

1. **Rule selection.** The active `allocation_rules` row with the lowest `priority` number whose `conditions` match wins. Conditions use a small JSON DSL with no `eval`, over payment mode, value, weight, warehouse, pincode, state, country, SKUs and tags. If no rule matches, FASTEST applies.
2. **`prefilter`** runs before any API call and checks: carrier enabled, warehouse mapped, weight range, COD/prepaid enabled.
3. **Offers.** `collect_offers` queries carriers in parallel with a per-run timeout. A failing carrier never blocks the others.
4. **Hard filters**: serviceable, payment mode available, pickup available, carrier max cost.
5. **Rule limits**: max cost, max EDD, min performance, allowed carriers.
6. **Strategy**: FASTEST (EDD, then cost, then priority), CHEAPEST, PRIORITY, BALANCED (weighted normalised score), CUSTOM (explicit order). All tie-breaks are deterministic.
7. **Fallback carrier.** Used only if no carrier survives the rule limits, and only if it passed every hard filter.

Every rejection carries a reason code and a readable detail, stored in `carrier_quotes` and shown on the order page.

## Webhook architecture

| Step | Where | Notes |
|---|---|---|
| Verify `X-Shopify-Hmac-Sha256` over the raw body | `webhooks/shopify.py` | constant-time compare; 401 on failure |
| Store: `INSERT … ON CONFLICT (source, external_event_id) DO NOTHING` | `webhooks/ingest.py` | key = `X-Shopify-Event-Id` (stable across Shopify retries); auth headers never stored |
| Enqueue after commit; respond 200 | `workers/enqueue.py` | if Redis is down, the event stays `RECEIVED` and the sweeper picks it up |
| Claim `RECEIVED|FAILED → PROCESSING` atomically | `webhooks/processor.py` | only one worker wins |
| Dispatch by topic | `webhooks/dispatch.py` | handlers re-read Shopify |
| Outcome | `PROCESSED`, `IGNORED`, or `FAILED` | transient failures retry with exponential backoff (30 s → 1 h, 8 attempts); permanent failures are not retried and are logged with the exact error |
| Sweeper (beat, every 2 min) | `workers/tasks/maintenance.py` | re-enqueues stale `RECEIVED` events; resets `PROCESSING` events stuck for over 15 min |

## Idempotency strategy

| Layer | Mechanism |
|---|---|
| Duplicate webhooks | `UNIQUE(source, external_event_id)` plus the atomic claim |
| Concurrent work on one FO | `SELECT … FOR UPDATE` on the FO row for the whole evaluation |
| Duplicate AWB | Partial unique index: one shipment per FO whose status is not `CANCELLED`/`CREATE_FAILED`. `CREATING` and `CREATION_UNKNOWN` also occupy the slot. |
| Intent before call | Insert the shipment as `CREATING` with `idempotency_key = {fo_id}:{attempt}` and commit *before* calling the carrier |
| Carrier retries | The same idempotency key is sent as the carrier reference |
| Timeout after send | `CarrierAmbiguousError` → `CREATION_UNKNOWN` → `find_shipment_by_reference` with the **same** carrier → found, retry the same carrier, or go to staff. **Never** fall back to another carrier while the outcome is unknown. |
| Tracking events | `UNIQUE(shipment_id, dedup_hash)` plus monotonic status progression |
| Celery | `acks_late`, `reject_on_worker_lost`, prefetch 1. All tasks are safe to run twice. |

## Retry policy

| Failure | Behaviour |
|---|---|
| Network error, HTTP 5xx, 429, Shopify THROTTLED | Retry with backoff (the GraphQL client honours `extensions.cost` restore rates) |
| Validation (invalid pincode, bad weight) | No retry. Try the next ranked carrier, or end in `NO_CARRIER_AVAILABLE`/`FAILED` with the reason |
| Carrier auth failure | No retry. `CARRIER_AUTH_FAILED` alert (critical); the carrier is skipped until its credentials are fixed |
| Ambiguous create | Reconcile first (see above) |

## Security

- Admin API: every request must carry an App Bridge session token, verified for HS256 signature, `exp`/`nbf`, audience, and a matching shop in `iss`/`dest`. `SHOPIFY_SHOP_ALLOWLIST` restricts the private app to your store.
- Offline access tokens come from token exchange (no OAuth redirect). They are stored Fernet-encrypted, and expiring tokens are refreshed automatically.
- Carrier credentials are Fernet-encrypted at rest. The API returns masked hints only, and a blank secret on update keeps the stored value.
- Webhooks are HMAC-verified. Carrier webhooks use per-account random path tokens plus the adapter's own verification.
- `Content-Security-Policy: frame-ancestors https://<shop> https://admin.shopify.com` is set on the embedded UI.
- HTTPS only (Nginx with HSTS). Secrets come only from the environment.
- Every automation step and staff action is written to `automation_logs`, including the actor.
