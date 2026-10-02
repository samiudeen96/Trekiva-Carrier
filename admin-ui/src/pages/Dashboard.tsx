import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading } from '../components/Feedback';
import { StatTile } from '../components/StatTile';
import { LevelBadge } from '../components/StatusBadges';
import { useApi } from '../hooks/useApi';
import { formatDateTime, humanizeCode } from '../utils';

export function DashboardPage() {
  const { data, loading, error, reload } = useApi(api.dashboard);

  return (
    <s-page heading="Dashboard">
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <ErrorBanner error={error} onRetry={() => void reload()} />

      {loading && !data && <Loading label="Loading dashboard" />}

      {data && (
        <s-stack gap="base">
          {!data.automation_enabled && (
            <s-banner tone="warning" heading="Automation is turned off">
              <s-paragraph>
                New orders are not being allocated or shipped automatically. Turn automation on in{' '}
                <s-link href="/settings">Settings</s-link> when carriers, warehouses and rules are ready.
              </s-paragraph>
            </s-banner>
          )}

          <s-grid gridTemplateColumns="repeat(auto-fit, minmax(160px, 1fr))" gap="base">
            <StatTile label="Orders today" value={data.orders_today} href="/orders" />
            <StatTile
              label="Shipments created today"
              value={data.shipments_created_today}
              tone="success"
              href="/shipments"
            />
            <StatTile
              label="Manual review"
              value={data.manual_review}
              tone={data.manual_review > 0 ? 'warning' : 'auto'}
              href="/manual-review"
            />
            <StatTile
              label="Awaiting allocation"
              value={data.awaiting_allocation}
              href="/orders?status=AWAITING_ALLOCATION"
            />
            <StatTile
              label="Failed"
              value={data.failed}
              tone={data.failed > 0 ? 'critical' : 'auto'}
              href="/orders?status=FAILED"
            />
            <StatTile label="Delivered" value={data.delivered} tone="success" href="/shipments?status=DELIVERED" />
            <StatTile label="RTO" value={data.rto} tone={data.rto > 0 ? 'caution' : 'auto'} />
          </s-grid>

          {data.manual_review > 0 && (
            <s-banner tone="info">
              <s-paragraph>
                {data.manual_review} order{data.manual_review === 1 ? ' is' : 's are'} waiting in{' '}
                <s-link href="/manual-review">Manual Review</s-link>.
              </s-paragraph>
            </s-banner>
          )}

          <s-section heading="Shipments by carrier">
            {data.carrier_breakdown.length === 0 ? (
              <EmptyMessage heading="No shipments yet">Shipments will appear here once orders are allocated.</EmptyMessage>
            ) : (
              <s-table>
                <s-table-header-row>
                  <s-table-header listSlot="primary">Carrier</s-table-header>
                  <s-table-header format="numeric">Shipments</s-table-header>
                </s-table-header-row>
                <s-table-body>
                  {data.carrier_breakdown.map((row) => (
                    <s-table-row key={row.carrier_code}>
                      <s-table-cell>{row.carrier_code}</s-table-cell>
                      <s-table-cell>{row.shipments}</s-table-cell>
                    </s-table-row>
                  ))}
                </s-table-body>
              </s-table>
            )}
          </s-section>

          <s-section heading="Recent activity">
            <s-button slot="secondary-actions" variant="tertiary" href="/automation-logs">
              View all logs
            </s-button>
            {data.recent_activity.length === 0 ? (
              <EmptyMessage heading="No activity yet">Automation events will be listed here.</EmptyMessage>
            ) : (
              <s-table>
                <s-table-header-row>
                  <s-table-header listSlot="kicker">Time</s-table-header>
                  <s-table-header listSlot="inline">Level</s-table-header>
                  <s-table-header listSlot="secondary">Step</s-table-header>
                  <s-table-header listSlot="primary">Message</s-table-header>
                </s-table-header-row>
                <s-table-body>
                  {data.recent_activity.map((item) => (
                    <s-table-row key={item.id}>
                      <s-table-cell>{formatDateTime(item.created_at)}</s-table-cell>
                      <s-table-cell>
                        <LevelBadge level={item.level} />
                      </s-table-cell>
                      <s-table-cell>{humanizeCode(item.step)}</s-table-cell>
                      <s-table-cell>
                        {item.message}
                        {item.order_id !== null && (
                          <s-text color="subdued">
                            {' '}
                            (<s-link href={`/orders/${item.order_id}`}>order #{item.order_id}</s-link>)
                          </s-text>
                        )}
                      </s-table-cell>
                    </s-table-row>
                  ))}
                </s-table-body>
              </s-table>
            )}
          </s-section>
        </s-stack>
      )}
    </s-page>
  );
}
