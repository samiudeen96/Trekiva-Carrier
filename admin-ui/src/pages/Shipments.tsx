import { useRef } from 'react';
import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import { CarrierFilterSelect, PageSummary, SearchInput } from '../components/Filters';
import { ShipmentActionModals, type ShipmentActionsHandle } from '../components/ShipmentActions';
import { ShipmentsTable } from '../components/ShipmentsTable';
import { SHIPMENT_STATUSES } from '../components/StatusBadges';
import { useApi } from '../hooks/useApi';
import { useNotice } from '../hooks/useNotice';
import { useSearchFilters } from '../hooks/useSearchFilters';
import { humanizeCode } from '../utils';

const PAGE_SIZE = 50;
const FILTER_KEYS = ['status', 'carrier', 'q'] as const;

export function ShipmentsPage() {
  const filters = useSearchFilters(FILTER_KEYS);
  const { status, carrier, q } = filters.values;
  const { offset } = filters;
  const notice = useNotice();
  const actions = useRef<ShipmentActionsHandle>(null);

  const { data, loading, error, reload } = useApi(
    () => api.listShipments({ status, carrier, q, limit: PAGE_SIZE, offset }),
    [status, carrier, q, offset],
  );
  const { data: carriers } = useApi(api.listCarriers);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const limit = data?.limit || PAGE_SIZE;

  return (
    <s-page heading="Shipments" inlineSize="large">
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <s-stack gap="base">
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner error={error} onRetry={() => void reload()} />

        <s-section>
          <s-stack gap="base">
            <s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base" alignItems="end">
              <s-select label="Status" value={status} onChange={(e) => filters.setFilter('status', e.currentTarget.value)}>
                <s-option value="">All statuses</s-option>
                {SHIPMENT_STATUSES.map((s) => (
                  <s-option key={s} value={s}>
                    {humanizeCode(s)}
                  </s-option>
                ))}
              </s-select>
              <CarrierFilterSelect carriers={carriers} value={carrier} onChange={(v) => filters.setFilter('carrier', v)} />
              <SearchInput
                label="Search"
                placeholder="AWB or order"
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

            {loading && !data && <Loading label="Loading shipments" />}

            {data && items.length === 0 && (
              <EmptyMessage heading={filters.active ? 'No matching shipments' : 'No shipments yet'}>
                {filters.active
                  ? 'Try a different status, carrier or search.'
                  : 'Shipments are created automatically once orders are allocated to a carrier.'}
              </EmptyMessage>
            )}

            {data && items.length > 0 && (
              <ShipmentsTable
                shipments={items}
                carriers={carriers}
                actions={actions}
                loading={loading}
                pagination={{
                  hasPreviousPage: offset > 0,
                  hasNextPage: offset + items.length < total,
                  onPreviousPage: () => filters.setOffset(Math.max(0, offset - limit)),
                  onNextPage: () => filters.setOffset(offset + limit),
                }}
              />
            )}
          </s-stack>
        </s-section>
      </s-stack>

      <ShipmentActionModals ref={actions} notice={notice} onChanged={() => reload()} />
    </s-page>
  );
}
