import type { RefObject } from 'react';
import type { Carrier, ShipmentRow } from '../types';
import { formatDate, formatDateTime, formatMoney } from '../utils';
import { carrierName, type TablePagination } from './Filters';
import { ShipmentActionButtons, type ShipmentActionsHandle } from './ShipmentActions';
import { ShipmentStatusBadge, SyncBadge } from './StatusBadges';

/** AWB, linked to the carrier's tracking page when one is known. */
export function AwbLink({ awb, trackingUrl }: { awb: string | null; trackingUrl: string | null }) {
  if (!awb) return <s-text color="subdued">—</s-text>;
  if (!trackingUrl) return <s-text fontVariantNumeric="tabular-nums">{awb}</s-text>;
  return (
    <s-link href={trackingUrl} target="_blank">
      {awb}
    </s-link>
  );
}

export function ShipmentsTable({
  shipments,
  carriers,
  actions,
  showOrder = true,
  loading = false,
  pagination,
}: {
  shipments: ShipmentRow[];
  carriers: readonly Carrier[] | null;
  actions: RefObject<ShipmentActionsHandle | null>;
  showOrder?: boolean;
  loading?: boolean;
  pagination?: TablePagination;
}) {
  return (
    <s-table
      loading={loading}
      paginate={pagination !== undefined}
      hasPreviousPage={pagination?.hasPreviousPage}
      hasNextPage={pagination?.hasNextPage}
      onPreviousPage={pagination?.onPreviousPage}
      onNextPage={pagination?.onNextPage}
    >
      <s-table-header-row>
        {showOrder && <s-table-header listSlot="primary">Order</s-table-header>}
        <s-table-header listSlot={showOrder ? 'secondary' : 'primary'}>Carrier</s-table-header>
        <s-table-header listSlot="kicker">AWB</s-table-header>
        <s-table-header listSlot="inline">Status</s-table-header>
        <s-table-header format="currency">Cost</s-table-header>
        <s-table-header format="currency">COD amount</s-table-header>
        <s-table-header>EDD</s-table-header>
        <s-table-header>Shopify sync</s-table-header>
        <s-table-header>Created</s-table-header>
        <s-table-header>Actions</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {shipments.map((s) => (
          <s-table-row key={s.id}>
            {showOrder && (
              <s-table-cell>
                <s-link href={`/orders/${s.order_id}`}>{s.shopify_order_name ?? `Order #${s.order_id}`}</s-link>
              </s-table-cell>
            )}
            <s-table-cell>
              <s-stack gap="small-500">
                <s-text>{carrierName(s.carrier_code, carriers)}</s-text>
                {s.attempt > 1 && <s-text color="subdued">Attempt {s.attempt}</s-text>}
              </s-stack>
            </s-table-cell>
            <s-table-cell>
              <s-stack gap="small-500">
                <AwbLink awb={s.awb} trackingUrl={s.tracking_url} />
                <s-stack direction="inline" gap="small-200">
                  {s.label_url && (
                    <s-link href={s.label_url} target="_blank">
                      Label
                    </s-link>
                  )}
                  <s-link href={`/tracking?shipment_id=${s.id}`}>Events</s-link>
                </s-stack>
              </s-stack>
            </s-table-cell>
            <s-table-cell>
              <s-stack gap="small-500">
                <ShipmentStatusBadge status={s.status} />
                {s.last_error && <s-text color="subdued">{s.last_error}</s-text>}
                {s.cancel_reason && <s-text color="subdued">Cancelled: {s.cancel_reason}</s-text>}
              </s-stack>
            </s-table-cell>
            <s-table-cell>{formatMoney(s.shipping_cost, s.currency)}</s-table-cell>
            <s-table-cell>{formatMoney(s.cod_amount, s.currency)}</s-table-cell>
            <s-table-cell>{formatDate(s.estimated_delivery_date)}</s-table-cell>
            <s-table-cell>
              <s-stack gap="small-500">
                <SyncBadge status={s.shopify_sync_status} />
                {s.shopify_sync_error && <s-text tone="critical">{s.shopify_sync_error}</s-text>}
              </s-stack>
            </s-table-cell>
            <s-table-cell>{formatDateTime(s.created_at)}</s-table-cell>
            <s-table-cell>
              <ShipmentActionButtons shipment={s} actions={actions} />
            </s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}
