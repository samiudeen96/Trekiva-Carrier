/**
 * Typed client for the embedded admin API (`/api/admin/*`, same origin).
 *
 * Inside the Shopify admin, App Bridge already adds `Authorization: Bearer <session token>` to
 * same-origin `fetch` calls; we also set it explicitly when a token is obtainable. Outside the
 * admin (local dev with `DEV_AUTH_BYPASS_SHOP`) no auth header is sent.
 */
import { getSessionToken } from './appBridge';
import type {
  AllocateIn,
  AllocateResult,
  AllocationPreview,
  ApproveResult,
  CancelShipmentIn,
  Carrier,
  CarrierAccountUpdate,
  CarrierSettingsUpdate,
  Dashboard,
  LogListParams,
  LogPage,
  OrderAutomationResult,
  OrderDetail,
  OrderListParams,
  OrderRow,
  Page,
  ResolveShipmentIn,
  ReviewItem,
  Rule,
  RuleIn,
  SessionInfo,
  SettingsResponse,
  SettingsUpdate,
  ShipmentListParams,
  ShipmentRow,
  SimulateTrackingIn,
  SimulateTrackingResult,
  SyncShopifyResult,
  TrackingRow,
  Warehouse,
  WarehouseIn,
  WarehouseMapping,
} from './types';

export const API_BASE = '/api/admin';

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
  type?: string;
}

/** Error thrown for any non-2xx response (or network failure, with `status` 0). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function formatValidationIssues(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (!Array.isArray(detail)) return 'Validation failed';
  const lines = (detail as ValidationIssue[]).map((issue) => {
    const loc = (issue.loc ?? []).filter((part) => part !== 'body').join('.');
    return loc ? `${loc}: ${issue.msg ?? 'invalid'}` : (issue.msg ?? 'invalid');
  });
  return lines.length ? lines.join('; ') : 'Validation failed';
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    const text = await response.text();
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (isRecord(body)) {
    if (typeof body.message === 'string') {
      return new ApiError(response.status, typeof body.error === 'string' ? body.error : 'error', body.message, body);
    }
    if ('detail' in body) {
      return new ApiError(response.status, 'validation_error', formatValidationIssues(body.detail), body.detail);
    }
  }
  const fallback =
    response.status === 401
      ? 'Your session has expired. Reload the app from the Shopify admin.'
      : `Request failed (${response.status} ${response.statusText || 'error'})`;
  return new ApiError(response.status, 'http_error', fallback, body);
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE';

export async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const headers = new Headers({ Accept: 'application/json' });
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  const token = await getSessionToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new ApiError(0, 'network_error', `Network error: ${message}`);
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

const enc = encodeURIComponent;

/** `{a: 1, b: '', c: undefined}` -> `?a=1` (empty/undefined values are dropped). */
export function queryString(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  session: () => request<SessionInfo>('GET', '/session'),
  dashboard: () => request<Dashboard>('GET', '/dashboard'),

  getSettings: () => request<SettingsResponse>('GET', '/settings'),
  putSettings: (body: SettingsUpdate) => request<SettingsResponse>('PUT', '/settings', body),

  listCarriers: () => request<Carrier[]>('GET', '/carriers'),
  getCarrier: (code: string) => request<Carrier>('GET', `/carriers/${enc(code)}`),
  putCarrierSettings: (code: string, body: CarrierSettingsUpdate) =>
    request<Carrier>('PUT', `/carriers/${enc(code)}/settings`, body),
  putCarrierAccount: (code: string, body: CarrierAccountUpdate) =>
    request<Carrier>('PUT', `/carriers/${enc(code)}/account`, body),
  putCarrierWarehouses: (code: string, body: WarehouseMapping[]) =>
    request<Carrier>('PUT', `/carriers/${enc(code)}/warehouses`, body),

  listWarehouses: () => request<Warehouse[]>('GET', '/warehouses'),
  createWarehouse: (body: WarehouseIn) => request<Warehouse>('POST', '/warehouses', body),
  updateWarehouse: (id: number, body: WarehouseIn) => request<Warehouse>('PUT', `/warehouses/${id}`, body),

  listRules: () => request<Rule[]>('GET', '/allocation-rules'),
  createRule: (body: RuleIn) => request<Rule>('POST', '/allocation-rules', body),
  updateRule: (id: number, body: RuleIn) => request<Rule>('PUT', `/allocation-rules/${id}`, body),
  deleteRule: (id: number) => request<void>('DELETE', `/allocation-rules/${id}`),

  manualReview: () => request<ReviewItem[]>('GET', '/manual-review'),
  approveReview: (fulfillmentOrderId: number) =>
    request<ApproveResult>('POST', `/manual-review/${fulfillmentOrderId}/approve`),

  listOrders: (params: OrderListParams) => request<Page<OrderRow>>('GET', `/orders${queryString(params)}`),
  getOrder: (orderId: number | string) => request<OrderDetail>('GET', `/orders/${enc(String(orderId))}`),
  setOrderAutomation: (orderId: number, disabled: boolean) =>
    request<OrderAutomationResult>('POST', `/orders/${orderId}/automation`, { disabled }),

  previewAllocation: (fulfillmentOrderId: number) =>
    request<AllocationPreview>('POST', `/fulfillment-orders/${fulfillmentOrderId}/preview-allocation`),
  allocate: (fulfillmentOrderId: number, body: AllocateIn) =>
    request<AllocateResult>('POST', `/fulfillment-orders/${fulfillmentOrderId}/allocate`, body),

  listShipments: (params: ShipmentListParams) =>
    request<Page<ShipmentRow>>('GET', `/shipments${queryString(params)}`),
  cancelShipment: (id: number, body: CancelShipmentIn) =>
    request<ShipmentRow>('POST', `/shipments/${id}/cancel`, body),
  resolveShipment: (id: number, body: ResolveShipmentIn) =>
    request<ShipmentRow>('POST', `/shipments/${id}/resolve`, body),
  syncShipmentToShopify: (id: number) => request<SyncShopifyResult>('POST', `/shipments/${id}/sync-shopify`),
  simulateTracking: (id: number, body: SimulateTrackingIn) =>
    request<SimulateTrackingResult>('POST', `/shipments/${id}/simulate-tracking`, body),

  listTracking: (params: { shipment_id?: string; limit?: number }) =>
    request<TrackingRow[]>('GET', `/tracking${queryString(params)}`),

  listLogs: (params: LogListParams) => request<LogPage>('GET', `/logs${queryString(params)}`),
};
