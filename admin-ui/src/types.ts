/** Types mirroring the backend `/api/admin/*` contract. */

/** Pydantic serialises `Decimal` as a string ("75.00"); be lenient and accept numbers too. */
export type Decimal = string | number;

export interface SessionInfo {
  shop_domain: string;
  user_id: string | null;
  automation_enabled: boolean;
  app_env: string;
  mock_carriers_enabled: boolean;
  api_version: string;
}

// --- dashboard ---------------------------------------------------------------------------

export type LogLevel = 'INFO' | 'WARNING' | 'ERROR';

export interface ActivityItem {
  id: number;
  created_at: string;
  step: string;
  level: LogLevel;
  message: string;
  order_id: number | null;
}

export interface Dashboard {
  automation_enabled: boolean;
  orders_today: number;
  shipments_created_today: number;
  manual_review: number;
  awaiting_allocation: number;
  failed: number;
  delivered: number;
  rto: number;
  carrier_breakdown: { carrier_code: string; shipments: number }[];
  recent_activity: ActivityItem[];
}

// --- settings ----------------------------------------------------------------------------

export type FulfillOn = 'AWB_CREATED' | 'PICKED_UP';

export interface ShopSettings {
  timezone: string;
  settle_window_seconds: number;
  wait_for_risk_analysis: boolean;
  risk_wait_max_seconds: number;
  review_tags: string[];
  block_on_high_risk: boolean;
  location_check_enabled: boolean;
  location_check_ip: boolean;
  location_check_billing: boolean;
  risk_review_tag: string;
  duplicate_check_enabled: boolean;
  duplicate_window_hours: number;
  duplicate_review_tag: string;
  cod_gateway_names: string[];
  fulfill_on: FulfillOn;
  notify_customer: boolean;
  shipped_tag: string | null;
  max_create_attempts_per_carrier: number;
  cancel_shipment_on_order_cancel: boolean;
}

export interface SettingsResponse {
  automation_enabled: boolean;
  settings: ShopSettings;
}

export interface SettingsUpdate {
  automation_enabled?: boolean;
  settings?: ShopSettings;
}

// --- carriers ----------------------------------------------------------------------------

export type CarrierEnvironment = 'MOCK' | 'SANDBOX' | 'PRODUCTION';

/** A (subset of a) JSON Schema document as produced by Pydantic's `model_json_schema()`. */
export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  format?: string;
  writeOnly?: boolean;
  default?: unknown;
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  additionalProperties?: boolean | JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  minimum?: number;
  maximum?: number;
}

export interface CarrierSettings {
  enabled: boolean;
  cod_enabled: boolean;
  prepaid_enabled: boolean;
  priority: number;
  max_shipping_cost: Decimal | null;
  min_weight_g: number | null;
  max_weight_g: number | null;
  performance_score: Decimal | null;
  active_account_id: number | null;
}

export interface CarrierSettingsUpdate {
  enabled?: boolean;
  cod_enabled?: boolean;
  prepaid_enabled?: boolean;
  priority?: number;
  max_shipping_cost?: string | null;
  min_weight_g?: number | null;
  max_weight_g?: number | null;
  performance_score?: string | null;
}

export interface CarrierAccount {
  id: number;
  environment: CarrierEnvironment;
  label: string;
  credentials_hint: Record<string, unknown>;
  is_active: boolean;
  webhook_path: string;
  updated_at: string;
}

export interface WarehouseMapping {
  warehouse_id: number;
  carrier_warehouse_ref: string | null;
  enabled: boolean;
}

export interface Carrier {
  code: string;
  display_name: string;
  has_real_adapter: boolean;
  has_mock_adapter: boolean;
  implementation_status: 'ready' | 'awaiting_api_docs';
  capabilities: Record<string, boolean>;
  credentials_schema: JsonSchema;
  mock_credentials_schema: JsonSchema | null;
  settings: CarrierSettings;
  accounts: CarrierAccount[];
  warehouse_mappings: WarehouseMapping[];
}

export interface CarrierAccountUpdate {
  environment: CarrierEnvironment;
  label: string;
  credentials: Record<string, unknown>;
  is_active: boolean;
  make_active: boolean;
}

// --- warehouses --------------------------------------------------------------------------

export interface PackageDefaults {
  length_cm?: Decimal | null;
  breadth_cm?: Decimal | null;
  height_cm?: Decimal | null;
  min_weight_g?: number | null;
}

export interface WarehouseIn {
  code: string;
  name: string;
  contact_name?: string | null;
  phone: string;
  email?: string | null;
  address1: string;
  address2?: string | null;
  city: string;
  state: string;
  pincode: string;
  country: string;
  shopify_location_id?: string | null;
  default_package: PackageDefaults;
  is_active: boolean;
}

export interface Warehouse extends WarehouseIn {
  id: number;
}

// --- allocation rules --------------------------------------------------------------------

export type AllocationStrategy = 'FASTEST' | 'CHEAPEST' | 'BALANCED' | 'PRIORITY' | 'CUSTOM';

export interface BalancedWeights {
  speed: number;
  cost: number;
  performance: number;
  priority: number;
}

export interface RuleParams {
  max_cost?: Decimal;
  max_edd_days?: number;
  min_performance_score?: Decimal;
  fallback_carrier?: string;
  carrier_order?: string[];
  only_carriers?: string[];
  weights?: BalancedWeights;
}

export interface RuleIn {
  name: string;
  priority: number;
  is_active: boolean;
  strategy: AllocationStrategy;
  conditions: Record<string, unknown>;
  params: RuleParams;
}

export interface Rule extends RuleIn {
  id: number;
  updated_at: string;
}

// --- manual review -----------------------------------------------------------------------

export interface ShippingAddress {
  name?: string | null;
  address1?: string | null;
  address2?: string | null;
  city?: string | null;
  province?: string | null;
  zip?: string | null;
  country?: string | null;
  phone?: string | null;
  [key: string]: unknown;
}

export interface HoldReason {
  reason: string;
  reason_notes: string | null;
  display_reason: string | null;
}

export interface ReviewLineItem {
  name: string;
  sku: string | null;
  quantity: number;
}

export interface ReviewItem {
  fulfillment_order_id: number;
  shopify_fulfillment_order_id: string;
  order_id: number;
  shopify_order_id: string;
  order_name: string;
  order_created_at: string | null;
  tags: string[];
  customer_name: string | null;
  phone: string | null;
  shipping_address: ShippingAddress | null;
  payment_mode: string | null;
  risk_level: string | null;
  shopify_status: string | null;
  hold_reasons: HoldReason[];
  reason: string | null;
  detail: string | null;
  is_shopify_hold: boolean;
  can_override: boolean;
  line_items: ReviewLineItem[];
  updated_at: string | null;
}

export interface ApproveResult {
  approved: boolean;
  recheck_queued: boolean;
}

// --- shared list envelope ----------------------------------------------------------------

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// --- shipments ---------------------------------------------------------------------------

export type ShipmentStatus =
  | 'CREATING'
  | 'CREATION_UNKNOWN'
  | 'CREATE_FAILED'
  | 'AWB_CREATED'
  | 'PICKUP_SCHEDULED'
  | 'PICKED_UP'
  | 'IN_TRANSIT'
  | 'OUT_FOR_DELIVERY'
  | 'DELIVERED'
  | 'DELIVERY_FAILED'
  | 'RTO_INITIATED'
  | 'RTO_IN_TRANSIT'
  | 'RTO_DELIVERED'
  | 'CANCEL_REQUESTED'
  | 'CANCELLED'
  | 'EXCEPTION';

export type ShopifySyncStatus = 'PENDING' | 'SYNCED' | 'FAILED';

export interface ShipmentRow {
  id: number;
  order_id: number;
  fulfillment_order_id: number;
  shopify_order_name: string | null;
  carrier_code: string;
  carrier_shipment_id: string | null;
  awb: string | null;
  tracking_url: string | null;
  label_url: string | null;
  shipping_cost: Decimal | null;
  currency: string | null;
  cod_amount: Decimal | null;
  estimated_delivery_date: string | null;
  status: ShipmentStatus;
  attempt: number;
  idempotency_key: string;
  shopify_sync_status: ShopifySyncStatus;
  shopify_sync_error: string | null;
  shopify_fulfillment_id: string | null;
  last_error: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
  delivered_at: string | null;
  cancelled_at: string | null;
  cancel_reason: string | null;
  can_cancel: boolean;
  can_resolve: boolean;
  can_retry_shopify_sync: boolean;
}

export interface ShipmentListParams {
  status?: string;
  carrier?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface CancelShipmentIn {
  reallocate: boolean;
  reason: string;
}

export interface ResolveShipmentIn {
  created: boolean;
  awb?: string;
  carrier_shipment_id?: string;
}

export interface SyncShopifyResult {
  outcome: string;
  detail: string | null;
  shipment: ShipmentRow;
}

/** Normalised tracking statuses accepted by the mock-carrier simulator. */
export type TrackingStatus =
  | 'AWB_CREATED'
  | 'PICKUP_SCHEDULED'
  | 'PICKED_UP'
  | 'IN_TRANSIT'
  | 'OUT_FOR_DELIVERY'
  | 'DELIVERED'
  | 'DELIVERY_FAILED'
  | 'RTO_INITIATED'
  | 'RTO_IN_TRANSIT'
  | 'RTO_DELIVERED'
  | 'CANCELLED'
  | 'EXCEPTION';

export interface SimulateTrackingIn {
  status: TrackingStatus;
  location?: string;
}

export interface SimulateTrackingResult {
  applied: boolean;
  shipment: ShipmentRow;
}

// --- tracking ----------------------------------------------------------------------------

export type TrackingSource = 'WEBHOOK' | 'POLL' | 'MANUAL' | 'SYSTEM';

export interface TrackingRow {
  id: number;
  shipment_id: number;
  order_id: number | null;
  shopify_order_name: string | null;
  carrier_code: string;
  awb: string | null;
  raw_status: string | null;
  status: string;
  description: string | null;
  location: string | null;
  occurred_at: string;
  source: TrackingSource;
  applied: boolean;
  pushed_to_shopify: boolean;
}

// --- automation logs ---------------------------------------------------------------------

export interface LogRow {
  id: number;
  created_at: string;
  order_id: number | null;
  fulfillment_order_id: number | null;
  shipment_id: number | null;
  step: string;
  level: LogLevel;
  message: string;
  actor: string | null;
  data: Record<string, unknown> | null;
}

export interface LogPage {
  items: LogRow[];
  total: number;
}

export interface LogListParams {
  order_id?: string;
  level?: string;
  step?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

// --- orders ------------------------------------------------------------------------------

export type LogisticsStatus =
  | 'RECEIVED'
  | 'SETTLING'
  | 'MANUAL_REVIEW'
  | 'AWAITING_ALLOCATION'
  | 'ALLOCATING'
  | 'ALLOCATED'
  | 'NO_CARRIER_AVAILABLE'
  | 'SHIPMENT_PENDING'
  | 'RECONCILING'
  | 'SHIPPED'
  | 'FAILED'
  | 'SKIPPED'
  | 'AUTOMATION_DISABLED'
  | 'CANCELLED';

export type PaymentMode = 'COD' | 'PREPAID' | 'UNKNOWN';

export interface OrderRow {
  order_id: number;
  fulfillment_order_id: number;
  shopify_order_id: string;
  order_name: string;
  created_at: string | null;
  customer_name: string | null;
  phone: string | null;
  destination: { city: string | null; state: string | null; pincode: string | null };
  payment_mode: PaymentMode;
  total_price: Decimal | null;
  fulfillment_status: string | null;
  logistics_status: LogisticsStatus;
  status_reason: string | null;
  status_detail: string | null;
  selected_carrier: string | null;
  shipment: ShipmentRow | null;
  automation_disabled: boolean;
}

export interface OrderListParams {
  status?: string;
  carrier?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface FOLineItem {
  name: string;
  sku: string | null;
  remaining_quantity: number;
  total_quantity: number;
  unit_weight_g: number | null;
}

export interface RankedCarrier {
  carrier_code: string;
  cost: Decimal | null;
  currency: string | null;
  edd: string | null;
  days: number | null;
  score: Decimal | number | null;
  failed: boolean;
}

export interface RunQuote {
  carrier_code: string;
  eligible: boolean;
  selected: boolean;
  rank: number | null;
  cost: Decimal | null;
  currency: string | null;
  transit_days: number | null;
  edd: string | null;
  score: Decimal | number | null;
  rejection_reason: string | null;
  rejection_detail: string | null;
  strategy: string | null;
}

export interface ServiceabilityCheck {
  carrier_code: string;
  serviceable: boolean | null;
  cod_available: boolean | null;
  error_class: string | null;
  error_message: string | null;
  duration_ms: number | null;
  request: unknown;
  response: unknown;
  created_at: string;
}

export interface LatestRun {
  run_id: string | number | null;
  quotes: RunQuote[];
  checks: ServiceabilityCheck[];
}

export interface FODetail {
  id: number;
  shopify_fulfillment_order_id: string;
  shopify_status: string | null;
  logistics_status: LogisticsStatus;
  status_reason: string | null;
  status_detail: string | null;
  hold_reasons: HoldReason[];
  line_items: FOLineItem[];
  weight_g: number | null;
  warehouse_id: number | null;
  selected_carrier: string | null;
  forced_carrier: string | null;
  allocation_ranking: RankedCarrier[] | null;
  can_reallocate: boolean;
  shipments: ShipmentRow[];
  latest_run: LatestRun | null;
  tracking: TrackingRow[];
}

export interface OrderDetail {
  id: number;
  shopify_order_id: string;
  name: string;
  created_at: string | null;
  cancelled_at: string | null;
  customer_name: string | null;
  phone: string | null;
  email: string | null;
  shipping_address: ShippingAddress | null;
  payment_mode: PaymentMode;
  payment_gateways: string[];
  total_price: Decimal | null;
  outstanding_amount: Decimal | null;
  currency: string | null;
  tags: string[];
  risk_level: string | null;
  automation_disabled: boolean;
  fulfillment_orders: FODetail[];
  logs: LogRow[];
}

export interface AllocationPreview {
  run_id: string | number | null;
  /** The matched allocation rule (name, or an object with `name`/`id`), if any. */
  rule: unknown;
  strategy: string | null;
  selected: string | null;
  explanation: string | null;
  ranked: RankedCarrier[];
  rejected: { carrier_code: string; reason: string; detail: string | null }[];
}

export interface AllocateIn {
  carrier_code?: string;
}

export interface AllocateResult {
  queued: boolean;
  fulfillment_order_id: number;
  forced_carrier: string | null;
}

export interface OrderAutomationResult {
  order_id: number;
  automation_disabled: boolean;
}
