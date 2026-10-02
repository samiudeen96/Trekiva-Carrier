import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading } from '../components/Feedback';
import { CarrierFilterSelect, PageSummary, SearchInput, carrierName } from '../components/Filters';
import { AwbLink } from '../components/ShipmentsTable';
import {
  FulfillmentStatusBadge,
  LOGISTICS_STATUSES,
  LogisticsStatusBadge,
  PaymentBadge,
  ShipmentStatusBadge,
} from '../components/StatusBadges';
import { useApi } from '../hooks/useApi';
import { useSearchFilters } from '../hooks/useSearchFilters';
import type { OrderRow } from '../types';
import { formatDate, formatDateTime, humanizeCode } from '../utils';

const PAGE_SIZE = 50;
const FILTER_KEYS = ['status', 'carrier', 'q'] as const;

function destination(row: OrderRow): string {
  const { city, pincode } = row.destination ?? { city: null, pincode: null };
  return [city, pincode].filter(Boolean).join(', ') || '—';
}

export function OrdersPage() {
  const filters = useSearchFilters(FILTER_KEYS);
  const { status, carrier, q } = filters.values;
  const { offset } = filters;

  const { data, loading, error, reload } = useApi(
    () => api.listOrders({ status, carrier, q, limit: PAGE_SIZE, offset }),
    [status, carrier, q, offset],
  );
  const { data: carriers } = useApi(api.listCarriers);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const limit = data?.limit || PAGE_SIZE;

  return (
    <s-page heading="Orders" inlineSize="large">
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />

        <s-section>
          <s-stack gap="base">
            <s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base" alignItems="end">
              <s-select label="Review status" value={status} onChange={(e) => filters.setFilter('status', e.currentTarget.value)}>
                <s-option value="">All statuses</s-option>
                {LOGISTICS_STATUSES.map((s) => (
                  <s-option key={s} value={s}>
                    {humanizeCode(s)}
                  </s-option>
                ))}
              </s-select>
              <CarrierFilterSelect carriers={carriers} value={carrier} onChange={(v) => filters.setFilter('carrier', v)} />
              <SearchInput
                label="Search"
                placeholder="Order, phone, customer, AWB or pincode"
                value={q}
                onSearch={(v) => filters.setFilter('q', v)}
              />
            </s-grid>
            <s-stack direction="inline" gap="base" alignItems="center" justifyContent="space-between">
              <PageSummary offset={data?.offset ?? offset} count={items.length} total={total} />
              {filters.active && (
                <s-button variant="tertiary" onClick={filters.clear}>
                  Clear filters
                </s-button>
              )}
            </s-stack>

            {loading && !data && <Loading label="Loading orders" />}

            {data && items.length === 0 && (
              <EmptyMessage heading={filters.active ? 'No matching orders' : 'No orders yet'}>
                {filters.active
                  ? 'Try a different status, carrier or search.'
                  : 'Orders appear here as soon as Shopify sends them to Trekiva.'}
              </EmptyMessage>
            )}

            {data && items.length > 0 && (
              <s-table
                loading={loading}
                paginate
                hasPreviousPage={offset > 0}
                hasNextPage={offset + items.length < total}
                onPreviousPage={() => filters.setOffset(Math.max(0, offset - limit))}
                onNextPage={() => filters.setOffset(offset + limit)}
              >
                <s-table-header-row>
                  <s-table-header listSlot="primary">Shopify Order</s-table-header>
                  <s-table-header listSlot="secondary">Customer</s-table-header>
                  <s-table-header>Phone</s-table-header>
                  <s-table-header>Destination</s-table-header>
                  <s-table-header listSlot="inline">Payment Type</s-table-header>
                  <s-table-header>Fulfillment Status</s-table-header>
                  <s-table-header listSlot="inline">Review Status</s-table-header>
                  <s-table-header>Selected Carrier</s-table-header>
                  <s-table-header listSlot="kicker">AWB</s-table-header>
                  <s-table-header>Shipment Status</s-table-header>
                  <s-table-header>EDD</s-table-header>
                  <s-table-header>Actions</s-table-header>
                </s-table-header-row>
                <s-table-body>
                  {items.map((row) => (
                    <s-table-row key={row.fulfillment_order_id}>
                      <s-table-cell>
                        <s-stack gap="small-500">
                          <s-link href={`/orders/${row.order_id}`}>{row.order_name}</s-link>
                          <s-text color="subdued">{formatDateTime(row.created_at)}</s-text>
                        </s-stack>
                      </s-table-cell>
                      <s-table-cell>{row.customer_name || '—'}</s-table-cell>
                      <s-table-cell>{row.phone || '—'}</s-table-cell>
                      <s-table-cell>{destination(row)}</s-table-cell>
                      <s-table-cell>
                        <PaymentBadge mode={row.payment_mode} />
                      </s-table-cell>
                      <s-table-cell>
                        <FulfillmentStatusBadge status={row.fulfillment_status} />
                      </s-table-cell>
                      <s-table-cell>
                        <s-stack gap="small-500">
                          <s-stack direction="inline" gap="small-200">
                            <LogisticsStatusBadge status={row.logistics_status} />
                            {row.automation_disabled && row.logistics_status !== 'AUTOMATION_DISABLED' && (
                              <s-badge tone="caution">Automation off</s-badge>
                            )}
                          </s-stack>
                          {(row.status_detail || row.status_reason) && (
                            <s-text color="subdued">
                              {row.status_detail || humanizeCode(row.status_reason ?? '')}
                            </s-text>
                          )}
                        </s-stack>
                      </s-table-cell>
                      <s-table-cell>{carrierName(row.selected_carrier, carriers)}</s-table-cell>
                      <s-table-cell>
                        <AwbLink awb={row.shipment?.awb ?? null} trackingUrl={row.shipment?.tracking_url ?? null} />
                      </s-table-cell>
                      <s-table-cell>
                        <ShipmentStatusBadge status={row.shipment?.status} />
                      </s-table-cell>
                      <s-table-cell>{formatDate(row.shipment?.estimated_delivery_date)}</s-table-cell>
                      <s-table-cell>
                        <s-link href={`/orders/${row.order_id}`} accessibilityLabel={`View ${row.order_name}`}>
                          View
                        </s-link>
                      </s-table-cell>
                    </s-table-row>
                  ))}
                </s-table-body>
              </s-table>
            )}
          </s-stack>
        </s-section>
      </s-stack>
    </s-page>
  );
}
