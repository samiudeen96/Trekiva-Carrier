/** Status vocabularies and the badges that display them, shared by the order/shipment pages. */
import type { LogLevel, LogisticsStatus, ShipmentStatus, ShopifySyncStatus, TrackingStatus } from '../types';
import { humanizeCode } from '../utils';

export type BadgeTone = 'auto' | 'neutral' | 'info' | 'success' | 'caution' | 'warning' | 'critical';

export const LOGISTICS_STATUSES: readonly LogisticsStatus[] = [
  'RECEIVED',
  'SETTLING',
  'MANUAL_REVIEW',
  'AWAITING_ALLOCATION',
  'ALLOCATING',
  'ALLOCATED',
  'NO_CARRIER_AVAILABLE',
  'SHIPMENT_PENDING',
  'RECONCILING',
  'SHIPPED',
  'FAILED',
  'SKIPPED',
  'AUTOMATION_DISABLED',
  'CANCELLED',
];

export const SHIPMENT_STATUSES: readonly ShipmentStatus[] = [
  'CREATING',
  'CREATION_UNKNOWN',
  'CREATE_FAILED',
  'AWB_CREATED',
  'PICKUP_SCHEDULED',
  'PICKED_UP',
  'IN_TRANSIT',
  'OUT_FOR_DELIVERY',
  'DELIVERED',
  'DELIVERY_FAILED',
  'RTO_INITIATED',
  'RTO_IN_TRANSIT',
  'RTO_DELIVERED',
  'CANCEL_REQUESTED',
  'CANCELLED',
  'EXCEPTION',
];

export const TRACKING_STATUSES: readonly TrackingStatus[] = [
  'AWB_CREATED',
  'PICKUP_SCHEDULED',
  'PICKED_UP',
  'IN_TRANSIT',
  'OUT_FOR_DELIVERY',
  'DELIVERED',
  'DELIVERY_FAILED',
  'RTO_INITIATED',
  'RTO_IN_TRANSIT',
  'RTO_DELIVERED',
  'CANCELLED',
  'EXCEPTION',
];

const LOGISTICS_TONE: Record<LogisticsStatus, BadgeTone> = {
  RECEIVED: 'neutral',
  SETTLING: 'info',
  MANUAL_REVIEW: 'warning',
  AWAITING_ALLOCATION: 'info',
  ALLOCATING: 'info',
  ALLOCATED: 'info',
  NO_CARRIER_AVAILABLE: 'critical',
  SHIPMENT_PENDING: 'info',
  RECONCILING: 'caution',
  SHIPPED: 'success',
  FAILED: 'critical',
  SKIPPED: 'neutral',
  AUTOMATION_DISABLED: 'caution',
  CANCELLED: 'neutral',
};

const SHIPMENT_TONE: Record<ShipmentStatus, BadgeTone> = {
  CREATING: 'info',
  CREATION_UNKNOWN: 'warning',
  CREATE_FAILED: 'critical',
  AWB_CREATED: 'info',
  PICKUP_SCHEDULED: 'info',
  PICKED_UP: 'info',
  IN_TRANSIT: 'info',
  OUT_FOR_DELIVERY: 'info',
  DELIVERED: 'success',
  DELIVERY_FAILED: 'warning',
  RTO_INITIATED: 'caution',
  RTO_IN_TRANSIT: 'caution',
  RTO_DELIVERED: 'caution',
  CANCEL_REQUESTED: 'caution',
  CANCELLED: 'neutral',
  EXCEPTION: 'critical',
};

const SHOPIFY_FO_TONE: Record<string, BadgeTone> = {
  OPEN: 'info',
  IN_PROGRESS: 'info',
  SCHEDULED: 'neutral',
  ON_HOLD: 'warning',
  INCOMPLETE: 'warning',
  CLOSED: 'success',
  CANCELLED: 'neutral',
};

const SYNC_TONE: Record<ShopifySyncStatus, BadgeTone> = {
  PENDING: 'info',
  SYNCED: 'success',
  FAILED: 'critical',
};

export const LEVEL_TONE: Record<LogLevel, BadgeTone> = {
  INFO: 'info',
  WARNING: 'warning',
  ERROR: 'critical',
};

function toneOf(map: Record<string, BadgeTone>, value: string): BadgeTone {
  return map[value] ?? 'neutral';
}

export function LogisticsStatusBadge({ status }: { status: string }) {
  return <s-badge tone={toneOf(LOGISTICS_TONE, status)}>{humanizeCode(status)}</s-badge>;
}

/** Shipment and (normalised) tracking statuses share one vocabulary. */
export function ShipmentStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <s-text color="subdued">—</s-text>;
  return <s-badge tone={toneOf(SHIPMENT_TONE, status)}>{humanizeCode(status)}</s-badge>;
}

export function FulfillmentStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <s-text color="subdued">—</s-text>;
  return <s-badge tone={toneOf(SHOPIFY_FO_TONE, status)}>{humanizeCode(status)}</s-badge>;
}

export function PaymentBadge({ mode }: { mode: string | null | undefined }) {
  if (!mode || mode === 'UNKNOWN') return <s-badge tone="neutral">Unknown</s-badge>;
  return <s-badge tone={mode === 'COD' ? 'caution' : 'success'}>{mode === 'COD' ? 'COD' : humanizeCode(mode)}</s-badge>;
}

export function SyncBadge({ status }: { status: string }) {
  return <s-badge tone={toneOf(SYNC_TONE, status)}>{humanizeCode(status)}</s-badge>;
}

export function LevelBadge({ level }: { level: string }) {
  return <s-badge tone={toneOf(LEVEL_TONE, level)}>{level}</s-badge>;
}

export function YesNoBadge({
  value,
  yes = 'Yes',
  no = 'No',
  noTone = 'neutral',
}: {
  value: boolean | null | undefined;
  yes?: string;
  no?: string;
  noTone?: BadgeTone;
}) {
  if (value === null || value === undefined) return <s-text color="subdued">—</s-text>;
  return value ? <s-badge tone="success">{yes}</s-badge> : <s-badge tone={noTone}>{no}</s-badge>;
}
