# Trekiva Logistics

A private, embedded Shopify app that automates courier logistics for Shopify orders across multiple carriers: currently **Ekart** and **XpressBees**, with more to come.

```
Shopify order → fulfillment gate (holds / review / settle) → carrier serviceability → allocation
             → shipment + AWB → Shopify tracking → carrier tracking updates → Shopify
```

The app runs only **after** an order is placed. It never touches checkout, never replaces **COD King** (OTP and COD fee), and never releases the **Shopify Flow** holds (`DUPLICATE-REVIEW`, `RISK-REVIEW`). A fulfillment order that is on hold in Shopify is **never** sent to a courier.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Project structure, FastAPI, PostgreSQL, Redis/Celery, models + migration, Shopify auth/webhooks, carrier architecture + mocks, allocation engine, fulfillment-hold gate, basic admin UI | **Done** |
| 2 | Mock end-to-end flow: allocation run → shipment + mock AWB → Shopify fulfillment/tracking; reconciliation, fallback, cancellation; carrier tracking webhooks + polling; Orders/Shipments/Tracking/Logs pages; manual overrides | **Done** |
| 3 | Ekart adapter from official API docs | Waiting for docs |
| 4 | XpressBees adapter from official API docs | Waiting for docs |
| 5 | Alerts (email/Slack), health checks, Sentry, metrics, operations runbook | Code complete, tests not yet run |
| 5 | Real carrier tracking webhooks (with Phases 3/4), VM deployment | Planned |

The full flow runs end to end against the **mock** carriers. Ekart and XpressBees plug in by implementing their adapters (Phases 3 and 4); nothing else changes.

## Review checks (risk orders and duplicates)

Customers are never stopped from ordering. Once per order, when it is otherwise ready to ship, Trekiva checks:

| Check | Flags the order when | Tag |
|---|---|---|
| Location risk | The customer's IP location, or the billing address, is in a different **state** (or country) than the delivery address. Example: ordered from Mumbai, delivering to Chennai. Missing data never flags an order. | `RISK-REVIEW` |
| Duplicate order | Another order placed within 24 hours (either side) has the **same phone number, the same customer name and at least one SKU in common**. Cancelled orders are ignored. | `DUPLICATE-REVIEW` |

A flagged order is **not** shipped. Trekiva puts its fulfillment order on hold in Shopify (`HIGH_RISK_OF_FRAUD` for location risk, `OTHER` for duplicates, with the reason in the hold notes) and adds the tag, so the alert shows on the Shopify order and in Trekiva's Manual Review page. Staff contact the customer, then either cancel the order or **release the hold in Shopify**; Trekiva then ships it automatically and does not flag it again. If Shopify refuses the hold, the order stays in Manual Review and staff can approve it there.

Each check can be switched off, and the window and tags changed, under **Settings → Review checks**. Code: `logistics/review_checks.py`.

**IP location setup.** The IP comparison needs a MaxMind GeoLite2-City database (free account at maxmind.com). Put `GeoLite2-City.mmdb` in `./geoip/` (mounted into the containers) and set `GEOIP_CITY_DB_PATH=/app/geoip/GeoLite2-City.mmdb`. Lookups are local, so customer IPs are never sent to a third party. Without the file, the IP comparison is skipped and the billing-address comparison still runs. Before going live, place a test order and confirm that Shopify's `clientIp` is the customer's IP: if an app such as COD King creates orders from its own servers, the IP is the app's, not the customer's, and the IP comparison should be switched off.

### Trying it in a development store

1. Configure a warehouse mapped to your Shopify location, and a `MOCK` account for both carriers (Carriers page). Enable both carriers and turn automation on.
2. Place a test order. After the settle window (5 min by default; lower it in Settings for testing), the order is allocated: XpressBees wins on speed (2 days vs 3). A mock AWB is created and the Shopify order is fulfilled with that tracking number.
3. Use **Simulate tracking** on the order page to move the shipment through pickup, out-for-delivery and delivered (or RTO). Each step appears in Shopify's order timeline.
4. Use the scenario pincodes in [docs/carrier-adapters.md](docs/carrier-adapters.md) to exercise the failure paths: only one carrier serviceable, none serviceable, create timeout (reconciled), HTTP 503 (retry, then fall back), invalid pincode.

## Quick start (development)

Prerequisites: Docker. You only need Node 20 to work on the admin UI.

```bash
cp .env.example .env
# Fill in SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET and generate ENCRYPTION_KEYS:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

(cd admin-ui && npm install && npm run build)   # builds admin-ui/dist, served by the API
docker compose up --build                        # postgres, redis, migrate, api, worker, beat
```

- API: http://localhost:8000. Interactive API docs at `/api/docs` (outside production).
- Health: `/healthz` (liveness) and `/readyz` (database and Redis).
- To use the admin UI outside Shopify admin, set `DEV_AUTH_BYPASS_SHOP=<shop>.myshopify.com` (development only), then `cd admin-ui && npm run dev`.
- If ports 5432, 6379 or 8000 are taken on your machine, set `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` or `API_HOST_PORT` in `.env`.

### Tests and checks

```bash
docker compose run --rm api pytest          # real PostgreSQL (separate trekiva_test DB)
docker compose run --rm api ruff check .
docker compose run --rm api mypy app        # strict
```

## Repository layout

```
backend/
  app/
    api/         health checks, SPA serving (CSP frame-ancestors)
    admin/       embedded admin API (/api/admin/*), session-token auth
    core/        config, db, crypto (Fernet), enums, logging, errors
    models/      SQLAlchemy models (see docs/database.md)
    schemas/     Pydantic: Shopify snapshots, shop settings, admin DTOs
    services/    shops/tokens, order sync, carrier config, rules, dashboard, review, audit
    shopify/     GraphQL client, queries, parsing, HMAC + session tokens, token exchange
    carriers/    CarrierAdapter base, registry, types, errors, http; mock/, ekart/, xpressbees/, _template/
    logistics/   hold gate, pipeline, state machine, payment detection, offers, allocation/
    tracking/    shipment status progression rules
    webhooks/    Shopify ingress, idempotent storage, processor, topic dispatch
    alerts/      alert outbox, state-change triggers, Slack/email channels, dispatcher
    ops/         health checks, worker heartbeat, Prometheus metrics
    workers/     Celery app, tasks, retry policy, beat schedule
  migrations/    Alembic (0001_initial_schema)
  tests/         unit/ + integration/ (PostgreSQL)
admin-ui/        React + Vite + Polaris web components + App Bridge
deploy/          nginx, systemd, backup script
docs/            architecture, database, API, carrier adapters, Shopify setup, deployment
shopify.app.toml scopes + webhook subscriptions (deployed with Shopify CLI)
```

## Documentation

- [docs/architecture.md](docs/architecture.md): design, state machines, idempotency, safety rules
- [docs/database.md](docs/database.md): schema reference
- [docs/api.md](docs/api.md): admin API and webhook endpoints
- [docs/carrier-adapters.md](docs/carrier-adapters.md): how to add or implement a carrier
- [docs/shopify-setup.md](docs/shopify-setup.md): creating and installing the Shopify app
- [docs/deployment.md](docs/deployment.md): Linux VM, Docker, Nginx, Let's Encrypt, backups
- [docs/operations.md](docs/operations.md): alerts, uptime monitoring, Sentry, metrics, runbook

## Safety guarantees (and where they are enforced)

| Guarantee | Enforced by |
|---|---|
| Held fulfillment orders never reach a courier | `logistics/hold_gate.py` (checked first, on every evaluation); Shopify also rejects fulfilling held orders |
| Flow review tags block even if the hold has not landed yet | `hold_gate.py` (`REVIEW_TAG_WITHOUT_HOLD`) plus a settle window and a wait for risk analysis |
| The app never releases holds | No hold-release mutation exists in the codebase; the UI only explains "release in Shopify" |
| Risk and duplicate orders are held, not shipped | `logistics/review_checks.py` flags the order before allocation; the gate blocks it (`REVIEW_CHECK_FLAGGED`) until the Shopify hold Trekiva places is released |
| One active AWB per fulfillment order | PostgreSQL partial unique index `uq_shipments_active_fulfillment_order` |
| A webhook is processed at most once | `UNIQUE(source, external_event_id)` plus an atomic claim (`webhooks/processor.py`) |
| Holds are re-checked live immediately before every carrier call | `logistics/shipments.py` (re-reads Shopify under the fulfillment-order row lock) |
| A timed-out create never falls back to another carrier | `CarrierAmbiguousError` → `CREATION_UNKNOWN` → reconcile with the **same** carrier, or ask staff (`logistics/shipments.py`) |
| The shipment row is reserved before the carrier is called | "Intent before call" (`CREATING` row committed first); tested with two racing workers (`tests/integration/test_concurrency.py`) |
| No carrier change while an AWB is active | `services/overrides.py` refuses until the shipment is cancelled with the carrier |
| Shopify never gets a second fulfillment for one AWB | `shopify_sync.py` looks up an existing fulfillment by AWB before creating one |
| Stale or duplicate tracking events never move a shipment backwards | `tracking/transitions.py` + `UNIQUE(shipment_id, dedup_hash)` |
| No hard-coded carrier logic in the engine | `logistics/allocation/*` sees only `CarrierProfile` / `CarrierOffer` (tested with a third carrier) |
| Secrets are never stored or returned in plain text | Fernet encryption (`core/crypto.py`); API returns masked hints only |
| Every failure that needs a person reaches one | `alerts/triggers.py` hooks the *state* (FO → `FAILED`, needs-staff, Shopify sync `FAILED`), not each code path; alert rows commit with the failure (outbox) |
| No invented carrier endpoints | Ekart and XpressBees adapters raise `CarrierNotImplementedError` until official docs are supplied |
