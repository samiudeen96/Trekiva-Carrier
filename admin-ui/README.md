# Trekiva Logistics — embedded admin UI

React 19 + TypeScript + Vite, using Shopify **Polaris web components** (`polaris.js`) and **App Bridge**
(`app-bridge.js`), both loaded from Shopify's CDN in `index.html`. JSX typings come from
`@shopify/polaris-types` and `@shopify/app-bridge-types`.

## Scripts

```bash
npm install
npm run dev        # Vite dev server on http://localhost:5173 (proxies /api and /webhooks to the backend)
npm run typecheck  # tsc -b (strict)
npm run build      # tsc -b && vite build -> dist/index.html + dist/assets/*
```

## Local development (outside the Shopify admin)

1. Start the backend in development mode with the auth bypass, e.g. in `backend/.env`:

   ```env
   APP_ENV=development
   DEV_AUTH_BYPASS_SHOP=your-dev-store.myshopify.com
   ```

   and run it on port 8000 (`uvicorn app.main:app --reload --port 8000`).
2. `npm run dev` and open http://localhost:5173.

With the bypass active the backend accepts `/api/admin/*` requests without a session token, so the
UI sends none. In dev-server mode the App Bridge `<script>`/`<meta>` tags are stripped from
`index.html` (App Bridge would otherwise try to redirect into the admin and block `fetch` waiting for
a session token); a simple inline navigation bar replaces the admin sidebar.

Options (env vars or `admin-ui/.env.local`):

- `VITE_BACKEND_URL` — proxy target (default `http://localhost:8000`).
- `VITE_EMBEDDED_DEV=1` — keep App Bridge in dev mode, for when the dev server is tunnelled and loaded
  inside the Shopify admin.

## How the build is served

`npm run build` writes `dist/index.html` and hashed bundles in `dist/assets/` (Vite `base: '/'`).
The FastAPI backend mounts `dist/assets` at `/assets` and returns `dist/index.html` for every
non-API path (client-side routing via react-router), replacing the literal `__SHOPIFY_API_KEY__`
placeholder in `<meta name="shopify-api-key">` with the app's client ID at serve time and adding
the `frame-ancestors` CSP header. Rebuild after UI changes; the backend caches `index.html` per process.

Inside the admin, App Bridge attaches `Authorization: Bearer <session token>` to same-origin
`fetch` calls; `src/api.ts` additionally sets it explicitly from `shopify.idToken()` when available.

## Layout

```
src/
  api.ts               typed fetch client + ApiError (handles {error,message} and 422 {detail})
  appBridge.ts         guarded access to window.shopify (idToken, toast, embedded detection)
  app-bridge-jsx.d.ts  exposes <ui-nav-menu> on React 19's JSX namespace
  types.ts             API contract types
  session.tsx          session context (GET /api/admin/session)
  App.tsx, main.tsx    routes, <ui-nav-menu>, shopify:navigate -> react-router
  hooks/               useApi (load/reload), useNotice (banners + toasts)
  components/          Feedback (banners, spinner, empty), StatTile, ConfirmModal, JsonSchemaForm
  pages/               one file per admin page
```
