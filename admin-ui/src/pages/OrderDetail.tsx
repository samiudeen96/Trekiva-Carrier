import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '../api';
import { ConfirmModal, type ConfirmModalHandle } from '../components/ConfirmModal';
import { EmptyMessage, ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import { JsonDetails, carrierName } from '../components/Filters';
import { ShipmentActionModals, type ShipmentActionsHandle } from '../components/ShipmentActions';
import { ShipmentsTable } from '../components/ShipmentsTable';
import {
  FulfillmentStatusBadge,
  LogisticsStatusBadge,
  PaymentBadge,
  YesNoBadge,
} from '../components/StatusBadges';
import { useApi } from '../hooks/useApi';
import { useNotice } from '../hooks/useNotice';
import type {
  AllocationPreview,
  Carrier,
  FODetail,
  LatestRun,
  OrderDetail,
  RankedCarrier,
  ServiceabilityCheck,
  ShippingAddress,
} from '../types';
import { formatDate, formatDateTime, formatMoney, gidToNumericId, humanizeCode, parseId } from '../utils';
import { LogsTable } from './AutomationLogs';
import { TrackingTable } from './Tracking';

/** Delay before re-reading the order after queueing an allocation (the worker runs async). */
const REFETCH_AFTER_QUEUE_MS = 2000;

const RETRY_STATUSES = new Set(['FAILED', 'NO_CARRIER_AVAILABLE']);

function addressLines(address: ShippingAddress | null): string[] {
  if (!address) return [];
  const cityLine = [address.city, address.province, address.zip].filter(Boolean).join(', ');
  return [address.name, address.address1, address.address2, cityLine, address.country].filter(
    (line): line is string => typeof line === 'string' && line.trim() !== '',
  );
}

function foLabel(fo: FODetail): string {
  return `Fulfillment order #${gidToNumericId(fo.shopify_fulfillment_order_id) ?? fo.id}`;
}

function formatScore(score: string | number | null | undefined): string {
  if (score === null || score === undefined || score === '') return '—';
  const n = Number(score);
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : String(score);
}

function ruleLabel(rule: unknown): string | null {
  if (rule === null || rule === undefined || rule === '') return null;
  if (typeof rule === 'string' || typeof rule === 'number') return String(rule);
  if (typeof rule === 'object') {
    const r = rule as { name?: unknown; id?: unknown };
    if (typeof r.name === 'string') return r.name;
    if (r.id !== undefined) return `Rule #${String(r.id)}`;
  }
  return JSON.stringify(rule);
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <s-stack gap="small-500">
      <s-text color="subdued">{label}</s-text>
      <s-text>{children}</s-text>
    </s-stack>
  );
}

// --- order-level sections ----------------------------------------------------------------

function CustomerSection({ order }: { order: OrderDetail }) {
  const lines = addressLines(order.shipping_address);
  const phone = order.phone ?? order.shipping_address?.phone ?? null;
  return (
    <s-section heading="Customer & address">
      <s-stack gap="small">
        <s-text type="strong">{order.customer_name || 'No customer name'}</s-text>
        <s-text color="subdued">{phone || 'No phone'}</s-text>
        {order.email && <s-text color="subdued">{order.email}</s-text>}
        {lines.length === 0 ? (
          <s-text color="subdued">No shipping address</s-text>
        ) : (
          <s-text type="address">
            {lines.map((line, index) => (
              <span key={index}>
                {line}
                <br />
              </span>
            ))}
          </s-text>
        )}
      </s-stack>
    </s-section>
  );
}

function PaymentSection({ order }: { order: OrderDetail }) {
  const gateways = order.payment_gateways ?? [];
  return (
    <s-section heading="Payment">
      <s-stack gap="small">
        <s-stack direction="inline" gap="small-200" alignItems="center">
          <PaymentBadge mode={order.payment_mode} />
          {order.risk_level && (
            <s-badge tone={order.risk_level.toUpperCase() === 'HIGH' ? 'critical' : 'neutral'}>
              Risk: {humanizeCode(order.risk_level)}
            </s-badge>
          )}
        </s-stack>
        <Fact label="Gateways">{gateways.length ? gateways.join(', ') : '—'}</Fact>
        <Fact label="Total">{formatMoney(order.total_price, order.currency)}</Fact>
        <Fact label="Outstanding">{formatMoney(order.outstanding_amount, order.currency)}</Fact>
      </s-stack>
    </s-section>
  );
}

function TagsSection({ order }: { order: OrderDetail }) {
  const tags = order.tags ?? [];
  return (
    <s-section heading="Tags">
      {tags.length === 0 ? (
        <s-text color="subdued">No tags</s-text>
      ) : (
        <s-stack direction="inline" gap="small-200">
          {tags.map((tag) => (
            <s-badge key={tag}>{tag}</s-badge>
          ))}
        </s-stack>
      )}
    </s-section>
  );
}

// --- allocation --------------------------------------------------------------------------

function RankingTable({ ranking, carriers }: { ranking: RankedCarrier[]; carriers: readonly Carrier[] | null }) {
  return (
    <s-table>
      <s-table-header-row>
        <s-table-header format="numeric">Rank</s-table-header>
        <s-table-header listSlot="primary">Carrier</s-table-header>
        <s-table-header format="currency">Cost</s-table-header>
        <s-table-header format="numeric">Days</s-table-header>
        <s-table-header>EDD</s-table-header>
        <s-table-header format="numeric">Score</s-table-header>
        <s-table-header listSlot="inline">Attempt</s-table-header>
      </s-table-header-row>
      <s-table-body>
        {ranking.map((r, index) => (
          <s-table-row key={`${r.carrier_code}-${index}`}>
            <s-table-cell>{index + 1}</s-table-cell>
            <s-table-cell>{carrierName(r.carrier_code, carriers)}</s-table-cell>
            <s-table-cell>{formatMoney(r.cost, r.currency)}</s-table-cell>
            <s-table-cell>{r.days ?? '—'}</s-table-cell>
            <s-table-cell>{formatDate(r.edd)}</s-table-cell>
            <s-table-cell>{formatScore(r.score)}</s-table-cell>
            <s-table-cell>
              {r.failed ? <s-badge tone="critical">Failed</s-badge> : <s-text color="subdued">—</s-text>}
            </s-table-cell>
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}

function PreviewResult({
  preview,
  carriers,
  onDismiss,
}: {
  preview: AllocationPreview;
  carriers: readonly Carrier[] | null;
  onDismiss: () => void;
}) {
  const rule = ruleLabel(preview.rule);
  return (
    <s-section heading="Serviceability check result">
      <s-button slot="secondary-actions" variant="tertiary" onClick={onDismiss}>
        Dismiss
      </s-button>
      <s-stack gap="base">
        <s-banner tone={preview.selected ? 'success' : 'warning'}>
          <s-paragraph>
            {preview.selected
              ? `Would ship with ${carrierName(preview.selected, carriers)}.`
              : 'No carrier would be selected.'}{' '}
            {preview.explanation}
          </s-paragraph>
          <s-paragraph color="subdued">Preview only — nothing was changed.</s-paragraph>
        </s-banner>
        <s-grid gridTemplateColumns="repeat(auto-fit, minmax(160px, 1fr))" gap="base">
          <Fact label="Rule">{rule ?? 'Default (no rule matched)'}</Fact>
          <Fact label="Strategy">{preview.strategy ? humanizeCode(preview.strategy) : '—'}</Fact>
          <Fact label="Run">{preview.run_id ?? '—'}</Fact>
        </s-grid>
        {preview.ranked.length > 0 && <RankingTable ranking={preview.ranked} carriers={carriers} />}
        {preview.rejected.length > 0 && (
          <s-table>
            <s-table-header-row>
              <s-table-header listSlot="primary">Rejected carrier</s-table-header>
              <s-table-header listSlot="inline">Reason</s-table-header>
              <s-table-header listSlot="secondary">Detail</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {preview.rejected.map((r, index) => (
                <s-table-row key={`${r.carrier_code}-${index}`}>
                  <s-table-cell>{carrierName(r.carrier_code, carriers)}</s-table-cell>
                  <s-table-cell>
                    <s-badge tone="warning">{humanizeCode(r.reason)}</s-badge>
                  </s-table-cell>
                  <s-table-cell>{r.detail || '—'}</s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
      </s-stack>
    </s-section>
  );
}

function CheckCard({ check, carriers }: { check: ServiceabilityCheck; carriers: readonly Carrier[] | null }) {
  return (
    <s-box padding="base" border="base" borderRadius="base">
      <s-stack gap="small">
        <s-stack direction="inline" gap="small" alignItems="center" justifyContent="space-between">
          <s-stack direction="inline" gap="small-200" alignItems="center">
            <s-text type="strong">{carrierName(check.carrier_code, carriers)}</s-text>
            <YesNoBadge value={check.serviceable} yes="Serviceable" no="Not serviceable" noTone="warning" />
            {check.cod_available !== null && (
              <YesNoBadge value={check.cod_available} yes="COD available" no="No COD" noTone="neutral" />
            )}
            {check.error_class && <s-badge tone="critical">{check.error_class}</s-badge>}
          </s-stack>
          <s-text color="subdued">
            {check.duration_ms !== null ? `${check.duration_ms} ms · ` : ''}
            {formatDateTime(check.created_at)}
          </s-text>
        </s-stack>
        {check.error_message && <s-text tone="critical">{check.error_message}</s-text>}
        <JsonDetails summary="Request" value={check.request} />
        <JsonDetails summary={check.response === null ? 'Response (none)' : 'Response'} value={check.response} />
      </s-stack>
    </s-box>
  );
}

function LatestRunSection({ run, carriers }: { run: LatestRun | null; carriers: readonly Carrier[] | null }) {
  if (!run || (run.run_id === null && run.quotes.length === 0 && run.checks.length === 0)) {
    return (
      <s-section heading="Latest allocation run">
        <s-text color="subdued">No allocation has run for this fulfillment order yet.</s-text>
      </s-section>
    );
  }
  const strategy = run.quotes.find((q) => q.strategy)?.strategy;
  return (
    <s-section heading="Latest allocation run">
      <s-stack gap="base">
        <s-text color="subdued">
          Run {run.run_id ?? '—'}
          {strategy ? ` · Strategy: ${humanizeCode(strategy)}` : ''}
        </s-text>
        {run.quotes.length === 0 ? (
          <s-text color="subdued">No carrier quotes were recorded.</s-text>
        ) : (
          <s-table>
            <s-table-header-row>
              <s-table-header listSlot="primary">Carrier</s-table-header>
              <s-table-header listSlot="inline">Eligible</s-table-header>
              <s-table-header>Selected</s-table-header>
              <s-table-header format="numeric">Rank</s-table-header>
              <s-table-header format="currency">Cost</s-table-header>
              <s-table-header format="numeric">Days</s-table-header>
              <s-table-header>EDD</s-table-header>
              <s-table-header format="numeric">Score</s-table-header>
              <s-table-header listSlot="secondary">Rejection</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {run.quotes.map((quote, index) => (
                <s-table-row key={`${quote.carrier_code}-${index}`}>
                  <s-table-cell>{carrierName(quote.carrier_code, carriers)}</s-table-cell>
                  <s-table-cell>
                    <YesNoBadge value={quote.eligible} yes="Eligible" no="Rejected" noTone="warning" />
                  </s-table-cell>
                  <s-table-cell>
                    {quote.selected ? <s-badge tone="success">Selected</s-badge> : <s-text color="subdued">—</s-text>}
                  </s-table-cell>
                  <s-table-cell>{quote.rank ?? '—'}</s-table-cell>
                  <s-table-cell>{formatMoney(quote.cost, quote.currency)}</s-table-cell>
                  <s-table-cell>{quote.transit_days ?? '—'}</s-table-cell>
                  <s-table-cell>{formatDate(quote.edd)}</s-table-cell>
                  <s-table-cell>{formatScore(quote.score)}</s-table-cell>
                  <s-table-cell>
                    {quote.rejection_reason || quote.rejection_detail ? (
                      <s-stack gap="small-500">
                        {quote.rejection_reason && <s-text>{humanizeCode(quote.rejection_reason)}</s-text>}
                        {quote.rejection_detail && <s-text color="subdued">{quote.rejection_detail}</s-text>}
                      </s-stack>
                    ) : (
                      <s-text color="subdued">—</s-text>
                    )}
                  </s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}

        <s-heading>Carrier responses</s-heading>
        {run.checks.length === 0 ? (
          <s-text color="subdued">No serviceability calls were recorded.</s-text>
        ) : (
          <s-stack gap="small">
            {run.checks.map((check, index) => (
              <CheckCard key={`${check.carrier_code}-${index}`} check={check} carriers={carriers} />
            ))}
          </s-stack>
        )}
      </s-stack>
    </s-section>
  );
}

// --- fulfillment order -------------------------------------------------------------------

interface FOActions {
  onPreview: (fo: FODetail) => void;
  onReallocate: (fo: FODetail) => void;
  onManualSelect: (fo: FODetail) => void;
}

function FulfillmentOrderSection({
  fo,
  carriers,
  previewing,
  preview,
  onDismissPreview,
  shipmentActions,
  actions,
}: {
  fo: FODetail;
  carriers: readonly Carrier[] | null;
  previewing: boolean;
  preview: AllocationPreview | undefined;
  onDismissPreview: () => void;
  shipmentActions: RefObject<ShipmentActionsHandle | null>;
  actions: FOActions;
}) {
  const holds = fo.hold_reasons ?? [];
  const lineItems = fo.line_items ?? [];
  const ranking = fo.allocation_ranking ?? [];
  const shipments = fo.shipments ?? [];
  // Timeline reads top-to-bottom, oldest first (like the automation timeline).
  const tracking = [...(fo.tracking ?? [])].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at));
  const retry = RETRY_STATUSES.has(fo.logistics_status);

  return (
    <s-section heading={foLabel(fo)}>
      <s-stack gap="base">
        <s-stack gap="small-200">
          <s-stack direction="inline" gap="small-200" alignItems="center">
            <LogisticsStatusBadge status={fo.logistics_status} />
            <FulfillmentStatusBadge status={fo.shopify_status} />
            {fo.forced_carrier && (
              <s-badge tone="info">Manually selected: {carrierName(fo.forced_carrier, carriers)}</s-badge>
            )}
          </s-stack>
          {fo.status_reason && <s-text type="strong">{humanizeCode(fo.status_reason)}</s-text>}
          {fo.status_detail && <s-text color="subdued">{fo.status_detail}</s-text>}
        </s-stack>

        {holds.length > 0 && (
          <s-banner tone="warning" heading="Shopify fulfillment holds">
            <s-unordered-list>
              {holds.map((hold, index) => (
                <s-list-item key={`${hold.reason}-${index}`}>
                  {hold.display_reason || humanizeCode(hold.reason)}
                  {hold.reason_notes ? ` — ${hold.reason_notes}` : ''}
                </s-list-item>
              ))}
            </s-unordered-list>
          </s-banner>
        )}

        <s-grid gridTemplateColumns="repeat(auto-fit, minmax(160px, 1fr))" gap="base">
          <Fact label="Selected carrier">{carrierName(fo.selected_carrier, carriers)}</Fact>
          <Fact label="Weight">{fo.weight_g !== null ? `${fo.weight_g} g` : '—'}</Fact>
          <Fact label="Warehouse">
            {fo.warehouse_id !== null ? (
              <s-link href={`/warehouses/${fo.warehouse_id}`}>Warehouse #{fo.warehouse_id}</s-link>
            ) : (
              'Not mapped'
            )}
          </Fact>
        </s-grid>

        <s-stack gap="small-200">
          <s-stack direction="inline" gap="small">
            <s-button loading={previewing} onClick={() => actions.onPreview(fo)}>
              Re-run serviceability
            </s-button>
            <s-button disabled={!fo.can_reallocate} onClick={() => actions.onReallocate(fo)}>
              {retry ? 'Retry allocation' : 'Re-run allocation'}
            </s-button>
            <s-button disabled={!fo.can_reallocate} onClick={() => actions.onManualSelect(fo)}>
              Manually select carrier
            </s-button>
          </s-stack>
          {!fo.can_reallocate && (
            <s-text color="subdued">
              Allocation can&apos;t be re-run in the current state (for example while a shipment is active — cancel it
              first).
            </s-text>
          )}
        </s-stack>

        {preview && <PreviewResult preview={preview} carriers={carriers} onDismiss={onDismissPreview} />}

        <s-section heading="Line items">
          {lineItems.length === 0 ? (
            <s-text color="subdued">No line items.</s-text>
          ) : (
            <s-table>
              <s-table-header-row>
                <s-table-header listSlot="primary">Product</s-table-header>
                <s-table-header listSlot="secondary">SKU</s-table-header>
                <s-table-header format="numeric">Qty</s-table-header>
                <s-table-header format="numeric">Unit weight</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {lineItems.map((line, index) => (
                  <s-table-row key={`${line.sku ?? line.name}-${index}`}>
                    <s-table-cell>{line.name}</s-table-cell>
                    <s-table-cell>{line.sku || '—'}</s-table-cell>
                    <s-table-cell>
                      {line.remaining_quantity === line.total_quantity
                        ? line.total_quantity
                        : `${line.remaining_quantity} of ${line.total_quantity}`}
                    </s-table-cell>
                    <s-table-cell>{line.unit_weight_g !== null ? `${line.unit_weight_g} g` : '—'}</s-table-cell>
                  </s-table-row>
                ))}
              </s-table-body>
            </s-table>
          )}
        </s-section>

        <s-section heading="Allocation ranking">
          {ranking.length === 0 ? (
            <s-text color="subdued">No ranking yet — it is computed when the order is allocated.</s-text>
          ) : (
            <RankingTable ranking={ranking} carriers={carriers} />
          )}
        </s-section>

        <s-section heading="Shipments">
          {shipments.length === 0 ? (
            <s-text color="subdued">No shipments for this fulfillment order yet.</s-text>
          ) : (
            <ShipmentsTable shipments={shipments} carriers={carriers} actions={shipmentActions} showOrder={false} />
          )}
        </s-section>

        <LatestRunSection run={fo.latest_run} carriers={carriers} />

        <s-section heading="Tracking timeline">
          {tracking.length === 0 ? (
            <s-text color="subdued">No tracking events yet.</s-text>
          ) : (
            <TrackingTable events={tracking} carriers={carriers} showOrder={false} />
          )}
        </s-section>
      </s-stack>
    </s-section>
  );
}

// --- page --------------------------------------------------------------------------------

export function OrderDetailPage() {
  const { orderId: orderParam } = useParams();
  const orderId = parseId(orderParam);
  const notice = useNotice();
  const { data: order, loading, error, reload, setData } = useApi(
    () => (orderId === null ? Promise.resolve(null) : api.getOrder(orderId)),
    [orderId],
  );
  const { data: carriers } = useApi(api.listCarriers);

  const shipmentActions = useRef<ShipmentActionsHandle>(null);
  const reallocateModal = useRef<ConfirmModalHandle>(null);
  const manualModal = useRef<ConfirmModalHandle>(null);
  const [pendingFo, setPendingFo] = useState<FODetail | null>(null);
  const [manualCarrier, setManualCarrier] = useState('');
  const [manualError, setManualError] = useState<string | undefined>();
  const [previewing, setPreviewing] = useState<number | null>(null);
  const [previews, setPreviews] = useState<Record<number, AllocationPreview>>({});
  const [togglingAutomation, setTogglingAutomation] = useState(false);

  const refetchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(refetchTimer.current), []);
  const reloadSoon = () => {
    clearTimeout(refetchTimer.current);
    refetchTimer.current = setTimeout(() => void reload(), REFETCH_AFTER_QUEUE_MS);
  };

  const preview = async (fo: FODetail) => {
    setPreviewing(fo.id);
    try {
      const result = await api.previewAllocation(fo.id);
      setPreviews((prev) => ({ ...prev, [fo.id]: result }));
      notice.clear();
      await reload();
    } catch (err) {
      notice.showError(err);
    } finally {
      setPreviewing(null);
    }
  };

  const dismissPreview = (foId: number) =>
    setPreviews((prev) => {
      const next = { ...prev };
      delete next[foId];
      return next;
    });

  const askReallocate = (fo: FODetail) => {
    setPendingFo(fo);
    reallocateModal.current?.open();
  };

  const askManualSelect = (fo: FODetail) => {
    setPendingFo(fo);
    setManualCarrier('');
    setManualError(undefined);
    manualModal.current?.open();
  };

  const allocate = async (carrierCode?: string) => {
    if (!pendingFo) return;
    try {
      const result = await api.allocate(pendingFo.id, carrierCode ? { carrier_code: carrierCode } : {});
      notice.showSuccess(
        result.forced_carrier
          ? `Queued: shipping ${foLabel(pendingFo)} with ${carrierName(result.forced_carrier, carriers)}.`
          : `Queued: allocation for ${foLabel(pendingFo)} will run shortly.`,
      );
      reloadSoon();
    } catch (err) {
      notice.showError(err);
    }
  };

  const confirmManual = async () => {
    if (!manualCarrier) {
      setManualError('Choose a carrier');
      return false;
    }
    await allocate(manualCarrier);
  };

  const toggleAutomation = async () => {
    if (!order) return;
    setTogglingAutomation(true);
    try {
      const result = await api.setOrderAutomation(order.id, !order.automation_disabled);
      setData({ ...order, automation_disabled: result.automation_disabled });
      notice.showSuccess(
        result.automation_disabled
          ? `Automation disabled for ${order.name}. Trekiva will not allocate or ship it automatically.`
          : `Automation enabled for ${order.name}.`,
      );
      await reload();
    } catch (err) {
      notice.showError(err);
    } finally {
      setTogglingAutomation(false);
    }
  };

  const shopifyId = gidToNumericId(order?.shopify_order_id);
  const fulfillmentOrders = order?.fulfillment_orders ?? [];
  const logs = order?.logs ?? [];

  if (orderId === null) {
    return (
      <s-page heading="Order not found">
        <s-link slot="breadcrumb-actions" href="/orders">
          Orders
        </s-link>
        <s-section>
          <EmptyMessage heading="Order not found">
            Go back to <s-link href="/orders">Orders</s-link>.
          </EmptyMessage>
        </s-section>
      </s-page>
    );
  }

  return (
    <s-page heading={order?.name ?? 'Order'} inlineSize="large">
      <s-link slot="breadcrumb-actions" href="/orders">
        Orders
      </s-link>
      {order && (
        <s-button
          slot="primary-action"
          variant="primary"
          tone={order.automation_disabled ? 'auto' : 'critical'}
          loading={togglingAutomation}
          onClick={() => void toggleAutomation()}
        >
          {order.automation_disabled ? 'Enable automation for this order' : 'Disable automation for this order'}
        </s-button>
      )}
      {shopifyId && (
        <s-button slot="secondary-actions" href={`shopify:admin/orders/${shopifyId}`}>
          View in Shopify
        </s-button>
      )}
      <s-button slot="secondary-actions" href={`/automation-logs?order_id=${orderId}`}>
        Automation logs
      </s-button>
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <s-stack gap="base">
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner heading="Could not load the order" error={error} onRetry={() => void reload()} />

        {loading && !order && <Loading label="Loading order" />}

        {order && (
          <>
            <s-stack direction="inline" gap="small-200" alignItems="center">
              <s-text color="subdued">Placed {formatDateTime(order.created_at)}</s-text>
              {order.cancelled_at && <s-badge tone="critical">Cancelled {formatDateTime(order.cancelled_at)}</s-badge>}
              {order.automation_disabled && <s-badge tone="caution">Automation disabled</s-badge>}
            </s-stack>

            {order.automation_disabled && (
              <s-banner tone="warning" heading="Automation is disabled for this order">
                <s-paragraph>
                  Trekiva will not allocate carriers or create shipments for this order automatically. Use the actions
                  below to ship it manually, or enable automation again.
                </s-paragraph>
              </s-banner>
            )}

            <s-grid gridTemplateColumns="repeat(auto-fit, minmax(240px, 1fr))" gap="base">
              <CustomerSection order={order} />
              <PaymentSection order={order} />
              <TagsSection order={order} />
            </s-grid>

            {fulfillmentOrders.length === 0 && (
              <s-section>
                <EmptyMessage heading="No fulfillment orders">
                  Shopify has not created fulfillment orders for this order yet.
                </EmptyMessage>
              </s-section>
            )}

            {fulfillmentOrders.map((fo) => (
              <FulfillmentOrderSection
                key={fo.id}
                fo={fo}
                carriers={carriers}
                previewing={previewing === fo.id}
                preview={previews[fo.id]}
                onDismissPreview={() => dismissPreview(fo.id)}
                shipmentActions={shipmentActions}
                actions={{ onPreview: (f) => void preview(f), onReallocate: askReallocate, onManualSelect: askManualSelect }}
              />
            ))}

            <s-section heading="Automation timeline">
              <s-stack gap="base">
                <s-paragraph color="subdued">
                  Every step Trekiva took for this order, oldest first — from the order webhook to the Shopify tracking
                  update.
                </s-paragraph>
                {logs.length === 0 ? (
                  <s-text color="subdued">No automation activity recorded yet.</s-text>
                ) : (
                  <LogsTable logs={logs} showOrder={false} />
                )}
              </s-stack>
            </s-section>
          </>
        )}
      </s-stack>

      <ShipmentActionModals ref={shipmentActions} notice={notice} onChanged={() => reload()} />

      <ConfirmModal
        ref={reallocateModal}
        id="reallocate-modal"
        heading={pendingFo && RETRY_STATUSES.has(pendingFo.logistics_status) ? 'Retry allocation?' : 'Re-run allocation?'}
        confirmLabel={pendingFo && RETRY_STATUSES.has(pendingFo.logistics_status) ? 'Retry allocation' : 'Re-run allocation'}
        onConfirm={() => allocate()}
      >
        <s-paragraph>
          Trekiva will re-check serviceability for{' '}
          <s-text type="strong">{pendingFo ? foLabel(pendingFo) : 'this fulfillment order'}</s-text>, pick a carrier
          using your allocation rules and create the shipment.
        </s-paragraph>
      </ConfirmModal>

      <ConfirmModal
        ref={manualModal}
        id="manual-carrier-modal"
        heading="Manually select carrier"
        confirmLabel="Ship with this carrier"
        onConfirm={confirmManual}
      >
        <s-stack gap="base">
          <s-paragraph>
            Skip automatic carrier selection for{' '}
            <s-text type="strong">{pendingFo ? foLabel(pendingFo) : 'this fulfillment order'}</s-text> and create the
            shipment with the carrier you choose. Carriers without an active account can&apos;t be used.
          </s-paragraph>
          <s-select
            label="Carrier"
            value={manualCarrier}
            error={manualError}
            onChange={(e) => {
              setManualCarrier(e.currentTarget.value);
              setManualError(undefined);
            }}
          >
            <s-option value="">Choose a carrier</s-option>
            {(carriers ?? []).map((carrier) => {
              const usable = carrier.settings.active_account_id !== null;
              const note = !usable ? ' — no active account' : !carrier.settings.enabled ? ' — disabled in settings' : '';
              return (
                <s-option key={carrier.code} value={carrier.code} disabled={!usable}>
                  {`${carrier.display_name} (${carrier.code})${note}`}
                </s-option>
              );
            })}
          </s-select>
        </s-stack>
      </ConfirmModal>
    </s-page>
  );
}
