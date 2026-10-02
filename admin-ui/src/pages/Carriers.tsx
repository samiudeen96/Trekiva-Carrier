import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading } from '../components/Feedback';
import { useApi } from '../hooks/useApi';
import type { Carrier, CarrierAccount } from '../types';

export function activeAccount(carrier: Carrier): CarrierAccount | undefined {
  const id = carrier.settings.active_account_id;
  return id === null ? undefined : carrier.accounts.find((a) => a.id === id);
}

export function ImplementationBadge({ carrier }: { carrier: Carrier }) {
  return carrier.implementation_status === 'ready' ? (
    <s-badge tone="success">Ready</s-badge>
  ) : (
    <s-badge tone="warning">Awaiting API docs</s-badge>
  );
}

export function EnabledBadge({ enabled }: { enabled: boolean }) {
  return enabled ? <s-badge tone="success">Enabled</s-badge> : <s-badge tone="neutral">Disabled</s-badge>;
}

export function EnvironmentBadge({ environment }: { environment: string | undefined }) {
  if (!environment) return <s-text color="subdued">No active account</s-text>;
  const tone = environment === 'PRODUCTION' ? 'success' : environment === 'SANDBOX' ? 'info' : 'caution';
  return <s-badge tone={tone}>{environment}</s-badge>;
}

export function CarriersPage() {
  const { data, loading, error, reload } = useApi(api.listCarriers);
  const carriers = data ? [...data].sort((a, b) => a.settings.priority - b.settings.priority) : null;

  return (
    <s-page heading="Carriers">
      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />
        <s-section>
          <s-paragraph color="subdued">
            Configure each courier&apos;s credentials, payment modes, limits and supported warehouses. Only enabled
            carriers with an active account are considered by the allocation engine.
          </s-paragraph>
          {loading && !carriers && <Loading label="Loading carriers" />}
          {carriers && carriers.length === 0 && <EmptyMessage heading="No carriers registered" />}
          {carriers && carriers.length > 0 && (
            <s-table>
              <s-table-header-row>
                <s-table-header listSlot="primary">Carrier</s-table-header>
                <s-table-header listSlot="inline">Status</s-table-header>
                <s-table-header listSlot="inline">Implementation</s-table-header>
                <s-table-header listSlot="secondary">Active environment</s-table-header>
                <s-table-header format="numeric">Priority</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {carriers.map((carrier) => (
                  <s-table-row key={carrier.code} clickDelegate={`carrier-link-${carrier.code}`}>
                    <s-table-cell>
                      <s-link id={`carrier-link-${carrier.code}`} href={`/carriers/${encodeURIComponent(carrier.code)}`}>
                        {carrier.display_name}
                      </s-link>
                    </s-table-cell>
                    <s-table-cell>
                      <EnabledBadge enabled={carrier.settings.enabled} />
                    </s-table-cell>
                    <s-table-cell>
                      <ImplementationBadge carrier={carrier} />
                    </s-table-cell>
                    <s-table-cell>
                      <EnvironmentBadge environment={activeAccount(carrier)?.environment} />
                    </s-table-cell>
                    <s-table-cell>{carrier.settings.priority}</s-table-cell>
                  </s-table-row>
                ))}
              </s-table-body>
            </s-table>
          )}
        </s-section>
      </s-stack>
    </s-page>
  );
}
