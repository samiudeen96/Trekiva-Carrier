/** Small formatting/parsing helpers shared by pages. */

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** "gid://shopify/Order/123" -> "123" (null if it is not a GID). */
export function gidToNumericId(gid: string | null | undefined): string | null {
  if (!gid) return null;
  const match = /\/(\d+)$/.exec(gid);
  return match ? match[1] : null;
}

/** Comma-separated input -> trimmed, non-empty values. */
export function parseList(value: string): string[] {
  return value
    .split(',')
    .map((v) => v.trim())
    .filter(Boolean);
}

export function joinList(values: readonly string[] | null | undefined): string {
  return (values ?? []).join(', ');
}

/** Number/Decimal (possibly a string like "75.00") -> form string. */
export function toInput(value: string | number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value);
}

/** Optional integer input: '' -> null, invalid -> NaN (caller validates). */
export function parseOptionalInt(value: string): number | null {
  const text = value.trim();
  if (text === '') return null;
  const n = Number(text);
  return Number.isInteger(n) ? n : Number.NaN;
}

/** Optional decimal input: '' -> null; returns the trimmed string so Decimals stay exact. */
export function parseOptionalDecimal(value: string): string | null {
  const text = value.trim();
  return text === '' ? null : text;
}

export function isValidNumber(value: string): boolean {
  return value.trim() === '' || Number.isFinite(Number(value));
}

export function humanizeCode(value: string): string {
  const words = value.replace(/_/g, ' ').toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Date-only display. "2026-10-05" is treated as a calendar date (not UTC midnight). */
export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const date = dateOnly ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3])) : new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { dateStyle: 'medium' });
}

/** Decimal string ("75.00") + optional ISO currency -> "₹75.00"; null -> "—". */
export function formatMoney(value: string | number | null | undefined, currency?: string | null): string {
  if (value === null || value === undefined || value === '') return '—';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return String(value);
  if (currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(amount);
    } catch {
      return `${currency} ${String(value)}`;
    }
  }
  return String(value);
}

/** Numeric order id from a route param ("42" -> 42, anything else -> null). */
export function parseId(value: string | null | undefined): number | null {
  if (!value || !/^\d+$/.test(value)) return null;
  return Number(value);
}
