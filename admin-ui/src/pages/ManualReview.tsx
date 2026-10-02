import { useRef, useState } from 'react';
import { api } from '../api';
import { ConfirmModal, type ConfirmModalHandle } from '../components/ConfirmModal';
import { EmptyMessage, ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import { useApi } from '../hooks/useApi';
import { useNotice } from '../hooks/useNotice';
import type { ReviewItem, ShippingAddress } from '../types';
import { formatDateTime, gidToNumericId, humanizeCode } from '../utils';

function addressLines(address: ShippingAddress | null): string[] {
  if (!address) return [];
  const cityLine = [address.city, address.province, address.zip].filter(Boolean).join(', ');
  return [address.name, address.address1, address.address2, cityLine, address.country]
    .filter((line): line is string => typeof line === 'string' && line.trim() !== '');
}

function ReviewCard({ item, onApprove }: { item: ReviewItem; onApprove: (item: ReviewItem) => void }) {
  const orderId = gidToNumericId(item.shopify_order_id);
  const lines = addressLines(item.shipping_address);
  const phone = item.phone ?? item.shipping_address?.phone ?? null;
  const tags = item.tags ?? [];
  const holds = item.hold_reasons ?? [];
  const lineItems = item.line_items ?? [];

  return (
    <s-section>
      <s-stack gap="base">
        <s-stack direction="inline" gap="small" alignItems="center" justifyContent="space-between">
          <s-stack direction="inline" gap="small" alignItems="center">
            <s-heading>
              {orderId ? <s-link href={`shopify:admin/orders/${orderId}`}>{item.order_name}</s-link> : item.order_name}
            </s-heading>
            {item.shopify_status && <s-badge tone="neutral">{humanizeCode(item.shopify_status)}</s-badge>}
            {item.payment_mode && <s-badge tone="info">{item.payment_mode}</s-badge>}
            {item.risk_level && (
              <s-badge tone={item.risk_level.toUpperCase() === 'HIGH' ? 'critical' : 'neutral'}>
                Risk: {humanizeCode(item.risk_level)}
              </s-badge>
            )}
          </s-stack>
          <s-text color="subdued">Placed {formatDateTime(item.order_created_at)}</s-text>
        </s-stack>

        {tags.length > 0 && (
          <s-stack direction="inline" gap="small-200">
            {tags.map((tag) => (
              <s-badge key={tag}>{tag}</s-badge>
            ))}
          </s-stack>
        )}

        <s-grid gridTemplateColumns="repeat(auto-fit, minmax(220px, 1fr))" gap="base">
          <s-stack gap="small-200">
            <s-text type="strong">Why it is held</s-text>
            {item.reason && <s-text>{humanizeCode(item.reason)}</s-text>}
            {item.detail && <s-text color="subdued">{item.detail}</s-text>}
            {holds.length > 0 && (
              <s-unordered-list>
                {holds.map((hold, index) => (
                  <s-list-item key={`${hold.reason}-${index}`}>
                    {hold.display_reason || humanizeCode(hold.reason)}
                    {hold.reason_notes ? ` — ${hold.reason_notes}` : ''}
                  </s-list-item>
                ))}
              </s-unordered-list>
            )}
          </s-stack>

          <s-stack gap="small-200">
            <s-text type="strong">Customer</s-text>
            <s-text>{item.customer_name || '—'}</s-text>
            <s-text color="subdued">{phone || 'No phone'}</s-text>
          </s-stack>

          <s-stack gap="small-200">
            <s-text type="strong">Shipping address</s-text>
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
        </s-grid>

        {lineItems.length === 0 ? (
          <s-text color="subdued">No remaining line items.</s-text>
        ) : (
          <s-table>
            <s-table-header-row>
              <s-table-header listSlot="primary">Product</s-table-header>
              <s-table-header listSlot="secondary">SKU</s-table-header>
              <s-table-header format="numeric">Qty</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {lineItems.map((line, index) => (
                <s-table-row key={`${line.sku ?? line.name}-${index}`}>
                  <s-table-cell>{line.name}</s-table-cell>
                  <s-table-cell>{line.sku || '—'}</s-table-cell>
                  <s-table-cell>{line.quantity}</s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}

        {item.is_shopify_hold && (
          <s-banner tone="warning">
            <s-paragraph>
              Release the hold in Shopify to continue. Trekiva never releases fulfillment holds; once the hold is
              released in the Shopify admin, the order is re-checked automatically.
            </s-paragraph>
          </s-banner>
        )}

        {item.can_override && (
          <s-stack direction="inline" justifyContent="end">
            <s-button variant="primary" onClick={() => onApprove(item)}>
              Approve for processing
            </s-button>
          </s-stack>
        )}
      </s-stack>
    </s-section>
  );
}

export function ManualReviewPage() {
  const { data, loading, error, reload } = useApi(api.manualReview);
  const notice = useNotice();
  const modal = useRef<ConfirmModalHandle>(null);
  const [pending, setPending] = useState<ReviewItem | null>(null);

  const askApprove = (item: ReviewItem) => {
    setPending(item);
    modal.current?.open();
  };

  const approve = async () => {
    if (!pending) return;
    try {
      const result = await api.approveReview(pending.fulfillment_order_id);
      notice.showSuccess(
        result.recheck_queued
          ? `${pending.order_name} approved — it will be re-checked and processed shortly.`
          : `${pending.order_name} approved.`,
      );
      setPending(null);
      await reload();
    } catch (err) {
      notice.showError(err);
    }
  };

  return (
    <s-page heading="Manual Review">
      <s-button slot="secondary-actions" onClick={() => void reload()} disabled={loading}>
        Refresh
      </s-button>

      <s-stack gap="base">
        <s-paragraph color="subdued">
          Orders held by Shopify (fulfillment holds from Flow/risk workflows) or by Trekiva&apos;s safety checks
          (review tags, high risk). Shopify holds must be released in Shopify; Trekiva-only blocks can be approved here.
        </s-paragraph>

        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner error={error} onRetry={() => void reload()} />

        {loading && !data && <Loading label="Loading held orders" />}

        {data && data.length === 0 && (
          <s-section>
            <EmptyMessage heading="Nothing to review">No orders are currently held for manual review.</EmptyMessage>
          </s-section>
        )}

        {data?.map((item) => <ReviewCard key={item.fulfillment_order_id} item={item} onApprove={askApprove} />)}
      </s-stack>

      <ConfirmModal
        ref={modal}
        id="approve-review-modal"
        heading="Approve order for processing?"
        confirmLabel="Approve for processing"
        onConfirm={approve}
      >
        <s-paragraph>
          {pending ? (
            <>
              <s-text type="strong">{pending.order_name}</s-text> will be released from Trekiva&apos;s review queue and
              re-checked for allocation and shipping. Shopify holds (if any) are not affected.
            </>
          ) : (
            'Select an order to approve.'
          )}
        </s-paragraph>
      </ConfirmModal>
    </s-page>
  );
}
