# Operations runbook

How to know Trekiva Logistics is healthy, what each alert means, and what to do about it.

All commands run on the VM in `/opt/trekiva-logistics`. To shorten them:

```bash
alias dc='docker compose -f docker-compose.prod.yml --env-file .env'
alias psql-trekiva='dc exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'   # after: set -a; . ./.env; set +a
```

## 1. What tells you something is wrong

| Signal | Catches | Set up in |
|---|---|---|
| **Alerts** (Slack / email) | failed shipments, carrier credential problems, Shopify sync failures, stuck orders, failing webhooks | §2 |
| **Uptime monitor** on `/readyz` and `/healthz/worker` | API, database, Redis or the background worker being down. Alerts cannot do this: the worker sends them. | §3 |
| **Sentry** (optional) | unexpected exceptions, with stack traces | §4 |
| **Metrics** (optional) | trends and dashboards: backlog, statuses, queue length | §5 |

The admin UI remains the place to act: every alert says which order is affected, and each order page shows the exact reason and the available actions.

## 2. Alerts setup

Configure at least one channel in `.env`, then restart (`dc up -d`):

- **Slack:** create an Incoming Webhook for a channel (Slack → Apps → Incoming Webhooks) and set `ALERT_SLACK_WEBHOOK_URL`.
- **Email:** set `ALERT_EMAIL_TO` (comma-separated), `ALERT_EMAIL_FROM` and `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `SMTP_SECURITY` (`starttls` for 587, `ssl` for 465). Any transactional mail provider works (SES, Postmark, Brevo, Google Workspace SMTP relay).

Test it:

```bash
dc run --rm api python -m app.alerts test
# slack: ok
# email: ok
```

Behaviour:

- Alerts go out within about 30 seconds.
- After an alert is sent, more alerts of the same kind (and same reason or carrier) wait `ALERT_COOLDOWN_MINUTES` (default 30), then arrive as one message, e.g. "Order could not be shipped (MISSING_WEIGHT) (12x)", listing the orders.
- Alerts below `ALERT_MIN_SEVERITY` are recorded but not sent.
- With no channel configured, alerts are recorded only (status `SKIPPED`). The API logs a warning at start-up in production.

## 3. Uptime monitoring

Use any external monitor (UptimeRobot, Better Stack, Healthchecks.io, Uptime Kuma on another machine). Check every 1–5 minutes and alert on two consecutive failures:

| URL | Healthy | When it fails |
|---|---|---|
| `https://<domain>/readyz` | 200 | API down, or PostgreSQL / Redis unreachable |
| `https://<domain>/healthz/worker` | 200 | No Celery worker has run the heartbeat for 3 min: the worker or beat container is down, Redis is down, or the queue is badly backed up. **Orders are not being shipped and no alerts are being sent.** |

## 4. Sentry (optional)

Create a Python project in Sentry, set `SENTRY_DSN` (and optionally `SENTRY_RELEASE` to the git commit) and restart. The API, worker and beat report errors tagged `component=api|worker|beat`.

Customer data never leaves the VM: Sentry gets no PII, no request bodies (webhook payloads are customer data) and no local variables.

## 5. Metrics (optional)

Set `METRICS_TOKEN` to a long random value (`openssl rand -hex 32`). Scrape from the VM itself; Nginx returns 404 for `/metrics` from outside:

```bash
curl -s -H "Authorization: Bearer $METRICS_TOKEN" http://127.0.0.1:8000/metrics
```

Any Prometheus-compatible agent on the VM can scrape it, for example Grafana Alloy shipping to Grafana Cloud. The most useful series:

| Metric | Watch for |
|---|---|
| `trekiva_stuck_fulfillment_orders` | > 0 |
| `trekiva_stalled_shopify_syncs` | > 0 |
| `trekiva_fulfillment_orders{status="FAILED"}` / `{status="NO_CARRIER_AVAILABLE"}` | growth |
| `trekiva_queue_length{queue="default"}` | sustained growth |
| `trekiva_worker_heartbeat_age_seconds` | > 180 |
| `trekiva_alerts{status="FAILED"}` | > 0: alerts are not being delivered |

## 6. Alert reference

| Alert | Severity | Meaning | What to do |
|---|---|---|---|
| **CARRIER_AUTH_FAILED** | critical | The carrier rejected our credentials (expired or rotated key, account suspended). Orders skip this carrier and go to the others. | Fix the credentials on the **Carriers** page; check the carrier panel or contact the carrier. Then re-run allocation for orders that ended in `NO_CARRIER_AVAILABLE` or `FAILED`. |
| **SHIPMENT_NEEDS_STAFF** | critical | A shipment request timed out and the carrier cannot confirm whether it was created. Trekiva will not retry or switch carriers on its own, to avoid two AWBs. | Look up the reference shown on the order page (`{fo_id}:{attempt}`) in the carrier panel. On the order page, choose **created** (enter the AWB) or **not created**. |
| **SHIPMENT_FAILED** | error | An order cannot be shipped. The title carries the reason. | Open the order and fix the cause, then **Retry** or pick a carrier. Common reasons: `MISSING_WEIGHT` (set product weights or a warehouse minimum weight), `WAREHOUSE_NOT_MAPPED` (map the Shopify location on Warehouses), `INCOMPLETE_ADDRESS` (fix the address in Shopify), `MULTI_SHIPMENT_COD` (ship the split COD order manually), `SHIPMENT_CREATION_FAILED` (every carrier rejected it; read the carrier errors in the order's log), `CARRIER_CANCELLED` (the carrier cancelled the AWB; re-allocate). |
| **SHOPIFY_SYNC_FAILED** | error | The AWB exists with the carrier, but Shopify rejected the fulfillment (often a hold placed after the AWB was created), or a Shopify fulfillment could not be cancelled. | Release the hold in Shopify (only if the order really should ship), then **Retry Shopify sync** on the order page. Or cancel the shipment. For a fulfillment that could not be cancelled, cancel it in Shopify. |
| **CARRIER_CANCEL_FAILED** | error | The carrier refused to cancel a shipment. | Cancel it in the carrier panel before pickup, or arrange the return. |
| **RTO_NEEDED** | warning | An order was cancelled in Shopify, but its shipment was already picked up (or auto-cancel is off). | Arrange the return (RTO) with the carrier. |
| **STUCK_ORDERS** | warning | Orders have not moved for `OPS_STUCK_MINUTES` while they should have. | On each order, **Re-run allocation** / **Retry**. If many orders are stuck, the worker is probably unhealthy: see §7.1. |
| **SHOPIFY_SYNC_STALLED** | error | AWBs created but not on the Shopify order yet, so customers have no tracking. | **Retry Shopify sync** on the order page. Check Logs for Shopify errors (expired token, rate limits). |
| **WEBHOOKS_FAILING** | error | Shopify or carrier webhooks failed processing. | Find the error (§8). Fix the cause, then replay (§7.4). |
| **QUEUE_BACKLOG** | warning | Background work is piling up. | §7.1; consider raising worker `--concurrency`. |

## 7. Procedures

### 7.1 Worker or beat down / backlog

```bash
dc ps                                            # every service "running"? migrate "exited (0)"
dc logs --tail=200 worker beat
dc restart worker beat
curl -s https://<domain>/healthz/worker          # back to 200 within ~1 min
```

No work is lost. Webhooks are stored before processing, tasks are safe to run twice, and the sweepers re-enqueue anything left behind within a few minutes.

### 7.2 API down or `/readyz` failing

```bash
dc ps
dc logs --tail=200 api
curl -s localhost:8000/readyz                    # names the failing dependency
dc restart api
df -h /                                          # a full disk stops PostgreSQL and Redis
```

If PostgreSQL does not start, check `dc logs postgres`. Restore from backup only as a last resort ([deployment.md §4](deployment.md#4-backups)).

### 7.3 Carrier credentials rotated

Update them on the **Carriers** page (a blank secret keeps the stored value). No restart is needed.

### 7.4 Replaying failed webhooks

Failed events stay `FAILED` with their error. After fixing the cause, set them back to `RECEIVED`; the sweeper re-processes them within 2 minutes. Processing is idempotent, and handlers re-read Shopify, so replaying is safe.

```sql
UPDATE webhook_events SET processing_status = 'RECEIVED', attempts = 0
WHERE processing_status = 'FAILED' AND id IN (...);
```

### 7.5 Shopify access problems

Logs show `ShopifyAuthError` or 401 responses: open the app once from Shopify admin. The session-token exchange stores a fresh offline token. If the app was uninstalled, reinstall it ([shopify-setup.md](shopify-setup.md)).

### 7.6 Alerts not arriving

```bash
dc run --rm api python -m app.alerts test      # per-channel result
```

```sql
SELECT status, count(*) FROM alerts GROUP BY 1;
SELECT id, kind, attempts, last_error FROM alerts WHERE status IN ('PENDING','FAILED') ORDER BY id DESC LIMIT 20;
```

- `SKIPPED` means no channel was configured, or the alert was below `ALERT_MIN_SEVERITY`.
- `FAILED` means every attempt failed; see `last_error`.
- Alerts older than 90 days are deleted automatically.

## 8. Useful queries

```sql
-- Orders by state
SELECT logistics_status, status_reason, count(*) FROM shopify_fulfillment_orders GROUP BY 1, 2 ORDER BY 3 DESC;

-- Orders needing attention, with the reason shown in the UI
SELECT o.name, f.logistics_status, f.status_reason, f.status_detail, f.updated_at
FROM shopify_fulfillment_orders f JOIN shopify_orders o ON o.id = f.order_id
WHERE f.logistics_status IN ('FAILED', 'NO_CARRIER_AVAILABLE', 'MANUAL_REVIEW', 'RECONCILING')
ORDER BY f.updated_at;

-- Webhook backlog and failures
SELECT processing_status, count(*) FROM webhook_events GROUP BY 1;
SELECT id, topic, attempts, error_message FROM webhook_events
WHERE processing_status = 'FAILED' ORDER BY id DESC LIMIT 20;

-- AWBs not yet on the Shopify order
SELECT shopify_order_name, carrier_code, awb, status, shopify_sync_error FROM shipments
WHERE awb IS NOT NULL AND shopify_fulfillment_id IS NULL ORDER BY id DESC;

-- Recent alerts
SELECT created_at, severity, kind, title, status FROM alerts ORDER BY id DESC LIMIT 20;
```

## 9. Routine checks

| When | Check |
|---|---|
| Daily (2 min) | No unread alerts. Dashboard: nothing in Manual review or Failed that is older than a day. |
| Weekly | `dc logs --since 168h api worker | grep -c '"level": "ERROR"'` is not growing; disk below 70% (`df -h`); backups present in `/var/backups/trekiva` and copied off the VM. |
| Monthly | Restore the latest backup into a scratch database ([deployment.md §4](deployment.md#4-backups)). Run `sudo certbot renew --dry-run`. Apply OS updates (`sudo apt update && sudo apt upgrade`, reboot if needed). |
| On carrier credential rotation | Update the **Carriers** page the same day; watch for `CARRIER_AUTH_FAILED`. |
