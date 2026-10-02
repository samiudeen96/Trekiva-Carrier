/**
 * Shipment actions (cancel, resolve an unconfirmed creation, retry the Shopify sync and — in
 * development with mock carriers — simulate tracking), shared by the Shipments and Order pages.
 *
 * Render `<ShipmentActionModals ref={actions} …/>` once per page and `<ShipmentActionButtons
 * actions={actions} shipment={…}/>` per row; the buttons open the page's modals.
 */
import { useImperativeHandle, useRef, useState, type Ref, type RefObject } from 'react';
import { api } from '../api';
import type { Notice } from '../hooks/useNotice';
import { useSession } from '../session';
import type { SessionInfo, ShipmentRow, TrackingStatus } from '../types';
import { humanizeCode } from '../utils';
import { ConfirmModal, type ConfirmModalHandle } from './ConfirmModal';
import { TRACKING_STATUSES } from './StatusBadges';

export interface ShipmentActionsHandle {
  cancel: (shipment: ShipmentRow) => void;
  resolve: (shipment: ShipmentRow) => void;
  syncShopify: (shipment: ShipmentRow) => void;
  simulateTracking: (shipment: ShipmentRow) => void;
}

/** The tracking simulator only exists for mock carriers outside production. */
export function canSimulateTracking(session: SessionInfo | null): boolean {
  return session !== null && session.app_env !== 'production' && session.mock_carriers_enabled;
}

function shipmentLabel(shipment: ShipmentRow): string {
  const ref = shipment.awb ? `AWB ${shipment.awb}` : `shipment #${shipment.id}`;
  return shipment.shopify_order_name ? `${ref} (${shipment.shopify_order_name})` : ref;
}

interface CancelForm {
  reallocate: boolean;
  reason: string;
  error?: string;
}

type ResolveChoice = 'created' | 'not_created';

interface ResolveForm {
  choice: ResolveChoice | '';
  awb: string;
  carrierShipmentId: string;
  errors: { choice?: string; awb?: string };
}

interface SimulateForm {
  status: TrackingStatus;
  location: string;
}

const EMPTY_RESOLVE: ResolveForm = { choice: '', awb: '', carrierShipmentId: '', errors: {} };

export function ShipmentActionModals({
  ref,
  notice,
  onChanged,
}: {
  ref: Ref<ShipmentActionsHandle>;
  notice: Notice;
  /** Called with the updated shipment after any successful action (pages typically reload). */
  onChanged: (shipment: ShipmentRow) => void | Promise<void>;
}) {
  const cancelModal = useRef<ConfirmModalHandle>(null);
  const resolveModal = useRef<ConfirmModalHandle>(null);
  const syncModal = useRef<ConfirmModalHandle>(null);
  const simulateModal = useRef<ConfirmModalHandle>(null);

  const [target, setTarget] = useState<ShipmentRow | null>(null);
  const [cancelForm, setCancelForm] = useState<CancelForm>({ reallocate: false, reason: '' });
  const [resolveForm, setResolveForm] = useState<ResolveForm>(EMPTY_RESOLVE);
  const [simulateForm, setSimulateForm] = useState<SimulateForm>({ status: 'IN_TRANSIT', location: '' });

  useImperativeHandle(ref, () => ({
    cancel: (shipment) => {
      setTarget(shipment);
      setCancelForm({ reallocate: false, reason: '' });
      cancelModal.current?.open();
    },
    resolve: (shipment) => {
      setTarget(shipment);
      setResolveForm(EMPTY_RESOLVE);
      resolveModal.current?.open();
    },
    syncShopify: (shipment) => {
      setTarget(shipment);
      syncModal.current?.open();
    },
    simulateTracking: (shipment) => {
      setTarget(shipment);
      setSimulateForm((prev) => ({ ...prev, location: '' }));
      simulateModal.current?.open();
    },
  }));

  const cancel = async () => {
    if (!target) return;
    const reason = cancelForm.reason.trim();
    if (!reason) {
      setCancelForm((prev) => ({ ...prev, error: 'Enter a reason for the audit log' }));
      return false;
    }
    try {
      const updated = await api.cancelShipment(target.id, { reallocate: cancelForm.reallocate, reason });
      notice.showSuccess(
        `${shipmentLabel(target)} ${updated.status === 'CANCELLED' ? 'cancelled' : `is now ${humanizeCode(updated.status)}`}` +
          (cancelForm.reallocate ? ' — re-allocation queued.' : '.'),
      );
      await onChanged(updated);
    } catch (err) {
      notice.showError(err);
    }
  };

  const resolve = async () => {
    if (!target) return;
    const errors: ResolveForm['errors'] = {};
    if (!resolveForm.choice) errors.choice = 'Choose what happened at the carrier';
    if (resolveForm.choice === 'created' && !resolveForm.awb.trim()) errors.awb = 'Enter the AWB from the carrier panel';
    setResolveForm((prev) => ({ ...prev, errors }));
    if (Object.keys(errors).length > 0) return false;
    const created = resolveForm.choice === 'created';
    try {
      const updated = await api.resolveShipment(
        target.id,
        created
          ? {
              created: true,
              awb: resolveForm.awb.trim(),
              ...(resolveForm.carrierShipmentId.trim()
                ? { carrier_shipment_id: resolveForm.carrierShipmentId.trim() }
                : {}),
            }
          : { created: false },
      );
      notice.showSuccess(
        created
          ? `Shipment #${target.id} marked as created (AWB ${updated.awb ?? resolveForm.awb.trim()}).`
          : `Shipment #${target.id} marked as not created.`,
      );
      await onChanged(updated);
    } catch (err) {
      notice.showError(err);
    }
  };

  const syncShopify = async () => {
    if (!target) return;
    try {
      const result = await api.syncShipmentToShopify(target.id);
      const text = `Shopify sync: ${humanizeCode(result.outcome)}${result.detail ? ` — ${result.detail}` : ''}`;
      if (result.shipment.shopify_sync_status === 'FAILED') notice.showError(text);
      else notice.showSuccess(text);
      await onChanged(result.shipment);
    } catch (err) {
      notice.showError(err);
    }
  };

  const simulate = async () => {
    if (!target) return;
    try {
      const location = simulateForm.location.trim();
      const result = await api.simulateTracking(target.id, {
        status: simulateForm.status,
        ...(location ? { location } : {}),
      });
      notice.showSuccess(
        result.applied
          ? `Simulated "${humanizeCode(simulateForm.status)}" — shipment is now ${humanizeCode(result.shipment.status)}.`
          : `Simulated "${humanizeCode(simulateForm.status)}" — the event was recorded but not applied to the shipment.`,
      );
      await onChanged(result.shipment);
    } catch (err) {
      notice.showError(err);
    }
  };

  const label = target ? shipmentLabel(target) : 'this shipment';

  return (
    <>
      <ConfirmModal
        ref={cancelModal}
        id="cancel-shipment-modal"
        heading="Cancel shipment?"
        confirmLabel="Cancel shipment"
        destructive
        onConfirm={cancel}
      >
        <s-stack gap="base">
          <s-paragraph>
            Trekiva will ask the carrier to cancel <s-text type="strong">{label}</s-text>. This cannot be undone; a new
            shipment (and AWB) is needed to ship the order afterwards.
          </s-paragraph>
          <s-text-field
            label="Reason"
            value={cancelForm.reason}
            placeholder="e.g. Customer changed the address"
            error={cancelForm.error}
            required
            autocomplete="off"
            onInput={(e) => {
              const reason = e.currentTarget.value;
              setCancelForm((prev) => ({ ...prev, reason, error: undefined }));
            }}
          />
          <s-checkbox
            label="Re-allocate automatically after cancelling"
            details="Runs allocation again (possibly picking another carrier) once the cancellation is confirmed."
            checked={cancelForm.reallocate}
            onChange={(e) => {
              const reallocate = e.currentTarget.checked;
              setCancelForm((prev) => ({ ...prev, reallocate }));
            }}
          />
        </s-stack>
      </ConfirmModal>

      <ConfirmModal
        ref={resolveModal}
        id="resolve-shipment-modal"
        heading="Resolve unconfirmed shipment"
        confirmLabel="Save resolution"
        onConfirm={resolve}
      >
        <s-stack gap="base">
          <s-paragraph>
            The carrier did not confirm whether this shipment was created. Check the carrier panel for reference{' '}
            <s-text type="strong">{target?.idempotency_key ?? '—'}</s-text>
            {target ? ` (${target.carrier_code})` : ''}.
          </s-paragraph>
          <s-choice-list
            label="What does the carrier panel show?"
            name="resolve-choice"
            values={resolveForm.choice ? [resolveForm.choice] : []}
            error={resolveForm.errors.choice}
            onChange={(e) => {
              const choice = (e.currentTarget.values[0] ?? '') as ResolveChoice | '';
              setResolveForm((prev) => ({ ...prev, choice, errors: {} }));
            }}
          >
            <s-choice value="created">It was created</s-choice>
            <s-choice value="not_created">It was not created</s-choice>
          </s-choice-list>
          {resolveForm.choice === 'created' && (
            <s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base">
              <s-text-field
                label="AWB"
                value={resolveForm.awb}
                required
                error={resolveForm.errors.awb}
                autocomplete="off"
                onInput={(e) => {
                  const awb = e.currentTarget.value;
                  setResolveForm((prev) => ({ ...prev, awb, errors: { ...prev.errors, awb: undefined } }));
                }}
              />
              <s-text-field
                label="Carrier shipment ID"
                value={resolveForm.carrierShipmentId}
                details="Optional"
                autocomplete="off"
                onInput={(e) => {
                  const carrierShipmentId = e.currentTarget.value;
                  setResolveForm((prev) => ({ ...prev, carrierShipmentId }));
                }}
              />
            </s-grid>
          )}
          {resolveForm.choice === 'not_created' && (
            <s-paragraph color="subdued">
              Trekiva will record that the carrier has no such shipment, so the order can be allocated again.
            </s-paragraph>
          )}
        </s-stack>
      </ConfirmModal>

      <ConfirmModal
        ref={syncModal}
        id="sync-shipment-modal"
        heading="Retry Shopify sync?"
        confirmLabel="Retry sync"
        onConfirm={syncShopify}
      >
        <s-stack gap="small">
          <s-paragraph>
            Trekiva will push the fulfillment and tracking details for <s-text type="strong">{label}</s-text> to Shopify
            again.
          </s-paragraph>
          {target?.shopify_sync_error && (
            <s-paragraph color="subdued">Last error: {target.shopify_sync_error}</s-paragraph>
          )}
        </s-stack>
      </ConfirmModal>

      <ConfirmModal
        ref={simulateModal}
        id="simulate-tracking-modal"
        heading="Simulate tracking (mock carrier)"
        confirmLabel="Send tracking event"
        onConfirm={simulate}
      >
        <s-stack gap="base">
          <s-paragraph color="subdued">
            Development only: injects a tracking event for <s-text type="strong">{label}</s-text> as if the mock
            carrier had sent it, so the shipment lifecycle and Shopify sync can be tested.
          </s-paragraph>
          <s-select
            label="Tracking status"
            value={simulateForm.status}
            onChange={(e) => {
              const status = e.currentTarget.value as TrackingStatus;
              setSimulateForm((prev) => ({ ...prev, status }));
            }}
          >
            {TRACKING_STATUSES.map((status) => (
              <s-option key={status} value={status}>
                {humanizeCode(status)}
              </s-option>
            ))}
          </s-select>
          <s-text-field
            label="Location"
            value={simulateForm.location}
            details="Optional, e.g. Bengaluru hub"
            autocomplete="off"
            onInput={(e) => {
              const location = e.currentTarget.value;
              setSimulateForm((prev) => ({ ...prev, location }));
            }}
          />
        </s-stack>
      </ConfirmModal>
    </>
  );
}

/** Row-level buttons for whichever actions the shipment currently allows. */
export function ShipmentActionButtons({
  shipment,
  actions,
}: {
  shipment: ShipmentRow;
  actions: RefObject<ShipmentActionsHandle | null>;
}) {
  const { session } = useSession();
  const simulate = canSimulateTracking(session);
  const any = shipment.can_cancel || shipment.can_resolve || shipment.can_retry_shopify_sync || simulate;
  if (!any) return <s-text color="subdued">—</s-text>;

  return (
    <s-stack direction="inline" gap="small-200">
      {shipment.can_resolve && (
        <s-button variant="primary" onClick={() => actions.current?.resolve(shipment)}>
          Resolve
        </s-button>
      )}
      {shipment.can_retry_shopify_sync && (
        <s-button onClick={() => actions.current?.syncShopify(shipment)}>Retry Shopify sync</s-button>
      )}
      {shipment.can_cancel && (
        <s-button tone="critical" onClick={() => actions.current?.cancel(shipment)}>
          Cancel
        </s-button>
      )}
      {simulate && (
        <s-button variant="tertiary" onClick={() => actions.current?.simulateTracking(shipment)}>
          Simulate tracking (mock carrier)
        </s-button>
      )}
    </s-stack>
  );
}
