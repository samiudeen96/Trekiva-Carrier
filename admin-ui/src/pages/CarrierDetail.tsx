import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api';
import { ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import {
  JsonSchemaForm,
  buildCredentials,
  fieldsFromSchema,
  initialValues,
  type FieldValue,
  type FormValues,
} from '../components/JsonSchemaForm';
import { useApi } from '../hooks/useApi';
import { useNotice, type Notice } from '../hooks/useNotice';
import { useSession } from '../session';
import type { Carrier, CarrierEnvironment, SessionInfo, Warehouse } from '../types';
import { humanizeCode, isValidNumber, parseOptionalDecimal, parseOptionalInt, toInput } from '../utils';
import { EnabledBadge, EnvironmentBadge, ImplementationBadge, activeAccount } from './Carriers';

interface SectionProps {
  carrier: Carrier;
  onSaved: (carrier: Carrier) => void;
  notice: Notice;
}

// --- (a) settings ------------------------------------------------------------------------

interface SettingsForm {
  enabled: boolean;
  cod_enabled: boolean;
  prepaid_enabled: boolean;
  priority: string;
  max_shipping_cost: string;
  min_weight_g: string;
  max_weight_g: string;
  performance_score: string;
}

function settingsForm(carrier: Carrier): SettingsForm {
  const s = carrier.settings;
  return {
    enabled: s.enabled,
    cod_enabled: s.cod_enabled,
    prepaid_enabled: s.prepaid_enabled,
    priority: String(s.priority),
    max_shipping_cost: toInput(s.max_shipping_cost),
    min_weight_g: toInput(s.min_weight_g),
    max_weight_g: toInput(s.max_weight_g),
    performance_score: toInput(s.performance_score),
  };
}

function SettingsSection({ carrier, onSaved, notice }: SectionProps) {
  const [form, setForm] = useState<SettingsForm>(() => settingsForm(carrier));
  const [errors, setErrors] = useState<Partial<Record<keyof SettingsForm, string>>>({});
  const [saving, setSaving] = useState(false);
  const hasAccount = activeAccount(carrier)?.is_active === true;

  // Re-sync only when the stored settings change (not on unrelated carrier updates).
  const settingsKey = JSON.stringify(carrier.settings);
  useEffect(() => setForm(settingsForm(carrier)), [settingsKey]);

  const set = <K extends keyof SettingsForm>(key: K, value: SettingsForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    const next: Partial<Record<keyof SettingsForm, string>> = {};
    const priority = parseOptionalInt(form.priority);
    if (priority === null || Number.isNaN(priority) || priority < 0 || priority > 10000)
      next.priority = 'Enter a whole number between 0 and 10000';
    const minW = parseOptionalInt(form.min_weight_g);
    const maxW = parseOptionalInt(form.max_weight_g);
    if (Number.isNaN(minW) || (minW !== null && minW < 0)) next.min_weight_g = 'Enter a whole number of grams (≥ 0)';
    if (Number.isNaN(maxW) || (maxW !== null && maxW < 0)) next.max_weight_g = 'Enter a whole number of grams (≥ 0)';
    if (minW !== null && maxW !== null && !Number.isNaN(minW) && !Number.isNaN(maxW) && maxW < minW)
      next.max_weight_g = 'Must be greater than or equal to the minimum weight';
    if (!isValidNumber(form.max_shipping_cost) || Number(form.max_shipping_cost) < 0)
      next.max_shipping_cost = 'Enter an amount ≥ 0';
    const perf = form.performance_score.trim();
    if (!isValidNumber(perf) || (perf !== '' && (Number(perf) < 0 || Number(perf) > 100)))
      next.performance_score = 'Enter a score between 0 and 100';
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    setSaving(true);
    try {
      const updated = await api.putCarrierSettings(carrier.code, {
        enabled: form.enabled,
        cod_enabled: form.cod_enabled,
        prepaid_enabled: form.prepaid_enabled,
        priority: priority ?? 100,
        max_shipping_cost: parseOptionalDecimal(form.max_shipping_cost),
        min_weight_g: minW,
        max_weight_g: maxW,
        performance_score: parseOptionalDecimal(form.performance_score),
      });
      onSaved(updated);
      notice.showSuccess(`${carrier.display_name} settings saved`);
    } catch (err) {
      notice.showError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <s-section heading="Settings">
      <s-stack gap="base">
        <s-switch
          label="Carrier enabled"
          checked={form.enabled}
          details={
            hasAccount
              ? 'When enabled, the allocation engine may choose this carrier.'
              : 'Save and activate credentials below before enabling this carrier.'
          }
          onChange={(e) => set('enabled', e.currentTarget.checked)}
        />
        <s-stack direction="inline" gap="large">
          <s-checkbox
            label="COD orders"
            checked={form.cod_enabled}
            onChange={(e) => set('cod_enabled', e.currentTarget.checked)}
          />
          <s-checkbox
            label="Prepaid orders"
            checked={form.prepaid_enabled}
            onChange={(e) => set('prepaid_enabled', e.currentTarget.checked)}
          />
        </s-stack>
        <s-grid gridTemplateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="base">
          <s-number-field
            label="Priority"
            value={form.priority}
            step={1}
            min={0}
            max={10000}
            details="Lower = preferred"
            error={errors.priority}
            onInput={(e) => set('priority', e.currentTarget.value)}
          />
          <s-number-field
            label="Max shipping cost"
            value={form.max_shipping_cost}
            step={0.01}
            min={0}
            prefix="₹"
            details="Optional. Offers above this are skipped."
            error={errors.max_shipping_cost}
            onInput={(e) => set('max_shipping_cost', e.currentTarget.value)}
          />
          <s-number-field
            label="Min weight"
            value={form.min_weight_g}
            step={1}
            min={0}
            suffix="g"
            details="Optional"
            error={errors.min_weight_g}
            onInput={(e) => set('min_weight_g', e.currentTarget.value)}
          />
          <s-number-field
            label="Max weight"
            value={form.max_weight_g}
            step={1}
            min={0}
            suffix="g"
            details="Optional"
            error={errors.max_weight_g}
            onInput={(e) => set('max_weight_g', e.currentTarget.value)}
          />
          <s-number-field
            label="Performance score"
            value={form.performance_score}
            step={1}
            min={0}
            max={100}
            details="0–100, used by BALANCED rules"
            error={errors.performance_score}
            onInput={(e) => set('performance_score', e.currentTarget.value)}
          />
        </s-grid>
        <s-stack direction="inline" justifyContent="end">
          <s-button variant="primary" loading={saving} onClick={() => void save()}>
            Save settings
          </s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

// --- (b) credentials ---------------------------------------------------------------------

const ACCOUNT_LABEL = 'default';

function allowedEnvironments(carrier: Carrier, session: SessionInfo | null): CarrierEnvironment[] {
  const envs: CarrierEnvironment[] = [];
  if (session?.mock_carriers_enabled && carrier.has_mock_adapter) envs.push('MOCK');
  if (carrier.has_real_adapter) envs.push('SANDBOX', 'PRODUCTION');
  return envs;
}

function CredentialsSection({ carrier, onSaved, notice }: SectionProps) {
  const { session } = useSession();
  const envs = allowedEnvironments(carrier, session);
  const active = activeAccount(carrier);
  const defaultEnv = active && envs.includes(active.environment) ? active.environment : envs[0];
  const [environment, setEnvironment] = useState<CarrierEnvironment | undefined>(defaultEnv);

  const envKey = envs.join(',');
  useEffect(() => {
    if (!environment || !envs.includes(environment)) setEnvironment(defaultEnv);
  }, [envKey, defaultEnv]);

  const schema = environment === 'MOCK' ? carrier.mock_credentials_schema : carrier.credentials_schema;
  const schemaKey = JSON.stringify(schema ?? null);
  const fields = useMemo(() => fieldsFromSchema(schema), [schemaKey]);
  const account = carrier.accounts.find((a) => a.environment === environment && a.label === ACCOUNT_LABEL);
  const hint = account?.credentials_hint;
  const hintKey = `${account?.id ?? 'new'}:${account?.updated_at ?? ''}`;

  const [values, setValues] = useState<FormValues>(() => initialValues(fields, hint));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  // Reset when switching environment/schema or after the stored credentials change.
  useEffect(() => {
    setValues(initialValues(fields, hint));
    setErrors({});
  }, [fields, hintKey]);

  const onChange = (name: string, value: FieldValue) => setValues((prev) => ({ ...prev, [name]: value }));

  const save = async () => {
    if (!environment) return;
    const { credentials, errors: fieldErrors } = buildCredentials(fields, values, account !== undefined);
    setErrors(fieldErrors);
    if (Object.keys(fieldErrors).length > 0) return;
    setSaving(true);
    try {
      const updated = await api.putCarrierAccount(carrier.code, {
        environment,
        label: ACCOUNT_LABEL,
        credentials,
        is_active: true,
        make_active: true,
      });
      onSaved(updated);
      notice.showSuccess(`${carrier.display_name} ${environment} credentials saved and activated`);
    } catch (err) {
      notice.showError(err);
    } finally {
      setSaving(false);
    }
  };

  if (envs.length === 0) {
    return (
      <s-section heading="Credentials">
        <s-banner tone="warning">
          <s-paragraph>
            No adapter is available for this carrier in this environment
            {carrier.has_mock_adapter ? ' (mock carriers are disabled here)' : ''}.
          </s-paragraph>
        </s-banner>
      </s-section>
    );
  }

  return (
    <s-section heading="Credentials">
      <s-stack gap="base">
        {carrier.implementation_status === 'awaiting_api_docs' && (
          <s-banner tone="info" heading="Integration awaiting carrier API documentation">
            <s-paragraph>
              You can store credentials now; the carrier will not be used for live shipments until its integration is
              completed.
            </s-paragraph>
          </s-banner>
        )}
        <s-stack direction="inline" gap="base" alignItems="center">
          <s-text color="subdued">Currently active:</s-text>
          <EnvironmentBadge environment={active?.environment} />
        </s-stack>
        <s-select
          label="Environment"
          value={environment}
          details="Saving makes this environment's account the active one for the carrier."
          onChange={(e) => setEnvironment(e.currentTarget.value as CarrierEnvironment)}
        >
          {envs.map((env) => (
            <s-option key={env} value={env}>
              {humanizeCode(env)}
              {carrier.accounts.some((a) => a.environment === env) ? ' (saved)' : ''}
            </s-option>
          ))}
        </s-select>
        <JsonSchemaForm fields={fields} values={values} errors={errors} hint={hint} onChange={onChange} />
        <s-stack direction="inline" justifyContent="end">
          <s-button variant="primary" loading={saving} onClick={() => void save()}>
            Save and activate credentials
          </s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

// --- (c) warehouses ----------------------------------------------------------------------

type MappingState = Record<number, { enabled: boolean; ref: string }>;

function mappingState(carrier: Carrier, warehouses: Warehouse[]): MappingState {
  const state: MappingState = {};
  for (const w of warehouses) {
    const m = carrier.warehouse_mappings.find((x) => x.warehouse_id === w.id);
    state[w.id] = { enabled: m?.enabled ?? false, ref: m?.carrier_warehouse_ref ?? '' };
  }
  return state;
}

function WarehousesSection({ carrier, onSaved, notice, warehouses }: SectionProps & { warehouses: Warehouse[] }) {
  const navigate = useNavigate();
  const [state, setState] = useState<MappingState>(() => mappingState(carrier, warehouses));
  const [saving, setSaving] = useState(false);
  const hasAccount = activeAccount(carrier)?.is_active === true;

  const mappingKey = JSON.stringify([carrier.warehouse_mappings, warehouses.map((w) => w.id)]);
  useEffect(() => setState(mappingState(carrier, warehouses)), [mappingKey]);

  const update = (id: number, patch: Partial<MappingState[number]>) =>
    setState((prev) => ({ ...prev, [id]: { ...(prev[id] ?? { enabled: false, ref: '' }), ...patch } }));

  const save = async () => {
    setSaving(true);
    try {
      const body = warehouses.map((w) => ({
        warehouse_id: w.id,
        carrier_warehouse_ref: state[w.id]?.ref.trim() || null,
        enabled: state[w.id]?.enabled ?? false,
      }));
      const updated = await api.putCarrierWarehouses(carrier.code, body);
      onSaved(updated);
      notice.showSuccess('Supported warehouses saved');
    } catch (err) {
      notice.showError(err);
    } finally {
      setSaving(false);
    }
  };

  return (
    <s-section heading="Supported warehouses">
      <s-stack gap="base">
        {!hasAccount && (
          <s-banner tone="info">
            <s-paragraph>Save credentials first — warehouse mappings belong to the active carrier account.</s-paragraph>
          </s-banner>
        )}
        {warehouses.length === 0 ? (
          <s-stack gap="small">
            <s-paragraph color="subdued">No warehouses yet.</s-paragraph>
            <s-stack direction="inline">
              <s-button onClick={() => navigate('/warehouses')}>Add a warehouse</s-button>
            </s-stack>
          </s-stack>
        ) : (
          <s-table>
            <s-table-header-row>
              <s-table-header>Supported</s-table-header>
              <s-table-header listSlot="primary">Warehouse</s-table-header>
              <s-table-header>Carrier warehouse / pickup ref (optional)</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {warehouses.map((w) => (
                <s-table-row key={w.id}>
                  <s-table-cell>
                    <s-checkbox
                      accessibilityLabel={`Supported: ${w.name}`}
                      checked={state[w.id]?.enabled ?? false}
                      disabled={!hasAccount}
                      onChange={(e) => update(w.id, { enabled: e.currentTarget.checked })}
                    />
                  </s-table-cell>
                  <s-table-cell>
                    <s-stack gap="small-300">
                      <s-text type="strong">
                        {w.name} ({w.code})
                      </s-text>
                      <s-text color="subdued">
                        {w.city}, {w.state} {w.pincode}
                        {!w.is_active ? ' · inactive' : ''}
                      </s-text>
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>
                    <s-text-field
                      label="Carrier reference"
                      labelAccessibilityVisibility="exclusive"
                      value={state[w.id]?.ref ?? ''}
                      placeholder="e.g. pickup location name"
                      disabled={!hasAccount}
                      onInput={(e) => update(w.id, { ref: e.currentTarget.value })}
                    />
                  </s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
        {warehouses.length > 0 && (
          <s-stack direction="inline" justifyContent="end">
            <s-button variant="primary" loading={saving} disabled={!hasAccount} onClick={() => void save()}>
              Save warehouses
            </s-button>
          </s-stack>
        )}
      </s-stack>
    </s-section>
  );
}

// --- (d) capabilities --------------------------------------------------------------------

function CapabilitiesSection({ carrier }: { carrier: Carrier }) {
  const entries = Object.entries(carrier.capabilities);
  return (
    <s-section heading="Capabilities">
      {entries.length === 0 ? (
        <s-paragraph color="subdued">No capabilities reported.</s-paragraph>
      ) : (
        <s-stack direction="inline" gap="small">
          {entries.map(([name, supported]) => (
            <s-badge key={name} tone={supported ? 'success' : 'neutral'} icon={supported ? 'check' : 'x'}>
              {humanizeCode(name)}
            </s-badge>
          ))}
        </s-stack>
      )}
    </s-section>
  );
}

// --- page --------------------------------------------------------------------------------

export function CarrierDetailPage() {
  const { code = '' } = useParams();
  const notice = useNotice();
  const { data, loading, error, reload, setData } = useApi(
    async () => {
      const [carrier, warehouses] = await Promise.all([api.getCarrier(code), api.listWarehouses()]);
      return { carrier, warehouses };
    },
    [code],
  );

  const onSaved = (carrier: Carrier) => {
    if (data) setData({ ...data, carrier });
  };

  const carrier = data?.carrier;

  return (
    <s-page heading={carrier?.display_name ?? 'Carrier'}>
      <s-link slot="breadcrumb-actions" href="/carriers">
        Carriers
      </s-link>
      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        {loading && !data && <Loading label="Loading carrier" />}
        {carrier && data && (
          <>
            <s-stack direction="inline" gap="small" alignItems="center">
              <s-text color="subdued">{carrier.code}</s-text>
              <EnabledBadge enabled={carrier.settings.enabled} />
              <ImplementationBadge carrier={carrier} />
              <EnvironmentBadge environment={activeAccount(carrier)?.environment} />
            </s-stack>
            <SettingsSection carrier={carrier} onSaved={onSaved} notice={notice} />
            <CredentialsSection carrier={carrier} onSaved={onSaved} notice={notice} />
            <WarehousesSection carrier={carrier} onSaved={onSaved} notice={notice} warehouses={data.warehouses} />
            <CapabilitiesSection carrier={carrier} />
          </>
        )}
      </s-stack>
    </s-page>
  );
}
