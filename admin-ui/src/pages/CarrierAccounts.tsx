import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading } from '../components/Feedback';
import { useApi } from '../hooks/useApi';
import { formatDateTime } from '../utils';
import { EnvironmentBadge } from './Carriers';

export function CarrierAccountsPage() {
  const { data, loading, error, reload } = useApi(api.listCarriers);

  const rows =
    data?.flatMap((carrier) =>
      carrier.accounts.map((account) => ({
        carrier,
        account,
        isCarrierActive: carrier.settings.active_account_id === account.id,
      })),
    ) ?? null;

  return (
    <s-page heading="Carrier Accounts">
      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />
        <s-section>
          <s-paragraph color="subdued">
            Every credential set saved for a carrier. Secrets are encrypted and never shown. Edit credentials from the
            carrier&apos;s page. Give the webhook path (prefixed with this app&apos;s URL) to the carrier for tracking
            updates.
          </s-paragraph>
          {loading && !rows && <Loading label="Loading accounts" />}
          {rows && rows.length === 0 && (
            <EmptyMessage heading="No carrier accounts yet">
              Open a carrier from the Carriers page and save its credentials.
            </EmptyMessage>
          )}
          {rows && rows.length > 0 && (
            <s-table>
              <s-table-header-row>
                <s-table-header listSlot="primary">Carrier</s-table-header>
                <s-table-header listSlot="inline">Environment</s-table-header>
                <s-table-header>Label</s-table-header>
                <s-table-header listSlot="inline">Active</s-table-header>
                <s-table-header>Last updated</s-table-header>
                <s-table-header listSlot="secondary">Webhook path</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {rows.map(({ carrier, account, isCarrierActive }) => (
                  <s-table-row key={account.id}>
                    <s-table-cell>
                      <s-link href={`/carriers/${encodeURIComponent(carrier.code)}`}>{carrier.display_name}</s-link>
                    </s-table-cell>
                    <s-table-cell>
                      <EnvironmentBadge environment={account.environment} />
                    </s-table-cell>
                    <s-table-cell>{account.label}</s-table-cell>
                    <s-table-cell>
                      {isCarrierActive ? (
                        <s-badge tone="success">In use</s-badge>
                      ) : account.is_active ? (
                        <s-badge tone="info">Active</s-badge>
                      ) : (
                        <s-badge tone="neutral">Inactive</s-badge>
                      )}
                    </s-table-cell>
                    <s-table-cell>{formatDateTime(account.updated_at)}</s-table-cell>
                    <s-table-cell>
                      <s-text fontVariantNumeric="tabular-nums">{account.webhook_path}</s-text>
                    </s-table-cell>
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
