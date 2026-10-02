/**
 * Thin, defensive wrappers around the App Bridge global (`window.shopify`).
 *
 * The app also runs outside the Shopify admin during local development (backend started with
 * `DEV_AUTH_BYPASS_SHOP`), where `window.shopify` is undefined or not connected to a host, so
 * every access is guarded.
 */
import type { ShopifyGlobal } from '@shopify/app-bridge-types';

export function getShopify(): ShopifyGlobal | undefined {
  return typeof window !== 'undefined' && typeof window.shopify !== 'undefined' ? window.shopify : undefined;
}

/** True when rendered inside an iframe (i.e. embedded in the Shopify admin). */
export function isEmbedded(): boolean {
  try {
    return window.top !== window.self;
  } catch {
    // Cross-origin access to window.top throws: we are definitely framed.
    return true;
  }
}

const ID_TOKEN_TIMEOUT_MS = 10_000;

/** Fetches a fresh App Bridge session token, or `null` when not available (dev mode). */
export async function getSessionToken(): Promise<string | null> {
  const shopify = getShopify();
  if (!shopify || typeof shopify.idToken !== 'function' || !isEmbedded()) return null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), ID_TOKEN_TIMEOUT_MS);
    });
    return await Promise.race([shopify.idToken(), timeout]);
  } catch {
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Shows an admin toast when App Bridge is available; returns whether it was shown. */
export function toast(message: string, isError = false): boolean {
  const shopify = getShopify();
  if (!shopify?.toast || !isEmbedded()) return false;
  try {
    shopify.toast.show(message, { isError, duration: isError ? 8000 : 4000 });
    return true;
  } catch {
    return false;
  }
}
