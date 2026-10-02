import { useEffect, useState } from 'react';
import { api } from '../api';
import { ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import { useApi } from '../hooks/useApi';
import { useNotice } from '../hooks/useNotice';
import { useSession } from '../session';
import type { FulfillOn, SettingsResponse, ShopSettings } from '../types';
import { joinList, parseList, parseOptionalInt } from '../utils';

interface SettingsForm {
  automation_enabled: boolean;
  timezone: string;
  settle_window_seconds: string;
  wait_for_risk_analysis: boolean;
  risk_wait_max_seconds: string;
  review_tags: string;
  block_on_high_risk: boolean;
  cod_gateway_names: string;
  fulfill_on: FulfillOn;
  notify_customer: boolean;
  shipped_tag: string;
  max_create_attempts_per_carrier: string;
  cancel_shipment_on_order_cancel: boolean;
}

type FormErrors = Partial<Record<keyof SettingsForm, string>>;

const MAX_SECONDS = 86_400;

function toForm({ automation_enabled, settings: s }: SettingsResponse): SettingsForm {
  return {
    automation_enabled,
    timezone: s.timezone,
    settle_window_seconds: String(s.settle_window_seconds),
    wait_for_risk_analysis: s.wait_for_risk_analysis,
    risk_wait_max_seconds: String(s.risk_wait_max_seconds),
    review_tags: joinList(s.review_tags),
    block_on_high_risk: s.block_on_high_risk,
    cod_gateway_names: joinList(s.cod_gateway_names),
    fulfill_on: s.fulfill_on,
    notify_customer: s.notify_customer,
    shipped_tag: s.shipped_tag ?? '',
    max_create_attempts_per_carrier: String(s.max_create_attempts_per_carrier),
    cancel_shipment_on_order_cancel: s.cancel_shipment_on_order_cancel,
  };
}

function toPayload(form: SettingsForm): { settings: ShopSettings | null; errors: FormErrors } {
  const errors: FormErrors = {};
  const seconds = (key: 'settle_window_seconds' | 'risk_wait_max_seconds'): number => {
    const n = parseOptionalInt(form[key]);
    if (n === null || Number.isNaN(n) || n < 0 || n > MAX_SECONDS) {
      errors[key] = `Enter whole seconds between 0 and ${MAX_SECONDS}`;
      return 0;
    }
    return n;
  };
  const settle = seconds('settle_window_seconds');
  const riskWait = seconds('risk_wait_max_seconds');
  if (!form.timezone.trim()) errors.timezone = 'Required';
  const attempts = parseOptionalInt(form.max_create_attempts_per_carrier);
  if (attempts === null || Number.isNaN(attempts) || attempts < 1 || attempts > 10) {
    errors.max_create_attempts_per_carrier = 'Enter a whole number between 1 and 10';
  }
  if (Object.keys(errors).length > 0) return { settings: null, errors };
  return {
    settings: {
      timezone: form.timezone.trim(),
      settle_window_seconds: settle,
      wait_for_risk_analysis: form.wait_for_risk_analysis,
      risk_wait_max_seconds: riskWait,
      review_tags: parseList(form.review_tags),
      block_on_high_risk: form.block_on_high_risk,
      cod_gateway_names: parseList(form.cod_gateway_names),
      fulfill_on: form.fulfill_on,
      notify_customer: form.notify_customer,
      shipped_tag: form.shipped_tag.trim() || null,
      max_create_attempts_per_carrier: attempts ?? 3,
      cancel_shipment_on_order_cancel: form.cancel_shipment_on_order_cancel,
    },
    errors,
  };
}

export function SettingsPage() {
  const { data, loading, error, reload, setData } = useApi(api.getSettings);
  const { reload: reloadSession } = useSession();
  const notice = useNotice();
  const [form, setForm] = useState<SettingsForm | null>(null);
  const [errors, setErrors] = useState<FormErrors>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (data) setForm(toForm(data));
  }, [data]);

  const set = <K extends keyof SettingsForm>(key: K, value: SettingsForm[K]) =>
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));

  const save = async () => {
    if (!form) return;
    const { settings, errors: next } = toPayload(form);
    setErrors(next);
    if (!settings) {
      notice.showError('Please fix the highlighted fields.');
      return;
    }
    setSaving(true);
    try {
      const saved = await api.putSettings({ automation_enabled: form.automation_enabled, settings });
      setData(saved);
      void reloadSession();
      notice.showSuccess('Settings saved');
    } catch (err) {
      notice.showError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <s-page heading="Settings">
      <s-button slot="primary-action" variant="primary" loading={saving} disabled={!form} onClick={() => void save()}>
        Save
      </s-button>
      <s-stack gap="base">
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner error={error} onRetry={() => void reload()} />
        {loading && !form && <Loading label="Loading settings" />}

        {form && (
          <>
            <s-section heading="Automation">
              <s-stack gap="base">
                <s-switch
                  label="Automatic shipping enabled"
                  checked={form.automation_enabled}
                  details="Master switch. When on, Trekiva allocates a carrier and creates shipments for new eligible orders automatically. When off, nothing is shipped automatically (orders are still recorded)."
                  onChange={(e) => set('automation_enabled', e.currentTarget.checked)}
                />
                {data && form.automation_enabled !== data.automation_enabled && (
                  <s-banner tone={form.automation_enabled ? 'warning' : 'info'}>
                    <s-paragraph>
                      {form.automation_enabled
                        ? 'Automation will be turned ON when you save. Make sure carriers, warehouses and rules are configured.'
                        : 'Automation will be turned OFF when you save.'}
                    </s-paragraph>
                  </s-banner>
                )}
                <s-text-field
                  label="Timezone"
                  value={form.timezone}
                  details="IANA timezone used for daily stats, e.g. Asia/Kolkata"
                  error={errors.timezone}
                  onInput={(e) => set('timezone', e.currentTarget.value)}
                />
              </s-stack>
            </s-section>

            <s-section heading="Order gate">
              <s-stack gap="base">
                <s-paragraph color="subdued">
                  Trekiva waits before processing a new order so Shopify Flow duplicate/risk workflows can tag it or place
                  a fulfillment hold. Holds and review tags can only ever block shipping, never allow it.
                </s-paragraph>
                <s-number-field
                  label="Settle window"
                  value={form.settle_window_seconds}
                  suffix="seconds"
                  min={0}
                  max={MAX_SECONDS}
                  step={1}
                  details="Wait this long after order creation before allocating."
                  error={errors.settle_window_seconds}
                  onInput={(e) => set('settle_window_seconds', e.currentTarget.value)}
                />
                <s-checkbox
                  label="Wait for Shopify risk analysis"
                  checked={form.wait_for_risk_analysis}
                  details="Also wait until Shopify's fraud analysis has completed (up to the maximum below)."
                  onChange={(e) => set('wait_for_risk_analysis', e.currentTarget.checked)}
                />
                {form.wait_for_risk_analysis && (
                  <s-number-field
                    label="Maximum risk analysis wait"
                    value={form.risk_wait_max_seconds}
                    suffix="seconds"
                    min={0}
                    max={MAX_SECONDS}
                    step={1}
                    error={errors.risk_wait_max_seconds}
                    onInput={(e) => set('risk_wait_max_seconds', e.currentTarget.value)}
                  />
                )}
                <s-text-field
                  label="Review tags"
                  value={form.review_tags}
                  placeholder="DUPLICATE-REVIEW, RISK-REVIEW"
                  details="Comma-separated. Orders carrying any of these tags go to Manual Review even without a Shopify hold."
                  onInput={(e) => set('review_tags', e.currentTarget.value)}
                />
                <s-checkbox
                  label="Block high-risk orders"
                  checked={form.block_on_high_risk}
                  details="Send HIGH-risk orders to Manual Review even if no Shopify hold was placed (in case a Flow workflow failed)."
                  onChange={(e) => set('block_on_high_risk', e.currentTarget.checked)}
                />
              </s-stack>
            </s-section>

            <s-section heading="Payment detection">
              <s-text-field
                label="COD gateway names"
                value={form.cod_gateway_names}
                placeholder="Cash on Delivery (COD), Cash on Delivery, COD"
                details="Comma-separated, case-insensitive. Read-only detection: orders paid through these gateways are shipped as COD. COD King remains responsible for OTP verification and COD fees — Trekiva never changes them."
                onInput={(e) => set('cod_gateway_names', e.currentTarget.value)}
              />
            </s-section>

            <s-section heading="Shopify fulfillment sync">
              <s-stack gap="base">
                <s-select
                  label="Mark orders fulfilled in Shopify when"
                  value={form.fulfill_on}
                  onChange={(e) => set('fulfill_on', e.currentTarget.value as FulfillOn)}
                >
                  <s-option value="AWB_CREATED">The AWB is created</s-option>
                  <s-option value="PICKED_UP">The carrier picks up the shipment</s-option>
                </s-select>
                <s-checkbox
                  label="Notify customer"
                  checked={form.notify_customer}
                  details="Let Shopify send the shipping confirmation email when the fulfillment is created."
                  onChange={(e) => set('notify_customer', e.currentTarget.checked)}
                />
                <s-text-field
                  label="Shipped tag"
                  value={form.shipped_tag}
                  placeholder="TREKIVA-SHIPPED"
                  details="Optional. Added to the order (never removed) when a shipment is created."
                  onInput={(e) => set('shipped_tag', e.currentTarget.value)}
                />
              </s-stack>
            </s-section>

            <s-section heading="Shipments">
              <s-stack gap="base">
                <s-number-field
                  label="Attempts per carrier on temporary errors"
                  value={form.max_create_attempts_per_carrier}
                  min={1}
                  max={10}
                  step={1}
                  details="Network errors and HTTP 503s are retried on the same carrier this many times before falling back to the next ranked carrier. Invalid pincodes and other rejections are never retried."
                  error={errors.max_create_attempts_per_carrier}
                  onInput={(e) => set('max_create_attempts_per_carrier', e.currentTarget.value)}
                />
                <s-checkbox
                  label="Cancel the courier shipment when an order is cancelled in Shopify"
                  checked={form.cancel_shipment_on_order_cancel}
                  details="Only possible before pickup. After pickup the order is flagged so you can arrange the return with the carrier."
                  onChange={(e) => set('cancel_shipment_on_order_cancel', e.currentTarget.checked)}
                />
              </s-stack>
            </s-section>

            <s-stack direction="inline" justifyContent="end">
              <s-button variant="primary" loading={saving} onClick={() => void save()}>
                Save settings
              </s-button>
            </s-stack>
          </>
        )}
      </s-stack>
    </s-page>
  );
}
