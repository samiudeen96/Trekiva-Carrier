# Shopify setup

Trekiva Logistics is a **custom app** for a single store, created in the Shopify **Dev Dashboard** and installed through Shopify-managed installation. Scopes and webhooks are declared in `shopify.app.toml` and deployed with the Shopify CLI. The Python backend handles everything else.

## 1. Create the app

1. Install the Shopify CLI (`npm install -g @shopify/cli`).
2. In the Dev Dashboard (dev.shopify.com), create an app named **Trekiva Logistics**.
3. Copy the **Client ID** and **Client secret** into `.env` as `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`.
4. Edit `shopify.app.toml`:
   - `client_id`: your client ID
   - `application_url`: `https://<your-domain>/`
   - `redirect_urls`: `https://<your-domain>/auth/callback`
5. Link and deploy the config from the repository root:
   ```bash
   shopify app config link     # choose the app you created
   shopify app deploy          # pushes scopes + webhook subscriptions
   ```
   If deploy rejects a webhook topic for the pinned API version, remove it from the list and redeploy. The handlers for the core topics (`orders/*`, `fulfillment_orders/placed_on_hold`, `fulfillment_orders/hold_released`, `app/uninstalled`) are the ones that matter.

## 2. Protected customer data (required)

The app reads customer name, phone and shipping address, which Shopify classes as protected customer data. In the Dev Dashboard → **API access → Protected customer data access**:
- select the data the app uses (name, email, phone, address), and
- give the reason: "Order fulfillment and shipping".

Without this, GraphQL returns `null` for these fields, and orders land in Manual Review with reason `MISSING_SHIPPING_ADDRESS`.

## 3. Scopes

From `shopify.app.toml`:

| Scope | Why |
|---|---|
| `read_orders` | Order details, tags, payment gateways, risk |
| `write_orders` | Add (never remove) tags, e.g. the optional `TREKIVA-SHIPPED` |
| `read_merchant_managed_fulfillment_orders` | Fulfillment-order status and **holds** |
| `write_merchant_managed_fulfillment_orders` | Create fulfillments with tracking, add tracking events, cancel fulfillments |
| `read_locations` | Map Shopify Locations to warehouses |
| `read_products`, `read_inventory` | Weights and SKUs |

The app does **not** request permission to release holds, and it contains no hold-release code.

## 4. Install

1. Make sure the backend is reachable over HTTPS at `application_url` (see [deployment.md](deployment.md)).
2. In the Dev Dashboard, choose **Install app** and select your store.
3. Open **Apps → Trekiva Logistics** in Shopify admin. On first load, the backend exchanges the App Bridge session token for an offline access token and stores it encrypted.
4. Set `SHOPIFY_SHOP_ALLOWLIST=<your-store>.myshopify.com` so no other shop can use this deployment.

## 5. Configure in the app

1. **Warehouses**: add each pickup location with its Shopify Location GID (`gid://shopify/Location/<id>`; the numeric id is in the Shopify admin URL under Settings → Locations).
2. **Carriers**: for each carrier, save a `MOCK` account (development) and map it to warehouses. Enable the carrier.
3. **Allocation rules**: optional. Without rules, FASTEST applies.
4. **Settings**:
   - Check the **COD gateway names**. Open a COD King order in Shopify; the payment gateway shown there must be in the list.
   - Turn on **automation** last.

## 6. Interaction with COD King and Shopify Flow

- **COD King**: untouched. Trekiva only reads the payment gateway name (to detect COD) and the order's outstanding amount (the COD amount to collect, including COD King's fee).
- **Flow duplicate/risk protection**: keep your workflows as they are. Trekiva:
  1. waits 5 minutes after order creation (and for Shopify's risk analysis) before processing, so Flow has time to act;
  2. treats any fulfillment hold as `MANUAL_REVIEW` and never sends it to a courier;
  3. also blocks orders tagged `DUPLICATE-REVIEW` or `RISK-REVIEW` that do not have a hold yet;
  4. continues automatically once staff release the hold in Shopify. The release is detected by webhook, plus a 10-minute reconciliation as a backup.

  The review tags and wait times are configurable in Settings.

## API version

The backend pins `SHOPIFY_API_VERSION` (default `2026-07`), and `shopify.app.toml` pins the webhook API version. Bump both together, and validate the queries in `backend/app/shopify/queries.py` against the new version (each stable version is supported for 12 months).
