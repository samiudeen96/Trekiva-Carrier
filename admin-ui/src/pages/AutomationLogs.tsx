import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading } from '../components/Feedback';
import { JsonDetails, PageSummary, SearchInput, type TablePagination } from '../components/Filters';
import { LevelBadge } from '../components/StatusBadges';
import { useApi } from '../hooks/useApi';
import { useSearchFilters } from '../hooks/useSearchFilters';
import type { LogLevel, LogRow } from '../types';
import { formatDateTime } from '../utils';

const PAGE_SIZE = 50;
const FILTER_KEYS = ['order_id', 'level', 'step', 'q'] as const;
const LEVELS: readonly LogLevel[] = ['INFO', 'WARNING', 'ERROR'];

function hasData(data: LogRow['data']): data is Record<string, unknown> {
  return data !== null && typeof data === 'object' && Object.keys(data).length > 0;
}

/**
 * Automation log entries. The message is the exact, human-readable reason for each step (why an
 * order was held, which carrier was chosen and why, what the carrier/Shopify answered).
 */
export function LogsTable({
  logs,
  showOrder = true,
  loading = false,
  pagination,
}: {
  logs: LogRow[];
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
        <s-table-header listSlot="kicker">Time</s-table-header>
        <s-table-header listSlot="inline">Level</s-table-header>
        <s-table-header listSlot="secondary">Step</s-table-header>
        <s-table-header listSlot="primary">Message</s-table-header>
        <s-table-header>Actor</s-table-header>
        {showOrder && <s-table-header>Order</s-table-header>}
      </s-table-header-row>
      <s-table-body>
        {logs.map((log) => (
          <s-table-row key={log.id}>
            <s-table-cell>{formatDateTime(log.created_at)}</s-table-cell>
            <s-table-cell>
              <LevelBadge level={log.level} />
            </s-table-cell>
            <s-table-cell>
              <s-text fontVariantNumeric="tabular-nums">{log.step}</s-text>
            </s-table-cell>
            <s-table-cell>
              <s-stack gap="small-300">
                <s-text tone={log.level === 'ERROR' ? 'critical' : 'auto'}>{log.message}</s-text>
                {hasData(log.data) && <JsonDetails summary="Details" value={log.data} />}
              </s-stack>
            </s-table-cell>
            <s-table-cell>{log.actor || '—'}</s-table-cell>
            {showOrder && (
              <s-table-cell>
                {log.order_id !== null ? <s-link href={`/orders/${log.order_id}`}>#{log.order_id}</s-link> : '—'}
              </s-table-cell>
            )}
          </s-table-row>
        ))}
      </s-table-body>
    </s-table>
  );
}

export function AutomationLogsPage() {
  const filters = useSearchFilters(FILTER_KEYS);
  const { order_id: orderId, level, step, q } = filters.values;
  const { offset } = filters;

  const { data, loading, error, reload } = useApi(
    () => api.listLogs({ order_id: orderId, level, step, q, limit: PAGE_SIZE, offset }),
    [orderId, level, step, q, offset],
  );

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <s-page heading="Automation Logs" inlineSize="large">
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />

        {orderId && (
          <s-banner tone="info">
            <s-paragraph>
              Showing the audit trail for order #{orderId} (oldest first).{' '}
              <s-link href={`/orders/${orderId}`}>Open the order</s-link>
            </s-paragraph>
          </s-banner>
        )}

        <s-section>
          <s-stack gap="base">
            <s-paragraph color="subdued">
              Every automation step with the exact reason: why an order was held, which carrier was chosen (and why
              others were rejected), and what the carrier and Shopify answered.
            </s-paragraph>

            <s-grid gridTemplateColumns="repeat(auto-fit, minmax(180px, 1fr))" gap="base" alignItems="end">
              <s-select label="Level" value={level} onChange={(e) => filters.setFilter('level', e.currentTarget.value)}>
                <s-option value="">All levels</s-option>
                {LEVELS.map((l) => (
                  <s-option key={l} value={l}>
                    {l}
                  </s-option>
                ))}
              </s-select>
              <SearchInput
                label="Step"
                placeholder="e.g. ALLOCATION"
                value={step}
                onSearch={(v) => filters.setFilter('step', v)}
              />
              <SearchInput
                label="Search messages"
                placeholder="Text in the message"
                value={q}
                onSearch={(v) => filters.setFilter('q', v)}
              />
              <SearchInput
                label="Order ID"
                placeholder="Trekiva order ID"
                value={orderId}
                onSearch={(v) => filters.setFilter('order_id', v.replace(/\D/g, ''))}
              />
            </s-grid>
            <s-stack direction="inline" gap="base" alignItems="center" justifyContent="space-between">
              <PageSummary offset={offset} count={items.length} total={total} />
              {filters.active && (
                <s-button variant="tertiary" onClick={filters.clear}>
                  Clear filters
                </s-button>
              )}
            </s-stack>

            {loading && !data && <Loading label="Loading logs" />}

            {data && items.length === 0 && (
              <EmptyMessage heading={filters.active ? 'No matching log entries' : 'No automation activity yet'}>
                {filters.active ? 'Try different filters.' : 'Every automation step is recorded here.'}
              </EmptyMessage>
            )}

            {data && items.length > 0 && (
              <LogsTable
                logs={items}
                loading={loading}
                pagination={{
                  hasPreviousPage: offset > 0,
                  hasNextPage: offset + items.length < total,
                  onPreviousPage: () => filters.setOffset(Math.max(0, offset - PAGE_SIZE)),
                  onNextPage: () => filters.setOffset(offset + PAGE_SIZE),
                }}
              />
            )}
          </s-stack>
        </s-section>
      </s-stack>
    </s-page>
  );
}

