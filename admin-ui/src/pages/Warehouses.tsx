import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api';
import { EmptyMessage, ErrorBanner, Loading, NoticeBanners } from '../components/Feedback';
import { useApi } from '../hooks/useApi';
import { useNotice } from '../hooks/useNotice';
import type { Warehouse, WarehouseIn } from '../types';
import { parseOptionalDecimal, parseOptionalInt, toInput } from '../utils';

// --- list --------------------------------------------------------------------------------

export function WarehousesPage() {
  const navigate = useNavigate();
  const { data, loading, error, reload } = useApi(api.listWarehouses);

  return (
    <s-page heading="Warehouses">
      <s-button variant="primary" slot="primary-action" onClick={() => navigate('/warehouses/new')}>
        Add warehouse
      </s-button>
      <s-stack gap="base">
        <ErrorBanner error={error} onRetry={() => void reload()} />
        <s-section>
          <s-paragraph color="subdued">
            Pickup locations shipments are created from. Map each warehouse to the carriers that serve it on the
            carrier&apos;s page.
          </s-paragraph>
          {loading && !data && <Loading label="Loading warehouses" />}
          {data && data.length === 0 && (
            <EmptyMessage heading="No warehouses yet">Add your first pickup location to start shipping.</EmptyMessage>
          )}
          {data && data.length > 0 && (
            <s-table>
              <s-table-header-row>
                <s-table-header listSlot="primary">Warehouse</s-table-header>
                <s-table-header listSlot="kicker">Code</s-table-header>
                <s-table-header listSlot="secondary">Location</s-table-header>
                <s-table-header>Phone</s-table-header>
                <s-table-header>Shopify location</s-table-header>
                <s-table-header listSlot="inline">Status</s-table-header>
              </s-table-header-row>
              <s-table-body>
                {data.map((w) => (
                  <s-table-row key={w.id} clickDelegate={`warehouse-link-${w.id}`}>
                    <s-table-cell>
                      <s-link id={`warehouse-link-${w.id}`} href={`/warehouses/${w.id}`}>
                        {w.name}
                      </s-link>
                    </s-table-cell>
                    <s-table-cell>{w.code}</s-table-cell>
                    <s-table-cell>
                      {w.city}, {w.state} {w.pincode}
                    </s-table-cell>
                    <s-table-cell>{w.phone}</s-table-cell>
                    <s-table-cell>{w.shopify_location_id ? 'Linked' : '—'}</s-table-cell>
                    <s-table-cell>
                      {w.is_active ? <s-badge tone="success">Active</s-badge> : <s-badge tone="neutral">Inactive</s-badge>}
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

// --- form --------------------------------------------------------------------------------

interface WarehouseForm {
  code: string;
  name: string;
  contact_name: string;
  phone: string;
  email: string;
  address1: string;
  address2: string;
  city: string;
  state: string;
  pincode: string;
  country: string;
  shopify_location_id: string;
  length_cm: string;
  breadth_cm: string;
  height_cm: string;
  min_weight_g: string;
  is_active: boolean;
}

type FormErrors = Partial<Record<keyof WarehouseForm, string>>;
type TextKey = Exclude<keyof WarehouseForm, 'is_active'>;

const EMPTY_FORM: WarehouseForm = {
  code: '',
  name: '',
  contact_name: '',
  phone: '',
  email: '',
  address1: '',
  address2: '',
  city: '',
  state: '',
  pincode: '',
  country: 'IN',
  shopify_location_id: '',
  length_cm: '',
  breadth_cm: '',
  height_cm: '',
  min_weight_g: '',
  is_active: true,
};

function toForm(w: Warehouse): WarehouseForm {
  const pkg = w.default_package ?? {};
  return {
    code: w.code,
    name: w.name,
    contact_name: w.contact_name ?? '',
    phone: w.phone,
    email: w.email ?? '',
    address1: w.address1,
    address2: w.address2 ?? '',
    city: w.city,
    state: w.state,
    pincode: w.pincode,
    country: w.country || 'IN',
    shopify_location_id: w.shopify_location_id ?? '',
    length_cm: toInput(pkg.length_cm),
    breadth_cm: toInput(pkg.breadth_cm),
    height_cm: toInput(pkg.height_cm),
    min_weight_g: toInput(pkg.min_weight_g),
    is_active: w.is_active,
  };
}

function validate(form: WarehouseForm): FormErrors {
  const errors: FormErrors = {};
  const required: TextKey[] = ['code', 'name', 'phone', 'address1', 'city', 'state', 'pincode'];
  for (const key of required) if (!form[key].trim()) errors[key] = 'Required';
  if (form.code.trim() && !/^[A-Za-z0-9_-]+$/.test(form.code.trim()))
    errors.code = 'Letters, numbers, "-" and "_" only';
  if (form.code.trim().length > 50) errors.code = 'At most 50 characters';
  const phone = form.phone.trim();
  if (phone && (phone.length < 5 || phone.length > 32)) errors.phone = 'Enter 5–32 characters';
  if (form.pincode.trim() && !/^\d{6}$/.test(form.pincode.trim())) errors.pincode = 'Enter a 6-digit pincode';
  if (form.email.trim() && !/^[^\s@]+@[^\s@]+$/.test(form.email.trim())) errors.email = 'Enter a valid email';
  const loc = form.shopify_location_id.trim();
  if (loc && !/^gid:\/\/shopify\/Location\/\d+$/.test(loc))
    errors.shopify_location_id = 'Format: gid://shopify/Location/123';
  for (const key of ['length_cm', 'breadth_cm', 'height_cm'] as const) {
    const v = form[key].trim();
    if (v && !(Number(v) > 0)) errors[key] = 'Must be greater than 0';
  }
  const minW = parseOptionalInt(form.min_weight_g);
  if (minW !== null && (Number.isNaN(minW) || minW <= 0)) errors.min_weight_g = 'Whole number of grams, > 0';
  return errors;
}

function toPayload(form: WarehouseForm): WarehouseIn {
  const opt = (v: string) => (v.trim() === '' ? null : v.trim());
  return {
    code: form.code.trim(),
    name: form.name.trim(),
    contact_name: opt(form.contact_name),
    phone: form.phone.trim(),
    email: opt(form.email),
    address1: form.address1.trim(),
    address2: opt(form.address2),
    city: form.city.trim(),
    state: form.state.trim(),
    pincode: form.pincode.trim(),
    country: form.country.trim().toUpperCase() || 'IN',
    shopify_location_id: opt(form.shopify_location_id),
    default_package: {
      length_cm: parseOptionalDecimal(form.length_cm),
      breadth_cm: parseOptionalDecimal(form.breadth_cm),
      height_cm: parseOptionalDecimal(form.height_cm),
      min_weight_g: parseOptionalInt(form.min_weight_g),
    },
    is_active: form.is_active,
  };
}

export function WarehouseEditPage() {
  const { id } = useParams();
  const isNew = id === undefined || id === 'new';
  const warehouseId = isNew ? null : Number(id);
  const navigate = useNavigate();
  const notice = useNotice();

  const { data, loading, error, reload } = useApi(
    async () => (warehouseId === null ? null : ((await api.listWarehouses()).find((w) => w.id === warehouseId) ?? null)),
    [warehouseId],
  );

  const [form, setForm] = useState<WarehouseForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<FormErrors>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setForm(data ? toForm(data) : EMPTY_FORM);
    setErrors({});
  }, [data]);

  const set = <K extends keyof WarehouseForm>(key: K, value: WarehouseForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    const next = validate(form);
    setErrors(next);
    if (Object.keys(next).length > 0) {
      notice.showError('Please fix the highlighted fields.');
      return;
    }
    setSaving(true);
    try {
      const payload = toPayload(form);
      const saved = warehouseId === null ? await api.createWarehouse(payload) : await api.updateWarehouse(warehouseId, payload);
      notice.showSuccess(`Warehouse ${saved.name} saved`);
      navigate('/warehouses');
    } catch (err) {
      notice.showError(err);
    } finally {
      setSaving(false);
    }
  };

  const text = (key: TextKey, label: string, opts: { details?: string; placeholder?: string; required?: boolean } = {}) => (
    <s-text-field
      label={label}
      name={key}
      value={form[key]}
      required={opts.required}
      details={opts.details}
      placeholder={opts.placeholder}
      error={errors[key]}
      onInput={(e) => set(key, e.currentTarget.value)}
    />
  );

  const notFound = !isNew && !loading && !error && data === null;

  return (
    <s-page heading={isNew ? 'Add warehouse' : (data?.name ?? 'Edit warehouse')}>
      <s-link slot="breadcrumb-actions" href="/warehouses">
        Warehouses
      </s-link>
      <s-button slot="primary-action" variant="primary" loading={saving} disabled={notFound} onClick={() => void save()}>
        Save
      </s-button>
      <s-button slot="secondary-actions" onClick={() => navigate('/warehouses')}>
        Cancel
      </s-button>

      <s-stack gap="base">
        <NoticeBanners error={notice.error} success={notice.success} onClear={notice.clear} />
        <ErrorBanner error={error} onRetry={() => void reload()} />
        {notFound && <ErrorBanner heading="Not found" error="This warehouse does not exist." />}
        {!isNew && loading && <Loading label="Loading warehouse" />}

        {(isNew || data) && (
          <>
            <s-section heading="Details">
              <s-grid gridTemplateColumns="repeat(auto-fit, minmax(240px, 1fr))" gap="base">
                {text('name', 'Name', { required: true })}
                {text('code', 'Code', { required: true, details: 'Unique short code, e.g. BLR-01' })}
                {text('contact_name', 'Contact name')}
                {text('phone', 'Phone', { required: true })}
                <s-email-field
                  label="Email"
                  name="email"
                  value={form.email}
                  error={errors.email}
                  onInput={(e) => set('email', e.currentTarget.value)}
                />
                {text('shopify_location_id', 'Shopify location ID', {
                  placeholder: 'gid://shopify/Location/123',
                  details: 'Optional. Links this warehouse to a Shopify location.',
                })}
              </s-grid>
              <s-box paddingBlockStart="base">
                <s-switch
                  label="Active"
                  checked={form.is_active}
                  details="Inactive warehouses are never used for new shipments."
                  onChange={(e) => set('is_active', e.currentTarget.checked)}
                />
              </s-box>
            </s-section>

            <s-section heading="Address">
              <s-grid gridTemplateColumns="repeat(auto-fit, minmax(240px, 1fr))" gap="base">
                {text('address1', 'Address line 1', { required: true })}
                {text('address2', 'Address line 2')}
                {text('city', 'City', { required: true })}
                {text('state', 'State', { required: true })}
                {text('pincode', 'Pincode', { required: true, details: '6 digits' })}
                {text('country', 'Country', { details: 'ISO code (India: IN)' })}
              </s-grid>
            </s-section>

            <s-section heading="Default package">
              <s-paragraph color="subdued">Used when an order does not specify package dimensions.</s-paragraph>
              <s-grid gridTemplateColumns="repeat(auto-fit, minmax(160px, 1fr))" gap="base">
                {(['length_cm', 'breadth_cm', 'height_cm'] as const).map((key) => (
                  <s-number-field
                    key={key}
                    label={key === 'length_cm' ? 'Length' : key === 'breadth_cm' ? 'Breadth' : 'Height'}
                    value={form[key]}
                    suffix="cm"
                    min={0}
                    step={0.1}
                    error={errors[key]}
                    onInput={(e) => set(key, e.currentTarget.value)}
                  />
                ))}
                <s-number-field
                  label="Minimum weight"
                  value={form.min_weight_g}
                  suffix="g"
                  min={1}
                  step={1}
                  error={errors.min_weight_g}
                  onInput={(e) => set('min_weight_g', e.currentTarget.value)}
                />
              </s-grid>
            </s-section>
          </>
        )}
      </s-stack>
    </s-page>
  );
}
