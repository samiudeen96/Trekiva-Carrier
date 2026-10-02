import { useSearchParams } from 'react-router-dom';
import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading } from '../components/Feedback';
import { carrierName } from '../components/Filters';
import { ShipmentStatusBadge, YesNoBadge } from '../components/StatusBadges';
import { useApi } from '../hooks/useApi';
import type { Carrier, TrackingRow } from '../types';
import { formatDateTime, humanizeCode } from '../utils';

const LIMIT = 100;

const SOURCE_TONE: Record<string, 'info' | 'neutral' | 'caution'> = {
  WEBHOOK: 'info',
  POLL: 'neutral',
  MANUAL: 'caution',
  SYSTEM: 'neutral',
};

/**
 * Tracking events table, used by the Tracking page and the order detail timeline. `showOrder`
 * adds order/carrier/AWB columns (not needed inside a single fulfillment order).
 */
export function TrackingTable({
  events,
  carriers,
  showOrder = true,
}: {
  events: TrackingRow[];
  carriers: readonly Carrier[] | null;
  showOrder?: boolean;
}) {
  return (
    <s-table>
      <s-table-header-row>
        <s-table-header listSlot="kicker">Time</s-table-header>
        {showOrder && <s-table-header listSlot="primary">Order</s-table-header>}
        {showOrder && <s-table-header>Carrier</s-table-header>}
        {showOrder && <s-table-header>AWB</s-table-header>}
        <s-table-header listSlot="inline">Status</s-table-header>
        <s-table-header listSlot={showOrder ? 'secondary' : 'primary'}>Raw status</s-table-header>
        <s-table-header>Location</s-table-header>
        <s-table-header>Source</s-table-header>
        <s-table-header>Applied</s-table-header>
        <s-table-header>Pushed to Shopify</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {events.map((event) => (
          <s-table-row key={event.id}>
            <s-table-cell>{formatDateTime(event.occurred_at)}</s-table-cell>
            {showOrder && (
              <s-table-cell>
                {event.order_id !== null ? (
                  <s-link href={`/orders/${event.order_id}`}>
                    {event.shopify_order_name ?? `Order #${event.order_id}`}
                  </s-link>
                ) : (
                  (event.shopify_order_name ?? '—')
                )}
              </s-table-cell>
            )}
            {showOrder && <s-table-cell>{carrierName(event.carrier_code, carriers)}</s-table-cell>}
            {showOrder && <s-table-cell>{event.awb ?? '—'}</s-table-cell>}
            <s-table-cell>
              <ShipmentStatusBadge status={event.status} />
            </s-table-cell>
            <s-table-cell>
              <s-stack gap="small-500">
                <s-text>{event.raw_status || '—'}</s-text>
                {event.description && <s-text color="subdued">{event.description}</s-text>}
              </s-stack>
            </s-table-cell>
            <s-table-cell>{event.location || '—'}</s-table-cell>
            <s-table-cell>
              <s-badge tone={SOURCE_TONE[event.source] ?? 'neutral'}>{humanizeCode(event.source)}</s-badge>
            </s-table-cell>
            <s-table-cell>
              <YesNoBadge value={event.applied} yes="Applied" no="Not applied" />
            </s-table-cell>
            <s-table-cell>
              <YesNoBadge value={event.pushed_to_shopify} yes="Pushed" no="Not pushed" />
            </s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}

export function TrackingPage() {
  const [params, setParams] = useSearchParams();
  const shipmentId = params.get('shipment_id') ?? '';
  const { data, loading, error, reload } = useApi(
    () => api.listTracking({ shipment_id: shipmentId, limit: LIMIT }),
    [shipmentId],
  );
  const { data: carriers } = useApi(api.listCarriers);

  return (
    <s-page heading="Tracking" inlineSize="large">
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />

        {shipmentId && (
          <s-banner tone="info">
            <s-paragraph>Showing tracking events for shipment #{shipmentId} only.</s-paragraph>
            <s-button slot="secondary-actions" onClick={() => setParams(new URLSearchParams())}>
              Show all events
            </s-button>
          </s-banner>
        )}

        <s-section>
          <s-stack gap="base">
            <s-paragraph color="subdued">
              Carrier tracking updates (webhooks and polling), normalised to Trekiva statuses. &quot;Applied&quot; events
              advanced the shipment; &quot;Pushed&quot; events were sent to Shopify as fulfillment tracking updates.
              Showing the latest {LIMIT}.
            </s-paragraph>

            {loading && !data && <Loading label="Loading tracking events" />}

            {data && data.length === 0 && (
              <EmptyMessage heading="No tracking events yet">
                Events appear once carriers start reporting on created shipments.
              </EmptyMessage>
            )}

            {data && data.length > 0 && <TrackingTable events={data} carriers={carriers} />}
          </s-stack>
        </s-section>
      </s-stack>
    </s-page>
  );
}
