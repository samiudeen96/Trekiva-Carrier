import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, type Plugin } from 'vite';

/**
 * Outside the Shopify admin (plain `npm run dev` in a browser tab) App Bridge would try to
 * redirect the top-level window into the admin and every same-origin `fetch` would wait for a
 * session token that never arrives. In dev-server mode we therefore strip the App Bridge tags
 * unless `VITE_EMBEDDED_DEV=1` (e.g. when the dev server is tunnelled into the admin).
 * Production builds are never touched.
 */
function stripAppBridgeInDev(enabled: boolean): Plugin {
  return {
    name: 'trekiva:strip-app-bridge-in-dev',
    apply: 'serve',
    transformIndexHtml(html) {
      if (!enabled) return html;
      return html
        .replace(/\s*<meta name="shopify-api-key"[^>]*>/, '')
        .replace(/\s*<script src="https:\/\/cdn\.shopify\.com\/shopifycloud\/app-bridge\.js"><\/script>/, '');
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backend = env.VITE_BACKEND_URL || 'http://localhost:8000';
  const embeddedDev = env.VITE_EMBEDDED_DEV === '1';

  return {
    base: '/',
    plugins: [react(), stripAppBridgeInDev(!embeddedDev)],
    server: {
      port: 5173,
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/webhooks': { target: backend, changeOrigin: true },
      },
    },
    build: {
      outDir: 'dist',
      assetsDir: 'assets',
      emptyOutDir: true,
      sourcemap: false,
    },
  };
});
